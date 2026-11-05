"""DEMO-01: intervention requests + reviewed CSV import (duplicates, atomicity, isolation)."""
from datetime import date
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from models.models import CompanyProfile, User
from project_management.filters import ActionItemFilter
from project_management.models import (
    ActionItem,
    InterventionImportBatch,
    InterventionImportRow,
    InterventionRequest,
    Milestone,
    Project,
    ProjectTeamMembers,
)
from project_management.services.intervention_import import (
    ACTION_TEMPLATES,
    BatchNotConfirmable,
    ImportFileError,
    confirm_import,
    preview_import,
)
from project_management.test_action_item_query_services import ActionItemQueryFixtureMixin

HEADER = "reference;titre;date_reception;date_fin_attendue;budget;devise;description\n"
ROW_A = "EXT-001;Confinement salle A;03/11/2026;20/11/2026;1 500,50;EUR;Premier\n"
ROW_B = "EXT-002;Confinement salle B;04/11/2026;25/11/2026;;;\n"
ROW_C = "EXT-003;Confinement salle C;05/11/2026;30/11/2026;200;EUR;\n"


def csv_bytes(*rows, header=HEADER, encoding="utf-8"):
    return (header + "".join(rows)).encode(encoding)


class InterventionImportTests(ActionItemQueryFixtureMixin, TestCase):
    def preview(self, raw, **kwargs):
        return preview_import(project=self.project, user=self.user, file_name="t.csv", raw=raw, **kwargs)

    def outcomes(self, batch):
        return {r.external_reference or f"line{r.line_number}": r.outcome for r in batch.rows.all()}

    # ---- preview ----
    def test_preview_classifies_rows_and_creates_nothing(self):
        batch = self.preview(csv_bytes(ROW_A, ROW_B))
        self.assertEqual(batch.status, "previewed")
        self.assertEqual(batch.summary["requests_to_create"], 2)
        self.assertEqual(batch.summary["actions_to_create"], 8)
        self.assertEqual(InterventionRequest.objects.count(), 0)
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 0)

    def test_windows_1252_and_comma_delimiter_are_read(self):
        raw = "reference,titre,date_reception,date_fin_attendue\nEXT-9,Réception été,2026-11-03,2026-11-20\n"
        batch = self.preview(raw.encode("cp1252"))
        self.assertEqual(batch.summary["encoding"], "cp1252")
        row = batch.rows.get()
        self.assertEqual(row.outcome, "new")
        self.assertEqual(row.data["title"], "Réception été")

    def test_bad_file_is_refused_without_storing_a_batch(self):
        for raw, code in (
            (b"", "empty_file"),
            (csv_bytes(ROW_A, header="reference;titre\n"), "missing_columns"),
        ):
            with self.assertRaises(ImportFileError) as ctx:
                self.preview(raw)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(InterventionImportBatch.objects.count(), 0)

    def test_row_errors(self):
        bad = (
            ";Sans référence;03/11/2026;20/11/2026;;;\n"
            "EXT-5;Date KO;31/02/2026;20/11/2026;;;\n"
            "EXT-6;Fin avant réception;10/11/2026;01/11/2026;;;\n"
            "EXT-7;Budget KO;03/11/2026;20/11/2026;abc;;\n"
        )
        batch = self.preview(csv_bytes(bad))
        rows = list(batch.rows.order_by("line_number"))
        self.assertEqual([r.outcome for r in rows], ["error"] * 4)
        self.assertIn("missing_external_reference", rows[0].errors)
        self.assertIn("invalid_received_date", rows[1].errors)
        self.assertIn("end_before_received", rows[2].errors)
        self.assertIn("invalid_budget", rows[3].errors)
        self.assertEqual(batch.summary["requests_to_create"], 0)

    def test_identical_duplicate_in_file_is_skipped_and_conflicting_ones_are_all_rejected(self):
        batch = self.preview(csv_bytes(ROW_A, ROW_A, ROW_B, "EXT-002;Autre titre;04/11/2026;25/11/2026;;;\n"))
        rows = list(batch.rows.order_by("line_number"))
        self.assertEqual([r.outcome for r in rows], ["new", "duplicate_in_file", "error", "error"])
        self.assertIn("conflicting_duplicate_in_file", rows[2].errors)
        confirmed, _ = confirm_import(batch_id=batch.id, user=self.user)
        self.assertEqual(confirmed.result["requests_created"], 1)
        self.assertEqual(InterventionRequest.objects.count(), 1)

    def test_title_alone_is_never_an_identity(self):
        # Same title, different external references -> two distinct requests.
        batch = self.preview(csv_bytes(ROW_A, ROW_A.replace("EXT-001", "EXT-777")))
        self.assertEqual(batch.summary["requests_to_create"], 2)

    # ---- confirm ----
    def test_confirm_creates_requests_with_four_unassigned_actions(self):
        batch = self.preview(csv_bytes(ROW_A, ROW_B))
        confirmed, applied = confirm_import(batch_id=batch.id, user=self.user)
        self.assertTrue(applied)
        self.assertEqual(confirmed.result["requests_created"], 2)
        self.assertEqual(confirmed.result["actions_created"], 8)
        first = InterventionRequest.objects.order_by("id").first()
        self.assertTrue(first.reference.startswith("IR-"))
        self.assertTrue(first.reference.endswith("00001"))
        self.assertEqual(first.received_date, date(2026, 11, 3))
        self.assertEqual(str(first.budget), "1500.50")
        self.assertEqual(first.company_profile_id, self.company.id)
        actions = list(first.action_items.order_by("id"))
        self.assertEqual([a.intervention_action_key for a in actions], [k for k, _ in ACTION_TEMPLATES])
        self.assertEqual(
            [a.name.split(" — ", 1)[1] for a in actions], ["Préparation matériel", "Acheminement", "Montage", "Contrôle"]
        )
        for action in actions:
            self.assertEqual(action.status, "open")
            self.assertIsNone(action.start_date)  # no invented start date
            self.assertEqual(action.target_date, first.expected_end_date)
            self.assertEqual(action.assignees.count(), 0)
            self.assertIsNone(action.assignee)
            self.assertEqual(action.milestone.project_id, self.project.id)
            self.assertEqual(action.milestone.name, "Interventions")

    def test_double_confirm_is_idempotent(self):
        batch = self.preview(csv_bytes(ROW_A, ROW_B))
        confirm_import(batch_id=batch.id, user=self.user)
        again, applied = confirm_import(batch_id=batch.id, user=self.user)
        self.assertFalse(applied)
        self.assertEqual(again.result["requests_created"], 2)
        self.assertEqual(InterventionRequest.objects.count(), 2)
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 8)
        self.assertEqual(Milestone.objects.filter(project=self.project, name="Interventions").count(), 1)

    def test_reimport_of_same_file_creates_nothing(self):
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A, ROW_B)).id, user=self.user)
        second = self.preview(csv_bytes(ROW_A, ROW_B))
        self.assertEqual(set(self.outcomes(second).values()), {"already_imported"})
        self.assertEqual(second.summary["requests_to_create"], 0)
        confirmed, _ = confirm_import(batch_id=second.id, user=self.user)
        self.assertEqual(confirmed.result["requests_created"], 0)
        self.assertEqual(InterventionRequest.objects.count(), 2)

    def test_changed_source_data_is_reported_but_never_applied(self):
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        request = InterventionRequest.objects.get()
        action = request.action_items.first()
        action.target_date = date(2027, 1, 15)
        action.status = "in progress"
        action.save()
        action.assignees.add(self.user)

        changed = ROW_A.replace("Confinement salle A", "Titre modifié à la source").replace("20/11/2026", "28/12/2026")
        second = self.preview(csv_bytes(changed))
        self.assertEqual(self.outcomes(second), {"EXT-001": "source_changed"})
        confirm_import(batch_id=second.id, user=self.user)

        request.refresh_from_db()
        action.refresh_from_db()
        self.assertEqual(request.title, "Confinement salle A")
        self.assertEqual(request.expected_end_date, date(2026, 11, 20))
        self.assertEqual(request.received_date, date(2026, 11, 3))
        self.assertEqual(action.target_date, date(2027, 1, 15))
        self.assertEqual(action.status, "in progress")
        self.assertEqual(list(action.assignees.all()), [self.user])
        self.assertEqual(InterventionRequest.objects.count(), 1)

    def test_two_previews_confirmed_one_after_the_other_create_once(self):
        # Two users upload the same file before either confirms (concurrent imports).
        first = self.preview(csv_bytes(ROW_A, ROW_B))
        second = self.preview(csv_bytes(ROW_A, ROW_B))
        confirm_import(batch_id=first.id, user=self.user)
        confirmed, _ = confirm_import(batch_id=second.id, user=self.user)
        self.assertEqual(confirmed.result["requests_created"], 0)
        self.assertEqual(set(self.outcomes(confirmed).values()), {"already_imported"})
        self.assertEqual(InterventionRequest.objects.count(), 2)
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 8)

    def test_failure_rolls_everything_back_and_can_be_retried(self):
        batch = self.preview(csv_bytes(ROW_A, ROW_B, ROW_C))
        real_save = ActionItem.save
        calls = {"n": 0}

        def flaky_save(instance, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 6:
                raise RuntimeError("boom")
            return real_save(instance, *args, **kwargs)

        with patch.object(ActionItem, "save", flaky_save):
            with self.assertRaises(RuntimeError):
                confirm_import(batch_id=batch.id, user=self.user)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "previewed")
        self.assertEqual(InterventionRequest.objects.count(), 0)
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 0)
        self.assertFalse(Milestone.objects.filter(name="Interventions").exists())

        confirmed, applied = confirm_import(batch_id=batch.id, user=self.user)
        self.assertTrue(applied)
        self.assertEqual(InterventionRequest.objects.count(), 3)
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 12)

    def test_references_are_sequential_and_unique(self):
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        confirm_import(batch_id=self.preview(csv_bytes(ROW_B, ROW_C)).id, user=self.user)
        refs = list(InterventionRequest.objects.order_by("id").values_list("reference", flat=True))
        self.assertEqual(len(set(refs)), 3)
        self.assertEqual([r[-5:] for r in refs], ["00001", "00002", "00003"])

    def test_unconfirmable_batch_is_refused(self):
        batch = self.preview(csv_bytes(ROW_A))
        InterventionImportBatch.objects.filter(pk=batch.pk).update(status="failed")
        with self.assertRaises(BatchNotConfirmable):
            confirm_import(batch_id=batch.id, user=self.user)

    # ---- database backstops and links ----
    def test_database_refuses_a_second_action_with_the_same_key_and_a_duplicate_external_reference(self):
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        request = InterventionRequest.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActionItem.objects.create(
                milestone=request.action_items.first().milestone, name="dup", status="open",
                target_date=date(2026, 12, 1), intervention_request=request, intervention_action_key="assembly",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            InterventionRequest.objects.create(
                project=self.project, reference="IR-X-1", title="x", received_date=date(2026, 1, 1),
                expected_end_date=date(2026, 1, 2), source="csv", external_reference="EXT-001",
            )

    def test_request_with_actions_cannot_be_deleted_and_independent_actions_still_work(self):
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        with self.assertRaises(ProtectedError):
            InterventionRequest.objects.get().delete()
        independent = self.create_parent("Independent")
        self.assertIsNone(independent.intervention_request_id)

    def test_action_filter_returns_only_the_actions_of_one_request(self):
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A, ROW_B)).id, user=self.user)
        self.create_parent("Independent")
        first = InterventionRequest.objects.order_by("id").first()
        qs = ActionItemFilter({"intervention_request": first.id}, queryset=ActionItem.objects.all()).qs
        self.assertEqual(qs.count(), 4)
        self.assertEqual({a.intervention_request_id for a in qs}, {first.id})


class InterventionApiTests(ActionItemQueryFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.other_user = User.objects.create_user(
            email="other-company@example.com", password="password", first_name="Other",
            last_name="Owner", user_type="company_user",
        )
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_user, company_name="Other Company", company_address="S",
            company_town="T", company_tell_no="1", company_mob_no="2",
            company_web_address="https://other.example.com", company_email_address="o@example.com",
            company_tax_reference="tax-other", company_iban="iban-other",
        )
        User.objects.filter(pk=self.other_user.pk).update(company=self.other_company)
        self.other_user.refresh_from_db()
        self.client = APIClient()
        self.allow = patch.multiple(
            "project_management.views.intervention",
            can_import_interventions=lambda user, project: True,
            can_view_interventions=lambda user, project: True,
        )

    def upload(self, user, raw, project=None):
        self.client.force_authenticate(user)
        return self.client.post(
            reverse("intervention-import-preview"),
            {"project": (project or self.project).id, "file": SimpleUploadedFile("c.csv", raw)},
            format="multipart",
        )

    def test_full_flow_through_the_api(self):
        with self.allow:
            response = self.upload(self.user, csv_bytes(ROW_A, ROW_B))
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(response.data["summary"]["actions_to_create"], 8)
            batch_id = response.data["id"]
            confirm_url = reverse("intervention-import-confirm", args=[batch_id])
            first = self.client.post(confirm_url)
            second = self.client.post(confirm_url)
            self.assertEqual((first.status_code, first.data["applied_now"]), (200, True))
            self.assertEqual((second.status_code, second.data["applied_now"]), (200, False))
            listing = self.client.get(reverse("intervention-requests"), {"project": self.project.id, "search": "salle B"})
            self.assertEqual(listing.data["count"], 1)
            self.assertTrue(listing.data["can_import"])
            detail = self.client.get(reverse("intervention-request-detail", args=[listing.data["results"][0]["id"]]))
            self.assertEqual(len(detail.data["actions"]), 4)

    def _member(self, email, in_team=True):
        member = User.objects.create_user(
            email=email, password="password", first_name="Team", last_name=email[:4], user_type="company_user",
        )
        if in_team:
            # bulk_create: skips the team-member post_save notification signal (not under test here)
            ProjectTeamMembers.objects.bulk_create(
                [ProjectTeamMembers(project=self.project, team_member=member, role="Tech")]
            )
        return member

    def _confirm(self, raw, **body):
        batch_id = self.upload(self.user, raw).data["id"]
        return self.client.post(reverse("intervention-import-confirm", args=[batch_id]), body, format="json")

    def test_confirm_with_assignee_sets_owner_on_new_actions_only(self):
        member = self._member("member@example.com")
        with self.allow:
            response = self._confirm(csv_bytes(ROW_A, ROW_B), assignee=member.id)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["result"]["assignee_id"], member.id)
        actions = ActionItem.objects.filter(intervention_request__isnull=False)
        self.assertEqual(actions.count(), 8)
        for action in actions:
            self.assertEqual(list(action.assignees.values_list("id", flat=True)), [member.id])
            self.assertIsNone(action.start_date)  # no date invented
            self.assertEqual(action.target_date, action.intervention_request.expected_end_date)

    def test_confirm_without_assignee_keeps_current_behaviour(self):
        with self.allow:
            response = self._confirm(csv_bytes(ROW_A))
            self.assertEqual(response.status_code, 200, response.data)
        actions = ActionItem.objects.filter(intervention_request__isnull=False)
        self.assertEqual(actions.count(), 4)
        self.assertFalse(any(a.assignees.exists() for a in actions))

    def test_assignee_outside_the_project_is_refused_server_side(self):
        same_company_not_member = self._member("nonmember@example.com", in_team=False)
        with self.allow:
            for bad in (same_company_not_member.id, self.other_user.id, 999999, "abc"):
                batch_id = self.upload(self.user, csv_bytes(ROW_A)).data["id"]
                response = self.client.post(
                    reverse("intervention-import-confirm", args=[batch_id]), {"assignee": bad}, format="json"
                )
                self.assertEqual(response.status_code, 400, (bad, response.data))
                self.assertEqual(response.data["code"], "invalid_assignee")
                self.assertEqual(InterventionImportBatch.objects.get(pk=batch_id).status, "previewed")
        self.assertEqual(InterventionRequest.objects.count(), 0)
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 0)

    def test_reimport_with_another_assignee_changes_nothing_existing(self):
        first = self._member("first@example.com")
        second = self._member("second@example.com")
        with self.allow:
            self._confirm(csv_bytes(ROW_A), assignee=first.id)
            tracked = ActionItem.objects.filter(intervention_request__isnull=False).order_by("id")
            ActionItem.objects.filter(pk=tracked[0].pk).update(status="closed")
            before = [
                (a.id, a.status, a.start_date, a.target_date, tuple(a.assignees.values_list("id", flat=True)))
                for a in tracked
            ]
            # same file again, then the same reference with changed source data, each with another owner
            for raw in (csv_bytes(ROW_A), csv_bytes(ROW_A.replace("20/11/2026", "28/12/2026"))):
                again = self._confirm(raw, assignee=second.id)
                self.assertEqual(again.status_code, 200, again.data)
                self.assertEqual(again.data["result"]["actions_created"], 0)
        after = [
            (a.id, a.status, a.start_date, a.target_date, tuple(a.assignees.values_list("id", flat=True)))
            for a in ActionItem.objects.filter(intervention_request__isnull=False).order_by("id")
        ]
        self.assertEqual(before, after)
        self.assertEqual(InterventionRequest.objects.count(), 1)

    def test_progress_status_follows_the_generated_actions(self):
        with self.allow:
            batch_id = self.upload(self.user, csv_bytes(ROW_A)).data["id"]
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
            request = InterventionRequest.objects.get()

            def progress():
                row = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"][0]
                return row["progress_status"], row["actions_closed_count"], row["actions_count"]

            self.assertEqual(progress(), ("not_started", 0, 4))
            first = request.action_items.order_by("id").first()
            ActionItem.objects.filter(pk=first.pk).update(status="closed")
            self.assertEqual(progress(), ("in_progress", 1, 4))
            request.action_items.update(status="closed")
            self.assertEqual(progress(), ("completed", 4, 4))

    def test_other_company_cannot_see_import_or_confirm(self):
        with self.allow:
            batch_id = self.upload(self.user, csv_bytes(ROW_A)).data["id"]
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
            request_id = InterventionRequest.objects.get().id

            self.assertEqual(self.upload(self.other_user, csv_bytes(ROW_B)).status_code, 404)
            self.client.force_authenticate(self.other_user)
            self.assertEqual(self.client.get(reverse("intervention-requests"), {"project": self.project.id}).status_code, 404)
            self.assertEqual(self.client.get(reverse("intervention-request-detail", args=[request_id])).status_code, 404)
            self.assertEqual(self.client.get(reverse("intervention-import-batch", args=[batch_id])).status_code, 404)
            self.assertEqual(self.client.post(reverse("intervention-import-confirm", args=[batch_id])).status_code, 404)
        self.assertEqual(InterventionRequest.objects.count(), 1)

    def test_user_of_another_project_in_the_same_company_scope_is_not_leaked(self):
        # A request of project 1 is never listed under project 2.
        second_project = Project.objects.create(
            project_name="Second", solution="Energy Consultant Solution", contract_value=1,
            company_profile=self.company, user=self.user, created_by=self.user,
            project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
        )
        with self.allow:
            batch_id = self.upload(self.user, csv_bytes(ROW_A)).data["id"]
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
            listing = self.client.get(reverse("intervention-requests"), {"project": second_project.id})
        self.assertEqual(listing.data["count"], 0)

    def test_missing_rights_give_403_and_create_nothing(self):
        self.client.force_authenticate(self.user)
        with patch.multiple(
            "project_management.views.intervention",
            can_import_interventions=lambda user, project: False,
            can_view_interventions=lambda user, project: False,
        ):
            response = self.upload(self.user, csv_bytes(ROW_A))
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.client.get(reverse("intervention-requests"), {"project": self.project.id}).status_code, 403)
        self.assertEqual(InterventionImportBatch.objects.count(), 0)

    def test_pagination_and_bad_file_errors(self):
        with self.allow:
            bad = self.upload(self.user, b"nope;nope\n1;2\n")
            self.assertEqual((bad.status_code, bad.data["code"]), (400, "missing_columns"))
            batch_id = self.upload(self.user, csv_bytes(ROW_A, ROW_B, ROW_C)).data["id"]
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
            page = self.client.get(reverse("intervention-requests"), {"project": self.project.id, "page_size": 2})
        self.assertEqual((page.data["count"], len(page.data["results"])), (3, 2))
