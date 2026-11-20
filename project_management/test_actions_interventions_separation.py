"""Actions / Interventions separation.

The Actions screens ask for `scope=classic`, the Interventions screens for `scope=interventions`; without
the parameter nothing is filtered. An intervention action is any action born from an intervention request:
the mother, its sub-actions (generated or added by hand) and the flat actions of older requests. The
common engine (hierarchy, weights, progress, statuses, closing, assignees, dates, numbering) is shared."""
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from project_management.models import ActionItem, InterventionRequest
from project_management.serializers.landing_screen import LandingScreenSerializer
from project_management.test_action_item_list_columns import CHECKER, ListFixture, PERM


class SeparationFixture(ListFixture):
    def setUp(self):
        super().setUp()
        self.classic = self.create_parent("Classic action")
        self.classic_child = ActionItem.objects.create(
            parent_action_item=self.classic, name="Classic child", status="open", target_date=date(2026, 12, 1)
        )
        # a sub-action ADDED BY HAND under the intervention mother (no request link of its own)
        self.manual_sub = ActionItem.objects.create(
            parent_action_item=self.mother, name="Manual sub", status="open", target_date=date(2026, 12, 1)
        )
        # an older request with FLAT actions: no mother, no sub-action
        request = self.mother.intervention_request
        self.old_request = InterventionRequest.objects.create(
            project=request.project, company_profile=request.company_profile, reference="IR-OLD-0001",
            title="Old request", received_date=date(2026, 1, 5), expected_end_date=date(2026, 2, 5),
            source="csv", external_reference="OLD-1", created_by=self.user,
        )
        self.flat = ActionItem(
            milestone=self.mother.milestone, name="IR-OLD-0001 - Montage", status="open",
            target_date=date(2026, 2, 5), created_by=self.user,
            intervention_request=self.old_request, intervention_action_key="assembly",
        )
        self.flat._suppress_action_item_notifications = True
        self.flat.save()

    def call(self, name, scope=None, **params):
        self.client.force_authenticate(self.user)
        if scope:
            params["scope"] = scope
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            response = self.client.get(reverse(name), {"project": self.project.id, "page_size": 1000, **params})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data

    def tree_ids(self, scope):
        rows = self.call("actionitem-list", scope)["results"]
        ids = set()
        for row in rows:
            ids.add(row["id"])
            ids |= {child["id"] for child in row.get("sub_action_items", [])}
        return ids

    def flat_ids(self, name, scope):
        data = self.call(name, scope)
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        return {row["id"] for row in rows}


class ListScopeTests(SeparationFixture, TestCase):
    def test_classic_scope_shows_classic_actions_only(self):
        ids = self.tree_ids("classic")
        self.assertEqual({self.classic.id, self.classic_child.id} - ids, set())
        for item in (self.mother, *self.subs, self.manual_sub, self.flat):
            self.assertNotIn(item.id, ids)

    def test_interventions_scope_shows_mothers_subs_manual_subs_and_flat_actions(self):
        ids = self.tree_ids("interventions")
        for item in (self.mother, *self.subs, self.manual_sub, self.flat):
            self.assertIn(item.id, ids)
        self.assertNotIn(self.classic.id, ids)
        self.assertNotIn(self.classic_child.id, ids)

    def test_without_scope_nothing_is_filtered(self):
        ids = self.tree_ids(None)
        for item in (self.classic, self.mother, self.manual_sub, self.flat):
            self.assertIn(item.id, ids)

    def test_the_two_scopes_never_overlap_and_cover_everything(self):
        classic, interventions, everything = self.tree_ids("classic"), self.tree_ids("interventions"), self.tree_ids(None)
        self.assertEqual(classic & interventions, set())
        self.assertEqual(classic | interventions, everything)

    def test_a_sub_action_stays_with_its_mother_in_the_interventions_list(self):
        rows = {r["id"]: r for r in self.call("actionitem-list", "interventions")["results"]}
        self.assertIn(self.manual_sub.id, {c["id"] for c in rows[self.mother.id]["sub_action_items"]})
        self.assertNotIn(self.manual_sub.id, rows)  # never a top-level row of its own


class OtherViewsScopeTests(SeparationFixture, TestCase):
    def test_the_board_scopes(self):
        classic = self.flat_ids("actionitem-board", "classic")
        interventions = self.flat_ids("actionitem-board", "interventions")
        self.assertIn(self.classic.id, classic)
        self.assertNotIn(self.mother.id, classic)
        self.assertIn(self.mother.id, interventions)
        self.assertNotIn(self.classic.id, interventions)

    def test_the_calendar_scopes(self):
        for item in (self.classic, self.mother, self.flat):
            ActionItem.objects.filter(pk=item.pk).update(start_date=date(2026, 11, 10), target_date=date(2026, 12, 1))
        params = {"range_start": "2026-11-01", "range_end": "2026-12-31"}
        ids = {row["id"] for row in self.call("actionitem-calendar", "classic", **params)}
        self.assertNotIn(self.mother.id, ids)
        self.assertNotIn(self.flat.id, ids)
        self.assertIn(self.classic.id, ids)
        interventions = {row["id"] for row in self.call("actionitem-calendar", "interventions", **params)}
        self.assertIn(self.mother.id, interventions)
        self.assertNotIn(self.classic.id, interventions)

    def test_the_gantt_scopes(self):
        data = self.call("actionitem-gantt", "classic")
        text = str(data)
        self.assertNotIn("Manual sub", text)
        self.assertNotIn("IR-OLD-0001", text)
        self.assertIn("Classic action", str(self.call("actionitem-gantt", "classic")))
        self.assertIn("Manual sub", str(self.call("actionitem-gantt", "interventions")))


class LegacyFlatRequestTests(SeparationFixture, TestCase):
    def test_a_flat_action_of_an_older_request_is_listed_with_its_request_data(self):
        row = {r["id"]: r for r in self.call("actionitem-list", "interventions")["results"]}[self.flat.id]
        self.assertEqual((row["external_reference"], row["links_reference"]), ("OLD-1", "IR-OLD-0001"))
        self.assertEqual(str(row["received_date"]), "2026-01-05")
        self.assertEqual(row["sub_action_items"], [])

    def test_a_flat_action_follows_its_own_status_for_the_progress(self):
        def progress():
            return {r["id"]: r for r in self.call("actionitem-list", "interventions")["results"]}[self.flat.id]["progress_percent"]

        self.assertEqual(progress(), 0)
        ActionItem.objects.filter(pk=self.flat.pk).update(status="in progress")
        self.assertEqual(progress(), 10)
        ActionItem.objects.filter(pk=self.flat.pk).update(status="closed", closed_at=date(2026, 1, 20))
        self.assertEqual(self.call("actionitem-list", "interventions", show_closed="true")["results"][0:0], [])
        closed = {r["id"]: r for r in self.call("actionitem-list", "interventions", show_closed="true")["results"]}[self.flat.id]
        self.assertEqual(closed["progress_percent"], 100)

    def test_nothing_is_migrated_or_deleted(self):
        self.assertTrue(ActionItem.objects.filter(pk=self.flat.pk, intervention_action_key="assembly").exists())
        self.assertTrue(InterventionRequest.objects.filter(pk=self.old_request.pk).exists())


class CounterTests(SeparationFixture, TestCase):
    def test_the_landing_open_actions_counter_ignores_interventions(self):
        data = LandingScreenSerializer(self.project).data
        classic_open = ActionItem.objects.filter(milestone__project=self.project).exclude(status="closed")
        expected = sum(
            1 for a in classic_open
            if not a.intervention_request_id and not (a.parent_action_item_id and a.parent_action_item.intervention_request_id)
        )
        self.assertEqual(data["open_action_items"], expected)
        self.assertNotEqual(expected, classic_open.count())  # interventions really are excluded

class ClosedItemsScopeTests(SeparationFixture, TestCase):
    """Closed items follow the same scope in every space: nothing leaks from one space to the other."""

    def test_closed_items_stay_in_their_own_space(self):
        ActionItem.objects.filter(pk__in=[self.classic.pk, self.classic_child.pk]).update(status="closed", closed_at=date(2026, 11, 2))
        ActionItem.objects.filter(pk=self.flat.pk).update(status="closed", closed_at=date(2026, 11, 2))

        def ids(scope, **extra):
            return {r["id"] for r in self.call("actionitem-list", scope, **extra)["results"]}

        self.assertNotIn(self.classic.id, ids("classic"))
        self.assertNotIn(self.flat.id, ids("interventions"))
        self.assertIn(self.classic.id, ids("classic", show_closed="true"))
        self.assertNotIn(self.flat.id, ids("classic", show_closed="true"))
        self.assertIn(self.flat.id, ids("interventions", show_closed="true"))
        self.assertNotIn(self.classic.id, ids("interventions", show_closed="true"))


class RestrictedUserScopeTests(SeparationFixture, TestCase):
    """A user limited to what is assigned to / created by them: the scope never widens the rights."""

    VIEW = "project_management.views.action_item.require_any_permission_multiple_modules"
    LOW = "ecpmpapp.permissions.BaseModulePermission._check_permission"
    BROAD = "project_management.permissions.check_company_project_permission"

    def setUp(self):
        super().setUp()
        self.limited = self.member("limited-scope@example.com")
        self.subs[0].assignees.add(self.limited)
        self.classic_child.assignees.add(self.limited)

    def tree(self, scope):
        self.client.force_authenticate(self.limited)
        only = lambda user, modules, s, context: tuple(next(iter(modules.values()))) == ("view_assigned_created",)
        low = lambda permission, u, module, action, s, context: module == "action_item" and action == "view_assigned_created"
        with patch(self.VIEW, side_effect=only), patch(self.LOW, autospec=True, side_effect=low), patch(self.BROAD, return_value=False):
            response = self.client.get(reverse("actionitem-list"), {"project": self.project.id, "page_size": 1000, "scope": scope})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        ids = set()
        for row in response.data["results"]:
            ids.add(row["id"])
            ids |= {c["id"] for c in row.get("sub_action_items", [])}
        return ids

    def test_each_space_shows_only_what_the_restricted_user_may_see(self):
        interventions, classic = self.tree("interventions"), self.tree("classic")
        self.assertIn(self.subs[0].id, interventions)
        self.assertNotIn(self.subs[1].id, interventions)   # not theirs
        self.assertNotIn(self.flat.id, interventions)       # not theirs
        self.assertNotIn(self.subs[0].id, classic)           # an intervention never shows in Actions
        self.assertIn(self.classic_child.id, classic)
        self.assertNotIn(self.mother.id, classic)
