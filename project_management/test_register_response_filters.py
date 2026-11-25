from unittest.mock import Mock, patch, call
from django.test import SimpleTestCase
from project_management.services import correspondances_register as register

class RegisterResponseFiltersTests(SimpleTestCase):
    def kwargs(self, **overrides):
        values = dict(project=None, company_scope=False, direction=None, company=None,
                      contact=None, correspondence_type=None, search=None, status=None)
        values.update(overrides)
        return values

    def test_lifecycle_and_response_are_intersected_on_the_scoped_queryset(self):
        scoped, issued, overdue = Mock(), Mock(), Mock()
        with patch.object(register, 'correspondence_queryset_for_user', return_value=scoped), patch.object(register, '_correspondence_status_filter', side_effect=[issued, overdue]) as predicate:
            result = register._build_correspondence_queryset(object(), **self.kwargs(status='ISSUED', response_status='OVERDUE'))
        self.assertIs(result, overdue)
        self.assertEqual(predicate.call_args_list, [call(scoped, 'ISSUED'), call(issued, 'OVERDUE')])

    def test_pending_response_reuses_the_existing_due_date_rules(self):
        scoped = Mock()
        with patch.object(register, 'correspondence_queryset_for_user', return_value=scoped), patch.object(register, '_correspondence_status_filter') as predicate:
            register._build_correspondence_queryset(object(), **self.kwargs(response_status='PENDING'))
        predicate.assert_called_once_with(scoped, 'PENDING')

    def test_invalid_response_never_becomes_a_lifecycle_filter(self):
        scoped = Mock()
        with patch.object(register, 'correspondence_queryset_for_user', return_value=scoped), patch.object(register, '_correspondence_status_filter') as predicate:
            result = register._build_correspondence_queryset(object(), **self.kwargs(response_status='ISSUED'))
        self.assertIs(result, scoped.none.return_value)
        predicate.assert_not_called()

    def test_response_filter_never_matches_transmittals(self):
        with patch.object(register.Transmittal.objects, 'none') as empty, patch.object(register, 'transmittal_queryset_for_user') as scope:
            result = register._build_transmittal_queryset(object(), **self.kwargs(response_status='ANSWERED'))
        self.assertIs(result, empty.return_value)
        scope.assert_not_called()
