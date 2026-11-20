"""PROSEVIEW MOD-02: company templates wired into the CSV import (editable plan per request,
mother action + sub-actions, per-line BPU, one-off or reusable template, manual additions)."""
from datetime import date
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from project_management.models import (
    ActionItem,
    ActionItemProgressSetting,
    ActionTemplate,
    ActionTemplateSubAction,
    InterventionImportBatch,
    InterventionRequest,
    ProjectTeamMembers,
)
from project_management.services.action_progress import compute_progress, effective_bpu
from project_management.services.intervention_import import (
    PlanIncomplete,
    PlanRejected,
    confirm_import,
    preview_import,
)
from project_management.services.intervention_plan import MAX_SUB_ACTIONS
from project_management.test_action_item_query_services import ActionItemQueryFixtureMixin
from project_management.test_action_templates import make_company, make_project
from project_management.test_intervention_import import HEADER, ROW_A, ROW_B, ROW_C, csv_bytes

SERIES_A = [("Préparation", 10, 30), ("Montage", 10, 20), ("Contrôle", 10, 50)]
SERIES_B = [("Étude", 0, 40), ("Livraison", 20, 60)]
INTERVENTIONS = "project_management.views.intervention"
PLAN_MODULE = "project_management.services.intervention_plan"


def line(title, in_progress, closed, **extra):
    return {"title": title, "in_progress_points": in_progress, "closed_points": closed, **extra}


def lines_of(series):
    return [line(*item) for item in series]


class PlanFixture(ActionItemQueryFixtureMixin):
    def setUp(self):
        super().setUp()
        self.other_user, self.other_company = make_company("plan-other")
        self.client = APIClient()
        self.allow = patch.multiple(
            INTERVENTIONS,
            can_import_interventions=lambda user, project: True,
            can_view_interventions=lambda user, project: True,
        )

    def template(self, title, series=SERIES_A, company=None, **flags):
        template = ActionTemplate.objects.create(company=company or self.company, title=title, **flags)
        for position, (name, a, b) in enumerate(series, start=1):
            ActionTemplateSubAction.objects.create(
                template=template, position=position, title=name, in_progress_points=a, closed_points=b
            )
        return template

    def preview(self, raw, project=None):
        return preview_import(project=project or self.project, user=self.user, file_name="t.csv", raw=raw)

    def row(self, batch, line_number):
        return batch.rows.get(line_number=line_number)

    def set_plan(self, batch, line_number, plan):
        """Store a plan through the same server validation as the API (rights patched in)."""
        from project_management.services.intervention_plan import clean_plan

        row = self.row(batch, line_number)
        row.plan = clean_plan(batch.project, plan, user=None)
        row.save(update_fields=["plan"])

    def member(self, email, project=None):
        from models.models import User

        user = User.objects.create_user(
            email=email, password="password", first_name="Team", last_name=email[:4], user_type="company_user"
        )
        ProjectTeamMembers.objects.bulk_create(
            [ProjectTeamMembers(project=project or self.project, team_member=user, role="Tech")]
        )
        return user


class PlanProposalTests(PlanFixture, TestCase):
    def test_two_titles_give_two_different_series_of_sub_actions(self):
        self.template("Confinement salle A", SERIES_A)
        self.template("Confinement salle B", SERIES_B)
        batch = self.preview(csv_bytes(ROW_A, ROW_B))
        confirm_import(batch_id=batch.id, user=self.user)
        first, second = InterventionRequest.objects.order_by("id")
        subs = lambda request: list(  # noqa: E731
            ActionItem.objects.filter(parent_action_item__intervention_request=request)
            .order_by("id").values_list("name", "progress_setting__closed_points")
        )
        self.assertEqual(subs(first), [("Préparation", 30), ("Montage", 20), ("Contrôle", 50)])
        self.assertEqual(subs(second), [("Étude", 40), ("Livraison", 60)])

    def test_title_matching_ignores_case_and_spaces_only_and_never_fuzzy(self):
        self.template("Confinement salle A", SERIES_A)
        raw = csv_bytes(
            "E1;  CONFINEMENT   salle a ;03/11/2026;20/11/2026;;;\n",
            "E2;Confinement salle;03/11/2026;20/11/2026;;;\n",
            "E3;Confinement salle AA;03/11/2026;20/11/2026;;;\n",
        )
        batch = self.preview(raw)
        plans = {r.external_reference: r.plan for r in batch.rows.all()}
        self.assertEqual(len(plans["E1"]["sub_actions"]), 3)
        self.assertEqual((plans["E2"], plans["E3"]), ({}, {}))  # no approximate match
        self.assertEqual(batch.summary["rows_needing_plan"], 2)

    def test_ambiguous_template_is_excluded(self):
        self.template("Confinement salle A", SERIES_A, needs_review=True)
        batch = self.preview(csv_bytes(ROW_A))
        self.assertEqual(self.row(batch, 2).plan, {})

    def test_company_template_is_reused_in_two_projects(self):
        self.template("Confinement salle A", SERIES_A)
        second = make_project(self.user, self.company, "Second project")
        first_batch = self.preview(csv_bytes(ROW_A))
        second_batch = self.preview(csv_bytes(ROW_A), project=second)
        for batch in (first_batch, second_batch):
            self.assertEqual(len(self.row(batch, 2).plan["sub_actions"]), 3)
            confirm_import(batch_id=batch.id, user=self.user)
        self.assertEqual(InterventionRequest.objects.count(), 2)
        for request in InterventionRequest.objects.all():
            mother = request.action_items.get(intervention_action_key="mother")
            self.assertEqual(mother.sub_action_items.count(), 3)
            self.assertEqual(mother.milestone.project_id, request.project_id)

    def test_template_of_another_company_is_never_proposed_nor_accepted(self):
        foreign = self.template("Confinement salle A", SERIES_B, company=self.other_company)
        batch = self.preview(csv_bytes(ROW_A))
        self.assertEqual(self.row(batch, 2).plan, {})
        from project_management.services.intervention_plan import PlanError, clean_plan

        with self.assertRaises(PlanError) as ctx:
            clean_plan(self.project, {"template_id": foreign.pk, "sub_actions": lines_of(SERIES_B)})
        self.assertEqual(ctx.exception.code, "template_invalid")

    def test_bpu_defaults_to_false_on_every_proposed_line(self):
        self.template("Confinement salle A", SERIES_A)
        plan = self.row(self.preview(csv_bytes(ROW_A)), 2).plan
        # MOD-02b: BPU is chosen once per request (mother), false by default, never per line
        self.assertEqual(plan["is_bpu"], False)
        self.assertTrue(all("is_bpu" not in l for l in plan["sub_actions"]))


class PlanEditingApiTests(PlanFixture, TestCase):
    def upload(self, raw, user=None):
        self.client.force_authenticate(user or self.user)
        return self.client.post(
            reverse("intervention-import-preview"),
            {"project": self.project.id, "file": SimpleUploadedFile("c.csv", raw)},
            format="multipart",
        )

    def patch_plan(self, batch_id, line_number, plan):
        return self.client.patch(
            reverse("intervention-import-row", args=[batch_id, line_number]), {"plan": plan}, format="json"
        )

    def test_no_template_requires_a_plan_before_confirmation_and_nothing_is_written(self):
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            response = self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        self.assertEqual((response.status_code, response.data["code"]), (400, "plan_incomplete"))
        self.assertEqual(response.data["lines"], [2])
        self.assertEqual(InterventionRequest.objects.count(), 0)
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 0)
        self.assertEqual(InterventionImportBatch.objects.get(pk=batch_id).status, "previewed")

    def test_one_off_plan_creates_request_mother_and_sub_actions_without_a_template(self):
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            saved = self.patch_plan(batch_id, 2, {"sub_actions": lines_of(SERIES_B)})
            self.assertEqual(saved.status_code, 200, saved.data)
            self.assertEqual(saved.data["summary"]["rows_needing_plan"], 0)
            self.assertEqual(saved.data["summary"]["actions_to_create"], 3)  # mother + 2
            confirmed = self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(ActionTemplate.objects.count(), 0)  # one-off: no template
        mother = ActionItem.objects.get(intervention_action_key="mother")
        self.assertEqual(mother.name, "Confinement salle A")
        self.assertEqual(list(mother.sub_action_items.order_by("id").values_list("name", flat=True)), ["Étude", "Livraison"])

    def test_plan_edit_is_request_specific_and_never_changes_the_company_template(self):
        template = self.template("Confinement salle A", SERIES_A)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            edited = [line("Seule étape", 5, 100)]
            self.assertEqual(self.patch_plan(batch_id, 2, {"template_id": template.pk, "sub_actions": edited, "is_bpu": True}).status_code, 200)
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        template.refresh_from_db()
        self.assertEqual(
            list(template.sub_actions.order_by("position").values_list("title", "closed_points")),
            [("Préparation", 30), ("Montage", 20), ("Contrôle", 50)],
        )
        sub = ActionItem.objects.get(parent_action_item__isnull=False)
        self.assertEqual((sub.name, sub.progress_setting.closed_points, sub.parent_action_item.is_bpu, effective_bpu(sub)),
                         ("Seule étape", 100, True, (True, "mother")))
        self.assertEqual(sub.progress_setting.source_template_id, template.pk)

    def test_invalid_plans_are_refused_by_the_server(self):
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            member_less = self.member("outsider@example.com", project=make_project(self.user, self.company, "Elsewhere"))
            bad = {
                "empty_list": ([], "no_sub_action"),
                "blank_title": ([line("  ", 0, 100)], "title_required"),
                "long_title": ([line("x" * 256, 0, 100)], "title_too_long"),
                "total_99": ([line("a", 0, 99)], "points_invalid"),
                "in_above_closed": ([line("a", 60, 50), line("b", 0, 50)], "points_invalid"),
                "negative": ([line("a", -1, 100)], "points_invalid"),
                "float": ([line("a", 0, 100.5)], "points_invalid"),
                "string_points": ([line("a", "0", "100")], "points_invalid"),
                "bpu_not_bool": ([line("a", 0, 100, is_bpu="yes")], "bpu_invalid"),
                "owner_sent": ([line("a", 0, 100, assignee_id=member_less.id)], "assignee_not_at_import"),
                "too_many": ([line(f"s{i}", 0, 0) for i in range(MAX_SUB_ACTIONS + 1)], "too_many_sub_actions"),
            }
            for name, (lines, code) in bad.items():
                response = self.patch_plan(batch_id, 2, {"sub_actions": lines})
                self.assertEqual((name, response.status_code), (name, 400), response.data)
                self.assertEqual((name, response.data["code"]), (name, code))
            self.assertEqual(self.client.patch(
                reverse("intervention-import-row", args=[batch_id, 2]), {"plan": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.row(InterventionImportBatch.objects.get(pk=batch_id), 2).plan, {})

    def test_tampered_stored_plan_is_refused_at_confirmation(self):
        batch = self.preview(csv_bytes(ROW_A))
        row = self.row(batch, 2)
        row.plan = {"template_id": None, "sub_actions": [line("a", 0, 60)], "save_as_template": False}  # bypasses the API
        row.save()
        with self.assertRaises(PlanRejected) as ctx:
            confirm_import(batch_id=batch.id, user=self.user)
        self.assertEqual((ctx.exception.line, ctx.exception.code), (2, "points_invalid"))
        self.assertEqual(InterventionRequest.objects.count(), 0)

    def test_bpu_is_set_on_the_mother_per_request_and_inherited(self):
        self.template("Confinement salle A", SERIES_A)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A, ROW_B.replace("salle B", "salle A"))).data["id"]
            template_id = ActionTemplate.objects.get().pk
            plan = {"template_id": template_id, "is_bpu": True, "sub_actions": lines_of(SERIES_A)}
            self.assertEqual(self.patch_plan(batch_id, 2, plan).status_code, 200)  # row 3 keeps the default
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        first, second = InterventionRequest.objects.order_by("id")
        mother = lambda request: request.action_items.get(intervention_action_key="mother")  # noqa: E731
        self.assertEqual((mother(first).is_bpu, mother(second).is_bpu), (True, False))
        for request, expected in ((first, True), (second, False)):
            subs = list(mother(request).sub_action_items.order_by("id"))
            self.assertEqual([effective_bpu(sub)[0] for sub in subs], [expected] * 3)
            self.assertEqual([s.progress_setting.is_bpu for s in subs], [False] * 3)  # nothing stored per line
        self.assertEqual(effective_bpu(list(mother(first).sub_action_items.all())[0]), (True, "mother"))
        self.assertFalse(hasattr(ActionTemplateSubAction, "is_bpu"))  # never a template default

    def test_a_per_line_bpu_is_refused_and_the_plan_default_is_false(self):
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            refused = self.patch_plan(batch_id, 2, {"sub_actions": [line("a", 0, 100, is_bpu=True)]})
            self.assertEqual((refused.status_code, refused.data["code"]), (400, "bpu_inherited"))
            bad = self.patch_plan(batch_id, 2, {"sub_actions": lines_of(SERIES_B), "is_bpu": "yes"})
            self.assertEqual((bad.status_code, bad.data["code"]), (400, "bpu_invalid"))
            ok = self.patch_plan(batch_id, 2, {"sub_actions": lines_of(SERIES_B)})
            self.assertEqual(ok.data["row"]["plan"]["is_bpu"], False)

    def test_a_line_owner_is_refused_and_the_import_creates_no_owner(self):
        self.template("Confinement salle A", SERIES_A)
        line_owner = self.member("line@example.com")
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            template_id = ActionTemplate.objects.get().pk
            refused = self.patch_plan(batch_id, 2, {"template_id": template_id, "sub_actions": [
                line("Préparation", 10, 30, assignee_id=line_owner.id), line("Montage", 10, 20), line("Contrôle", 10, 50)]})
            self.assertEqual((refused.status_code, refused.data["code"]), (400, "assignee_not_at_import"))
            ok = self.patch_plan(batch_id, 2, {"template_id": template_id, "sub_actions": lines_of(SERIES_A)})
            self.assertEqual(ok.status_code, 200, ok.data)
            self.assertTrue(all(l["assignee_id"] is None for l in ok.data["row"]["plan"]["sub_actions"]))
            self.assertEqual(self.client.post(reverse("intervention-import-confirm", args=[batch_id]), {}, format="json").status_code, 200)
        mother = ActionItem.objects.get(intervention_action_key="mother")
        self.assertFalse(mother.assignees.exists())
        self.assertFalse(any(s.assignees.exists() for s in mother.sub_action_items.all()))

    def test_dates_are_the_reliable_ones_of_the_mapping(self):
        self.template("Confinement salle A", SERIES_A)
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        request = InterventionRequest.objects.get()
        for action in request.action_items.all().union(ActionItem.objects.filter(parent_action_item__intervention_request=request)):
            self.assertIsNone(action.start_date)
            self.assertEqual(action.target_date, request.expected_end_date)

    def test_rows_that_are_not_new_or_batches_not_previewed_cannot_be_edited(self):
        self.template("Confinement salle A", SERIES_A)
        with self.allow:
            first = self.upload(csv_bytes(ROW_A)).data["id"]
            self.client.post(reverse("intervention-import-confirm", args=[first]))
            self.assertEqual(self.patch_plan(first, 2, {"sub_actions": lines_of(SERIES_A)}).status_code, 409)
            again = self.upload(csv_bytes(ROW_A)).data["id"]  # same reference: already imported
            self.assertEqual(self.patch_plan(again, 2, {"sub_actions": lines_of(SERIES_A)}).status_code, 409)

    def test_rights_and_isolation_of_the_plan_endpoint(self):
        batch_id = None
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
        url = reverse("intervention-import-row", args=[batch_id, 2])
        self.client.force_authenticate(self.other_user)
        with self.allow:
            self.assertEqual(self.client.patch(url, {"plan": {"sub_actions": lines_of(SERIES_A)}}, format="json").status_code, 404)
        self.client.force_authenticate(self.user)
        with patch.multiple(INTERVENTIONS, can_import_interventions=lambda user, project: False):
            self.assertEqual(self.client.patch(url, {"plan": {"sub_actions": lines_of(SERIES_A)}}, format="json").status_code, 403)
        self.assertEqual(self.row(InterventionImportBatch.objects.get(pk=batch_id), 2).plan, {})


class TemplateCreationFromImportTests(PlanFixture, TestCase):
    def upload(self, raw):
        self.client.force_authenticate(self.user)
        return self.client.post(
            reverse("intervention-import-preview"),
            {"project": self.project.id, "file": SimpleUploadedFile("c.csv", raw)},
            format="multipart",
        )

    def save_template_plan(self, batch_id, line_number=2, series=SERIES_A, bpu=True):
        return self.client.patch(
            reverse("intervention-import-row", args=[batch_id, line_number]),
            {"plan": {"sub_actions": lines_of(series), "save_as_template": True, "is_bpu": bpu}}, format="json",
        )

    def test_template_is_created_only_at_confirmation_without_bpu(self):
        with self.allow, patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.assertEqual(self.save_template_plan(batch_id).status_code, 200)
            self.assertEqual(ActionTemplate.objects.count(), 0)  # nothing before the confirmation
            response = self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        self.assertEqual(response.status_code, 200, response.data)
        template = ActionTemplate.objects.get()
        self.assertEqual((template.title, template.company_id, template.origin_project_id), ("Confinement salle A", self.company.id, None))
        self.assertEqual(list(template.sub_actions.values_list("title", "closed_points")),
                         [("Préparation", 30), ("Montage", 20), ("Contrôle", 50)])
        self.assertEqual(response.data["result"]["templates_created"][0]["template_id"], template.pk)
        # the BPU choice stays on the request's mother (inherited by its sub-actions), never on the template
        self.assertTrue(ActionItem.objects.get(intervention_action_key="mother").is_bpu)
        self.assertEqual(list(ActionItem.objects.filter(parent_action_item__isnull=False).order_by("id")
                              .values_list("progress_setting__is_bpu", flat=True)), [False, False, False])

    def test_cancelling_the_preview_creates_nothing(self):
        with self.allow, patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.save_template_plan(batch_id)
            cancelled = self.client.delete(reverse("intervention-import-batch", args=[batch_id]))
        self.assertEqual(cancelled.status_code, 204)
        self.assertFalse(InterventionImportBatch.objects.filter(pk=batch_id).exists())
        self.assertEqual((ActionTemplate.objects.count(), InterventionRequest.objects.count(),
                          ActionItem.objects.filter(intervention_request__isnull=False).count()), (0, 0, 0))

    def test_confirmed_batch_cannot_be_cancelled(self):
        self.template("Confinement salle A", SERIES_A)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
            self.assertEqual(self.client.delete(reverse("intervention-import-batch", args=[batch_id])).status_code, 409)

    def test_saving_a_template_needs_the_company_configuration_right(self):
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            # the real check (no role in this fixture) refuses; nothing is stored
            denied = self.save_template_plan(batch_id)
            self.assertEqual((denied.status_code, denied.data["code"]), (403, "template_right_required"))
            self.assertEqual(self.client.get(reverse("intervention-import-batch", args=[batch_id])).data["can_manage_templates"], False)
            # a plan stored while the user had the right is re-checked at confirmation
            with patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
                self.assertEqual(self.save_template_plan(batch_id).status_code, 200)
            refused = self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        self.assertEqual((refused.status_code, refused.data["code"]), (403, "template_right_required"))
        self.assertEqual((ActionTemplate.objects.count(), InterventionRequest.objects.count()), (0, 0))
        self.assertEqual(InterventionImportBatch.objects.get(pk=batch_id).status, "previewed")

    def test_template_created_meanwhile_by_another_user_is_not_duplicated(self):
        with self.allow, patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.save_template_plan(batch_id)
            self.template(" CONFINEMENT salle A", SERIES_B)  # created by someone else after the preview
            response = self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ActionTemplate.objects.count(), 1)
        self.assertEqual(ActionTemplate.objects.get().sub_actions.count(), 2)  # the other user's, untouched
        self.assertEqual(response.data["result"]["templates_already_existing"][0]["line"], 2)
        self.assertEqual(InterventionRequest.objects.count(), 1)  # the request still follows the user's plan
        self.assertEqual(ActionItem.objects.get(intervention_action_key="mother").sub_action_items.count(), 3)

    def test_two_rows_with_the_same_title_in_one_file_create_one_template(self):
        twin = "E2;confinement SALLE A;04/11/2026;25/11/2026;;;\n"
        with self.allow, patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
            batch_id = self.upload(csv_bytes(ROW_A, twin)).data["id"]
            self.save_template_plan(batch_id, 2)
            self.save_template_plan(batch_id, 3)
            response = self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ActionTemplate.objects.count(), 1)
        self.assertEqual(len(response.data["result"]["templates_already_existing"]), 1)
        self.assertEqual(InterventionRequest.objects.count(), 2)

    def test_template_creation_is_rolled_back_with_the_rest_on_error(self):
        with self.allow, patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.save_template_plan(batch_id)
        real_save = ActionItem.save
        calls = {"n": 0}

        def flaky(instance, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom")
            return real_save(instance, *args, **kwargs)

        with patch.object(ActionItem, "save", flaky), patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
            with self.assertRaises(RuntimeError):
                confirm_import(batch_id=batch_id, user=self.user)
        self.assertEqual((ActionTemplate.objects.count(), InterventionRequest.objects.count(),
                          ActionItem.objects.filter(intervention_request__isnull=False).count()), (0, 0, 0))
        with patch(f"{PLAN_MODULE}.can_manage_templates", return_value=True):
            _, applied = confirm_import(batch_id=batch_id, user=self.user)  # a retry works
        self.assertTrue(applied)
        self.assertEqual(ActionTemplate.objects.count(), 1)


class HierarchyAndProgressTests(PlanFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.template("Confinement salle A", SERIES_A)
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        self.request = InterventionRequest.objects.get()
        self.mother = self.request.action_items.get(intervention_action_key="mother")
        self.subs = list(self.mother.sub_action_items.order_by("id"))

    def close(self, item):
        item.refresh_from_db()
        item.status, item.closed_at, item.closed_by = "closed", date(2026, 11, 10), self.user
        item.save()

    def test_request_to_mother_to_sub_actions_and_progress_of_mod_01(self):
        self.assertEqual([s.intervention_action_key for s in self.subs], ["s1", "s2", "s3"])
        self.assertTrue(all(s.milestone_id is None and s.parent_action_item_id == self.mother.id for s in self.subs))
        self.assertEqual(self.mother.milestone.name, "Interventions")
        path = [compute_progress(self.mother)["progress"]]
        for sub in self.subs:
            self.close(sub)
            path.append(compute_progress(self.mother)["progress"])
        self.assertEqual(path, [0, 30, 50, 100])
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "closed")  # automatic closing of MOD-01

    def test_mother_is_not_counted_as_a_sub_action_in_the_request_list(self):
        self.client.force_authenticate(self.user)
        with self.allow:
            row = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"][0]
            self.assertEqual((row["actions_count"], row["actions_closed_count"], row["progress_status"]), (3, 0, "not_started"))
            self.close(self.subs[0])
            row = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"][0]
            self.assertEqual((row["actions_count"], row["actions_closed_count"], row["progress_status"]), (3, 1, "in_progress"))
            for sub in self.subs[1:]:
                self.close(sub)
            row = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"][0]
            self.assertEqual((row["actions_count"], row["progress_status"]), (3, "completed"))

    def test_request_detail_exposes_hierarchy_points_and_bpu(self):
        ActionItemProgressSetting.objects.filter(action_item=self.subs[1]).update(is_bpu=True)
        self.client.force_authenticate(self.user)
        with self.allow:
            data = self.client.get(reverse("intervention-request-detail", args=[self.request.id])).data
        by_id = {a["id"]: a for a in data["actions"]}
        mother = by_id[self.mother.id]
        self.assertEqual((mother["parent_action_item"], mother["is_bpu"], mother["closed_points"]), (None, False, None))
        # priority rule: a flag stored by MOD-02 on a line keeps applying while the mother is not BPU
        self.assertEqual(by_id[self.subs[1].id]["bpu_source"], "legacy_line")
        self.assertEqual([(by_id[s.id]["parent_action_item"], by_id[s.id]["is_bpu"]) for s in self.subs],
                         [(self.mother.id, False), (self.mother.id, True), (self.mother.id, False)])
        self.assertEqual(by_id[self.subs[0].id]["closed_points"], 30)

    def test_reimport_creates_no_second_mother_or_sub_action_and_overwrites_nothing(self):
        before = list(ActionItem.objects.filter(intervention_request=self.request).values_list(
            "id", "name", "status", "target_date", "intervention_action_key").order_by("id"))
        settings_before = list(ActionItemProgressSetting.objects.order_by("id").values_list(
            "action_item_id", "in_progress_points", "closed_points", "is_bpu"))
        for raw in (csv_bytes(ROW_A), csv_bytes(ROW_A.replace("20/11/2026", "28/12/2026"))):
            batch = self.preview(raw)
            confirmed, _ = confirm_import(batch_id=batch.id, user=self.user)
            self.assertEqual(confirmed.result["actions_created"], 0)
        self.assertEqual(before, list(ActionItem.objects.filter(intervention_request=self.request).values_list(
            "id", "name", "status", "target_date", "intervention_action_key").order_by("id")))
        self.assertEqual(settings_before, list(ActionItemProgressSetting.objects.order_by("id").values_list(
            "action_item_id", "in_progress_points", "closed_points", "is_bpu")))
        self.assertEqual(ActionItem.objects.filter(intervention_action_key="mother").count(), 1)

    def test_legacy_flat_requests_are_still_counted_by_their_own_actions(self):
        legacy = InterventionRequest.objects.create(
            project=self.project, reference="IR-LEG-1", title="Legacy", received_date=date(2026, 1, 1),
            expected_end_date=date(2026, 2, 1), source="csv", external_reference="OLD-1",
        )
        for key in ("preparation", "delivery", "assembly", "control"):
            ActionItem.objects.create(
                milestone=self.mother.milestone, name=f"IR-LEG-1 — {key}", status="open",
                target_date=date(2026, 2, 1), intervention_request=legacy, intervention_action_key=key,
            )
        self.client.force_authenticate(self.user)
        with self.allow:
            rows = {r["reference"]: r for r in self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"]}
        self.assertEqual(rows["IR-LEG-1"]["actions_count"], 4)


class ManualSubActionTests(PlanFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.template("Confinement salle A", SERIES_A)
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        self.mother = ActionItem.objects.get(intervention_action_key="mother")
        self.subs = list(self.mother.sub_action_items.order_by("id"))
        self.owner = self.member("manual-owner@example.com")  # assignees must be project team members
        self.client.force_authenticate(self.user)

    def add(self, parent=None, **extra):
        body = {
            "name": "Ajout manuel", "parent_action_item": (parent or self.mother).id, "status": "open",
            "target_date": "2026-11-15", "assignees": [self.owner.id],
        }
        body.update(extra)
        with patch("project_management.permissions.ActionItemPermission.has_permission", return_value=True), patch(
            "project_management.permissions.ActionItemPermission.has_object_permission", return_value=True
        ):
            return self.client.post(reverse("actionitem-list"), body, format="json")

    def adjust(self, **values):
        return [{"id": s.id, **values.get(i, {})} for i, s in enumerate(self.subs) if i in values]

    def test_adding_requires_explicit_points_and_sibling_adjustments_to_keep_100(self):
        missing = self.add()
        self.assertEqual(missing.status_code, 400, missing.data)
        unbalanced = self.add(progress_setting={"in_progress_points": 5, "closed_points": 10})
        self.assertEqual(unbalanced.status_code, 400)  # 100 + 10 != 100: nothing is redistributed
        self.assertEqual(self.mother.sub_action_items.count(), 3)
        ok = self.add(
            progress_setting={"in_progress_points": 5, "closed_points": 10},
            sibling_progress=[{"id": self.subs[2].id, "in_progress_points": 10, "closed_points": 40}],
        )
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(self.mother.sub_action_items.count(), 4)
        settings = ActionItemProgressSetting.objects.get(action_item_id=ok.data["id"])
        self.assertEqual((settings.in_progress_points, settings.closed_points, settings.is_bpu), (5, 10, False))
        self.assertEqual(ActionItemProgressSetting.objects.get(action_item=self.subs[2]).closed_points, 40)
        # untouched siblings keep their points: nothing is recalculated
        self.assertEqual([ActionItemProgressSetting.objects.get(action_item=s).closed_points for s in self.subs[:2]], [30, 20])
        total = sum(ActionItemProgressSetting.objects.filter(action_item__parent_action_item=self.mother).values_list("closed_points", flat=True))
        self.assertEqual(total, 100)

    def test_zero_point_addition_keeps_the_existing_total(self):
        response = self.add(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(ActionItemProgressSetting.objects.get(action_item_id=response.data["id"]).is_bpu)  # default

    def test_invalid_adjustments_are_refused(self):
        base = {"in_progress_points": 0, "closed_points": 10}
        cases = {
            "foreign_sibling": [{"id": 999999, "in_progress_points": 0, "closed_points": 40}],
            "bool_points": [{"id": self.subs[2].id, "in_progress_points": True, "closed_points": 40}],
            "twice": [{"id": self.subs[2].id, "in_progress_points": 0, "closed_points": 40}] * 2,
            "in_above_closed": [{"id": self.subs[2].id, "in_progress_points": 45, "closed_points": 40}],
        }
        for name, siblings in cases.items():
            response = self.add(progress_setting=base, sibling_progress=siblings)
            self.assertEqual((name, response.status_code), (name, 400), response.data)
        self.assertEqual(self.add(progress_setting={**base, "is_bpu": "yes"}).status_code, 400)
        self.assertEqual(self.mother.sub_action_items.count(), 3)

    def test_closed_siblings_cannot_be_adjusted(self):
        ActionItem.objects.filter(pk=self.subs[2].pk).update(status="closed")
        response = self.add(
            progress_setting={"in_progress_points": 0, "closed_points": 10},
            sibling_progress=[{"id": self.subs[2].id, "in_progress_points": 10, "closed_points": 40}],
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ActionItemProgressSetting.objects.get(action_item=self.subs[2]).closed_points, 50)

    def test_closed_mother_is_locked(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(status="closed")
        response = self.add(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.mother.sub_action_items.count(), 3)

    def test_extension_applies_only_to_mothers_using_the_points_mechanism(self):
        plain = self.create_parent("Plain")
        refused = self.add(parent=plain, progress_setting={"in_progress_points": 0, "closed_points": 100})
        self.assertEqual(refused.status_code, 400)
        normal = self.add(parent=plain)  # classic manual addition: unchanged behaviour
        self.assertEqual(normal.status_code, 201, normal.data)
        self.assertFalse(ActionItemProgressSetting.objects.filter(action_item_id=normal.data["id"]).exists())

    def test_added_sub_action_counts_in_the_progress_and_blocks_automatic_closing(self):
        added = self.add(
            progress_setting={"in_progress_points": 0, "closed_points": 20},
            sibling_progress=[{"id": self.subs[2].id, "in_progress_points": 10, "closed_points": 30}],
        ).data["id"]
        for sub in self.subs:
            sub.refresh_from_db()
            sub.status, sub.closed_at, sub.closed_by = "closed", date(2026, 11, 10), self.user
            sub.save()
        self.mother.refresh_from_db()
        self.assertEqual((self.mother.status, compute_progress(self.mother)["progress"]), ("in progress", 80))  # started, not yet closable
        manual = ActionItem.objects.get(pk=added)
        manual.status, manual.closed_at, manual.closed_by = "closed", date(2026, 11, 11), self.user
        manual.save()
        self.mother.refresh_from_db()
        self.assertEqual((self.mother.status, compute_progress(self.mother)["progress"]), ("closed", 100))

    def test_action_item_detail_exposes_the_points_and_bpu(self):
        from project_management.serializers.action_item import ActionItemSerializer

        class Req:
            user = self.user

        data = ActionItemSerializer(self.subs[0], context={"request": Req(), "serialize_sub_action_items": False}).data
        self.assertEqual(data["progress_setting"], {"in_progress_points": 10, "closed_points": 30, "is_bpu": False})
        mother = ActionItemSerializer(self.mother, context={"request": Req(), "serialize_sub_action_items": False}).data
        self.assertIsNone(mother["progress_setting"])


class ConcurrencyTests(PlanFixture, TestCase):
    def test_double_confirmation_and_two_previews_create_once(self):
        self.template("Confinement salle A", SERIES_A)
        first, second = self.preview(csv_bytes(ROW_A)), self.preview(csv_bytes(ROW_A))
        confirm_import(batch_id=first.id, user=self.user)
        _, applied_again = confirm_import(batch_id=first.id, user=self.user)
        self.assertFalse(applied_again)
        confirmed, _ = confirm_import(batch_id=second.id, user=self.user)
        self.assertEqual(confirmed.result["requests_created"], 0)
        self.assertEqual(ActionItem.objects.filter(intervention_action_key="mother").count(), 1)
        self.assertEqual(ActionItem.objects.filter(parent_action_item__isnull=False).count(), 3)

    def test_preview_alone_writes_no_template_request_or_action(self):
        self.preview(csv_bytes(ROW_A, ROW_B, ROW_C))
        self.assertEqual((ActionTemplate.objects.count(), InterventionRequest.objects.count(),
                          ActionItem.objects.filter(intervention_request__isnull=False).count()), (0, 0, 0))
