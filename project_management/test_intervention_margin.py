"""Interventions list: "Margin (days)" = customer deadline (InterventionRequest.expected_end_date) minus the
contractor's planned end (the action's OWN forecast_date), in calendar days. target_date is never used and a missing
forecast is never replaced by the deadline."""
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from project_management.models import ActionItem, InterventionRequest
from project_management.test_action_item_list_columns import CHECKER, PERM
from project_management.test_actions_interventions_separation import SeparationFixture


class MarginFixture(SeparationFixture):
    def setUp(self):
        super().setUp()
        self.request = InterventionRequest.objects.get(pk=self.mother.intervention_request_id)

    def set_deadline(self, day, request=None):
        InterventionRequest.objects.filter(pk=(request or self.request).pk).update(expected_end_date=day)

    def set_forecast(self, item, day):
        ActionItem.objects.filter(pk=item.pk).update(forecast_date=day)

    def rows(self, scope="interventions", **params):
        return self.call("actionitem-list", scope, **params)["results"]

    def mother_row(self, **params):
        return {r["id"]: r for r in self.rows(**params)}[self.mother.id]

    def flat_request(self, ref, deadline, forecast):
        request = InterventionRequest.objects.create(
            project=self.request.project, company_profile=self.request.company_profile, reference=f"IR-M-{ref}",
            title=f"flat {ref}", received_date=date(2026, 1, 5), expected_end_date=deadline, source="csv",
            external_reference=f"M-{ref}", created_by=self.user,
        )
        item = ActionItem(
            milestone=self.mother.milestone, name=f"flat {ref}", status="open", target_date=deadline,
            created_by=self.user, intervention_request=request, intervention_action_key="assembly",
            forecast_date=forecast,
        )
        item._suppress_action_item_notifications = True
        item.save()
        return item


class MarginRuleTests(MarginFixture, TestCase):
    def test_positive_zero_negative_and_missing_forecast(self):
        self.set_deadline(date(2026, 11, 20))
        cases = [(date(2026, 11, 12), 8), (date(2026, 11, 20), 0), (date(2026, 11, 25), -5), (None, None)]
        for forecast, expected in cases:
            self.set_forecast(self.mother, forecast)
            row = self.mother_row()
            self.assertEqual(row["margin_days"], expected, forecast)
            self.assertEqual(str(row["client_deadline"]), "2026-11-20")

    def test_target_date_is_not_the_forecast_and_a_missing_forecast_is_never_replaced_by_the_deadline(self):
        self.set_deadline(date(2026, 11, 20))
        ActionItem.objects.filter(pk=self.mother.pk).update(target_date=date(2026, 11, 30), forecast_date=None)
        self.assertIsNone(self.mother_row()["margin_days"])  # not 20 Nov - 30 Nov, not 0
        self.set_forecast(self.mother, date(2026, 11, 18))
        self.assertEqual(self.mother_row()["margin_days"], 2)  # the forecast counts, whatever target_date says

    def test_month_year_and_leap_day_crossings_have_no_day_shift(self):
        crossings = [
            (date(2027, 1, 3), date(2026, 12, 28), 6),    # year change
            (date(2026, 3, 1), date(2026, 2, 27), 2),     # month change, non-leap February
            (date(2028, 3, 1), date(2028, 2, 28), 2),     # leap year: 29 Feb exists
            (date(2028, 3, 1), date(2028, 2, 29), 1),
            (date(2026, 10, 26), date(2026, 10, 24), 2),  # around the end of daylight saving time (Europe)
            (date(2026, 3, 30), date(2026, 3, 28), 2),
        ]
        for deadline, forecast, expected in crossings:
            self.set_deadline(deadline)
            self.set_forecast(self.mother, forecast)
            self.assertEqual(self.mother_row()["margin_days"], expected, (deadline, forecast))

    def test_the_calculation_never_depends_on_the_time_zone(self):
        from django.test import override_settings

        self.set_deadline(date(2026, 11, 20))
        self.set_forecast(self.mother, date(2026, 11, 19))
        for tz in ("UTC", "Pacific/Honolulu", "Europe/Paris", "Pacific/Auckland"):
            with override_settings(TIME_ZONE=tz):
                self.assertEqual(self.mother_row()["margin_days"], 1, tz)

    def test_the_margin_belongs_to_the_intervention_sub_actions_carry_none(self):
        self.set_deadline(date(2026, 11, 20))
        self.set_forecast(self.mother, date(2026, 11, 10))
        self.set_forecast(self.subs[0], date(2026, 11, 18))      # their own planned ends change nothing
        self.set_forecast(self.manual_sub, date(2026, 11, 22))
        row = self.mother_row()
        self.assertEqual(row["margin_days"], 10)  # intervention level: its own forecast against the deadline
        self.assertEqual(str(row["client_deadline"]), "2026-11-20")
        for sub in row["sub_action_items"]:
            self.assertIsNone(sub["margin_days"], sub["id"])
            self.assertIsNone(sub["client_deadline"], sub["id"])

    def test_older_flat_actions_keep_working(self):
        self.set_forecast(self.flat, date(2026, 2, 1))
        row = {r["id"]: r for r in self.rows()}[self.flat.id]
        self.assertEqual(row["margin_days"], 4)  # deadline 2026-02-05 of its request
        self.assertEqual(str(row["client_deadline"]), "2026-02-05")

    def test_classic_actions_have_no_margin(self):
        ActionItem.objects.filter(pk=self.classic.pk).update(forecast_date=date(2026, 12, 1))
        rows = {r["id"]: r for r in self.rows("classic")}
        self.assertIsNone(rows[self.classic.id]["margin_days"])
        self.assertIsNone(rows[self.classic.id]["client_deadline"])
        for sub in rows[self.classic.id]["sub_action_items"]:
            self.assertIsNone(sub["margin_days"])

    def test_the_margin_follows_a_modification_of_a_date(self):
        self.set_deadline(date(2026, 11, 20))
        self.set_forecast(self.mother, date(2026, 11, 15))
        self.assertEqual(self.mother_row()["margin_days"], 5)
        self.client.force_authenticate(self.user)
        with patch(f"{PERM}.has_permission", return_value=True), patch(f"{PERM}.has_object_permission", return_value=True), \
                patch(CHECKER, return_value=True):
            response = self.client.patch(reverse("actionitem-detail", args=[self.mother.id]), {"forecast_date": "2026-11-23"}, format="json")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(self.mother_row()["margin_days"], -3)


class MarginOrderingTests(MarginFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.items = {
            "plus5": self.flat_request("a", date(2026, 3, 10), date(2026, 3, 5)),
            "zero": self.flat_request("b", date(2026, 3, 10), date(2026, 3, 10)),
            "minus3": self.flat_request("c", date(2026, 3, 10), date(2026, 3, 13)),
            "none": self.flat_request("d", date(2026, 3, 10), None),
        }
        self.ids = {name: item.id for name, item in self.items.items()}

    def order(self, ordering):
        ids = [r["id"] for r in self.rows(ordering=ordering) if r["id"] in self.ids.values()]
        names = {v: k for k, v in self.ids.items()}
        return [names[i] for i in ids]

    def test_numeric_sort_both_directions_with_missing_values_always_last(self):
        self.assertEqual(self.order("margin_days"), ["minus3", "zero", "plus5", "none"])
        self.assertEqual(self.order("-margin_days"), ["plus5", "zero", "minus3", "none"])  # not text order: 5 > 0 > -3

    def test_sort_is_numeric_not_lexicographic(self):
        for name, (deadline, forecast) in {"big": (date(2026, 4, 30), date(2026, 4, 5)), "small": (date(2026, 4, 3), date(2026, 4, 1))}.items():
            self.ids[name] = self.flat_request(name, deadline, forecast).id  # +25 and +2
        order = [r["id"] for r in self.rows(ordering="margin_days") if r["id"] in (self.ids["big"], self.ids["small"], self.ids["plus5"])]
        self.assertEqual(order, [self.ids["small"], self.ids["plus5"], self.ids["big"]])  # 2, 5, 25 (text order would put 25 before 5)


class MarginPermissionTests(MarginFixture, TestCase):
    def test_another_company_sees_no_margin(self):
        self.client.force_authenticate(self.other_user)
        response = self.client.get(reverse("actionitem-list"), {"project": self.project.id, "scope": "interventions"})
        rows = response.data.get("results", []) if response.status_code == 200 else []
        self.assertIn(response.status_code, (200, 403, 404))
        self.assertEqual(rows, [])
