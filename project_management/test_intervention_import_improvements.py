"""Import / intervention sheet improvements: BPU correction on the mother, BPU chosen at the selection step
of the import, atomic adaptation of the sub-actions after the import, import date and its period filter."""
from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from project_management.models import (
    ActionItem,
    ActionItemProgressSetting,
    ActionTemplate,
    InterventionImportBatch,
    InterventionRequest,
)
from project_management.services.action_progress import compute_progress
from project_management.services.intervention_import import confirm_import
from project_management.services.intervention_import_date import import_date_of, period_bounds
from project_management.test_action_item_list_columns import CHECKER, PERM, ListFixture
from project_management.test_actions_interventions_separation import SeparationFixture
from project_management.test_action_templates import make_project
from project_management.test_intervention_import import ROW_A, ROW_B, csv_bytes
from project_management.test_intervention_plans import SERIES_A, SERIES_B, line

IMPORT_DATE = "project_management.services.intervention_import_date.timezone.localdate"


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


def snapshot(mother):
    return [
        (s.pk, s.name, s.status, s.progress_setting.in_progress_points, s.progress_setting.closed_points)
        for s in mother.sub_action_items.select_related("progress_setting").order_by("id")
    ]


class SubsMixin:

    def put_subs(self, lines, user=None, **patches):
        self.client.force_authenticate(user or self.user)
        update = patches.get("update", True)
        delete = patches.get("delete", True)
        with patch(f"{PERM}.can_update_object", return_value=update), \
                patch(f"{PERM}._check_delete_permission_project", return_value=delete):
            return self.client.put(
                reverse("intervention-sub-actions", args=[patches.get("pk", self.mother.pk)]),
                {"sub_actions": lines}, format="json",
            )

    def current_lines(self):
        return [
            {"id": s.pk, "title": s.name, "in_progress_points": s.progress_setting.in_progress_points,
             "closed_points": s.progress_setting.closed_points}
            for s in self.mother.sub_action_items.select_related("progress_setting").order_by("id")
            if hasattr(s, "progress_setting")
        ]

    def kpis(self, **params):
        self.client.force_authenticate(self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            response = self.client.get(reverse("actionitem-intervention-kpis"), {"project": self.project.id, **params})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data


class Fixture(SubsMixin, ListFixture):
    """A mother imported from ROW_A with the three template sub-actions (30/20/50 closed points)."""


class DateFixture(SubsMixin, SeparationFixture):
    pass


class BpuCorrectionTests(Fixture, TestCase):
    def test_bpu_is_corrected_on_the_mother_and_shown_on_the_mother_only(self):
        response = self.api("patch", "actionitem-detail", [self.mother.id], {"is_bpu": True})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.mother.refresh_from_db()
        self.assertTrue(self.mother.is_bpu)
        rows = self.listing(scope="interventions")
        self.assertTrue(rows[self.mother.id]["effective_bpu"])
        again = self.api("patch", "actionitem-detail", [self.mother.id], {"is_bpu": False})
        self.assertEqual(again.status_code, 200)
        self.mother.refresh_from_db()
        self.assertFalse(self.mother.is_bpu)

    def test_the_kpi_follows_the_correction(self):
        self.assertEqual(self.kpis()["portfolio"]["bpu"], 0)
        self.api("patch", "actionitem-detail", [self.mother.id], {"is_bpu": True})
        self.assertEqual(self.kpis()["portfolio"]["bpu"], 1)
        self.api("patch", "actionitem-detail", [self.mother.id], {"is_bpu": False})
        self.assertEqual(self.kpis()["portfolio"]["bpu"], 0)

    def test_a_sub_action_cannot_be_changed_and_an_unchanged_echo_is_accepted(self):
        sub = self.subs[0]
        self.assertEqual(self.api("patch", "actionitem-detail", [sub.id], {"is_bpu": True}).status_code, 400)
        self.assertEqual(self.api("patch", "actionitem-detail", [sub.id], {"is_bpu": False}).status_code, 200)  # echo
        sub.refresh_from_db()
        self.assertFalse(sub.is_bpu)

    def test_a_classic_action_has_no_bpu(self):
        classic = self.create_parent("Classic")
        self.assertEqual(self.api("patch", "actionitem-detail", [classic.id], {"is_bpu": True}).status_code, 400)
        classic.refresh_from_db()
        self.assertFalse(classic.is_bpu)
        self.assertEqual(self.api("patch", "actionitem-detail", [classic.id], {"is_bpu": False}).status_code, 200)

    def test_without_the_update_right_nothing_changes(self):
        self.client.force_authenticate(self.other_user)  # another company: no access at all
        response = self.client.patch(reverse("actionitem-detail", args=[self.mother.id]), {"is_bpu": True}, format="json")
        self.assertIn(response.status_code, (403, 404))
        self.mother.refresh_from_db()
        self.assertFalse(self.mother.is_bpu)

    def test_the_inheritance_is_unchanged(self):
        from project_management.services.action_progress import effective_bpu

        self.api("patch", "actionitem-detail", [self.mother.id], {"is_bpu": True})
        self.assertEqual([effective_bpu(ActionItem.objects.get(pk=s.pk))[0] for s in self.subs], [True] * 3)
        self.assertEqual([s.progress_setting.is_bpu for s in ActionItem.objects.get(pk=self.mother.pk).sub_action_items.all()], [False] * 3)


class ImportBpuStepTests(Fixture, TestCase):
    """BPU is chosen on the selection step, before the sub-actions, and survives every later edit."""

    def setUp(self):
        super().setUp()
        self.template("Confinement salle B", SERIES_B)
        self.batch = self.preview(csv_bytes(ROW_B))  # EXT-002 matches the template: a plan is proposed
        self.line = self.batch.rows.get().line_number

    def patch_row(self, body):
        self.client.force_authenticate(self.user)
        with self.allow:
            return self.client.patch(
                reverse("intervention-import-row", args=[self.batch.id, self.line]), body, format="json"
            )

    def plan(self):
        return self.batch.rows.get().plan

    def test_bpu_is_stored_on_the_row_and_kept_when_the_plan_is_edited_without_it(self):
        self.assertEqual(self.patch_row({"is_bpu": True}).status_code, 200)
        self.assertTrue(self.plan()["is_bpu"])
        edited = self.patch_row({"plan": {"template_id": None, "no_sub_actions": False, "sub_actions": [
            line("Étude", 0, 40, assignee_id=None), line("Livraison", 20, 60, assignee_id=None)]}})
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertTrue(self.plan()["is_bpu"])

    def test_bpu_survives_a_template_change_and_reaches_the_mother(self):
        other = self.template("Autre modèle", SERIES_A)
        self.patch_row({"is_bpu": True})
        self.assertEqual(self.patch_row({"apply_template": other.pk}).status_code, 200)
        self.assertTrue(self.plan()["is_bpu"])
        confirm_import(batch_id=self.batch.id, user=self.user)
        mother = ActionItem.objects.get(intervention_request__external_reference="EXT-002", intervention_action_key="mother")
        self.assertTrue(mother.is_bpu)

    def test_bpu_can_be_chosen_before_any_plan_exists_and_the_row_still_needs_its_plan(self):
        batch = self.preview(csv_bytes(ROW_A.replace("EXT-001", "EXT-777").replace("Confinement salle A", "Sans modèle")))
        row = batch.rows.get()
        self.assertEqual(row.plan, {})
        self.client.force_authenticate(self.user)
        with self.allow:
            response = self.client.patch(
                reverse("intervention-import-row", args=[batch.id, row.line_number]), {"is_bpu": True}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.plan, {"is_bpu": True})
        batch.refresh_from_db()
        self.assertEqual(batch.summary["rows_needing_plan"], 1)  # a plan is still required

    def test_a_non_boolean_bpu_is_refused(self):
        self.assertEqual(self.patch_row({"is_bpu": "yes"}).status_code, 400)
        self.assertFalse(self.plan()["is_bpu"])


class ReimportTests(Fixture, TestCase):
    def test_reimport_overwrites_neither_a_manual_bpu_correction_nor_the_import_date(self):
        request = InterventionRequest.objects.get(pk=self.mother.intervention_request_id)
        first_date = import_date_of(InterventionRequest.objects.select_related("import_batch").get(pk=request.pk))
        first_batch = request.import_batch_id
        InterventionImportBatch.objects.filter(pk=request.import_batch_id).update(confirmed_at=utc(2026, 11, 3, 10))
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)  # manual correction
        again = self.preview(csv_bytes(ROW_A))
        self.assertEqual(again.rows.get().outcome, "already_imported")
        confirm_import(batch_id=again.id, user=self.user)
        request.refresh_from_db()
        self.mother.refresh_from_db()
        self.assertTrue(self.mother.is_bpu)
        self.assertEqual(request.import_batch_id, first_batch)  # the first import stays the origin
        self.assertNotEqual(request.import_batch_id, again.id)
        self.assertEqual(import_date_of(InterventionRequest.objects.select_related("import_batch").get(pk=request.pk)), date(2026, 11, 3))
        self.assertIsNotNone(first_date)
        self.assertEqual(InterventionRequest.objects.count(), 1)  # no duplicate

    def test_assignment_after_import_the_unassigned_counter_and_the_reimport(self):
        owner = self.member("assign@example.com")
        self.assertFalse(self.mother.assignees.exists())  # created without owner
        self.assertFalse(any(sub.assignees.exists() for sub in self.subs))
        self.assertEqual(self.kpis()["todo"]["unassigned"], 1)
        self.subs[1].assignees.set([owner])  # one sub-action assigned on its own: the intervention is taken in charge
        self.assertEqual(self.kpis()["todo"]["unassigned"], 0)
        self.assertFalse(self.mother.assignees.exists())  # nothing copied up to the mother
        self.assertFalse(self.subs[0].assignees.exists())
        self.mother.assignees.set([owner])  # the mother is assigned from its sheet
        self.assertFalse(self.subs[0].assignees.exists())  # nothing copied down either
        self.assertEqual([u.pk for u in self.subs[1].assignees.all()], [owner.pk])
        snapshot = lambda: sorted((a.pk, tuple(a.assignees.values_list("pk", flat=True))) for a in ActionItem.objects.filter(intervention_request__isnull=False) | ActionItem.objects.filter(parent_action_item=self.mother))  # noqa: E731
        before = snapshot()
        for raw in (csv_bytes(ROW_A), csv_bytes(ROW_A.replace("Premier", "Modifié"))):
            confirm_import(batch_id=self.preview(raw).id, user=self.user)
        self.assertEqual(snapshot(), before)  # a reimport never overwrites an assignment
        self.assertEqual(self.kpis()["todo"]["unassigned"], 0)

    def test_a_changed_source_is_reported_but_nothing_is_overwritten(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        changed = self.preview(csv_bytes(ROW_A.replace("Premier", "Modifié")))
        self.assertEqual(changed.rows.get().outcome, "source_changed")
        confirm_import(batch_id=changed.id, user=self.user)
        self.mother.refresh_from_db()
        self.assertTrue(self.mother.is_bpu)


class SubActionAdaptationTests(Fixture, TestCase):
    def test_add_a_sub_action_and_redistribute_in_one_atomic_save_keeping_identities_and_statuses(self):
        started = self.subs[1]
        started.refresh_from_db()
        started.status = "in progress"
        started.save()
        before = {s[0]: s for s in snapshot(self.mother)}
        lines = self.current_lines()
        for entry, closed in zip(lines, (24, 16, 40)):  # 30/20/50 scaled to 80 %
            entry["closed_points"] = closed
            entry["in_progress_points"] = min(entry["in_progress_points"], closed)
        lines.append({"id": None, "title": "Ajout manuel", "in_progress_points": 0, "closed_points": 20})
        response = self.put_subs(lines)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        after = snapshot(self.mother)
        self.assertEqual([a[0] for a in after[:3]], [s.pk for s in self.subs])  # identifiers kept
        self.assertEqual(len(after), 4)
        self.assertEqual(after[1][2], "in progress")  # status kept
        self.assertEqual(after[3][1:3], ("Ajout manuel", "open"))
        self.assertEqual(sum(a[4] for a in after), 100)
        self.assertEqual([a[1] for a in after[:3]], [before[pk][1] for pk in (after[0][0], after[1][0], after[2][0])])
        self.assertEqual(response.data["created"], [after[3][0]])
        # progress recomputed with the new weights, statuses kept: the in-progress one now contributes <= 16
        self.assertEqual(compute_progress(ActionItem.objects.get(pk=self.mother.pk))["progress"], min(self.subs[1].progress_setting.in_progress_points, 16))

    def test_the_company_template_is_never_modified(self):
        template = ActionTemplate.objects.get()
        stored = [(s.title, s.in_progress_points, s.closed_points) for s in template.sub_actions.all()]
        updated_at = template.updated_at
        lines = self.current_lines()
        lines[0].update(title="Renommé", closed_points=10)
        lines[2].update(closed_points=70, in_progress_points=10)
        self.assertEqual(self.put_subs(lines).status_code, 200)
        template.refresh_from_db()
        self.assertEqual([(s.title, s.in_progress_points, s.closed_points) for s in template.sub_actions.all()], stored)
        self.assertEqual(template.updated_at, updated_at)
        self.assertEqual(ActionItemProgressSetting.objects.filter(source_template=template).count(), 3)  # origin kept

    def test_a_total_other_than_100_is_refused_and_nothing_changes(self):
        before = snapshot(self.mother)
        lines = self.current_lines() + [{"id": None, "title": "Trop", "in_progress_points": 0, "closed_points": 10}]
        response = self.put_subs(lines)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "weight_insufficient")
        self.assertEqual(snapshot(self.mother), before)
        short = self.current_lines()
        short[2]["closed_points"] = 40  # 90 in total: a plain total error
        refused = self.put_subs(short)
        self.assertEqual((refused.status_code, refused.data["code"]), (400, "points_invalid"))
        self.assertEqual(snapshot(self.mother), before)

    def test_mixed_open_and_closed_keep_closed_weights_and_take_the_new_weight_from_the_open_ones(self):
        from project_management.test_intervention_plans_b import close

        close(self.subs[0], self.user)  # closed: 30 points, frozen
        lines = self.current_lines()
        lines[1].update(closed_points=14, in_progress_points=7)  # open 20 -> 14
        lines[2].update(closed_points=36, in_progress_points=7)  # open 50 -> 36
        lines.append({"id": None, "title": "Nouvelle", "in_progress_points": 0, "closed_points": 20})
        response = self.put_subs(lines)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        weights = [(s[0], s[4]) for s in snapshot(self.mother)]
        self.assertEqual(weights[0], (self.subs[0].pk, 30))  # closed weight kept
        self.assertEqual(sum(w for _, w in weights), 100)
        self.assertEqual(sum(w for _, w in weights[1:]), 70)  # new + open share exactly what was available

    def test_not_enough_available_weight_is_an_explicit_refusal_and_nothing_changes(self):
        from project_management.test_intervention_plans_b import close

        close(self.subs[0], self.user)
        before = snapshot(self.mother)
        lines = self.current_lines() + [{"id": None, "title": "Trop gros", "in_progress_points": 0, "closed_points": 80}]
        response = self.put_subs(lines)  # open weights untouched: 30 + 20 + 50 + 80
        self.assertEqual((response.status_code, response.data["code"]), (409, "weight_insufficient"))
        self.assertIn("70", response.data["detail"])  # the share the open and new sub-actions may take
        self.assertEqual(snapshot(self.mother), before)

    def test_manual_weights_with_closed_sub_actions_are_checked_the_same_way(self):
        from project_management.test_intervention_plans_b import close

        close(self.subs[0], self.user)
        lines = self.current_lines()
        lines[1].update(closed_points=25, in_progress_points=5)
        lines[2].update(closed_points=45, in_progress_points=5)  # 30 + 25 + 45 = 100 by hand
        self.assertEqual(self.put_subs(lines).status_code, 200)
        lines = self.current_lines()
        lines[1]["closed_points"] = 40  # 30 + 40 + 45 = 115
        over = self.put_subs(lines)
        self.assertEqual((over.status_code, over.data["code"]), (409, "weight_insufficient"))
        lines[1]["closed_points"] = 10  # 30 + 10 + 45 = 85
        under = self.put_subs(lines)
        self.assertEqual((under.status_code, under.data["code"]), (400, "points_invalid"))
        lines[1]["closed_points"] = 30
        lines[0]["closed_points"] = 20  # touching the closed weight is refused
        self.assertEqual(self.put_subs(lines).status_code, 409)

    def test_an_invalid_line_in_the_middle_cancels_everything(self):
        before = snapshot(self.mother)
        lines = self.current_lines()
        lines[0]["title"] = "Renommé"
        lines.append({"id": None, "title": "", "in_progress_points": 0, "closed_points": 0})
        self.assertEqual(self.put_subs(lines).status_code, 400)
        self.assertEqual(snapshot(self.mother), before)

    def test_a_late_failure_rolls_back_the_whole_save(self):
        before = snapshot(self.mother)
        lines = self.current_lines()
        lines[0].update(title="Renommé", closed_points=10)
        lines[2].update(closed_points=70, in_progress_points=10)
        lines.append({"id": None, "title": "Ajout", "in_progress_points": 0, "closed_points": 0})
        with patch("project_management.services.intervention_subactions.ActionItemProgressSetting.objects.create",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.put_subs(lines)
        self.assertEqual(snapshot(self.mother), before)
        self.assertEqual(self.mother.sub_action_items.count(), 3)

    def test_removing_an_open_sub_action_and_reweighting_the_others(self):
        lines = self.current_lines()[:2]
        lines[0].update(closed_points=40, in_progress_points=10)
        lines[1].update(closed_points=60, in_progress_points=10)
        response = self.put_subs(lines)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.data["removed"], [self.subs[2].pk])
        self.assertFalse(ActionItem.objects.filter(pk=self.subs[2].pk).exists())

    def test_removal_needs_the_delete_right_but_adding_does_not(self):
        lines = self.current_lines()[:2]
        lines[0].update(closed_points=40, in_progress_points=10)
        lines[1].update(closed_points=60, in_progress_points=10)
        self.assertEqual(self.put_subs(lines, delete=False).status_code, 403)
        self.assertEqual(self.mother.sub_action_items.count(), 3)
        added = self.current_lines()
        added[2].update(closed_points=40, in_progress_points=10)
        added.append({"id": None, "title": "Ajout", "in_progress_points": 0, "closed_points": 10})
        self.assertEqual(self.put_subs(added, delete=False).status_code, 200)

    def test_a_closed_sub_action_is_frozen(self):
        from project_management.test_intervention_plans_b import close

        close(self.subs[0], self.user)
        lines = self.current_lines()
        lines[0]["closed_points"] = 10
        lines[1]["closed_points"] = 40
        self.assertEqual(self.put_subs(lines).status_code, 409)
        removal = self.put_subs([l for l in self.current_lines() if l["id"] != self.subs[0].pk])
        self.assertIn(removal.status_code, (400, 409))
        self.assertTrue(ActionItem.objects.filter(pk=self.subs[0].pk).exists())

    def test_adding_under_a_finished_intervention_is_refused_like_every_other_addition(self):
        from project_management.test_intervention_plans_b import close

        for sub in self.subs:
            close(sub, self.user)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "closed")  # the existing rule closed it
        lines = self.current_lines() + [{"id": None, "title": "Tard", "in_progress_points": 0, "closed_points": 0}]
        response = self.put_subs(lines)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "mother_closed")
        self.assertEqual(self.mother.sub_action_items.count(), 3)

    def test_adding_when_every_sub_action_is_closed_but_the_mother_is_still_open(self):
        from project_management.test_intervention_plans_b import close

        for sub in self.subs[:2]:
            close(sub, self.user)
        ActionItem.objects.filter(pk=self.subs[2].pk).update(status="closed", closed_at=date(2026, 11, 10))
        self.mother.refresh_from_db()
        self.assertNotEqual(self.mother.status, "closed")  # "to verify before closing": mother not closed yet
        lines = self.current_lines() + [{"id": None, "title": "Dernier contrôle", "in_progress_points": 0, "closed_points": 0}]
        response = self.put_subs(lines)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        result = compute_progress(ActionItem.objects.get(pk=self.mother.pk))
        self.assertFalse(result["all_closed"])  # the new open sub-action keeps the mother open
        self.assertEqual(response.data["mother_status"], self.mother.status)

    def test_rights_scope_and_target(self):
        lines = self.current_lines()
        self.assertEqual(self.put_subs(lines, update=False).status_code, 403)
        self.assertEqual(self.put_subs(lines, user=self.other_user).status_code, 404)  # another company
        self.assertEqual(self.put_subs(lines, pk=self.subs[0].pk).status_code, 400)    # not a mother
        classic = self.create_parent("Classic")
        self.assertEqual(self.put_subs(lines, pk=classic.pk).status_code, 400)

    def test_a_single_sub_action_may_not_be_emptied_away(self):
        self.assertEqual(self.put_subs([]).status_code, 400)
        self.assertEqual(self.mother.sub_action_items.count(), 3)


class ImportDateTests(DateFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.request = InterventionRequest.objects.get(pk=self.mother.intervention_request_id)
        self.set_import(utc(2026, 11, 17, 10, 0))

    def set_import(self, moment, request=None):
        InterventionImportBatch.objects.filter(pk=(request or self.request).import_batch_id).update(confirmed_at=moment)

    def ids(self, today=date(2026, 11, 18), **params):
        with patch(IMPORT_DATE, return_value=today):
            return set(self.listing(scope="interventions", **params))

    def test_the_server_fills_the_first_successful_import_date_and_the_list_shows_it_on_the_mother(self):
        rows = self.listing(scope="interventions")
        self.assertEqual(rows[self.mother.id]["imported_on"], "2026-11-17")
        for sub in rows[self.mother.id]["sub_action_items"]:
            self.assertIsNone(sub["imported_on"])

    def test_a_preview_or_a_cancelled_import_creates_no_import_date(self):
        batch = self.preview(csv_bytes(ROW_B))
        self.assertEqual(InterventionRequest.objects.filter(import_batch=batch).count(), 0)
        self.assertIsNone(import_date_of(None))
        InterventionImportBatch.objects.filter(pk=batch.pk).update(confirmed_at=utc(2026, 11, 17, 10))  # still previewed
        request = InterventionRequest.objects.create(
            project=self.project, company_profile=self.request.company_profile, reference="IR-X-1", title="x",
            received_date=date(2026, 1, 1), expected_end_date=date(2026, 2, 1), import_batch=batch,
        )
        self.assertIsNone(import_date_of(InterventionRequest.objects.select_related("import_batch").get(pk=request.pk)))

    def test_older_data_has_no_invented_date_and_stays_visible_in_all_dates_only(self):
        self.assertIsNone(self.listing(scope="interventions")[self.flat.id]["imported_on"])
        self.assertIn(self.flat.id, self.ids(import_period="all"))
        self.assertIn(self.flat.id, self.ids())
        for period in ("today", "week", "last7", "month"):
            self.assertNotIn(self.flat.id, self.ids(import_period=period))
        self.assertNotIn(self.flat.id, self.ids(import_period="custom", import_from="2000-01-01"))

    def test_sub_actions_added_afterwards_are_not_an_import(self):
        self.manual_sub.delete()  # keep a pure template mother for this check
        before = self.listing(scope="interventions")[self.mother.id]["imported_on"]
        lines = self.current_lines()
        lines[2].update(closed_points=40, in_progress_points=10)
        lines.append({"id": None, "title": "Ajout", "in_progress_points": 0, "closed_points": 10})
        self.assertEqual(self.put_subs(lines).status_code, 200)
        self.assertEqual(self.listing(scope="interventions")[self.mother.id]["imported_on"], before)

    def test_today_week_last7_month_with_inclusive_bounds(self):
        wed = date(2026, 11, 18)  # Wednesday: the week starts on Monday 2026-11-16
        self.assertEqual(period_bounds("week", today=wed), (date(2026, 11, 16), wed))
        self.assertEqual(period_bounds("last7", today=wed), (date(2026, 11, 12), wed))
        self.assertEqual(period_bounds("month", today=wed), (date(2026, 11, 1), wed))
        self.assertEqual(period_bounds("today", today=wed), (wed, wed))
        self.assertIsNone(period_bounds("all", today=wed))
        cases = [
            ("today", utc(2026, 11, 18, 0, 0), True), ("today", utc(2026, 11, 17, 23, 59), False),
            ("week", utc(2026, 11, 16, 0, 0), True), ("week", utc(2026, 11, 15, 23, 59), False),
            ("last7", utc(2026, 11, 12, 0, 0), True), ("last7", utc(2026, 11, 11, 23, 59), False),
            ("month", utc(2026, 11, 1, 0, 0), True), ("month", utc(2026, 10, 31, 23, 59), False),
        ]
        for period, moment, expected in cases:
            self.set_import(moment)
            self.assertEqual(self.mother.id in self.ids(import_period=period), expected, (period, moment))

    def test_week_on_a_monday_is_that_day_alone_and_month_changes_with_the_calendar(self):
        self.set_import(utc(2026, 11, 16, 9))
        self.assertIn(self.mother.id, self.ids(today=date(2026, 11, 16), import_period="week"))
        self.assertNotIn(self.mother.id, self.ids(today=date(2026, 12, 1), import_period="month"))
        self.assertIn(self.mother.id, self.ids(today=date(2026, 11, 22), import_period="last7"))

    def test_custom_period_has_inclusive_bounds_and_accepts_one_bound(self):
        self.set_import(utc(2026, 11, 10, 23, 59))
        custom = lambda a, b: self.mother.id in self.ids(import_period="custom", import_from=a or "", import_to=b or "")  # noqa: E731
        self.assertTrue(custom("2026-11-10", "2026-11-10"))
        self.assertFalse(custom("2026-11-11", "2026-11-12"))
        self.assertFalse(custom("2026-11-08", "2026-11-09"))
        self.assertTrue(custom("2026-11-10", None))
        self.assertTrue(custom(None, "2026-11-10"))
        self.client.force_authenticate(self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            for bad in ({"import_period": "custom"}, {"import_period": "custom", "import_from": "2026-11-12", "import_to": "2026-11-10"},
                        {"import_period": "custom", "import_from": "nope"}, {"import_period": "yesterday"}):
                response = self.client.get(reverse("actionitem-list"), {"project": self.project.id, "scope": "interventions", **bad})
                self.assertEqual(response.status_code, 400, bad)

    def test_the_filter_keeps_the_sub_actions_of_matching_mothers_in_every_view_and_never_touches_classic(self):
        with patch(IMPORT_DATE, return_value=date(2026, 11, 18)):
            tree = self.listing(scope="interventions", import_period="today")
            self.assertEqual(set(tree), set())  # imported yesterday: nothing matches today
            tree = self.listing(scope="interventions", import_period="week")
            board_today = self.flat_ids("actionitem-board", "interventions")
            board_filtered_out = self.ids_of("actionitem-board", import_period="today")
            board_week = self.ids_of("actionitem-board", import_period="week")
            gantt_today = str(self.call("actionitem-gantt", "interventions", import_period="today"))
            gantt_week = str(self.call("actionitem-gantt", "interventions", import_period="week"))
        self.assertIn(self.mother.id, tree)
        self.assertEqual({s["id"] for s in tree[self.mother.id]["sub_action_items"]}, {s.pk for s in self.subs} | {self.manual_sub.pk})
        self.assertEqual(board_filtered_out, set())
        self.assertTrue({self.mother.id, self.manual_sub.id} <= board_week)  # the sub-actions follow their mother
        self.assertIn(self.flat.id, board_today)
        self.assertNotIn("Manual sub", gantt_today)
        self.assertIn("Manual sub", gantt_week)
        with patch(IMPORT_DATE, return_value=date(2026, 11, 18)):
            plain = self.flat_ids("actionitem-list", "classic")  # the parameter is ignored outside Interventions
            ignored = {r["id"] for r in self.call("actionitem-list", "classic", import_period="today")["results"]}
        self.assertIn(self.classic.id, plain)
        self.assertEqual(ignored, plain)

    def ids_of(self, name, **params):
        data = self.call(name, "interventions", **params)
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        return {row["id"] for row in rows}

    def test_the_team_counters_follow_the_import_period_while_the_default_covers_everything(self):
        self.set_import(utc(2026, 11, 10, 9))  # imported eight days before "today"
        with patch(IMPORT_DATE, return_value=date(2026, 11, 18)):
            everything = self.kpis()["total"]
            this_week = self.kpis(import_period="week")
            last_month_window = self.kpis(import_period="custom", import_from="2026-11-10", import_to="2026-11-10")
            filtered_rows = set(self.listing(scope="interventions", import_period="week", kpi="unassigned"))
        self.assertGreaterEqual(everything, 1)
        self.assertEqual(this_week["total"], 0)  # same perimeter as the view: nothing imported this week
        self.assertEqual(filtered_rows, set())    # ... and the shortcut shows exactly that
        # the older flat request has no known import date: counted under "all dates" only, never in a dated period
        self.assertEqual(everything, 2)
        self.assertEqual(last_month_window["total"], 1)

    def test_it_combines_with_the_kpi_filter(self):
        with patch(IMPORT_DATE, return_value=date(2026, 11, 18)):
            both = set(self.listing(scope="interventions", import_period="week", kpi="not_started"))
            none = set(self.listing(scope="interventions", import_period="today", kpi="not_started"))
        self.assertIn(self.mother.id, both)
        self.assertEqual(none, set())

    def test_another_company_is_never_reached_by_the_filter(self):
        other_project = make_project(self.other_user, self.other_company, "Other")
        batch = self.preview(csv_bytes(ROW_B), project=other_project)
        self.set_plan(batch, batch.rows.get().line_number, {"no_sub_actions": True, "sub_actions": []})
        confirm_import(batch_id=batch.id, user=self.user)
        other = ActionItem.objects.get(intervention_request__project=other_project, intervention_action_key="mother")
        self.assertNotIn(other.id, self.ids(import_period="all"))
        self.assertNotIn(other.id, self.ids(import_period="month"))

    @override_settings(TIME_ZONE="Europe/Paris")
    def test_display_and_filter_use_the_same_reference_zone(self):
        self.set_import(utc(2026, 11, 17, 23, 30))  # 00:30 on the 18th in Paris
        self.assertEqual(import_date_of(InterventionRequest.objects.select_related("import_batch").get(pk=self.request.pk)), date(2026, 11, 18))
        self.assertEqual(self.listing(scope="interventions")[self.mother.id]["imported_on"], "2026-11-18")
        self.assertIn(self.mother.id, self.ids(import_period="today"))
