"""Interventions list: the "closed interventions" toggle (`show_closed`) acts on the INTERVENTIONS only.
A visible mother keeps all its sub-actions, open or closed; a closed mother is hidden / shown with the toggle;
older flat requests follow the same rule grouped by request; the classic Actions space is unchanged."""
from datetime import date

from django.test import TestCase

from project_management.models import ActionItem, InterventionRequest
from project_management.test_actions_interventions_separation import SeparationFixture


class ClosedToggleFixture(SeparationFixture):
    def close(self, *items):
        for item in items:
            ActionItem.objects.filter(pk=item.pk).update(status="closed", closed_at=date(2026, 11, 10))

    def tops(self, show_closed=False, scope="interventions", **params):
        if show_closed:
            params["show_closed"] = "true"
        return {r["id"]: r for r in self.call("actionitem-list", scope, **params)["results"]}

    def sub_ids(self, row):
        return {s["id"] for s in row["sub_action_items"]}

    def flat_sibling(self):
        item = ActionItem(
            milestone=self.mother.milestone, name="IR-OLD-0001 - Second", status="open", target_date=date(2026, 2, 5),
            created_by=self.user, intervention_request=self.old_request, intervention_action_key="inspection",
        )
        item._suppress_action_item_notifications = True
        item.save()
        return item


class OpenMotherKeepsEverySubActionTests(ClosedToggleFixture, TestCase):
    def test_closed_and_open_sub_actions_stay_visible_in_both_toggle_positions(self):
        self.close(self.subs[0])  # one closed sub-action among open ones
        expected = {s.id for s in self.subs} | {self.manual_sub.id}
        for show_closed in (False, True):
            row = self.tops(show_closed)[self.mother.id]
            self.assertEqual(self.sub_ids(row), expected, show_closed)

    def test_every_sub_action_closed_but_the_mother_open_is_still_listed_complete(self):
        self.close(*self.subs, self.manual_sub)  # "to verify before closing": mother open, subs closed
        expected = {s.id for s in self.subs} | {self.manual_sub.id}
        for show_closed in (False, True):
            row = self.tops(show_closed)[self.mother.id]
            self.assertEqual(self.sub_ids(row), expected, show_closed)


class ClosedMotherTests(ClosedToggleFixture, TestCase):
    def test_a_closed_mother_follows_the_toggle_with_all_its_sub_actions(self):
        self.close(self.mother, self.subs[0])  # closed mother, one closed sub, the others still open
        hidden = self.tops(False)
        self.assertNotIn(self.mother.id, hidden)  # even though it still has open sub-actions
        shown = self.tops(True)
        self.assertIn(self.mother.id, shown)
        self.assertEqual(self.sub_ids(shown[self.mother.id]), {s.id for s in self.subs} | {self.manual_sub.id})

    def test_a_closed_mother_with_only_closed_sub_actions(self):
        self.close(self.mother, *self.subs, self.manual_sub)
        self.assertNotIn(self.mother.id, self.tops(False))
        shown = self.tops(True)[self.mother.id]
        self.assertEqual(self.sub_ids(shown), {s.id for s in self.subs} | {self.manual_sub.id})


class FlatRequestTests(ClosedToggleFixture, TestCase):
    def test_an_older_request_is_one_intervention_grouped_by_request(self):
        second = self.flat_sibling()
        self.close(self.flat, second)  # the whole request is closed
        self.assertNotIn(self.flat.id, self.tops(False))
        self.assertNotIn(second.id, self.tops(False))
        self.assertTrue({self.flat.id, second.id} <= set(self.tops(True)))

    def test_a_request_with_one_open_flat_action_stays_complete(self):
        second = self.flat_sibling()
        self.close(self.flat)  # one closed, one open: the request is still open
        for show_closed in (False, True):
            tops = self.tops(show_closed)
            self.assertTrue({self.flat.id, second.id} <= set(tops), show_closed)


class ExistingBehaviourTests(ClosedToggleFixture, TestCase):
    def test_classic_actions_space_is_unchanged(self):
        self.close(self.classic_child)
        off = self.tops(False, scope="classic")
        self.assertIn(self.classic.id, off)
        self.assertNotIn(self.classic_child.id, {s["id"] for s in off[self.classic.id]["sub_action_items"]})  # hidden as before
        on = self.tops(True, scope="classic")
        self.assertIn(self.classic_child.id, {s["id"] for s in on[self.classic.id]["sub_action_items"]})

    def test_an_explicit_status_filter_keeps_its_usual_meaning(self):
        self.close(self.subs[0])
        closed_only = self.tops(False, status="closed")
        self.assertIn(self.mother.id, closed_only)  # a mother matching through its closed sub-action
        self.assertEqual(self.sub_ids(closed_only[self.mother.id]), {self.subs[0].id})

    def test_other_filters_still_apply_to_the_visible_interventions(self):
        row = self.tops(False, search=str(self.mother.id))
        self.assertIn(self.mother.id, row)
        self.assertEqual(set(self.tops(False, search="zzz-no-match")), set())
        self.assertEqual(InterventionRequest.objects.count(), 2)
