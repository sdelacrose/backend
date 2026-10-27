"""LINKS-RECETTE-05AN: LicensePermission must refuse (never return a truthy JsonResponse) for an inactive or
deleted user. Run with DJANGO_SETTINGS_MODULE=ecpmpapp.settings.recette on a throw-away database."""
from datetime import timedelta
from types import SimpleNamespace

from django.http import JsonResponse
from django.test import TestCase
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from ecpmpapp.custom_exceptions import InactiveAccountException, InactiveLicenseException
from ecpmpapp.permissions import LicensePermission
from models.models import User
from package_license.models import License, Package, Product

BUSINESS_DATA_READS = []


class ProtectedView(APIView):
    authentication_classes = []
    permission_classes = [LicensePermission]

    def get(self, request):
        BUSINESS_DATA_READS.append(request.user.pk)
        return Response({"secret": "business data"})


class LicensePermissionTests(TestCase):
    def setUp(self):
        BUSINESS_DATA_READS.clear()
        self.user = User.objects.create(email="perm@example.invalid", last_name="Perm", auth0_sub="auth0|perm")
        self.product = Product.objects.create(name="Energy Consultant Solution", price=0)

    def grant(self, *, package_active=True, license_active=True, expires_in=timedelta(days=30)):
        package = Package.objects.create(
            is_active=package_active, expires_at=timezone.now() + expires_in, status="Cancelled", is_freetrial=True,
        )
        License.objects.create(
            package=package, product=self.product, user=self.user, is_active=license_active,
            license_assigned_at=timezone.now(),
        )

    def http_get(self):
        request = APIRequestFactory().get("/protected/")
        force_authenticate(request, user=User.objects.get(pk=self.user.pk))
        return ProtectedView.as_view()(request)

    def direct(self):
        request = SimpleNamespace(user=User.objects.get(pk=self.user.pk), query_params={})
        return LicensePermission().has_permission(request, None)

    def assert_refused(self, status, exception):
        with self.assertRaises(exception):
            self.direct()
        response = self.http_get()
        self.assertEqual(response.status_code, status)
        self.assertNotIn("secret", response.data)
        self.assertEqual(BUSINESS_DATA_READS, [])  # the view body never ran

    def test_active_user_with_valid_licence_is_accepted(self):
        self.grant()
        self.assertIs(self.direct(), True)
        response = self.http_get()
        self.assertEqual((response.status_code, response.data), (200, {"secret": "business data"}))
        self.assertEqual(BUSINESS_DATA_READS, [self.user.pk])

    def test_active_user_without_licence_is_refused(self):
        self.assert_refused(423, InactiveLicenseException)

    def test_expired_licence_is_refused(self):
        self.grant(expires_in=timedelta(hours=-1))
        self.assert_refused(423, InactiveLicenseException)

    def test_revoked_licence_or_package_is_refused(self):
        self.grant(license_active=False)
        self.assert_refused(423, InactiveLicenseException)
        License.objects.all().delete()
        Package.objects.all().delete()
        self.grant(package_active=False)
        self.assert_refused(423, InactiveLicenseException)

    def test_inactive_user_is_refused_even_with_valid_licence(self):
        self.grant()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assert_refused(410, InactiveAccountException)

    def test_deleted_user_is_refused_even_if_active_and_licensed(self):
        self.grant()
        User.objects.filter(pk=self.user.pk).update(is_active=True, is_deleted=True)
        self.assert_refused(410, InactiveAccountException)

    def test_has_permission_never_returns_a_json_response(self):
        self.grant()
        for fields in ({}, {"is_active": False}, {"is_active": True, "is_deleted": True}):
            User.objects.filter(pk=self.user.pk).update(**{"is_active": True, "is_deleted": False, **fields})
            try:
                result = self.direct()
            except (InactiveAccountException, InactiveLicenseException):
                continue
            self.assertNotIsInstance(result, JsonResponse)
            self.assertIs(result, True)
