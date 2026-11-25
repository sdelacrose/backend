"""Action Items list: request columns, progress, and the status of a mother that follows its sub-actions.

Columns: External ref. / LINKS ref. / Received on / Expected end come from the customer request (mother
only, never invented); Planned end is the action's own `forecast_date`, distinct from the customer's
expected end. A mother still "open" becomes "in progress" as soon as one sub-action has started."""
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from project_management.models import ActionItem, InterventionRequest
from project_management.test_intervention_plans_b import (
    PERM,
    ImportedMotherFixture,
    close,
)

CHECKER = "project_management.views.action_item.require_any_permission_multiple_modules"


class ListFixture(ImportedMotherFixture):
    def listing(self, user=None, **params):
        self.client.force_authenticate(user or self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            response = self.client.get(
                reverse("actionitem-list"), {"project": self.project.id, "page_size": 1000, **params}
            )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return {row["id"]: row for row in response.data["results"]}

    def set_status(self, item, status):
        item.refresh_from_db()
        item.status = status
        item.save()
        self.mother.refresh_from_db()


class ColumnMappingTests(ListFixture, TestCase):
    def test_the_mother_exposes_the_request_data_and_planned_end_stays_distinct(self):
        request = InterventionRequest.objects.get(pk=self.mother.intervention_request_id)
        ActionItem.objects.filter(pk=self.mother.pk).update(forecast_date=date(2026, 11, 12))
        row = self.listing()[self.mother.id]
        self.assertEqual(row["external_reference"], "EXT-001")
        self.assertEqual(row["links_reference"], request.reference)
        self.assertEqual(str(row["received_date"]), "2026-11-03")
        self.assertEqual(str(row["expected_end_date"]), "2026-11-20")  # customer deadline
        self.assertEqual(str(row["forecast_date"]), "2026-11-12")      # planned end: another field
        self.assertIsNone(row["start_date"])                             # no start date is invented

    def test_dates_stay_empty_when_absent(self):
        row = self.listing()[self.mother.id]
        self.assertIsNone(row["forecast_date"])
        self.assertIsNone(row["start_date"])

    def test_sub_actions_keep_their_reference_and_carry_no_request_columns(self):
        subs = self.listing()[self.mother.id]["sub_action_items"]
        self.assertEqual([s["sub_reference"] for s in subs], [f"{self.mother.id}-{n}" for n in (1, 2, 3)])
        for sub in subs:
            self.assertIsNone(sub["external_reference"])
            self.assertIsNone(sub["links_reference"])
            self.assertIsNone(sub["received_date"])
            self.assertIsNone(sub["expected_end_date"])

    def test_an_independent_action_has_no_request_column(self):
        plain = self.create_parent("Independent")
        row = self.listing()[plain.id]
        for key in ("external_reference", "links_reference", "received_date", "expected_end_date"):
            self.assertIsNone(row[key])
        self.assertIsNone(row["progress_percent"])  # no calculation exists: shown as "-"

    def test_the_bpu_badge_data_stays_on_the_mother(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(is_bpu=True)
        row = self.listing()[self.mother.id]
        self.assertTrue(row["effective_bpu"])
        self.assertTrue(all(sub["effective_bpu"] for sub in row["sub_action_items"]))  # inherited, UI shows the mother only

    def test_progress_is_the_weighted_progress_of_the_request_list(self):
        close(self.subs[0], self.user)
        row = self.listing()[self.mother.id]
        self.assertEqual(row["progress_percent"], compute_expected(self.mother))

    def test_the_list_costs_a_constant_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for n in range(4):
            self.create_parent(f"extra {n}")
        self.listing()  # warm-up (one-off caches)
        with CaptureQueriesContext(connection) as small:
            self.listing()
        for n in range(6):
            self.create_parent(f"more {n}")
        with CaptureQueriesContext(connection) as large:
            self.listing()
        self.assertEqual(len(small), len(large))


def compute_expected(mother):
    from project_management.services.action_progress import compute_progress

    return compute_progress(ActionItem.objects.get(pk=mother.pk))["progress"]


class MotherStatusTests(ListFixture, TestCase):
    def test_a_mother_with_only_open_sub_actions_stays_open(self):
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "open")

    def test_a_started_sub_action_makes_the_open_mother_in_progress(self):
        self.set_status(self.subs[0], "in progress")
        self.assertEqual(self.mother.status, "in progress")
        self.assertEqual(self.listing()[self.mother.id]["status"], "in progress")

    def test_a_closed_sub_action_alone_also_starts_the_mother(self):
        close(self.subs[0], self.user)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "in progress")

    def test_open_then_in_progress_then_closed(self):
        self.assertEqual(ActionItem.objects.get(pk=self.mother.pk).status, "open")
        self.set_status(self.subs[1], "in progress")
        self.assertEqual(self.mother.status, "in progress")
        for sub in self.subs:
            close(sub, self.user)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "closed")  # existing closing rule, unchanged

    def test_an_unknown_sub_action_status_is_never_read_as_in_progress(self):
        self.set_status(self.subs[0], "waiting for customer")
        self.assertEqual(self.mother.status, "open")

    def test_a_mother_with_an_unknown_status_is_left_alone(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(status="parked")
        self.set_status(self.subs[0], "in progress")
        self.assertEqual(self.mother.status, "parked")

    def test_a_closed_mother_is_not_reopened(self):
        ActionItem.objects.filter(pk=self.mother.pk).update(status="closed")
        self.set_status(self.subs[0], "in progress")
        self.assertEqual(self.mother.status, "closed")

    def test_the_mother_is_not_downgraded_when_the_sub_action_goes_back_to_open(self):
        self.set_status(self.subs[0], "in progress")
        self.set_status(self.subs[0], "open")
        self.assertEqual(self.mother.status, "in progress")

    def test_a_manual_sub_action_added_already_started_starts_the_mother(self):
        # (the API only accepts statuses configured on the project; the rule itself is on save)
        added = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(added.status_code, 201, added.data)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "open")
        self.set_status(ActionItem.objects.get(pk=added.data["id"]), "in progress")
        self.assertEqual(self.mother.status, "in progress")
        # created directly started (any creation path that does not go through that validation)
        other = self.create_parent("Mother 2")
        ActionItem.objects.create(parent_action_item=other, name="s", status="in progress", target_date=date(2026, 1, 10))
        other.refresh_from_db()
        self.assertEqual(other.status, "in progress")

    def test_a_manual_open_sub_action_does_not_change_an_open_mother(self):
        added = self.add_sub(progress_setting={"in_progress_points": 0, "closed_points": 0})
        self.assertEqual(added.status_code, 201, added.data)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "open")

    def test_an_action_without_sub_action_keeps_its_own_status(self):
        plain = self.create_parent("Independent")
        plain.refresh_from_db()
        self.assertEqual(plain.status, "open")
        plain.status = "in progress"
        plain.save()
        plain.refresh_from_db()
        self.assertEqual(plain.status, "in progress")

    def test_the_weighted_progress_is_unchanged_by_the_status_following(self):
        self.set_status(self.subs[0], "in progress")
        before = compute_expected(self.mother)
        self.set_status(self.subs[0], "in progress")  # idempotent
        self.assertEqual(compute_expected(self.mother), before)

    def test_existing_mothers_are_not_rewritten_in_bulk(self):
        # an old mother left open next to an in-progress child is NOT touched by any migration: it is
        # evaluated only when one of its sub-actions is saved
        ActionItem.objects.filter(pk=self.subs[0].pk).update(status="in progress")
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.status, "open")
        self.set_status(ActionItem.objects.get(pk=self.subs[1].pk), "in progress")
        self.assertEqual(self.mother.status, "in progress")
