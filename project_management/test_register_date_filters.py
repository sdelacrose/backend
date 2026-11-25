from datetime import date
from unittest.mock import Mock
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError
from project_management.services.correspondances_register import _filter_register_dates
from project_management.views.correspondances_register import parse_register_dates

class RegisterDateFiltersTests(SimpleTestCase):
    def test_open_ended_periods(self):
        self.assertEqual(parse_register_dates({'date_from':'2026-11-22'}), (date(2026,11,22),None))
        self.assertEqual(parse_register_dates({'date_to':'2026-11-22'}), (None,date(2026,11,22)))
        self.assertEqual(parse_register_dates({}), (None,None))

    def test_same_day_is_allowed(self):
        self.assertEqual(parse_register_dates({'date_from':'2026-11-22','date_to':'2026-11-22'}), (date(2026,11,22),date(2026,11,22)))

    def test_invalid_or_reversed_dates_are_rejected(self):
        for params in ({'date_from':'bad'}, {'date_to':'2026-02-30'}, {'date_from':'2026-11-23','date_to':'2026-11-22'}):
            with self.subTest(params=params), self.assertRaises(ValidationError):
                parse_register_dates(params)

    def test_bounds_are_inclusive_and_intersect_on_the_displayed_date(self):
        scoped, after, before = Mock(), Mock(), Mock()
        scoped.alias.return_value = scoped
        scoped.filter.return_value = after
        after.filter.return_value = before
        result = _filter_register_dates(scoped, date(2026,11,1), date(2026,11,22))
        scoped.filter.assert_called_once_with(register_filter_date__gte=date(2026,11,1))
        after.filter.assert_called_once_with(register_filter_date__lte=date(2026,11,22))
        self.assertIs(result,before)

    def test_no_period_keeps_the_existing_scoped_queryset(self):
        scoped=Mock()
        self.assertIs(_filter_register_dates(scoped),scoped)
        scoped.filter.assert_not_called()

    def test_projected_date_expression_compiles_for_both_register_sources(self):
        from project_management.models import Correspondence, Transmittal
        from project_management.services.correspondances_register import _correspondence_projection, _transmittal_projection
        for model, projection in ((Correspondence, _correspondence_projection), (Transmittal, _transmittal_projection)):
            with self.subTest(model=model.__name__):
                query = _filter_register_dates(projection(model.objects.all()), date(2026,11,1), date(2026,11,22))
                sql, params = query.query.sql_with_params()
                self.assertIn(date(2026,11,1), params)
                self.assertIn(date(2026,11,22), params)
                self.assertNotIn('register_filter_date', query.query.values_select)
