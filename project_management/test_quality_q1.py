# QUALITY Q1: access check, list and dashboard -- permissions and company isolation.
from unittest.mock import patch

from django.db.models.signals import post_save, pre_delete
from django.test import TestCase
from rest_framework.test import APIClient

from models.models import CompanyProfile, Permission, Role, RolePermission, User, UserRole
from project_management.models import ContactCompany, Project, SupplierEvaluation
from project_management.signals import (
    notify_on_contact_company_delete,
    notify_on_contact_company_save,
)
from project_management.test_document_revision import NoNotificationsMixin

ACCESS = "/api/project_management/quality/access"
DASHBOARD = "/api/project_management/quality/dashboard"
LIST = "/api/project_management/quality/evaluations"


def make_company(admin, name):
    return CompanyProfile.objects.create(
        admin_user=admin, company_name=name, company_address="1", post_code="0",
        company_town="T", company_country="C", company_tell_no="0", company_mob_no="1",
        company_web_address="http://x", company_email_address=f"{name}@x.invalid",
        company_tax_reference="T", company_iban="I",
    )


def grant(user, company, *actions):
    role = Role.objects.create(
        name=f"Quality {'-'.join(actions) or 'none'} {user.id}",
        company=company, type="solution", scope="company",
    )
    for action in actions:
        key = f"quality.{action}"
        permission, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": "quality", "description": "test"}
        )
        RolePermission.objects.create(role=role, permission=permission)
    UserRole.objects.create(user=user, role=role, company=company, assigned_by=user)


class QualityQ1Tests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        for signal, receiver in ((post_save, notify_on_contact_company_save), (pre_delete, notify_on_contact_company_delete)):
            signal.disconnect(receiver, sender=ContactCompany)
            self.addCleanup(signal.connect, receiver, sender=ContactCompany)
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.admin_a = self._user("qa.admin@links-local.invalid")
        self.company_a = make_company(self.admin_a, "QualityA")
        self.admin_b = self._user("qb.admin@links-local.invalid")
        self.company_b = make_company(self.admin_b, "QualityB")
        self.viewer_a = self._member("qa.viewer", self.company_a, "view")
        self.full_a = self._member("qa.full", self.company_a, "full_access")
        self.none_a = self._member("qa.none", self.company_a)
        self.viewer_b = self._member("qb.viewer", self.company_b, "view")

        self.supplier_a = ContactCompany.objects.create(
            company_profile=self.company_a, company_name="Supplier A", contact_email="s@a.invalid"
        )
        self.supplier_b = ContactCompany.objects.create(
            company_profile=self.company_b, company_name="Supplier B", contact_email="s@b.invalid"
        )
        S = SupplierEvaluation.Status
        for status in (S.DRAFT, S.IN_PROGRESS, S.PENDING_REVIEW, S.APPROVED, S.CLOSED, S.CANCELLED):
            SupplierEvaluation.objects.create(
                company=self.company_a, supplier=self.supplier_a, status=status
            )
        self.foreign = SupplierEvaluation.objects.create(
            company=self.company_b, supplier=self.supplier_b, status=S.PENDING_REVIEW,
            reference="B-ONLY",
        )

    def _user(self, email):
        return User.objects.create_user(
            email=email, password="x", first_name="T", last_name="U", user_type="company_user"
        )

    def _member(self, tag, company, *actions):
        user = self._user(f"{tag}@links-local.invalid")
        user.company = company
        user.save(update_fields=["company"])
        if actions:
            grant(user, company, *actions)
        return user

    def api(self, user=None):
        client = APIClient()
        if user:
            client.force_authenticate(user)
        return client

    # access
    def test_access_flag_follows_permission(self):
        self.assertTrue(self.api(self.viewer_a).get(ACCESS).json()["can_view"])
        self.assertTrue(self.api(self.full_a).get(ACCESS).json()["can_view"])
        self.assertFalse(self.api(self.none_a).get(ACCESS).json()["can_view"])

    def test_anonymous_is_refused(self):
        for url in (ACCESS, DASHBOARD, LIST):
            self.assertIn(self.api().get(url).status_code, (401, 403))

    # refusals
    def test_user_without_right_is_refused_on_dashboard_and_list(self):
        self.assertEqual(self.api(self.none_a).get(DASHBOARD).status_code, 403)
        self.assertEqual(self.api(self.none_a).get(LIST).status_code, 403)

    def test_other_module_right_does_not_open_quality(self):
        other = self._member("qa.other", self.company_a)
        role = Role.objects.create(name="Other", company=self.company_a, type="solution", scope="company")
        permission, _ = Permission.objects.get_or_create(
            key="contact_management.view", defaults={"name": "x", "module": "contact_management", "description": "t"}
        )
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=other, role=role, company=self.company_a, assigned_by=other)
        self.assertEqual(self.api(other).get(LIST).status_code, 403)

    # dashboard
    def test_dashboard_counts_only_own_company(self):
        data = self.api(self.viewer_a).get(DASHBOARD).json()
        self.assertEqual(data, {"total": 6, "in_progress": 2, "pending_review": 1, "approved_or_closed": 2})
        other = self.api(self.viewer_b).get(DASHBOARD).json()
        self.assertEqual(other["total"], 1)

    def test_dashboard_empty_company_returns_zeros(self):
        admin_c = self._user("qc.admin@links-local.invalid")
        company_c = make_company(admin_c, "QualityC")
        viewer_c = self._member("qc.viewer", company_c, "view")
        data = self.api(viewer_c).get(DASHBOARD).json()
        self.assertEqual(data, {"total": 0, "in_progress": 0, "pending_review": 0, "approved_or_closed": 0})
        self.assertEqual(self.api(viewer_c).get(LIST).json()["count"], 0)

    # list
    def test_list_never_exposes_another_company(self):
        body = self.api(self.viewer_a).get(LIST).json()
        self.assertEqual(body["count"], 6)
        self.assertNotIn(self.foreign.id, [row["id"] for row in body["results"]])
        self.assertEqual({row["supplier"]["name"] for row in body["results"]}, {"Supplier A"})

    def test_company_parameter_cannot_widen_scope(self):
        body = self.api(self.viewer_a).get(LIST, {"company": self.company_b.id}).json()
        self.assertEqual(body["count"], 6)

    def test_list_filters_and_search(self):
        client = self.api(self.viewer_a)
        self.assertEqual(client.get(LIST, {"status": "CLOSED"}).json()["count"], 1)
        self.assertEqual(client.get(LIST, {"status": "NOPE"}).json()["count"], 6)
        self.assertEqual(client.get(LIST, {"search": "Supplier A"}).json()["count"], 6)
        self.assertEqual(client.get(LIST, {"search": "B-ONLY"}).json()["count"], 0)

    def test_list_pagination(self):
        body = self.api(self.viewer_a).get(LIST, {"page_size": 4, "page": 2}).json()
        self.assertEqual((body["count"], len(body["results"])), (6, 2))
