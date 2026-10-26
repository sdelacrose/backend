"""LOT 3D-3: documents.full_access must override CorrespondencePermission/TransmittalPermission
granular checks, matching the established Documents-adjacent convention (DocumentPermission
etc.) -- targeted tests only, no re-audit of LOT 3B/3C/3D-2 behavior otherwise.
"""

from datetime import date

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, Permission, Role, RolePermission, User, UserRole
from project_management.models import ContactCompany, Project, ProjectTeamMembers, Transmittal
from project_management.permissions import CorrespondencePermission, TransmittalPermission
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.test_document_revision import NoNotificationsMixin
from project_management.views.correspondances_register import CorrespondancesRegisterView


def make_user(email, **extra):
    return User.objects.create_user(
        email=email, first_name="T", last_name="U", title="Mr", user_type="company_user", **extra,
    )


def grant_company_permission(user, company, keys, role_name):
    role, _ = Role.objects.get_or_create(
        name=role_name, company=company, type="solution", scope="company",
        defaults={"is_default": False, "is_active": True, "created_by": user},
    )
    for key in keys:
        perm, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": "documents", "description": "test"},
        )
        RolePermission.objects.get_or_create(role=role, permission=perm)
    UserRole.objects.get_or_create(user=user, role=role, defaults={"company": company, "assigned_by": user})


def grant_project_permission(user, project, company, keys, role_name):
    role, _ = Role.objects.get_or_create(
        name=role_name, company=company, type="solution", scope="project",
        defaults={"is_default": False, "is_active": True, "created_by": user},
    )
    for key in keys:
        perm, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": "documents", "description": "test"},
        )
        RolePermission.objects.get_or_create(role=role, permission=perm)
    ProjectTeamMembers.objects.update_or_create(
        project=project, team_member=user,
        defaults={"role": "Editor", "project_role": role, "project_access": "RW"},
    )


class DocumentsFullAccessTestBase(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.owner = make_user("fullaccess.owner@links-local.invalid")
        self.company = CompanyProfile.objects.create(
            admin_user=self.owner, company_name="Full Access Test Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="fa@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Full Access Test Project", user=self.owner, company_profile=self.company,
            contract_value=0, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
        )
        self.client_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Full Access Client", company_role="Client",
            contact_email="client@fa.invalid",
        )


class CorrespondencePermissionFullAccessTests(DocumentsFullAccessTestBase):
    def test_full_access_grants_correspondence_view_at_company_scope(self):
        grant_company_permission(
            self.owner, self.company, ["documents.full_access"], "FA Company Full Access Role",
        )
        permission = CorrespondencePermission("view")
        self.assertTrue(
            permission._check_action_or_full_access(self.owner, "correspondence_view", "company", {})
        )

    def test_full_access_grants_correspondence_view_at_project_scope(self):
        grant_project_permission(
            self.owner, self.project, self.company, ["documents.full_access"], "FA Project Full Access Role",
        )
        permission = CorrespondencePermission("view")
        self.assertTrue(
            permission._check_action_or_full_access(
                self.owner, "correspondence_view", "project", {"project": self.project},
            )
        )

    def test_granular_correspondence_view_still_works_without_full_access(self):
        grant_company_permission(
            self.owner, self.company, ["documents.correspondence_view"], "FA Granular Role",
        )
        permission = CorrespondencePermission("view")
        self.assertTrue(
            permission._check_action_or_full_access(self.owner, "correspondence_view", "company", {})
        )

    def test_no_permission_denied(self):
        permission = CorrespondencePermission("view")
        self.assertFalse(
            permission._check_action_or_full_access(self.owner, "correspondence_view", "company", {})
        )

    def test_full_access_grants_object_level_permission(self):
        grant_company_permission(
            self.owner, self.company, ["documents.full_access"], "FA Company Object Role",
        )
        serializer = CorrespondenceSerializer(
            data={
                "direction": "OUT", "status": "ISSUED", "subject": "x",
                "recipient_company": self.client_company.id,
            },
            context={"user": self.owner},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        correspondence = serializer.save()
        factory = APIRequestFactory()
        request = factory.get(f"/correspondence/{correspondence.id}")
        request.user = self.owner
        permission = CorrespondencePermission("view")
        self.assertTrue(permission.has_object_permission(request, None, correspondence))


class TransmittalPermissionFullAccessTests(DocumentsFullAccessTestBase):
    def test_full_access_grants_transmittal_view_at_project_scope(self):
        grant_project_permission(
            self.owner, self.project, self.company, ["documents.full_access"], "FA Transmittal Full Access Role",
        )
        permission = TransmittalPermission("view")
        self.assertTrue(
            permission._check_action_or_full_access(
                self.owner, "transmittal_view", "project", {"project": self.project},
            )
        )

    def test_granular_transmittal_view_still_works_without_full_access(self):
        grant_project_permission(
            self.owner, self.project, self.company, ["documents.transmittal_view"], "FA Granular Transmittal Role",
        )
        permission = TransmittalPermission("view")
        self.assertTrue(
            permission._check_action_or_full_access(
                self.owner, "transmittal_view", "project", {"project": self.project},
            )
        )

    def test_no_permission_denied(self):
        permission = TransmittalPermission("view")
        self.assertFalse(
            permission._check_action_or_full_access(
                self.owner, "transmittal_view", "project", {"project": self.project},
            )
        )


class CompanyScopeFullAccessNeverExposesTransmittalTests(DocumentsFullAccessTestBase):
    """LOT 3D-3 requirement 4: Company must never expose Transmittals, even with full_access."""

    def call_register(self, params):
        factory = APIRequestFactory()
        request = factory.get("/correspondances", params)
        force_authenticate(request, user=self.owner)
        return CorrespondancesRegisterView.as_view(permission_classes=[])(request)

    def test_company_full_access_excludes_transmittal_type(self):
        grant_company_permission(
            self.owner, self.company, ["documents.full_access"], "FA Company Full Access Excl Role",
        )
        response = self.call_register({"company_scope": "true", "entity_type": "TRANSMITTAL"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_company_full_access_default_type_never_includes_transmittal(self):
        grant_company_permission(
            self.owner, self.company, ["documents.full_access"], "FA Company Full Access Default Role",
        )
        response = self.call_register({"company_scope": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            all(row["entity_type"] != "TRANSMITTAL" for row in response.data["results"])
        )
