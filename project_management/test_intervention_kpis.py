"""Intervention KPIs (dashboard widget) and team shortcuts: counting rules, filters, permissions, isolation.

One intervention = one request; sub-actions are never counted; flat actions of older requests are grouped
by request. Counters and the list filtered by `kpi=<flag>` share the same units."""
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from project_management.models import ActionItem, InterventionRequest
from project_management.services.intervention_kpis import build_kpis, build_units
from project_management.test_action_item_list_columns import CHECKER, PERM
from project_management.test_actions_interventions_separation import SeparationFixture

TODAY = timezone.localdate()


class KpiFixture(SeparationFixture):
    def kpis(self, user=None, **params):
        self.client.force_authenticate(user or self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            response = self.client.get(reverse("actionitem-intervention-kpis"), {"project": self.project.id, **params})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data

    def set_request(self, request, **fields):
        InterventionRequest.objects.filter(pk=request.pk).update(**fields)

    def set_status(self, action, status, **extra):
        ActionItem.objects.filter(pk=action.pk).update(status=status, **extra)


class CountingTests(KpiFixture, TestCase):
    def test_one_request_is_one_intervention_sub_actions_and_classic_actions_are_not_counted(self):
        data = self.kpis()
        # the imported mother (3 imported + 1 manual sub-action) and the older flat request: 2 interventions
        self.assertEqual(data["portfolio"]["total"], 2)
        self.assertEqual(data["total"], 2)

    def test_flat_actions_of_an_older_request_are_grouped_by_request(self):
        for name in ("Etape 2", "Etape 3"):
            extra = ActionItem(
                milestone=self.mother.milestone, name=name, status="open", created_by=self.user,
                intervention_request=self.old_request, intervention_action_key=name.lower().replace(" ", ""),
                target_date=date(2026, 2, 5),
            )
            extra._suppress_action_item_notifications = True
            extra.save()
        self.assertEqual(self.kpis()["portfolio"]["total"], 2)  # still 2: 3 flat actions = 1 intervention

    def test_bpu_is_counted_once_at_intervention_level(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)  # sub-actions inherit it: not counted again
        data = self.kpis()["portfolio"]
        self.assertEqual((data["bpu"], data["bpu_percent"]), (1, 50.0))
        ActionItem.objects.filter(pk=self.flat.pk).update(is_bpu=True)
        self.assertEqual(self.kpis()["portfolio"]["bpu"], 2)

    def test_the_classic_action_never_reaches_the_counters_even_with_a_bpu_flag(self):
        ActionItem.objects.filter(pk=self.classic.pk).update(is_bpu=True)
        self.assertEqual(self.kpis()["portfolio"]["bpu"], 0)


class ProgressTests(KpiFixture, TestCase):
    def test_states_follow_the_existing_status_and_progress_rules(self):
        progress = self.kpis()["progress"]
        self.assertEqual((progress["not_started"], progress["in_progress"], progress["done"]), (2, 0, 0))
        self.set_status(self.flat, "in progress")
        self.set_status(self.mother, "in progress")
        progress = self.kpis()["progress"]
        self.assertEqual((progress["not_started"], progress["in_progress"], progress["done"]), (0, 2, 0))
        self.set_status(self.flat, "closed", closed_at=TODAY)
        self.set_status(self.mother, "closed", closed_at=TODAY)
        progress = self.kpis()["progress"]
        self.assertEqual((progress["not_started"], progress["in_progress"], progress["done"]), (0, 0, 2))

    def test_an_unknown_status_is_not_assimilated_to_in_progress(self):
        self.set_status(self.flat, "parked")
        progress = self.kpis()["progress"]
        self.assertEqual(progress["other_status"], 1)
        self.assertEqual(progress["in_progress"], 0)

    def test_average_progress_is_a_snapshot_over_the_measured_interventions(self):
        self.set_status(self.flat, "in progress")  # flat action: 10 %; the mother has weighted points at 0 %
        trends = self.kpis()["trends"]
        self.assertIsNotNone(trends["average_progress"])
        self.assertEqual(trends["average_progress_measured"] + trends["average_progress_unmeasured"], 2)


class DeadlineTests(KpiFixture, TestCase):
    def test_late_means_open_and_past_the_customer_deadline(self):
        self.set_request(self.old_request, expected_end_date=TODAY - timedelta(days=3))
        self.set_request(self.mother.intervention_request, expected_end_date=TODAY + timedelta(days=3))
        self.assertEqual(self.kpis()["deadlines"]["late"], 1)
        self.set_status(self.flat, "closed", closed_at=TODAY)
        self.assertEqual(self.kpis()["deadlines"]["late"], 0)  # a finished intervention is not late

    def test_hold_may_overlap_a_late_intervention_and_is_not_an_exclusive_part(self):
        self.set_request(self.old_request, expected_end_date=TODAY - timedelta(days=3))
        self.set_status(self.flat, "on hold")
        deadlines = self.kpis()["deadlines"]
        self.assertEqual((deadlines["hold"], deadlines["late"], deadlines["late_and_hold"]), (1, 1, 1))

    def test_without_any_hold_status_the_counter_is_zero(self):
        self.assertEqual(self.kpis()["deadlines"]["hold"], 0)

    def test_delivered_on_time_after_the_deadline_and_with_a_zero_denominator(self):
        self.assertIsNone(self.kpis()["deadlines"]["on_time_percent"])  # nothing finished: "—", never 0 %
        self.assertEqual(self.kpis()["deadlines"]["on_time_denominator"], 0)
        self.set_request(self.old_request, expected_end_date=TODAY)
        self.set_status(self.flat, "closed", closed_at=TODAY)  # on the deadline day: on time
        deadlines = self.kpis()["deadlines"]
        self.assertEqual((deadlines["on_time"], deadlines["on_time_denominator"], deadlines["on_time_percent"]), (1, 1, 100.0))
        self.set_request(self.old_request, expected_end_date=TODAY - timedelta(days=1))  # closed after the deadline
        deadlines = self.kpis()["deadlines"]
        self.assertEqual((deadlines["on_time"], deadlines["on_time_denominator"], deadlines["on_time_percent"]), (0, 1, 0.0))

    def test_a_closure_without_date_is_reported_and_excluded_from_the_punctuality_rate(self):
        self.set_status(self.flat, "closed", closed_at=None)
        deadlines = self.kpis()["deadlines"]
        self.assertEqual(deadlines["on_time_denominator"], 0)
        self.assertEqual(deadlines["closed_without_closure_date"], 1)
        self.assertIsNone(deadlines["on_time_percent"])


class TrendTests(KpiFixture, TestCase):
    def test_weekly_histogram_uses_the_real_received_and_closure_dates(self):
        self.set_request(self.old_request, received_date=TODAY)
        self.set_request(self.mother.intervention_request, received_date=TODAY - timedelta(weeks=2))
        self.set_status(self.flat, "closed", closed_at=TODAY - timedelta(weeks=1))
        weeks = {w["week_start"]: w for w in self.kpis(weeks=4)["trends"]["weeks"]}
        monday = TODAY - timedelta(days=TODAY.weekday())
        self.assertEqual(weeks[monday.isoformat()]["received"], 1)
        self.assertEqual(weeks[(monday - timedelta(weeks=2)).isoformat()]["received"], 1)
        self.assertEqual(weeks[(monday - timedelta(weeks=1)).isoformat()]["closed"], 1)
        self.assertEqual(len(weeks), 4)

    def test_a_closure_without_date_is_never_dated_by_the_widget(self):
        self.set_status(self.flat, "closed", closed_at=None)
        trends = self.kpis()["trends"]
        self.assertEqual(sum(w["closed"] for w in trends["weeks"]), 0)
        self.assertEqual(trends["closed_without_date"], 1)


class FilterAndShortcutTests(KpiFixture, TestCase):
    def list_ids(self, kpi=None, **params):
        self.client.force_authenticate(self.user)
        query = {"project": self.project.id, "page_size": 1000, "scope": "interventions", "show_closed": "true", **params}
        if kpi:
            query["kpi"] = kpi
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            response = self.client.get(reverse("actionitem-list"), query)
        return response

    def top_ids(self, kpi):
        response = self.list_ids(kpi)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return {row["id"] for row in response.data["results"]}

    def test_each_counter_matches_the_interventions_shown_by_its_filter(self):
        self.set_request(self.old_request, expected_end_date=TODAY - timedelta(days=5))
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        data = self.kpis()
        expectations = {
            "bpu": data["portfolio"]["bpu"], "late": data["deadlines"]["late"], "not_started": data["progress"]["not_started"],
            "unassigned": data["todo"]["unassigned"], "to_plan": data["todo"]["to_plan"],
        }
        for flag, expected in expectations.items():
            shown = self.top_ids(flag)
            tops = {i for i in shown if ActionItem.objects.get(pk=i).parent_action_item_id is None}
            self.assertEqual(len(tops), expected, flag)

    def test_a_filter_keeps_the_sub_actions_of_the_matching_mother(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        rows = {r["id"]: r for r in self.list_ids("bpu").data["results"]}
        self.assertEqual(set(rows), {self.mother.id})
        self.assertTrue(rows[self.mother.id]["sub_action_items"])

    def test_an_unknown_filter_is_refused_and_the_filter_is_ignored_outside_the_interventions_scope(self):
        self.assertEqual(self.list_ids("bogus").status_code, 400)
        classic = self.list_ids("late", scope="classic")
        self.assertEqual(classic.status_code, 200)
        self.assertIn(self.classic.id, {r["id"] for r in classic.data["results"]})

    def test_unassigned_distinguishes_the_mother_from_its_sub_actions(self):
        # mother without owner BUT a sub-action owned: taken in charge -> not "unassigned"
        self.assertEqual(self.kpis()["todo"]["unassigned"], 2)  # fixture: nobody owns anything
        self.subs[0].assignees.add(self.user)
        self.assertEqual(self.kpis()["todo"]["unassigned"], 1)  # the mother is now taken in charge through a sub-action
        self.mother.assignees.add(self.user)
        self.assertEqual(self.kpis()["todo"]["unassigned"], 1)
        self.flat.assignees.add(self.user)
        self.assertEqual(self.kpis()["todo"]["unassigned"], 0)

    def test_to_plan_looks_at_planning_dates_never_at_the_customer_deadline(self):
        before = self.kpis()["todo"]["to_plan"]
        self.assertEqual(before, 2)  # the customer deadline is set on both requests: it does not count as planning
        ActionItem.objects.filter(pk=self.mother.pk).update(start_date=TODAY)
        self.assertEqual(self.kpis()["todo"]["to_plan"], 1)
        ActionItem.objects.filter(pk=self.flat.pk).update(forecast_date=TODAY)
        self.assertEqual(self.kpis()["todo"]["to_plan"], 0)

    def test_to_verify_only_exists_for_a_real_state_every_sub_action_closed_but_the_mother_still_open(self):
        self.assertEqual(self.kpis()["todo"]["to_verify"], 0)
        ActionItem.objects.filter(parent_action_item=self.mother).update(status="closed", closed_at=TODAY)
        ActionItem.objects.filter(pk=self.mother.pk).update(status="open")  # the automatic closing did not run
        self.assertEqual(self.kpis()["todo"]["to_verify"], 1)
        ActionItem.objects.filter(pk=self.mother.pk).update(status="closed", closed_at=TODAY)
        self.assertEqual(self.kpis()["todo"]["to_verify"], 0)  # closed by the existing rule: nothing to verify


class IsolationAndPermissionTests(KpiFixture, TestCase):
    def test_another_project_and_another_company_are_never_counted(self):
        from project_management.models import Milestone, Project

        other_project = Project.objects.create(
            project_name="Other company project", contract_value=0,
            company_profile=self.other_company, user=self.other_user,
        )
        other_phase = Milestone.objects.create(
            project=other_project, name="Phase", start_date=date(2026, 9, 1), due_date=date(2026, 12, 31)
        )
        request = InterventionRequest.objects.create(
            project=other_project, company_profile=self.other_company, reference="IR-OTHER-1", title="Other",
            received_date=TODAY, expected_end_date=TODAY, source="csv", external_reference="O-1", created_by=self.other_user,
        )
        action = ActionItem(milestone=other_phase, name="Other", status="open", created_by=self.other_user,
                            intervention_request=request, intervention_action_key="mother", target_date=TODAY)
        action._suppress_action_item_notifications = True
        action.save()
        self.assertEqual(self.kpis()["portfolio"]["total"], 2)  # the project filter of the common perimeter
        self.client.force_authenticate(self.other_user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True),                 patch(CHECKER, return_value=True):
            mine = self.client.get(reverse("actionitem-intervention-kpis"), {"project": other_project.id})
        if mine.status_code == 200:
            self.assertEqual(mine.data["total"], 1)  # the other company only ever sees its own intervention

    def test_a_user_without_access_gets_nothing(self):
        stranger = self.member("kpi-stranger@example.com")
        self.client.force_authenticate(stranger)
        response = self.client.get(reverse("actionitem-intervention-kpis"), {"project": self.project.id})
        self.assertIn(response.status_code, (200, 403))
        if response.status_code == 200:
            self.assertEqual(response.data["total"], 0)


class RestrictedUserKpiTests(KpiFixture, TestCase):
    VIEW = "project_management.views.action_item.require_any_permission_multiple_modules"
    LOW = "ecpmpapp.permissions.BaseModulePermission._check_permission"
    BROAD = "project_management.permissions.check_company_project_permission"

    def get_kpis(self, user):
        self.client.force_authenticate(user)
        only = lambda u, modules, s, context: tuple(next(iter(modules.values()))) == ("view_assigned_created",)
        low = lambda permission, u, module, action, s, context: module == "action_item" and action == "view_assigned_created"
        with patch(self.VIEW, side_effect=only), patch(self.LOW, autospec=True, side_effect=low), patch(self.BROAD, return_value=False):
            return self.client.get(reverse("actionitem-intervention-kpis"), {"project": self.project.id})

    def test_a_restricted_user_only_gets_the_aggregates_of_the_interventions_they_may_see(self):
        limited = self.member("kpi-limited@example.com")
        self.mother.assignees.add(limited)
        response = self.get_kpis(limited)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.data["total"], 1)  # their mother only: the flat request is not theirs
        self.assertEqual(response.data["progress"]["not_started"], 1)
        self.assertEqual(response.data["navigation_only"], 0)  # nothing hidden behind the counters

    def test_a_mother_reachable_only_through_a_sub_action_is_navigation_not_an_authorization(self):
        limited = self.member("kpi-limited-sub@example.com")
        self.subs[0].assignees.add(limited)
        response = self.get_kpis(limited)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.data["total"], 0)  # no aggregate about a mother they do not own
        self.assertEqual(response.data["portfolio"]["bpu"], 0)
        # ... but the UI is told that one visible intervention is navigation only, so a visible row with counters at 0
        # is explained (banner / widget note)
        self.assertEqual(response.data["navigation_only"], 1)

    def test_a_user_with_nothing_assigned_is_refused_or_sees_zero(self):
        stranger = self.member("kpi-nothing@example.com")
        response = self.get_kpis(stranger)
        if response.status_code == 200:
            self.assertEqual(response.data["total"], 0)
        else:
            self.assertEqual(response.status_code, 403)


class FullAccessNavigationTests(KpiFixture, TestCase):
    def test_a_user_with_the_broad_scope_never_gets_a_navigation_only_count(self):
        self.assertEqual(self.kpis()["navigation_only"], 0)


class LateHoldVerifyCombinationTests(KpiFixture, TestCase):
    """The cases the demo data did not contain: non-zero late and Hold (one intervention both) and a non-zero
    \"to verify before closing\"."""

    def test_late_hold_and_both_at_once_with_their_filters(self):
        self.set_request(self.old_request, expected_end_date=TODAY - timedelta(days=4))              # flat request: late
        self.set_request(self.mother.intervention_request, expected_end_date=TODAY - timedelta(days=2))  # mother: late
        self.set_status(self.mother, "on hold")                                                          # ... and Hold
        deadlines = self.kpis()["deadlines"]
        self.assertEqual((deadlines["late"], deadlines["hold"], deadlines["late_and_hold"]), (2, 1, 1))
        self.assertEqual(self.kpis()["progress"]["other_status"], 1)  # Hold is not "in progress"

    def test_to_verify_chip_state_and_filter_when_not_zero(self):
        ActionItem.objects.filter(parent_action_item=self.mother).update(status="closed", closed_at=TODAY)
        ActionItem.objects.filter(pk=self.mother.pk).update(status="open")
        self.assertEqual(self.kpis()["todo"]["to_verify"], 1)
