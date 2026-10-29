"""Account guards require neither recette models nor a database."""
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from ecpmpapp.custom_exceptions import InactiveAccountException, InactiveLicenseException
from ecpmpapp.permissions import LicensePermission

class ProtectedView(APIView):
    authentication_classes = []
    permission_classes = [LicensePermission]
    def get(self, request):
        return Response({"secret": "business data"})

class LicensePermissionTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=1, pk=1, is_active=True, is_deleted=False)
        patcher = patch("ecpmpapp.permissions.connection")
        self.connection = patcher.start()
        self.addCleanup(patcher.stop)
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = (1,)

    def direct(self):
        return LicensePermission().has_permission(SimpleNamespace(user=self.user, query_params={}), None)

    def http_get(self):
        request = APIRequestFactory().get("/protected/")
        force_authenticate(request, user=self.user)
        return ProtectedView.as_view()(request)

    def test_valid_license_result_allows_access(self):
        self.assertIs(self.direct(), True)
        self.assertEqual(self.http_get().status_code, 200)
        self.assertEqual(self.cursor.execute.call_args.args[1], [1, "Energy Consultant Solution"])

    def test_no_license_result_refuses_access(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(InactiveLicenseException):
            self.direct()
        self.assertEqual(self.http_get().status_code, 423)

    def test_inactive_or_deleted_stops_before_query_and_view(self):
        for active, deleted in ((False, False), (True, True), (False, True)):
            with self.subTest(active=active, deleted=deleted):
                self.user.is_active, self.user.is_deleted = active, deleted
                with self.assertRaises(InactiveAccountException):
                    self.direct()
                with patch.object(ProtectedView, "get") as body:
                    response = self.http_get()
                    self.assertEqual(response.status_code, 410)
                    self.assertEqual(response.data["detail"].code, "inactive_account")
                    body.assert_not_called()
                self.connection.cursor.assert_not_called()

    def test_missing_user_is_denied(self):
        self.user = None
        self.assertIs(self.direct(), False)
        self.connection.cursor.assert_not_called()
