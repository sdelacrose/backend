"""Scope of BPU and of the templates: interventions only.

The templates of MOD-01/MOD-02 are intervention templates (company level, reusable across projects) and
BPU is an intervention notion set at the import and shown on the mother only. Classic actions never get
them, while the common action engine (hierarchy, weights, progress, statuses, closing, assignees, dates,
numbering, side panel) is untouched and no stored BPU data or template is deleted."""
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from project_management.models import ActionItem, ActionTemplate
from project_management.services.action_progress import (
    compute_progress,
    displayed_bpu,
    effective_bpu,
    is_intervention_action,
    uses_points_mechanism,
)
from project_management.test_action_item_list_columns import CHECKER, ListFixture, PERM


class ScopeFixture(ListFixture):
    def detail(self, item):
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            return self.client.get(reverse("actionitem-detail", args=[item.id]))

    def patch_item(self, item, body):
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            return self.client.patch(reverse("actionitem-detail", args=[item.id]), body, format="json")

    def classic_with_legacy_bpu(self):
        plain = self.create_parent("Classic mother")
        ActionItem.objects.filter(pk=plain.pk).update(is_bpu=True)  # stored data from before this rule
        child = ActionItem.objects.create(parent_action_item=plain, name="c", status="open", target_date=date(2026, 1, 10))
        plain.refresh_from_db()
        return plain, child


class BpuScopeTests(ScopeFixture, TestCase):
    def test_an_intervention_mother_keeps_its_bpu_badge_and_its_sub_actions_inherit_it(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        self.assertTrue(is_intervention_action(self.mother))
        rows = self.listing()
        self.assertTrue(rows[self.mother.id]["effective_bpu"])
        self.assertTrue(all(s["effective_bpu"] for s in rows[self.mother.id]["sub_action_items"]))
        self.assertTrue(self.detail(self.mother).data["effective_bpu"])

    def test_a_classic_action_shows_no_bpu_even_if_data_was_stored_but_nothing_is_deleted(self):
        plain, child = self.classic_with_legacy_bpu()
        self.assertFalse(is_intervention_action(plain))
        self.assertEqual(displayed_bpu(plain), (False, None))
        self.assertTrue(effective_bpu(plain)[0])  # the stored data is still there for the engine
        row = self.listing()[plain.id]
        self.assertFalse(row["effective_bpu"])
        self.assertFalse(any(s["effective_bpu"] for s in row["sub_action_items"]))
        detail = self.detail(plain).data
        self.assertFalse(detail["effective_bpu"])
        self.assertIsNone(detail["bpu_source"])
        plain.refresh_from_db()
        self.assertTrue(plain.is_bpu)  # never deleted

    def test_bpu_cannot_be_given_to_a_classic_action_through_the_api(self):
        plain = self.create_parent("Classic")
        refused = self.patch_item(plain, {"is_bpu": True})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("is_bpu", refused.data)
        plain.refresh_from_db()
        self.assertFalse(plain.is_bpu)

    def test_a_legacy_value_echoed_back_by_the_edit_window_is_not_refused(self):
        plain, _ = self.classic_with_legacy_bpu()
        ok = self.patch_item(plain, {"is_bpu": True, "notes": "edited"})
        self.assertEqual(ok.status_code, 200, getattr(ok, "data", None))
        plain.refresh_from_db()
        self.assertTrue(plain.is_bpu)

    def test_an_intervention_mother_can_still_be_edited_with_bpu(self):
        ok = self.patch_item(self.mother, {"is_bpu": True})
        self.assertEqual(ok.status_code, 200, getattr(ok, "data", None))
        self.mother.refresh_from_db()
        self.assertTrue(self.mother.is_bpu)


class TemplateScopeTests(ScopeFixture, TestCase):
    def test_a_classic_action_has_no_template_mechanism(self):
        plain = self.create_parent("Classic")
        self.assertFalse(uses_points_mechanism(plain))  # no points section in its sub-action form
        self.assertEqual(compute_progress(plain)["mode"], "unweighted")
        self.assertIsNone(self.detail(plain).data["progress_setting"])

    def test_templates_stay_company_level_and_are_not_deleted(self):
        before = ActionTemplate.objects.count()
        self.assertGreaterEqual(before, 1)  # the fixture template (reusable across the company's projects)
        self.assertEqual(ActionTemplate.objects.count(), before)
        self.assertTrue(all(t.company_id for t in ActionTemplate.objects.all()))

    def test_the_common_engine_is_unchanged_for_classic_actions(self):
        plain, child = self.classic_with_legacy_bpu()
        child.status = "in progress"
        child.save()
        plain.refresh_from_db()
        self.assertEqual(plain.status, "in progress")  # statuses / hierarchy keep working
        self.assertEqual(child.sub_reference, f"{plain.id}-1")  # numbering
