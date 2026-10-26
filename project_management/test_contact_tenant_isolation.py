"""SEC-02: Contact / Company by-id access is confined to the requester's tenant."""

from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, User
from project_management.models import ContactCompany, ContactUser
from project_management.signals import notify_on_contact_company_save, notify_on_contact_save
from project_management.test_action_item_query_services import ActionItemQueryFixtureMixin
from project_management.views.contact import ContactCompanyViewSet, ContactUserViewSet


def _company(admin, name):
    company = CompanyProfile.objects.create(
        admin_user=admin, company_name=name, company_address="Street 1", company_town="Town",
        company_tell_no="1", company_mob_no="2", company_web_address="https://example.com",
        company_email_address=f"{name.replace(' ', '').lower()}@example.com",
        company_tax_reference=f"tax-{name}", company_iban=f"iban-{name}",
    )
    User.objects.filter(pk=admin.pk).update(company=company)
    admin.refresh_from_db()
    return company


class ContactTenantIsolationTests(ActionItemQueryFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        for handler, sender in (
            (notify_on_contact_save, ContactUser),
            (notify_on_contact_company_save, ContactCompany),
        ):
            post_save.disconnect(handler, sender=sender)
            self.addCleanup(post_save.connect, handler, sender=sender)
        patcher = patch("ecpmpapp.utils.should_notify_user_comprehensive", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Requester holds every contact_management right: only the tenant can refuse.
        rights = patch("project_management.permissions.ContactPermission._check_permission", return_value=True)
        rights.start()
        self.addCleanup(rights.stop)
        # The product-license lookup is raw SQL on a table owned by admin-server-models.
        license_check = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        license_check.start()
        self.addCleanup(license_check.stop)

        self.factory = APIRequestFactory()
        self.acme = ContactCompany.objects.create(
            company_profile=self.company, company_name="ACME", contact_email="acme@example.com",
        )
        self.alice = ContactUser.objects.create(
            company_profile=self.company, name="Alice", surname="Martin",
            email="alice@example.com", contact_company=self.acme,
        )
        self.other_user = User.objects.create_user(
            email="other-contacts@example.com", password="password", first_name="Other",
            last_name="Tenant", user_type="company_user",
        )
        self.other_company = _company(self.other_user, "Other Co")
        self.foreign_company = ContactCompany.objects.create(
            company_profile=self.other_company, company_name="Foreign", contact_email="f@example.com",
        )
        self.foreign_contact = ContactUser.objects.create(
            company_profile=self.other_company, name="Bob", surname="Foreign", email="bob@example.com",
            contact_company=self.foreign_company,
        )

    def call(self, viewset, actions, method, path, data=None, pk=None, user=None):
        request = getattr(self.factory, method)(path, data or {}, format="json")
        force_authenticate(request, user=user or self.user)
        view = viewset.as_view(actions)
        return view(request, pk=pk) if pk else view(request)

    def _rows(self, response):
        data = response.data["results"] if isinstance(response.data, dict) else response.data
        return {row["id"] for row in data}

    RETRIEVE = {"get": "retrieve"}
    UPDATE = {"patch": "partial_update"}
    DESTROY = {"delete": "destroy"}

    # ---- same tenant: still allowed ---------------------------------------------------------
    def test_same_tenant_read_and_update_contact_and_company(self):
        for viewset, obj, field in (
            (ContactUserViewSet, self.alice, "name"),
            (ContactCompanyViewSet, self.acme, "company_name"),
        ):
            self.assertEqual(self.call(viewset, self.RETRIEVE, "get", "/x/", pk=obj.pk).status_code, 200)
            response = self.call(viewset, self.UPDATE, "patch", "/x/", {field: "Renamed"}, pk=obj.pk)
            self.assertEqual(response.status_code, 200, response.data)
            obj.refresh_from_db()
            self.assertEqual(getattr(obj, field), "Renamed")

    # ---- other tenant: refused by id, whatever the rights -----------------------------------
    def test_foreign_contact_and_company_are_refused_by_id(self):
        for viewset, obj, field in (
            (ContactUserViewSet, self.foreign_contact, "name"),
            (ContactCompanyViewSet, self.foreign_company, "company_name"),
        ):
            before = getattr(obj, field)
            self.assertEqual(self.call(viewset, self.RETRIEVE, "get", "/x/", pk=obj.pk).status_code, 404)
            self.assertEqual(
                self.call(viewset, self.UPDATE, "patch", "/x/", {field: "Hacked"}, pk=obj.pk).status_code, 404,
            )
            self.assertEqual(self.call(viewset, self.DESTROY, "delete", "/x/", pk=obj.pk).status_code, 404)
            obj.refresh_from_db()
            self.assertEqual(getattr(obj, field), before)
            self.assertFalse(obj.is_deleted)

    def test_the_other_tenant_cannot_reach_ours_either(self):
        for viewset, obj in ((ContactUserViewSet, self.alice), (ContactCompanyViewSet, self.acme)):
            for actions, method in ((self.RETRIEVE, "get"), (self.UPDATE, "patch"), (self.DESTROY, "delete")):
                response = self.call(viewset, actions, method, "/x/", {}, pk=obj.pk, user=self.other_user)
                self.assertEqual(response.status_code, 404)
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.is_deleted)

    # ---- lists, searches, relations ---------------------------------------------------------
    def test_lists_and_search_stay_in_the_tenant(self):
        users = self.call(ContactUserViewSet, {"get": "list"}, "get", "/x/")
        self.assertEqual(self._rows(users), {self.alice.pk})
        companies = self.call(ContactCompanyViewSet, {"get": "list"}, "get", "/x/")
        self.assertEqual(self._rows(companies), {self.acme.pk})
        searched = self.call(ContactUserViewSet, {"get": "list"}, "get", "/x/", {"search": "Foreign"})
        self.assertEqual(self._rows(searched), set())

    def test_contact_company_relation_is_preserved_and_tenant_bound(self):
        response = self.call(ContactUserViewSet, self.RETRIEVE, "get", "/x/", pk=self.alice.pk)
        self.assertEqual(response.data["contact_company"], self.acme.pk)
        # Re-pointing our contact at the foreign company is not accepted.
        self.call(
            ContactUserViewSet, self.UPDATE, "patch", "/x/", {"contact_company": self.foreign_company.pk},
            pk=self.alice.pk,
        )
        self.alice.refresh_from_db()
        self.assertNotEqual(self.alice.contact_company_id, self.foreign_company.pk)
