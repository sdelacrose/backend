# NETWORK-CONTACT-FIX-02: contact permissions (create / ordinary edit / soft delete / real delete)
# and the project links of a deleted contact.
from unittest.mock import patch

from django.db.models.signals import post_save, pre_delete
from django.test import TestCase
from rest_framework.test import APIClient

from models.models import (
    CompanyProfile,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from project_management.models import (
    ContactCompany,
    ContactUser,
    Project,
    ProjectExternalUsers,
)
from project_management.signals import (
    create_default_folders,
    notify_on_contact_company_delete,
    notify_on_contact_company_save,
    notify_on_contact_save,
    notify_on_contact_user_delete,
    notify_on_project_save,
)
from project_management.test_document_revision import NoNotificationsMixin

URL = "/api/project_management/contact-user/"
COMPANY_URL = "/api/project_management/contact-company/"
ROLE_CHOICES = ["consultant", "customer", "supplier"]


def grant(user, company, *actions):
    role = Role.objects.create(
        name=f"Contacts {'-'.join(actions) or 'none'} {user.id}",
        company=company, type="solution", scope="company",
    )
    for action in actions:
        key = f"contact_management.{action}"
        permission, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": "contact_management", "description": "test"}
        )
        RolePermission.objects.create(role=role, permission=permission)
    UserRole.objects.create(user=user, role=role, company=company, assigned_by=user)


class ContactPermissionTests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        for signal, receiver, sender in (
            (post_save, create_default_folders, Project),
            (post_save, notify_on_project_save, Project),
            (post_save, notify_on_contact_save, ContactUser),
            (post_save, notify_on_contact_company_save, ContactCompany),
            (pre_delete, notify_on_contact_user_delete, ContactUser),
            (pre_delete, notify_on_contact_company_delete, ContactCompany),
        ):
            signal.disconnect(receiver, sender=sender)
            self.addCleanup(signal.connect, receiver, sender=sender)
        # The global product-license check queries tables absent from the test DB.
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        choices = patch(
            "project_management.serializers.contact.get_valid_choices", return_value=ROLE_CHOICES
        )
        choices.start()
        self.addCleanup(choices.stop)

        self.admin = self._user("contacts.admin@links-local.invalid")
        self.company = CompanyProfile.objects.create(
            admin_user=self.admin, company_name="Contacts Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="c@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.admin.company = self.company
        self.admin.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Contacts Project", contract_value=1, company_profile=self.company,
            created_by=self.admin,
        )
        self.contact = ContactUser.objects.create(
            company_profile=self.company, name="Jane", surname="Doe",
            email="jane.doe@links-local.invalid",
        )
        self.other_contact = ContactUser.objects.create(
            company_profile=self.company, name="Sam", surname="Other",
            email="sam.other@links-local.invalid",
        )
        self.link = ProjectExternalUsers.objects.create(
            project=self.project, contact_user=self.contact, role="customer"
        )
        self.other_link = ProjectExternalUsers.objects.create(
            project=self.project, contact_user=self.other_contact, role="supplier"
        )

    def _user(self, email, *actions):
        user = User.objects.create_user(
            email=email, password="x", first_name="T", last_name="U", user_type="company_user"
        )
        if actions:
            user.company = self.company
            user.save(update_fields=["company"])
            grant(user, self.company, *actions)
        return user

    def as_user(self, tag, *actions):
        user = self._user(f"{tag}@links-local.invalid", *actions)
        if not actions:
            user.company = self.company
            user.save(update_fields=["company"])
        client = APIClient()
        client.force_authenticate(user)
        return client

    # --- ordinary edit ---------------------------------------------------------------------

    def test_a_user_who_may_update_can_edit_without_the_delete_permission(self):
        client = self.as_user("updater", "view", "update")
        response = client.patch(URL + f"{self.contact.id}/", {"name": "Janet"}, format="json")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.name, "Janet")
        self.assertFalse(self.contact.is_deleted)

    def test_an_edit_that_resends_the_unchanged_deleted_flag_is_still_an_ordinary_edit(self):
        client = self.as_user("updater2", "view", "update")
        response = client.patch(
            URL + f"{self.contact.id}/", {"name": "Janet", "is_deleted": False}, format="json"
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))

    def test_editing_keeps_the_stored_project_role(self):
        client = self.as_user("updater3", "view", "update")
        response = client.patch(
            URL + f"{self.contact.id}/",
            {"name": "Janet", "projects": [{"project_id": self.project.id}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.link.refresh_from_db()
        self.assertEqual(self.link.role, "customer")

    def test_editing_is_refused_without_the_update_permission(self):
        for actions in (("view",), ("view", "delete"), ("create",)):
            client = self.as_user("noupdate" + "".join(a[0] for a in actions), *actions)
            response = client.patch(URL + f"{self.contact.id}/", {"name": "Nope"}, format="json")
            self.assertEqual(response.status_code, 403, actions)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.name, "Jane")

    def test_full_access_still_does_everything(self):
        client = self.as_user("fullaccess", "full_access")
        self.assertEqual(
            client.patch(URL + f"{self.contact.id}/", {"name": "Full"}, format="json").status_code, 200
        )
        self.assertEqual(
            client.patch(URL + f"{self.contact.id}/", {"is_deleted": True}, format="json").status_code, 200
        )

    # --- soft delete / restore ----------------------------------------------------------------

    def test_moving_to_the_recycle_bin_requires_the_delete_permission(self):
        client = self.as_user("onlyupdate", "view", "update")
        response = client.patch(URL + f"{self.contact.id}/", {"is_deleted": True}, format="json")
        self.assertEqual(response.status_code, 403)
        # also when mixed with an ordinary edit
        response = client.patch(
            URL + f"{self.contact.id}/", {"name": "Sneaky", "is_deleted": "true"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.is_deleted)
        self.assertEqual(self.contact.name, "Jane")

    def test_soft_delete_and_restore_with_the_delete_permission_keep_the_project_links(self):
        client = self.as_user("deleter", "view", "delete")
        response = client.patch(URL + f"{self.contact.id}/", {"is_deleted": True}, format="json")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.contact.refresh_from_db()
        self.assertTrue(self.contact.is_deleted)
        # Kept for history and so the contact can be restored with its roles.
        self.link.refresh_from_db()
        self.assertEqual((self.link.contact_user_id, self.link.role), (self.contact.id, "customer"))
        response = client.patch(URL + f"{self.contact.id}/", {"is_deleted": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.is_deleted)

    # --- real delete ---------------------------------------------------------------------------

    def test_a_real_delete_is_refused_without_the_delete_permission(self):
        client = self.as_user("nodelete", "view", "update")
        self.assertEqual(client.delete(URL + f"{self.contact.id}/").status_code, 403)
        self.assertTrue(ContactUser.objects.filter(pk=self.contact.id).exists())
        self.assertTrue(ProjectExternalUsers.objects.filter(pk=self.link.id).exists())

    def test_a_real_delete_removes_the_links_that_would_be_orphans_and_only_those(self):
        client = self.as_user("realdeleter", "view", "delete")
        response = client.delete(URL + f"{self.contact.id}/")
        self.assertEqual(response.status_code, 204, getattr(response, "data", None))
        self.assertFalse(ContactUser.objects.filter(pk=self.contact.id).exists())
        self.assertFalse(ProjectExternalUsers.objects.filter(pk=self.link.id).exists())
        # No nameless external member is left behind...
        self.assertFalse(
            ProjectExternalUsers.objects.filter(
                contact_user__isnull=True, contact_company__isnull=True
            ).exists()
        )
        # ...and another contact's link is untouched.
        self.other_link.refresh_from_db()
        self.assertEqual(self.other_link.contact_user_id, self.other_contact.id)

    def test_a_link_that_still_points_to_a_company_is_kept_when_its_user_is_deleted(self):
        company_contact = ContactCompany.objects.create(
            company_profile=self.company, company_name="Acme", company_role="Client",
            contact_email="acme@links-local.invalid",
        )
        both = ProjectExternalUsers.objects.create(
            project=self.project, contact_user=self.contact, contact_company=company_contact,
            role="supplier",
        )
        self.contact.delete()
        both.refresh_from_db()
        self.assertIsNone(both.contact_user_id)
        self.assertEqual(both.contact_company_id, company_contact.id)

    def test_deleting_a_contact_company_removes_its_orphan_links(self):
        company_contact = ContactCompany.objects.create(
            company_profile=self.company, company_name="Acme", company_role="Client",
            contact_email="acme@links-local.invalid",
        )
        link = ProjectExternalUsers.objects.create(
            project=self.project, contact_company=company_contact, role="customer"
        )
        company_contact.delete()
        self.assertFalse(ProjectExternalUsers.objects.filter(pk=link.id).exists())
        self.assertTrue(ProjectExternalUsers.objects.filter(pk=self.link.id).exists())

    # --- creation ------------------------------------------------------------------------------

    def test_creation_still_works_with_the_create_permission_and_a_valid_role(self):
        client = self.as_user("creator", "create")
        response = client.post(
            URL,
            {
                "name": "New", "surname": "Contact", "email": "new.contact@links-local.invalid",
                "projects": [{"project_id": self.project.id, "role": "customer"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        link = ProjectExternalUsers.objects.get(contact_user_id=response.data["id"])
        self.assertEqual((link.project_id, link.role), (self.project.id, "customer"))

    def test_the_correspondence_quick_create_payload_needs_no_project_role(self):
        # QuickCreateContactDialog sends only name, surname, email and the known company.
        company_contact = ContactCompany.objects.create(
            company_profile=self.company, company_name="Acme", company_role="Client",
            contact_email="acme@links-local.invalid",
        )
        client = self.as_user("quickcreator", "create")
        response = client.post(
            URL,
            {
                "name": "Quick", "surname": "Contact", "email": "quick.contact@links-local.invalid",
                "contact_company": company_contact.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        created = ContactUser.objects.get(pk=response.data["id"])
        self.assertEqual(created.contact_company_id, company_contact.id)
        self.assertFalse(ProjectExternalUsers.objects.filter(contact_user=created).exists())

    def test_creation_is_refused_without_the_create_permission(self):
        client = self.as_user("nocreate", "view", "update", "delete")
        response = client.post(
            URL, {"name": "N", "surname": "C", "email": "n.c@links-local.invalid"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ContactUser.objects.filter(email="n.c@links-local.invalid").exists())

    # --- companies share the same rule ----------------------------------------------------------

    def test_a_contact_company_can_be_edited_with_update_only_but_not_recycled(self):
        company_contact = ContactCompany.objects.create(
            company_profile=self.company, company_name="Acme", company_role="Client",
            contact_email="acme@links-local.invalid",
        )
        client = self.as_user("companyupdater", "view", "update")
        ok = client.patch(COMPANY_URL + f"{company_contact.id}/", {"company_name": "Acme 2"}, format="json")
        self.assertEqual(ok.status_code, 200, getattr(ok, "data", None))
        denied = client.patch(COMPANY_URL + f"{company_contact.id}/", {"is_deleted": True}, format="json")
        self.assertEqual(denied.status_code, 403)
        company_contact.refresh_from_db()
        self.assertEqual(company_contact.company_name, "Acme 2")
        self.assertFalse(company_contact.is_deleted)
