"""Real behaviour of the license control, without any license table.

The license tables (package_license_*) belong to the admin service and are not installed in this app's test
DB. Tests whose scope is elsewhere isolate the notification decision explicitly (see the timesheet tests);
this module proves separately that the license control itself keeps working: the SQL is executed through
the cursor, and its result drives the decision (active solution, access granted / refused, inactive user).
No fake table is created and no check is bypassed.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import unittest
from rest_framework.test import APIRequestFactory

from ecpmpapp.custom_exceptions import InactiveLicenseException
from ecpmpapp.permissions import LicensePermission
from ecpmpapp.utils import get_user_active_solution


def _cursor_returning(row):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = row
    return cursor


def _request(user, solution=None):
    request = APIRequestFactory().get("/x", {"solution": solution} if solution else {})
    request.user = user
    request.query_params = request.GET
    return request


class UserActiveSolutionTests(unittest.TestCase):
    def test_the_active_license_decides_the_solution(self):
        for product, expected in (
            ("Energy Consultant Solution", "Energy Consultant Solution"),
            ("Core Solution", "Core Solution"),
            ("Enterprise Solution", "Enterprise Solution"),
        ):
            cursor = _cursor_returning((product, 2))
            with patch("ecpmpapp.utils.connection.cursor", return_value=cursor):
                self.assertEqual(get_user_active_solution(7), expected)
            sql, params = cursor.execute.call_args.args
            self.assertIn("package_license_license", sql)  # the real query is executed
            self.assertIn("pkg.is_active = TRUE", sql)
            self.assertEqual(params, [7])

    def test_without_active_license_the_documented_default_applies(self):
        with patch("ecpmpapp.utils.connection.cursor", return_value=_cursor_returning(None)):
            self.assertEqual(get_user_active_solution(7), "Enterprise Solution")

    def test_a_database_error_is_not_swallowed_by_the_lookup_itself(self):
        # a missing license table must stay visible (it is the notification layer that decides to tolerate it)
        cursor = _cursor_returning(None)
        cursor.execute.side_effect = RuntimeError('relation "package_license_license" does not exist')
        with patch("ecpmpapp.utils.connection.cursor", return_value=cursor):
            with self.assertRaises(RuntimeError):
                get_user_active_solution(7)


class LicensePermissionTests(unittest.TestCase):
    user = SimpleNamespace(id=11, is_deleted=False, is_active=True)

    def check(self, row, user=None, solution=None):
        cursor = _cursor_returning(row)
        with patch("ecpmpapp.permissions.connection.cursor", return_value=cursor):
            result = LicensePermission().has_permission(_request(user or self.user, solution), None)
        return result, cursor

    def test_an_active_license_grants_access_and_the_product_is_queried(self):
        result, cursor = self.check((1,), solution="Core Solution")
        self.assertTrue(result)
        sql, params = cursor.execute.call_args.args
        self.assertIn("package_license_license", sql)
        self.assertIn("pkg.expires_at > NOW()", sql)
        self.assertEqual(params, [11, "Core Solution"])

    def test_the_default_product_is_the_energy_consultant_solution(self):
        _, cursor = self.check((1,))
        self.assertEqual(cursor.execute.call_args.args[1], [11, "Energy Consultant Solution"])

    def test_no_active_license_is_refused(self):
        with self.assertRaises(InactiveLicenseException):
            self.check(None)

    def test_an_inactive_or_deleted_account_is_refused_before_any_license_query(self):
        for flags in ({"is_deleted": True, "is_active": True}, {"is_deleted": False, "is_active": False}):
            user = SimpleNamespace(id=11, **flags)
            cursor = _cursor_returning((1,))
            with patch("ecpmpapp.permissions.connection.cursor", return_value=cursor):
                response = LicensePermission().has_permission(_request(user), None)
            self.assertEqual(response.status_code, 410)
            cursor.execute.assert_not_called()
