"""Interventions page indicator cards: same KPI endpoint, `scope=interventions` adds the general filters.

The cards follow the general filters and the import period, always count closed interventions and ignore the `kpi`
pre-filter. Without `scope` (dashboard widget) the endpoint is unchanged. Counting rules (one request = one
intervention, flat actions grouped, BPU at intervention level, permissions, company isolation) are covered by
test_intervention_kpis.py and are re-checked here through the filtered path."""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from project_management.test_action_item_list_columns import CHECKER, PERM
from project_management.test_intervention_kpis import KpiFixture, RestrictedUserKpiTests

TODAY = timezone.localdate()
CARD_FLAGS = ("done", "in_progress", "not_started", "bpu")


class CardFixture(KpiFixture):
    def cards(self, user=None, **params):
        return self.kpis(user, scope="interventions", **params)

    def listed_interventions(self, flag, **params):
        """Top-level rows of the list for a pre-filter (sub-action rows are never counted)."""
        self.client.force_authenticate(self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            response = self.client.get(
                reverse("actionitem-list"),
                {"project": self.project.id, "page_size": 1000, "scope": "interventions", "show_closed": "true", "kpi": flag, **params},
            )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return {row["id"] for row in response.data["results"]}


class CardCountingTests(CardFixture, TestCase):
    def test_closed_interventions_are_always_counted_and_the_kpi_prefilter_never_reduces_the_cards(self):
        self.set_status(self.flat, "closed", closed_at=TODAY)
        plain = self.cards()  # no show_closed sent: the toggle does not drive the cards
        self.assertEqual((plain["portfolio"]["total"], plain["progress"]["done"]), (2, 1))
        for flag in ("done", "bpu", "not_started"):
            selected = self.cards(kpi=flag)
            self.assertEqual(selected["portfolio"]["total"], 2, flag)
            self.assertEqual(selected["progress"]["done"], 1, flag)
        self.assertEqual(self.cards(show_closed="false")["portfolio"]["total"], 2)

    def test_each_card_matches_the_interventions_listed_on_click_without_sub_action_rows(self):
        ActionItem_update = type(self.mother).objects.filter
        ActionItem_update(pk=self.mother.pk).update(is_bpu=True)  # its sub-actions inherit it: still ONE intervention
        self.set_status(self.flat, "in progress")
        data = self.cards()
        counts = {
            "done": data["progress"]["done"], "in_progress": data["progress"]["in_progress"],
            "not_started": data["progress"]["not_started"], "bpu": data["portfolio"]["bpu"],
        }
        for flag in CARD_FLAGS:
            self.assertEqual(len(self.listed_interventions(flag)), counts[flag], flag)
        total = self.cards()["portfolio"]["total"]
        self.assertEqual(len(self.listed_interventions(flag="not_started")) + counts["in_progress"] + counts["done"], total)

    def test_without_the_scope_parameter_the_dashboard_endpoint_is_unchanged(self):
        self.assertEqual(self.kpis(search="no-such-text", status="closed", assignee="999")["portfolio"]["total"], 2)


class CardFilterTests(CardFixture, TestCase):
    def test_search_follows_the_list_predicate(self):
        self.assertEqual(self.cards(search=self.mother.name[:8])["portfolio"]["total"], 1)
        none = self.cards(search="zzz-no-match-zzz")
        self.assertEqual((none["portfolio"]["total"], none["progress"]["not_started"], none["portfolio"]["bpu"]), (0, 0, 0))

    def test_owner_and_status_filters(self):
        self.mother.assignees.add(self.user)
        self.assertEqual(self.cards(assignee=str(self.user.id))["portfolio"]["total"], 1)
        self.set_status(self.flat, "in progress")
        by_status = self.cards(status="in progress")
        self.assertEqual((by_status["portfolio"]["total"], by_status["progress"]["in_progress"]), (1, 1))

    def test_filtered_cards_equal_the_filtered_list(self):
        self.mother.assignees.add(self.user)
        data = self.cards(assignee=str(self.user.id))
        listed = self.listed_interventions("not_started", assignee=str(self.user.id))
        self.assertEqual(data["progress"]["not_started"], len(listed))

    def test_a_classic_action_never_enters_the_filtered_counters(self):
        self.assertEqual(self.cards(search=self.classic.name[:6])["portfolio"]["total"], 0)


class CardRestrictedUserTests(RestrictedUserKpiTests):
    """Permissions apply before any aggregate, also through the filtered path."""

    def get_kpis(self, user):
        self.client.force_authenticate(user)
        only = lambda u, modules, s, context: tuple(next(iter(modules.values()))) == ("view_assigned_created",)
        low = lambda permission, u, module, action, s, context: module == "action_item" and action == "view_assigned_created"
        with patch(self.VIEW, side_effect=only), patch(self.LOW, autospec=True, side_effect=low), patch(self.BROAD, return_value=False):
            return self.client.get(
                reverse("actionitem-intervention-kpis"), {"project": self.project.id, "scope": "interventions"}
            )
