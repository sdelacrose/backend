"""PROSEVIEW MOD-02b: BPU on the mother (inherited), sub-action numbering, manual template choice,
request without sub-action (mother follows its status, then switches to contributions)."""
from datetime import date
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from project_management.models import (
    ActionItem,
    ActionItemProgressSetting,
    ActionItemSubCounter,
    ActionTemplate,
    InterventionImportBatch,
    InterventionRequest,
)
from project_management.models.action_template import allocate_sub_sequence
from project_management.services.action_progress import compute_progress, effective_bpu
from project_management.services.intervention_import import confirm_import
from project_management.test_action_templates import make_company, make_project
from project_management.test_intervention_import import ROW_A, ROW_B, csv_bytes
from project_management.test_intervention_plans import (
    SERIES_A,
    SERIES_B,
    PlanFixture,
    line,
    lines_of,
)

PERM = "project_management.permissions.ActionItemPermission"


def close(item, user, when=date(2026, 11, 10)):
    item.refresh_from_db()
    item.status, item.closed_at, item.closed_by = "closed", when, user
    item.save()


class ImportedMotherFixture(PlanFixture):
    def setUp(self):
        super().setUp()
        self.template("Confinement salle A", SERIES_A)
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        self.mother = ActionItem.objects.get(intervention_action_key="mother")
        self.subs = list(self.mother.sub_action_items.order_by("id"))
        self.owner = self.member("b-owner@example.com")
        self.client.force_authenticate(self.user)

    def api(self, method, name, args=None, body=None):
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True):
            return getattr(self.client, method)(reverse(name, args=args), body, format="json")

    def add_sub(self, parent=None, **extra):
        body = {"name": "Ajout manuel", "parent_action_item": (parent or self.mother).id, "status": "open",
                "target_date": "2026-11-15", "assignees": [self.owner.id]}
        body.update(extra)
        return self.api("post", "actionitem-list", body=body)


class SubNumberingTests(ImportedMotherFixture, TestCase):
    def test_imported_sub_actions_are_numbered_mother_id_rank_and_technical_keys_are_kept(self):
        refs = [s.sub_reference for s in self.subs]
        self.assertEqual(refs, [f"{self.mother.id}-1", f"{self.mother.id}-2", f"{self.mother.id}-3"])
        self.assertEqual([s.intervention_action_key for s in self.subs], ["s1", "s2", "s3"])  # untouched
        self.assertIsNone(self.mother.sub_reference)

    def test_manual_additions_continue_the_numbering_and_existing_references_never_change(self):
        before = [s.sub_reference for s in self.subs]
        added = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual(added.data["sub_reference"], f"{self.mother.id}-4")
        self.assertEqual(self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0}).data["sub_reference"],
                         f"{self.mother.id}-5")
        self.assertEqual(before, [ActionItem.objects.get(pk=s.pk).sub_reference for s in self.subs])

    def test_legacy_sub_actions_without_a_number_are_not_renumbered(self):
        plain = self.create_parent("Plain mother")
        legacy = ActionItem.objects.create(parent_action_item=plain, name="old", status="open", target_date=date(2026, 1, 10))
        ActionItem.objects.filter(pk=legacy.pk).update(sub_sequence=None)  # as before the numbering existed
        ActionItemSubCounter.objects.filter(parent=plain).delete()
        new = ActionItem.objects.create(parent_action_item=plain, name="new", status="open", target_date=date(2026, 1, 10))
        legacy.refresh_from_db()
        self.assertIsNone(legacy.sub_reference)  # nothing renumbered
        self.assertEqual(new.sub_reference, f"{plain.id}-1")

    def test_numbering_is_independent_per_mother_and_unique_by_constraint(self):
        other = self.create_parent("Other mother")
        first = ActionItem.objects.create(parent_action_item=other, name="s", status="open", target_date=date(2026, 1, 10))
        self.assertEqual(first.sub_reference, f"{other.id}-1")
        with self.assertRaises(IntegrityError), transaction.atomic():  # the database is the last backstop
            ActionItem.objects.filter(pk=self.subs[1].pk).update(sub_sequence=1)

    def test_allocation_never_reuses_a_rank_even_after_a_deletion(self):
        ranks = [allocate_sub_sequence(self.mother.id) for _ in range(3)]
        self.assertEqual(ranks, [4, 5, 6])
        ActionItemProgressSetting.objects.filter(action_item=self.subs[2]).delete()
        ActionItem.objects.filter(pk=self.subs[2].pk)._raw_delete("default")  # skips unrelated deletion signals
        self.assertEqual(allocate_sub_sequence(self.mother.id), 7)

    def test_two_imports_in_two_projects_number_each_mother_separately(self):
        second = make_project(self.user, self.company, "Second")
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A), project=second).id, user=self.user)
        mothers = ActionItem.objects.filter(intervention_action_key="mother").order_by("id")
        for mother in mothers:
            self.assertEqual([s.sub_reference for s in mother.sub_action_items.order_by("id")],
                             [f"{mother.id}-1", f"{mother.id}-2", f"{mother.id}-3"])

    def test_reimport_creates_no_new_number(self):
        confirm_import(batch_id=self.preview(csv_bytes(ROW_A)).id, user=self.user)
        self.assertEqual(ActionItemSubCounter.objects.get(parent=self.mother).last_sequence, 3)
        self.assertEqual(self.mother.sub_action_items.count(), 3)


class MotherBpuTests(ImportedMotherFixture, TestCase):
    def test_mother_bpu_is_inherited_by_every_sub_action(self):
        self.assertEqual([effective_bpu(s)[0] for s in self.subs], [False] * 3)
        response = self.api("patch", "actionitem-detail", [self.mother.id], {"is_bpu": True})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.mother.refresh_from_db()
        subs = list(self.mother.sub_action_items.order_by("id"))
        self.assertTrue(self.mother.is_bpu)
        self.assertEqual([effective_bpu(s) for s in subs], [(True, "mother")] * 3)
        self.assertEqual([s.progress_setting.is_bpu for s in subs], [False] * 3)  # no data rewritten

    def test_a_sub_action_cannot_carry_its_own_bpu(self):
        refused = self.api("patch", "actionitem-detail", [self.subs[0].id], {"is_bpu": True})
        self.assertEqual(refused.status_code, 400)
        self.assertFalse(ActionItem.objects.get(pk=self.subs[0].pk).is_bpu)
        created = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0}, is_bpu=True)
        self.assertEqual(created.status_code, 400)
        line_flag = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0, "is_bpu": True})
        self.assertEqual(line_flag.status_code, 400)

    def test_new_sub_actions_added_by_hand_inherit_the_mother(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        added = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual((added.data["effective_bpu"], added.data["bpu_source"]), (True, "mother"))

    def test_priority_rule_mother_wins_and_legacy_line_flags_are_kept_and_not_rewritten(self):
        ActionItemProgressSetting.objects.filter(action_item=self.subs[1]).update(is_bpu=True)  # MOD-02 data
        subs = list(self.mother.sub_action_items.order_by("id"))
        self.assertEqual([effective_bpu(s) for s in subs], [(False, None), (True, "legacy_line"), (False, None)])
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        subs = list(ActionItem.objects.get(pk=self.mother.pk).sub_action_items.order_by("id"))
        self.assertEqual([effective_bpu(s)[0] for s in subs], [True, True, True])
        self.assertTrue(ActionItemProgressSetting.objects.get(action_item=self.subs[1]).is_bpu)  # still stored
        self.assertFalse(ActionItemProgressSetting.objects.get(action_item=self.subs[0]).is_bpu)

    def test_list_serializers_show_bpu_through_the_mother_only_and_stay_query_free(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from project_management.serializers.action_item_list import ActionItemListChildSerializer

        def setting_queries(ctx):
            return sum("actionitemprogresssetting" in q["sql"].lower() for q in ctx)

        ActionItemProgressSetting.objects.filter(action_item=self.subs[1]).update(is_bpu=True)  # MOD-02 data
        mother_row = ActionItem.objects.prefetch_related("sub_action_items").get(pk=self.mother.pk)
        children = list(mother_row.sub_action_items.all())
        with CaptureQueriesContext(connection) as ctx:  # the list never looks up the historic flags
            flags = [ActionItemListChildSerializer(c, context={}).data["effective_bpu"] for c in children]
            mother_flag = ActionItemListChildSerializer(mother_row, context={}).data["effective_bpu"]
        self.assertEqual(setting_queries(ctx), 0)
        self.assertEqual(flags, [False, False, False])  # a historic line flag is not displayed in the list
        self.assertFalse(mother_flag)
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        mother_row = ActionItem.objects.prefetch_related("sub_action_items").get(pk=self.mother.pk)
        children = list(mother_row.sub_action_items.all())
        with CaptureQueriesContext(connection) as ctx:
            data = [ActionItemListChildSerializer(c, context={}).data["effective_bpu"] for c in children]
            mother_flag = ActionItemListChildSerializer(mother_row, context={}).data["effective_bpu"]
        self.assertEqual(setting_queries(ctx), 0)
        self.assertEqual((data, mother_flag), ([True, True, True], True))  # inheritance stays a business rule (API)
        # nothing was rewritten: the stored line flags are exactly what they were
        self.assertEqual(
            [ActionItemProgressSetting.objects.get(action_item=s).is_bpu for s in self.subs], [False, True, False]
        )

    def test_mother_bpu_cannot_change_once_the_mother_is_closed(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(status="closed")
        refused = self.api("patch", "actionitem-detail", [self.mother.id], {"is_bpu": True})
        self.assertEqual(refused.status_code, 400)
        self.assertFalse(ActionItem.objects.get(pk=self.mother.pk).is_bpu)

    def test_request_detail_and_weighted_progress_expose_the_effective_bpu_and_reference(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        with self.allow, patch("project_management.views.action_template.can_view_interventions", return_value=True):
            request = InterventionRequest.objects.get()
            data = self.client.get(reverse("intervention-request-detail", args=[request.id])).data
            by_id = {a["id"]: a for a in data["actions"]}
            self.assertTrue(by_id[self.mother.id]["is_bpu"])
            self.assertEqual([(by_id[s.id]["is_bpu"], by_id[s.id]["sub_reference"]) for s in self.subs],
                             [(True, s.sub_reference) for s in self.subs])
            progress = self.client.get(reverse("action-item-weighted-progress", args=[self.mother.id])).data
        self.assertEqual([(s["is_bpu"], s["sub_reference"]) for s in progress["sub_actions"]],
                         [(True, s.sub_reference) for s in self.subs])


class ProgressBarDataTests(ImportedMotherFixture, TestCase):
    """percent / state / to-close exposed for the PROSEVIEW progress bar (same calculation as MOD-01)."""

    def row(self, reference=None):
        self.client.force_authenticate(self.user)
        with self.allow:
            rows = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"]
        pick = [r for r in rows if reference is None or r["external_reference"] == reference][0]
        return pick["progress_percent"], pick["progress_status"], pick["progress_to_close"]

    def set_status(self, item, status):
        item.refresh_from_db()
        item.status = status
        if status == "closed":
            item.closed_at, item.closed_by = date(2026, 11, 10), self.user
        item.save()

    def test_not_started_intermediate_to_close_and_closed(self):
        self.assertEqual(self.row(), (0, "not_started", False))
        self.set_status(self.subs[0], "in progress")
        self.assertEqual(self.row(), (10, "in_progress", False))
        self.set_status(self.subs[0], "closed")
        self.assertEqual(self.row(), (30, "in_progress", False))
        # an extra open sub-action with 0 point: 100 % can be reached while something stays open
        added = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(added.status_code, 201, getattr(added, "data", None))
        self.set_status(self.subs[1], "closed")
        self.set_status(self.subs[2], "closed")
        self.assertEqual(self.row(), (100, "in_progress", True))  # orange + "to close", never green
        self.set_status(ActionItem.objects.get(pk=added.data["id"]), "closed")
        self.assertEqual(self.row(), (100, "completed", False))  # effective closing

    def test_a_mother_without_sub_action_goes_0_10_100(self):
        ActionItemProgressSetting.objects.filter(action_item__in=self.subs).delete()
        ActionItem.objects.filter(pk__in=[s.pk for s in self.subs])._raw_delete("default")  # no unrelated deletion signals
        self.assertEqual(self.row(), (0, "not_started", False))
        ActionItem.objects.filter(pk=self.mother.pk).update(status="in progress")
        self.assertEqual(self.row(), (10, "in_progress", False))
        ActionItem.objects.filter(pk=self.mother.pk).update(status="closed")
        self.assertEqual(self.row(), (100, "completed", False))

    def test_legacy_flat_request_uses_the_share_of_closed_actions(self):
        legacy = InterventionRequest.objects.create(
            project=self.project, reference="IR-LEG-9", title="Legacy", received_date=date(2026, 1, 1),
            expected_end_date=date(2026, 2, 1), source="csv", external_reference="OLD-9",
        )
        for index, key in enumerate(("preparation", "delivery", "assembly", "control")):
            ActionItem.objects.create(
                milestone=self.mother.milestone, name=f"x{key}", status="closed" if index == 0 else "open",
                target_date=date(2026, 2, 1), intervention_request=legacy, intervention_action_key=key,
            )
        self.assertEqual(self.row("OLD-9"), (25, "in_progress", False))

    def test_detail_lists_manually_added_sub_actions_too(self):
        added = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(added.status_code, 201, getattr(added, "data", None))
        request = InterventionRequest.objects.get()
        self.client.force_authenticate(self.user)
        with self.allow:
            data = self.client.get(reverse("intervention-request-detail", args=[request.id])).data
        ids = [a["id"] for a in data["actions"]]
        self.assertEqual(ids, sorted(ids))
        self.assertIn(added.data["id"], ids)  # visible although not linked to the request
        self.assertEqual(len(ids), 5)  # mother + 3 imported + 1 manual
        self.assertEqual((data["progress_percent"], data["progress_to_close"]), (0, False))

    def test_mother_status_is_the_real_status_and_the_calculation_is_unchanged(self):
        # MOD-03: the bar takes the colour of the mother's REAL status badge (custom statuses included);
        # a mother at 100 % that is still open keeps its real status, and the progress numbers do not move.
        self.client.force_authenticate(self.user)
        for item in self.subs:
            self.set_status(item, "closed")
        before = self.row()
        self.assertEqual(before, (100, "completed", False))
        for status in ("open", "in progress", "my custom status"):
            ActionItem.objects.filter(pk=self.mother.pk).update(status=status)
            with self.allow:
                rows = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"]
                request = InterventionRequest.objects.get()
                detail = self.client.get(reverse("intervention-request-detail", args=[request.id])).data
            self.assertEqual(rows[0]["mother_status"], status)
            self.assertEqual(detail["mother_status"], status)
            self.assertEqual(self.row(), before)  # same percent / state / to-close whatever the mother status is

    def test_mother_status_is_empty_for_a_legacy_request_without_a_mother(self):
        legacy = InterventionRequest.objects.create(
            project=self.project, reference="IR-LEG-8", title="Legacy", received_date=date(2026, 1, 1),
            expected_end_date=date(2026, 2, 1), source="csv", external_reference="OLD-8",
        )
        self.client.force_authenticate(self.user)
        with self.allow:
            rows = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"]
        self.assertIsNone([r for r in rows if r["external_reference"] == "OLD-8"][0]["mother_status"])

    def closure(self, reference=None):
        self.client.force_authenticate(self.user)
        with self.allow:
            rows = self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"]
        return [r for r in rows if reference is None or r["external_reference"] == reference][0]["closure_date"]

    def test_closure_date_is_empty_until_the_mother_is_really_closed(self):
        self.assertIsNone(self.closure())
        for sub in self.subs[:2]:
            self.set_status(sub, "closed")  # sub-actions closed one by one: still no closure date
        self.assertIsNone(self.closure())
        # never deduced from the expected end date or the last modification
        self.assertIsNotNone(InterventionRequest.objects.get().expected_end_date)

    def test_closure_date_is_the_effective_date_of_the_mother_action(self):
        self.set_status(self.mother, "closed")  # closed_at = 2026-11-10 (the mother's own date)
        self.assertEqual(str(self.closure()), "2026-11-10")

    def test_closure_date_follows_the_automatic_closing_of_the_mother(self):
        self.client.force_authenticate(self.user)
        for sub in self.subs:
            response = self.api("patch", "actionitem-detail", [sub.id], {"status": "closed"})
            self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "closed")
        self.assertIsNotNone(self.mother.closed_at)
        self.assertEqual(self.closure(), self.mother.closed_at)

    def test_a_closed_mother_without_recorded_closing_date_gives_no_invented_date(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(status="closed", closed_at=None)
        self.assertIsNone(self.closure())

    def test_legacy_request_without_mother_has_no_closure_date_even_if_all_closed(self):
        legacy = InterventionRequest.objects.create(
            project=self.project, reference="IR-LEG-8", title="Legacy", received_date=date(2026, 1, 1),
            expected_end_date=date(2026, 2, 1), source="csv", external_reference="OLD-8",
        )
        for key in ("preparation", "delivery"):
            ActionItem.objects.create(
                milestone=self.mother.milestone, name=f"x{key}", status="closed", closed_at=date(2026, 2, 1),
                target_date=date(2026, 2, 1), intervention_request=legacy, intervention_action_key=key,
            )
        self.assertIsNone(self.closure("OLD-8"))

    def test_a_page_of_requests_costs_no_query_per_row(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_authenticate(self.user)

        def queries():
            with self.allow, CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("intervention-requests"), {"project": self.project.id})
            return len(ctx)

        one = queries()
        for index in range(4):
            confirm_import(batch_id=self.preview(csv_bytes(ROW_B.replace("EXT-002", f"EXT-X{index}").replace("salle B", "salle A"))).id, user=self.user)
        self.assertEqual(InterventionRequest.objects.count(), 5)
        self.assertLessEqual(queries(), one + 1)


class EditWindowEchoTests(ImportedMotherFixture, TestCase):
    def test_the_detail_window_can_save_a_status_change_that_echoes_the_read_only_points(self):
        # Found in the browser: the window PATCHes the whole representation back, progress_setting included.
        sub = self.subs[0]
        read = self.api("get", "actionitem-detail", [sub.id]).data
        self.assertEqual(read["progress_setting"]["closed_points"], 30)
        response = self.api("patch", "actionitem-detail", [sub.id], {
            "status": "closed", "progress_setting": read["progress_setting"], "sibling_progress": [],
            "effective_bpu": read["effective_bpu"], "sub_reference": read["sub_reference"],
        })
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        sub.refresh_from_db()
        self.assertEqual(sub.status, "closed")
        setting = ActionItemProgressSetting.objects.get(action_item=sub)
        self.assertEqual((setting.in_progress_points, setting.closed_points), (10, 30))  # never applied on update

    def test_an_echoed_progress_setting_cannot_change_the_points_of_a_sub_action(self):
        sub = self.subs[1]
        response = self.api("patch", "actionitem-detail", [sub.id], {
            "name": "Montage renommé", "progress_setting": {"in_progress_points": 0, "closed_points": 99},
        })
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(ActionItemProgressSetting.objects.get(action_item=sub).closed_points, 20)

    def test_the_window_can_save_an_action_whose_echoed_progress_setting_is_null(self):
        # MOD-03 recette: an action without points is read with progress_setting = null and echoes it back.
        mother = self.mother
        read = self.api("get", "actionitem-detail", [mother.id]).data
        self.assertIsNone(read["progress_setting"])
        response = self.api("patch", "actionitem-detail", [mother.id], {
            "name": "Mère renommée", "progress_setting": None,
        })
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        mother.refresh_from_db()
        self.assertEqual(mother.name, "Mère renommée")
        self.assertFalse(ActionItemProgressSetting.objects.filter(action_item=mother).exists())


class MotherPanelSubActionsTests(ImportedMotherFixture, TestCase):
    """MOD-03: the mother panel lists ALL its visible sub-actions (imported, manual, closed) with their reference."""

    def listing(self, user=None):
        self.client.force_authenticate(user or self.user)
        return self.api("get", "actionitem-sub-action-items", [self.mother.id], {"page_size": 1000})

    def test_imported_manual_and_closed_sub_actions_are_all_listed_in_order(self):
        manual = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(manual.status_code, 201, getattr(manual, "data", None))
        close(self.subs[0], self.user)  # a closed sub-action stays visible
        response = self.listing()
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        rows = response.data["results"]
        ids = [row["id"] for row in rows]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(set(ids), {s.id for s in self.subs} | {manual.data["id"]})
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[self.subs[0].id]["status"], "closed")
        self.assertTrue(all(row["sub_reference"] for row in rows))  # ID-mother-n
        self.assertEqual(by_id[manual.data["id"]]["sub_reference"], ActionItem.objects.get(pk=manual.data["id"]).sub_reference)
        for row in rows:  # fields of the row: owner / due date available, no BPU on a sub-action
            self.assertIn("target_date", row)
            self.assertIn("assignees", row)
            self.assertNotIn("is_bpu", row)
            self.assertNotIn("effective_bpu", row)

    def test_another_company_cannot_read_the_sub_actions_of_a_mother(self):
        # real permission classes (no patch): company / project access is enforced by the server
        self.client.force_authenticate(self.other_user)
        url = reverse("actionitem-sub-action-items", args=[self.mother.id])
        self.assertIn(self.client.get(url, {"page_size": 1000}).status_code, (403, 404))
        self.assertIn(self.client.get(reverse("actionitem-detail", args=[self.mother.id])).status_code, (403, 404))

    def test_only_the_sub_actions_of_this_mother_are_listed(self):
        response = self.listing()
        self.assertEqual(len(response.data["results"]), 3)  # only this mother's imported lines


class LimitedUserPanelTests(ImportedMotherFixture, TestCase):
    """MOD-03: a user limited to the actions assigned to / created by them (no broad right is added for
    the panel). Only the visibility rules of the existing endpoints apply; the mother stays reachable
    for navigation, the other sub-actions stay hidden."""

    VIEW = "project_management.views.action_item.require_any_permission_multiple_modules"
    LOW = "ecpmpapp.permissions.BaseModulePermission._check_permission"
    BROAD = "project_management.permissions.check_company_project_permission"

    def setUp(self):
        super().setUp()
        self.limited = self.member("limited-panel@example.com")
        self.mine, self.others = self.subs[0], self.subs[1:]
        self.mine.assignees.add(self.limited)

    @staticmethod
    def only_assigned_created(user, modules, scope, context):
        module, actions = next(iter(modules.items()))
        return module == "action_item" and tuple(actions) == ("view_assigned_created",)

    @staticmethod
    def low_level(permission, user, module, action, scope, context):
        return module == "action_item" and action == "view_assigned_created"

    def get(self, name, args, user, **params):
        """Real permission classes (object-level rules included); only the user's ROLE is simulated:
        "view assigned / created" on Action Items, nothing broader."""
        self.client.force_authenticate(user)
        with patch(self.VIEW, side_effect=self.only_assigned_created),                 patch(self.LOW, autospec=True, side_effect=self.low_level),                 patch(self.BROAD, return_value=False):
            return self.client.get(reverse(name, args=args), params)

    def test_the_sub_action_list_of_the_mother_only_shows_what_the_user_may_see(self):
        response = self.get("actionitem-sub-action-items", [self.mother.id], self.limited, page_size=1000)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual([row["id"] for row in response.data["results"]], [self.mine.id])
        self.assertTrue(response.data["results"][0]["sub_reference"])

    def test_direct_access_to_an_assigned_sub_action_and_to_its_mother_for_navigation(self):
        sub = self.get("actionitem-detail", [self.mine.id], self.limited)
        self.assertEqual(sub.status_code, 200, getattr(sub, "data", None))
        mother = self.get("actionitem-detail", [self.mother.id], self.limited)  # "back to the mother"
        self.assertEqual(mother.status_code, 200, getattr(mother, "data", None))
        self.assertEqual(mother.data["id"], self.mother.id)

    def test_direct_access_to_a_sub_action_that_is_not_theirs_is_refused(self):
        for other in self.others:
            self.assertEqual(self.get("actionitem-detail", [other.id], self.limited).status_code, 403)

    def test_the_rights_are_not_widened_by_the_panel(self):
        # a user without any assigned / created action sees nothing, not even the mother
        stranger = self.member("stranger-panel@example.com")
        self.assertEqual(self.get("actionitem-detail", [self.mother.id], stranger).status_code, 403)
        listing = self.get("actionitem-sub-action-items", [self.mother.id], stranger, page_size=1000)
        self.assertEqual(listing.status_code, 403)

    def get_with(self, name, args, user, allowed):
        """Same as get(), with the given low-level action keys granted on Action Items."""
        self.client.force_authenticate(user)
        low = lambda permission, u, module, action, scope, context: module == "action_item" and action in allowed
        with patch(self.VIEW, side_effect=self.only_assigned_created), patch(self.LOW, autospec=True, side_effect=low), \
                patch(self.BROAD, return_value=False):
            return self.client.get(reverse(name, args=args))

    def test_view_only_user_gets_a_read_only_mother_and_sub_action(self):
        # the server-effective right is exposed on the detail, and a view-only role has none
        mother = self.get("actionitem-detail", [self.mother.id], self.limited)
        sub = self.get("actionitem-detail", [self.mine.id], self.limited)
        self.assertIs(mother.data["can_update"], False)
        self.assertIs(sub.data["can_update"], False)

    def test_mother_reached_through_an_authorised_child_stays_read_only_while_the_child_keeps_its_own_right(self):
        allowed = {"view_assigned_created", "update_assigned_created"}
        mother = self.get_with("actionitem-detail", [self.mother.id], self.limited, allowed)
        sub = self.get_with("actionitem-detail", [self.mine.id], self.limited, allowed)
        other = self.get_with("actionitem-detail", [self.others[0].id], self.limited, allowed)
        self.assertIs(mother.data["can_update"], False)  # neither assigned to nor created by the user
        self.assertIs(sub.data["can_update"], True)  # assigned: its own right, unchanged
        self.assertEqual(other.status_code, 403)  # not theirs: still refused

    def test_can_update_is_not_added_to_lists_and_the_patch_guard_is_unchanged(self):
        self.client.force_authenticate(self.limited)
        with patch(self.VIEW, side_effect=self.only_assigned_created), patch(self.LOW, autospec=True, side_effect=self.low_level), \
                patch(self.BROAD, return_value=False):
            listing = self.client.get(reverse("actionitem-sub-action-items", args=[self.mother.id]), {"page_size": 1000})
            refused = self.client.patch(reverse("actionitem-detail", args=[self.mother.id]), {"name": "x"}, format="json")
        self.assertEqual(listing.status_code, 200)
        self.assertFalse(any(row.get("can_update") for row in listing.data["results"]))
        self.assertEqual(refused.status_code, 403)

    def test_another_company_is_refused_even_for_an_assigned_sub_action(self):
        self.client.force_authenticate(self.other_user)  # real permission classes
        for name, args in (
            ("actionitem-detail", [self.mine.id]),
            ("actionitem-detail", [self.mother.id]),
            ("actionitem-sub-action-items", [self.mother.id]),
        ):
            self.assertIn(self.client.get(reverse(name, args=args)).status_code, (403, 404), name)


class ShowClosedItemsTests(ImportedMotherFixture, TestCase):
    """MOD-03: "Show closed items" must bring back the closed mothers the user is allowed to see."""

    CHECKER = "project_management.views.action_item.require_any_permission_multiple_modules"

    def listing(self, user=None, **params):
        self.client.force_authenticate(user or self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(self.CHECKER, return_value=True):
            response = self.client.get(
                reverse("actionitem-list"), {"project": self.project.id, "page_size": 1000, **params}
            )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return {row["id"]: row for row in response.data["results"]}

    def close_all(self):
        for item in (*self.subs, self.mother):
            close(item, self.user)

    def test_a_closed_mother_is_hidden_by_default_and_listed_with_show_closed(self):
        self.close_all()
        self.assertNotIn(self.mother.id, self.listing())
        shown = self.listing(show_closed="true")
        self.assertIn(self.mother.id, shown)
        self.assertEqual(shown[self.mother.id]["status"], "closed")
        # its (closed) sub-actions come with it
        self.assertEqual({c["id"] for c in shown[self.mother.id]["sub_action_items"]}, {s.id for s in self.subs})

    def test_open_mothers_are_unaffected_by_the_toggle(self):
        self.assertIn(self.mother.id, self.listing())
        self.assertIn(self.mother.id, self.listing(show_closed="true"))

    def test_show_closed_never_widens_visibility(self):
        self.close_all()
        # another company: nothing, closed or not
        self.client.force_authenticate(self.other_user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(self.CHECKER, return_value=True):
            response = self.client.get(
                reverse("actionitem-list"), {"project": self.project.id, "page_size": 1000, "show_closed": "true"}
            )
        rows = response.data["results"] if response.status_code == 200 else []
        self.assertNotIn(self.mother.id, [row["id"] for row in rows])


class ManualTemplateChoiceTests(PlanFixture, TestCase):
    def upload(self, raw):
        self.client.force_authenticate(self.user)
        return self.client.post(reverse("intervention-import-preview"),
                                {"project": self.project.id, "file": SimpleUploadedFile("c.csv", raw)}, format="multipart")

    def choose(self, batch_id, line_number, template_id):
        return self.client.patch(reverse("intervention-import-row", args=[batch_id, line_number]),
                                 {"apply_template": template_id}, format="json")

    def test_a_template_can_be_chosen_by_hand_even_if_the_title_differs(self):
        chosen = self.template("Gabarit confinement", SERIES_B)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]  # no template for "Confinement salle A"
            self.assertEqual(self.client.get(reverse("intervention-import-batch", args=[batch_id])).data["rows"][0]["plan"], {})
            response = self.choose(batch_id, 2, chosen.pk)
            self.assertEqual(response.status_code, 200, response.data)
            plan = response.data["row"]["plan"]
            self.assertEqual([l["title"] for l in plan["sub_actions"]], ["Étude", "Livraison"])
            self.assertEqual((plan["template_id"], plan["is_bpu"], plan["save_as_template"]), (chosen.pk, False, False))
            self.assertEqual(response.data["summary"]["rows_needing_plan"], 0)
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        mother = ActionItem.objects.get(intervention_action_key="mother")
        self.assertEqual(mother.name, "Confinement salle A")  # request/mother title kept
        self.assertEqual(InterventionRequest.objects.get().title, "Confinement salle A")
        self.assertEqual(list(mother.sub_action_items.order_by("id").values_list("name", flat=True)), ["Étude", "Livraison"])

    def test_chosen_template_can_then_be_edited_without_touching_the_company_template(self):
        chosen = self.template("Gabarit confinement", SERIES_B)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.choose(batch_id, 2, chosen.pk)
            edited = {"template_id": chosen.pk, "is_bpu": True, "sub_actions": [line("Seule", 10, 100)]}
            self.assertEqual(self.client.patch(reverse("intervention-import-row", args=[batch_id, 2]), {"plan": edited}, format="json").status_code, 200)
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
        chosen.refresh_from_db()
        self.assertEqual(list(chosen.sub_actions.order_by("position").values_list("title", "closed_points")), [("Étude", 40), ("Livraison", 60)])
        self.assertEqual(ActionTemplate.objects.count(), 1)

    def test_choosing_replaces_an_edited_plan_server_side_only_when_asked(self):
        first, second = self.template("Gabarit 1", SERIES_A), self.template("Gabarit 2", SERIES_B)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.choose(batch_id, 2, first.pk)
            self.client.patch(reverse("intervention-import-row", args=[batch_id, 2]),
                              {"plan": {"template_id": first.pk, "sub_actions": [line("Édité", 0, 100)]}}, format="json")
            row = InterventionImportBatch.objects.get(pk=batch_id).rows.get(line_number=2)
            self.assertEqual([l["title"] for l in row.plan["sub_actions"]], ["Édité"])  # plain edits never re-apply a template
            self.choose(batch_id, 2, second.pk)  # explicit replacement
            row.refresh_from_db()
            self.assertEqual([l["title"] for l in row.plan["sub_actions"]], ["Étude", "Livraison"])

    def test_foreign_ambiguous_or_unknown_templates_are_refused(self):
        foreign = self.template("Gabarit étranger", SERIES_A, company=self.other_company)
        ambiguous = self.template("Gabarit ambigu", SERIES_A, needs_review=True)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            for bad in (foreign.pk, ambiguous.pk, 999999, "abc", True):
                response = self.choose(batch_id, 2, bad)
                self.assertEqual((bad, response.status_code, response.data["code"]), (bad, 400, "template_invalid"))
        self.assertEqual(InterventionImportBatch.objects.get(pk=batch_id).rows.get(line_number=2).plan, {})

    def test_templates_endpoint_lists_only_the_company_templates_for_importers(self):
        mine = self.template("Mine", SERIES_A)
        self.template("Foreign", SERIES_B, company=self.other_company)
        self.template("Ambiguous", SERIES_B, needs_review=True)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            url = reverse("intervention-import-templates", args=[batch_id])
            data = self.client.get(url).data
            self.assertEqual([(t["id"], t["title"]) for t in data], [(mine.pk, "Mine")])
            self.assertNotIn("is_bpu", data[0]["sub_actions"][0])  # BPU is never part of a template
            self.client.force_authenticate(self.other_user)
            self.assertEqual(self.client.get(url).status_code, 404)  # other company: batch not visible
        self.client.force_authenticate(self.user)
        with patch.multiple("project_management.views.intervention", can_import_interventions=lambda u, p: False):
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_a_confirmed_batch_can_no_longer_take_a_template(self):
        chosen = self.template("Gabarit", SERIES_A)
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.choose(batch_id, 2, chosen.pk)
            self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
            self.assertEqual(self.choose(batch_id, 2, chosen.pk).status_code, 409)


class RequestWithoutSubActionTests(PlanFixture, TestCase):
    def upload(self, raw):
        self.client.force_authenticate(self.user)
        return self.client.post(reverse("intervention-import-preview"),
                                {"project": self.project.id, "file": SimpleUploadedFile("c.csv", raw)}, format="multipart")

    def empty_plan(self, batch_id, line_number=2, **extra):
        return self.client.patch(reverse("intervention-import-row", args=[batch_id, line_number]),
                                 {"plan": {"sub_actions": [], "no_sub_actions": True, **extra}}, format="json")

    def import_without_sub_actions(self, bpu=False):
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            self.assertEqual(self.empty_plan(batch_id, is_bpu=bpu).status_code, 200)
            self.assertEqual(self.client.post(reverse("intervention-import-confirm", args=[batch_id])).status_code, 200)
        return ActionItem.objects.get(intervention_action_key="mother")

    def test_an_empty_list_needs_the_explicit_confirmation_and_no_100_total(self):
        with self.allow:
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            blocked = self.client.post(reverse("intervention-import-confirm", args=[batch_id]))
            self.assertEqual((blocked.status_code, blocked.data["code"]), (400, "plan_incomplete"))
            implicit = self.client.patch(reverse("intervention-import-row", args=[batch_id, 2]), {"plan": {"sub_actions": []}}, format="json")
            self.assertEqual((implicit.status_code, implicit.data["code"]), (400, "no_sub_action"))
            explicit = self.empty_plan(batch_id)
            self.assertEqual(explicit.status_code, 200, explicit.data)
            self.assertEqual((explicit.data["summary"]["rows_needing_plan"], explicit.data["summary"]["actions_to_create"]), (0, 1))

    def test_inconsistent_empty_plans_are_refused(self):
        with self.allow, patch("project_management.services.intervention_plan.can_manage_templates", return_value=True):
            batch_id = self.upload(csv_bytes(ROW_A)).data["id"]
            both = self.client.patch(reverse("intervention-import-row", args=[batch_id, 2]),
                                     {"plan": {"no_sub_actions": True, "sub_actions": lines_of(SERIES_B)}}, format="json")
            self.assertEqual(both.status_code, 400)
            template = self.empty_plan(batch_id, save_as_template=True)
            self.assertEqual((template.status_code, template.data["code"]), (400, "template_needs_sub_action"))
            flag = self.client.patch(reverse("intervention-import-row", args=[batch_id, 2]),
                                     {"plan": {"no_sub_actions": "yes", "sub_actions": []}}, format="json")
            self.assertEqual(flag.status_code, 400)

    def test_only_the_mother_is_created_and_no_template(self):
        mother = self.import_without_sub_actions(bpu=True)
        self.assertEqual(mother.sub_action_items.count(), 0)
        self.assertEqual((mother.name, mother.is_bpu, mother.start_date), ("Confinement salle A", True, None))
        self.assertEqual(ActionItem.objects.filter(intervention_request__isnull=False).count(), 1)
        self.assertEqual(ActionTemplate.objects.count(), 0)

    def test_mother_follows_its_status_0_10_100_with_the_mod_01_mapping(self):
        mother = self.import_without_sub_actions()
        path = [compute_progress(mother)]
        self.assertEqual((path[0]["mode"], path[0]["progress"]), ("mother_only", 0))
        for status, expected in (("in progress", 10), ("closed", 100)):
            mother.refresh_from_db()
            mother.status = status
            if status == "closed":
                mother.closed_at, mother.closed_by = date(2026, 11, 10), self.user
            mother.save()
            self.assertEqual(compute_progress(mother)["progress"], expected)
        other = ActionItem.objects.get(pk=mother.pk)
        ActionItem.objects.filter(pk=mother.pk).update(status="En attente client")
        other.refresh_from_db()
        unknown = compute_progress(other)
        self.assertEqual((unknown["progress"], unknown["unmapped_statuses"]), (0, ["En attente client"]))  # never 10

    def test_request_list_follows_the_mother_when_it_has_no_sub_action(self):
        mother = self.import_without_sub_actions()
        self.client.force_authenticate(self.user)
        with self.allow:
            progress = lambda: self.client.get(reverse("intervention-requests"), {"project": self.project.id}).data["results"][0]["progress_status"]  # noqa: E731
            self.assertEqual(progress(), "not_started")
            ActionItem.objects.filter(pk=mother.pk).update(status="in progress")
            self.assertEqual(progress(), "in_progress")
            ActionItem.objects.filter(pk=mother.pk).update(status="closed")
            self.assertEqual(progress(), "completed")

    def test_first_manual_addition_switches_to_contributions_with_a_coherent_validation(self):
        mother = self.import_without_sub_actions()
        owner = self.member("first-add@example.com")
        self.client.force_authenticate(self.user)

        def add(**extra):
            body = {"name": "Première", "parent_action_item": mother.id, "status": "open", "target_date": "2026-11-15",
                    "assignees": [owner.id], **extra}
            with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True):
                return self.client.post(reverse("actionitem-list"), body, format="json")

        self.assertEqual(add().status_code, 400)  # points are explicit, never guessed
        self.assertEqual(add(progress_setting={"in_progress_points": 0, "closed_points": 60}).status_code, 400)  # must total 100
        self.assertEqual(mother.sub_action_items.count(), 0)
        first = add(progress_setting={"in_progress_points": 10, "closed_points": 100})
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["sub_reference"], f"{mother.id}-1")
        result = compute_progress(mother)
        self.assertEqual((result["mode"], result["progress"]), ("weighted", 0))  # contributions only, no 0/10/100 leftover
        sub = ActionItem.objects.get(pk=first.data["id"])
        sub.status = "in progress"
        sub.save()
        self.assertEqual(compute_progress(mother)["progress"], 10)  # in-progress contribution (10), not the mother's own 10 rule
        sub.status = "closed"
        sub.closed_at, sub.closed_by = date(2026, 11, 11), self.user
        sub.save()
        mother.refresh_from_db()
        self.assertEqual((mother.status, compute_progress(mother)["progress"]), ("closed", 100))  # automatic closing (MOD-01)

    def test_a_closed_mother_stays_locked_for_a_first_addition(self):
        mother = self.import_without_sub_actions()
        ActionItem.objects.filter(pk=mother.pk).update(status="closed")
        body = {"name": "x", "parent_action_item": mother.id, "status": "open", "target_date": "2026-11-15",
                "assignees": [self.member("late@example.com").id], "progress_setting": {"in_progress_points": 0, "closed_points": 100}}
        self.client.force_authenticate(self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True):
            self.assertEqual(self.client.post(reverse("actionitem-list"), body, format="json").status_code, 400)
        self.assertEqual(mother.sub_action_items.count(), 0)

    def test_plain_actions_without_sub_action_keep_the_classic_behaviour(self):
        plain = self.create_parent("Plain")
        self.assertEqual(compute_progress(plain)["mode"], "unweighted")  # no 0/10/100 rule outside the import mothers


class TitleLimitTests(PlanFixture, TestCase):
    def test_a_title_over_255_characters_is_refused_explicitly_and_never_truncated(self):
        long_title = "T" * 256
        ok_title = "U" * 255
        raw = csv_bytes(f"EXT-L;{long_title};03/11/2026;20/11/2026;;;\n", f"EXT-OK;{ok_title};03/11/2026;20/11/2026;;;\n")
        batch = self.preview(raw)
        rows = {r.external_reference: r for r in batch.rows.all()}
        self.assertEqual((rows["EXT-L"].outcome, rows["EXT-L"].errors), ("error", ["title_too_long"]))
        self.assertEqual(rows["EXT-OK"].outcome, "new")
        self.assertEqual(rows["EXT-L"].data["title"], long_title)  # shown as received, not cut

    def test_the_api_answers_a_row_error_not_a_500_and_nothing_is_created_for_it(self):
        self.template("U" * 255, SERIES_A)
        raw = csv_bytes(f"EXT-L;{'T' * 300};03/11/2026;20/11/2026;;;\n", f"EXT-OK;{'U' * 255};03/11/2026;20/11/2026;;;\n")
        self.client.force_authenticate(self.user)
        with self.allow:
            response = self.client.post(reverse("intervention-import-preview"),
                                        {"project": self.project.id, "file": SimpleUploadedFile("c.csv", raw)}, format="multipart")
            self.assertEqual(response.status_code, 201, response.data)
            outcomes = {r["external_reference"]: (r["outcome"], r["errors"]) for r in response.data["rows"]}
            self.assertEqual(outcomes["EXT-L"], ("error", ["title_too_long"]))
            confirmed = self.client.post(reverse("intervention-import-confirm", args=[response.data["id"]]))
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(list(InterventionRequest.objects.values_list("external_reference", flat=True)), ["EXT-OK"])
        self.assertEqual(ActionItem.objects.get(intervention_action_key="mother").name, "U" * 255)
