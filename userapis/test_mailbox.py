# MAIL-1.1B: MailboxConnection / MailboxProjectAssignment / MailboxUserGrant -- models,
# constraints, and the centralized access-control checks in userapis/mailbox_access.py.
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from models.models import CompanyProfile, Permission, Role, RolePermission, User, UserRole
from project_management.models import Project
from userapis.mailbox_access import (
    can_administer_mailbox,
    can_use_mailbox,
    is_mailbox_assigned_to_project,
)
from userapis.models import MailboxConnection, MailboxProjectAssignment, MailboxUserGrant
from userapis.permissions import MailboxAdminPermission


def _make_company(name="Acme"):
    slug = name.lower().replace(" ", "-")
    admin = User.objects.create(
        email=f"{slug}-admin@example.com", last_name="Admin", user_type="company_user"
    )
    company = CompanyProfile.objects.create(
        admin_user=admin,
        company_name=name,
        company_address="Street 1",
        post_code="00000",
        company_town="Town",
        company_tell_no="123",
        company_mob_no="456",
        company_web_address=f"https://{slug}.example.com",
        company_email_address=f"{slug}@example.com",
        company_tax_reference="tax",
        company_iban="iban",
    )
    admin.company = company
    admin.save(update_fields=["company"])
    return company


def _make_user(email, company=None):
    return User.objects.create(
        email=email, last_name="Test", user_type="company_user", company=company
    )


def _make_project(company):
    # project_start_date/project_end_date: a pre-existing post_save signal on Project
    # (create_default_folders, project_management/signals.py) creates a default Milestone
    # whose due_date is Project.project_end_date -- unrelated to this lot, but left unset it
    # violates that Milestone's own NOT NULL constraint, so both are supplied here.
    return Project.objects.create(
        project_name="P",
        company_profile=company,
        contract_value=0,
        project_start_date=timezone.now().date(),
        project_end_date=timezone.now().date() + timedelta(days=30),
    )


def _grant_mail_admin(user, company):
    role = Role.objects.create(
        name="Mailbox Admin", company=company, type="solution", scope="company",
    )
    permission, _ = Permission.objects.get_or_create(
        key="mail.admin_shared_mailbox",
        defaults={"name": "mail.admin_shared_mailbox", "module": "mail"},
    )
    RolePermission.objects.create(role=role, permission=permission)
    UserRole.objects.create(user=user, role=role, company=company)


class MailboxConnectionModelTests(TestCase):
    def setUp(self):
        self.company = _make_company()
        self.owner = _make_user("owner@example.com", self.company)

    def test_create_individual_mailbox(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="Owner@Example.com",
            company_profile=self.company,
            owner_user=self.owner,
            connected_by=self.owner,
        )
        self.assertEqual(mailbox.normalized_email_address, "owner@example.com")

    def test_create_shared_mailbox(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            connected_by=self.owner,
        )
        self.assertIsNone(mailbox.owner_user_id)

    def test_email_is_normalized_for_uniqueness(self):
        MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="Info@Example.com",
            company_profile=self.company,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MailboxConnection.objects.create(
                    provider=MailboxConnection.Provider.MICROSOFT,
                    connection_type=MailboxConnection.ConnectionType.SHARED,
                    email_address="info@example.com",  # same address, different casing
                    company_profile=self.company,
                )

    def test_unique_connection_per_company_provider_address(self):
        MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        other_company = _make_company("Other")
        # A different company CAN connect the "same" address -- it's a different real mailbox
        # as far as LINKS is concerned (no cross-company assumption either way).
        MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=other_company,
        )
        self.assertEqual(MailboxConnection.objects.count(), 2)

    def test_individual_requires_owner_shared_forbids_owner(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MailboxConnection.objects.create(
                    provider=MailboxConnection.Provider.MICROSOFT,
                    connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
                    email_address="no-owner@example.com",
                    company_profile=self.company,
                    owner_user=None,
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MailboxConnection.objects.create(
                    provider=MailboxConnection.Provider.MICROSOFT,
                    connection_type=MailboxConnection.ConnectionType.SHARED,
                    email_address="has-owner@example.com",
                    company_profile=self.company,
                    owner_user=self.owner,
                )

    def test_connected_by_can_be_replaced_without_losing_relations(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            connected_by=self.owner,
        )
        project = _make_project(self.company)
        assignment = MailboxProjectAssignment.objects.create(mailbox=mailbox, project=project)
        other_user = _make_user("other@example.com", self.company)
        grant = MailboxUserGrant.objects.create(
            mailbox=mailbox, user=other_user, can_send=True
        )

        new_delegate = _make_user("new-delegate@example.com", self.company)
        mailbox.connected_by = new_delegate
        mailbox.save(update_fields=["connected_by"])

        mailbox.refresh_from_db()
        self.assertEqual(mailbox.connected_by_id, new_delegate.id)
        # Assignment and grant, keyed off `mailbox` not `connected_by`, are untouched.
        self.assertTrue(
            MailboxProjectAssignment.objects.filter(pk=assignment.pk, mailbox=mailbox).exists()
        )
        self.assertTrue(
            MailboxUserGrant.objects.filter(pk=grant.pk, mailbox=mailbox, can_send=True).exists()
        )


class MailboxProjectAssignmentTests(TestCase):
    def setUp(self):
        self.company = _make_company()
        self.mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )

    def test_assign_to_multiple_projects_of_the_same_company(self):
        p1 = _make_project(self.company)
        p2 = _make_project(self.company)
        MailboxProjectAssignment.objects.create(mailbox=self.mailbox, project=p1)
        MailboxProjectAssignment.objects.create(mailbox=self.mailbox, project=p2)
        self.assertEqual(self.mailbox.project_assignments.count(), 2)

    def test_cross_company_assignment_is_refused(self):
        other_company = _make_company("Other")
        foreign_project = _make_project(other_company)
        with self.assertRaises(ValidationError):
            MailboxProjectAssignment.objects.create(
                mailbox=self.mailbox, project=foreign_project
            )

    def test_unique_mailbox_project_pair(self):
        project = _make_project(self.company)
        MailboxProjectAssignment.objects.create(mailbox=self.mailbox, project=project)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MailboxProjectAssignment.objects.create(mailbox=self.mailbox, project=project)


class MailboxUserGrantTests(TestCase):
    def setUp(self):
        self.company = _make_company()
        self.mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        self.user = _make_user("grantee@example.com", self.company)

    def test_grant_and_revoke_send_and_read_independently(self):
        grant = MailboxUserGrant.objects.create(
            mailbox=self.mailbox, user=self.user, can_send=True, can_read=False
        )
        self.assertTrue(
            can_use_mailbox(self.user, self.mailbox, for_sending=True)
        )
        self.assertFalse(
            can_use_mailbox(self.user, self.mailbox, for_sending=False)
        )
        grant.can_send = False
        grant.save(update_fields=["can_send"])
        self.assertFalse(can_use_mailbox(self.user, self.mailbox, for_sending=True))

    def test_grant_to_user_outside_the_company_is_refused(self):
        outsider = _make_user("outsider@example.com", _make_company("Other"))
        with self.assertRaises(ValidationError):
            MailboxUserGrant.objects.create(mailbox=self.mailbox, user=outsider, can_read=True)

    def test_unique_mailbox_user_pair(self):
        MailboxUserGrant.objects.create(mailbox=self.mailbox, user=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MailboxUserGrant.objects.create(mailbox=self.mailbox, user=self.user)


class MailboxAccessControlTests(TestCase):
    """Rules 1-9 of section 5, exercised through userapis/mailbox_access.py directly."""

    def setUp(self):
        self.company = _make_company()
        self.other_company = _make_company("Other")

    def test_individual_mailbox_usable_only_by_its_owner(self):
        owner = _make_user("owner@example.com", self.company)
        someone_else = _make_user("else@example.com", self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="owner@example.com",
            company_profile=self.company,
            owner_user=owner,
            connected_by=owner,
        )
        self.assertTrue(can_use_mailbox(owner, mailbox, for_sending=True))
        self.assertTrue(can_use_mailbox(owner, mailbox, for_sending=False))
        self.assertFalse(can_use_mailbox(someone_else, mailbox, for_sending=True))

    def test_shared_mailbox_not_automatically_accessible_to_project_members(self):
        user = _make_user("member@example.com", self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        project = _make_project(self.company)
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=project)
        # No MailboxUserGrant for `user` -- project assignment alone must not grant access.
        self.assertFalse(can_use_mailbox(user, mailbox, for_sending=True, project=project))

    def test_shared_mailbox_requires_project_assignment_for_project_scoped_use(self):
        user = _make_user("member@example.com", self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        MailboxUserGrant.objects.create(mailbox=mailbox, user=user, can_send=True)
        unassigned_project = _make_project(self.company)
        self.assertFalse(
            can_use_mailbox(user, mailbox, for_sending=True, project=unassigned_project)
        )
        self.assertFalse(is_mailbox_assigned_to_project(mailbox, unassigned_project))

    def test_revoked_connection_is_never_usable_even_with_a_grant(self):
        user = _make_user("member@example.com", self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            status=MailboxConnection.Status.REVOKED,
        )
        MailboxUserGrant.objects.create(mailbox=mailbox, user=user, can_send=True, can_read=True)
        self.assertFalse(can_use_mailbox(user, mailbox, for_sending=True))
        self.assertFalse(can_use_mailbox(user, mailbox, for_sending=False))

    def test_isolation_between_companies(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        foreign_user = _make_user("foreign@example.com", self.other_company)
        # No grant exists (and could never validly exist, per
        # MailboxUserGrantTests.test_grant_to_user_outside_the_company_is_refused) -- a foreign
        # user can never use this mailbox.
        self.assertFalse(can_use_mailbox(foreign_user, mailbox, for_sending=True))
        self.assertFalse(can_administer_mailbox(foreign_user, mailbox))

    def test_company_administrator_role_check(self):
        admin_user = _make_user("admin@example.com", self.company)
        plain_user = _make_user("plain@example.com", self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        role = Role.objects.create(
            name="Mailbox Admin", company=self.company, type="solution", scope="company",
        )
        permission, _ = Permission.objects.get_or_create(
            key="mail.admin_shared_mailbox",
            defaults={"name": "mail.admin_shared_mailbox", "module": "mail"},
        )
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=admin_user, role=role, company=self.company)

        self.assertTrue(can_administer_mailbox(admin_user, mailbox))
        self.assertFalse(can_administer_mailbox(plain_user, mailbox))

    def test_mail_admin_permission_is_provisioned_by_migration(self):
        # Section 3: the role-assignment UI (companyprofileapis/serializers/role.py) only ever
        # reads an existing Permission row (Permission.objects.get(key=...), raising
        # DoesNotExist otherwise) -- it never creates one on demand. This confirms migration
        # 0017 actually provisioned the catalog row this lot's permission code depends on,
        # distinct from (and in addition to) the code-level check already covered above.
        self.assertTrue(
            Permission.objects.filter(key="mail.admin_shared_mailbox").exists()
        )

    def test_access_without_any_grant_is_refused(self):
        user = _make_user("nogrant@example.com", self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        self.assertFalse(can_use_mailbox(user, mailbox, for_sending=True))
        self.assertFalse(can_use_mailbox(user, mailbox, for_sending=False))


# MAIL-1.1C1: the management API itself -- userapis/views/mailbox.py.
class MailboxManagementAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = _make_company()
        self.admin = _make_user("admin@example.com", self.company)
        _grant_mail_admin(self.admin, self.company)
        self.plain_user = _make_user("plain@example.com", self.company)

    def test_admin_can_create_a_shared_mailbox_and_it_is_pending_not_active(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/user/mailbox-connections/",
            {"provider": "microsoft", "email_address": "info@example.com", "display_name": "Info"},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["connection_type"], "shared")
        # Section 5: never artificially "active" -- no real OAuth happened.
        self.assertEqual(response.data["status"], "pending")
        mailbox = MailboxConnection.objects.get(pk=response.data["id"])
        self.assertEqual(mailbox.company_profile_id, self.company.id)
        self.assertIsNone(mailbox.owner_user_id)

    def test_create_ignores_sensitive_fields_from_payload(self):
        other_company = _make_company("Other")
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/user/mailbox-connections/",
            {
                "provider": "microsoft",
                "email_address": "info2@example.com",
                "company_profile": other_company.id,  # must be ignored
                "owner_user": self.plain_user.id,  # must be ignored
                "status": "active",  # must be ignored
                "connected_by": self.admin.id,  # must be ignored
            },
        )
        self.assertEqual(response.status_code, 201, response.data)
        mailbox = MailboxConnection.objects.get(pk=response.data["id"])
        self.assertEqual(mailbox.company_profile_id, self.company.id)  # not other_company
        self.assertIsNone(mailbox.owner_user_id)
        self.assertEqual(mailbox.status, "pending")  # not active
        self.assertIsNone(mailbox.connected_by_id)

    def test_create_without_admin_permission_is_refused(self):
        self.client.force_authenticate(self.plain_user)
        response = self.client.post(
            "/api/user/mailbox-connections/",
            {"provider": "microsoft", "email_address": "info@example.com"},
        )
        self.assertEqual(response.status_code, 403)

    def test_cross_company_retrieve_is_refused(self):
        other_company = _make_company("Other")
        other_mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="foreign@example.com",
            company_profile=other_company,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.get(f"/api/user/mailbox-connections/{other_mailbox.id}/")
        self.assertIn(response.status_code, (403, 404))

    def test_update_display_name(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            f"/api/user/mailbox-connections/{mailbox.id}/", {"display_name": "New Name"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.display_name, "New Name")

    def test_patch_ignores_sensitive_fields(self):
        other_company = _make_company("Other")
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            status=MailboxConnection.Status.ACTIVE,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            f"/api/user/mailbox-connections/{mailbox.id}/",
            {
                "display_name": "New Name",
                "status": "revoked",
                "company_profile": other_company.id,
                "owner_user": self.plain_user.id,
                "connected_by": self.plain_user.id,
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.display_name, "New Name")
        self.assertEqual(mailbox.status, MailboxConnection.Status.ACTIVE)  # unchanged
        self.assertEqual(mailbox.company_profile_id, self.company.id)  # unchanged
        self.assertIsNone(mailbox.owner_user_id)  # unchanged
        self.assertIsNone(mailbox.connected_by_id)  # unchanged

    def test_deactivate(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            status=MailboxConnection.Status.ACTIVE,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(f"/api/user/mailbox-connections/{mailbox.id}/deactivate/")
        self.assertEqual(response.status_code, 200, response.data)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, "revoked")

    def test_assign_and_unassign_project(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        project = _make_project(self.company)
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/projects/", {"project": project.id},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            MailboxProjectAssignment.objects.filter(mailbox=mailbox, project=project).exists()
        )
        response = self.client.delete(
            f"/api/user/mailbox-connections/{mailbox.id}/projects/{project.id}/"
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            MailboxProjectAssignment.objects.filter(mailbox=mailbox, project=project).exists()
        )

    def test_assign_project_from_another_company_is_refused(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        foreign_project = _make_project(_make_company("Other"))
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/projects/",
            {"project": foreign_project.id},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            MailboxProjectAssignment.objects.filter(mailbox=mailbox).exists()
        )

    def test_grant_modify_and_revoke(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        grantee = _make_user("grantee@example.com", self.company)
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/grants/",
            {"user": grantee.id, "can_send": True, "can_read": False},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            MailboxUserGrant.objects.get(mailbox=mailbox, user=grantee).can_send
        )

        response = self.client.patch(
            f"/api/user/mailbox-connections/{mailbox.id}/grants/{grantee.id}/",
            {"can_read": True},
        )
        self.assertEqual(response.status_code, 200, response.data)
        grant = MailboxUserGrant.objects.get(mailbox=mailbox, user=grantee)
        self.assertTrue(grant.can_send)  # unchanged
        self.assertTrue(grant.can_read)  # updated

        response = self.client.delete(
            f"/api/user/mailbox-connections/{mailbox.id}/grants/{grantee.id}/"
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            MailboxUserGrant.objects.filter(mailbox=mailbox, user=grantee).exists()
        )

    def test_grant_for_user_outside_company_is_refused(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        outsider = _make_user("outsider@example.com", _make_company("Other"))
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/grants/",
            {"user": outsider.id, "can_send": True},
        )
        self.assertEqual(response.status_code, 400)

    def test_replace_delegate_within_company(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            connected_by=self.admin,
        )
        new_delegate = _make_user("new-delegate@example.com", self.company)
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/replace-delegate/",
            {"connected_by": new_delegate.id},
        )
        self.assertEqual(response.status_code, 200, response.data)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.connected_by_id, new_delegate.id)

    def test_replace_delegate_on_an_active_mailbox_forces_reconsent(self):
        # Security fix: swapping connected_by on a previously ACTIVE mailbox must never leave
        # it looking usable with a delegate who has not themselves completed OAuth.
        project = _make_project(self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            connected_by=self.admin,
            status=MailboxConnection.Status.ACTIVE,
        )
        assignment = MailboxProjectAssignment.objects.create(mailbox=mailbox, project=project)
        grant = MailboxUserGrant.objects.create(
            mailbox=mailbox, user=self.plain_user, can_send=True, can_read=True
        )
        new_delegate = _make_user("new-delegate@example.com", self.company)
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/replace-delegate/",
            {"connected_by": new_delegate.id},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "needs_reconsent")
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.connected_by_id, new_delegate.id)
        self.assertEqual(mailbox.status, MailboxConnection.Status.NEEDS_RECONSENT)
        # Project assignments and grants are completely untouched.
        self.assertTrue(
            MailboxProjectAssignment.objects.filter(pk=assignment.pk, mailbox=mailbox).exists()
        )
        grant.refresh_from_db()
        self.assertTrue(grant.can_send)
        self.assertTrue(grant.can_read)

    def test_replace_delegate_on_a_non_active_mailbox_leaves_status_unchanged(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            status=MailboxConnection.Status.PENDING,
        )
        new_delegate = _make_user("new-delegate@example.com", self.company)
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/replace-delegate/",
            {"connected_by": new_delegate.id},
        )
        self.assertEqual(response.status_code, 200, response.data)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, MailboxConnection.Status.PENDING)

    def test_replace_delegate_with_user_outside_company_is_refused(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        outsider = _make_user("outsider@example.com", _make_company("Other"))
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{mailbox.id}/replace-delegate/",
            {"connected_by": outsider.id},
        )
        self.assertEqual(response.status_code, 400)

    def test_accessible_mailboxes_consultation(self):
        # Individual mailbox owned by plain_user.
        individual = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="plain@example.com",
            company_profile=self.company,
            owner_user=self.plain_user,
            connected_by=self.plain_user,
        )
        # Shared mailbox plain_user has an explicit grant on.
        shared = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            status=MailboxConnection.Status.ACTIVE,
        )
        MailboxUserGrant.objects.create(
            mailbox=shared, user=self.plain_user, can_send=True, can_read=False
        )
        # A different shared mailbox with no grant -- must not appear.
        MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="other@example.com",
            company_profile=self.company,
        )
        # Another company's mailbox -- must never appear regardless of anything else.
        foreign_company = _make_company("Other")
        MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="foreign@example.com",
            company_profile=foreign_company,
        )

        self.client.force_authenticate(self.plain_user)
        response = self.client.get("/api/user/mailboxes/accessible")
        self.assertEqual(response.status_code, 200, response.data)
        ids = {row["id"] for row in response.data}
        self.assertEqual(ids, {individual.id, shared.id})
        shared_row = next(row for row in response.data if row["id"] == shared.id)
        self.assertTrue(shared_row["can_send"])
        self.assertFalse(shared_row["can_read"])
