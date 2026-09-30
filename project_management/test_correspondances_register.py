"""LOT 3D-2: targeted tests for the unified, read-only Correspondances register
(GET /correspondances). No new model/table exists -- this only tests the projection/
union/authorization built on top of the existing Correspondence and Transmittal APIs.
"""

from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, Permission, Role, RolePermission, User, UserRole
from project_management.models import (
    ContactCompany,
    Document,
    DocumentOrigin,
    Folder,
    Project,
    ProjectTeamMembers,
    Transmittal,
)
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.serializers.transmittal import TransmittalSerializer
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import perform_action, start_workflow
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
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
    return role


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
    return role


class CorrespondancesRegisterTestBase(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.owner = make_user("reg.owner@links-local.invalid")
        self.approver = make_user("reg.approver@links-local.invalid")
        self.company = CompanyProfile.objects.create(
            admin_user=self.owner, company_name="Register Test Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="reg@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.approver.company = self.company
        self.approver.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Register Test Project", user=self.owner, company_profile=self.company,
            contract_value=0, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
        )
        self.folder = Folder.objects.create(project=self.project, name="Register Folder")
        self.client_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Register Client", company_role="Client",
            contact_email="client@reg.invalid",
        )
        self.supplier_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Register Supplier", company_role="Supplier",
            contact_email="supplier@reg.invalid",
        )

        self.other_owner = make_user("reg.other.owner@links-local.invalid")
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_owner, company_name="Register Other Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="other.reg@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.other_project = Project.objects.create(
            project_name="Register Other Project", user=self.other_owner, company_profile=self.other_company,
            contract_value=0, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
        )

    # --- fixtures ----------------------------------------------------------------

    # LOT 3D-3D1 pre-flight B: a new IN now requires external_reference -- auto-filled by both
    # helpers below (mirrors CorrespondenceTestBase.create in test_correspondence.py) so every
    # existing IN-creating test in this file keeps working unchanged.
    def create_correspondence(self, **data):
        payload = {
            "direction": "OUT", "status": "ISSUED", "subject": "Correspondence subject",
            "project": self.project.id, "recipient_company": self.client_company.id,
            **data,
        }
        if payload.get("direction") == "IN" and not payload.get("external_reference"):
            payload["external_reference"] = "TEST-EXTERNAL-REF"
        serializer = CorrespondenceSerializer(data=payload, context={"user": self.owner})
        assert serializer.is_valid(), serializer.errors
        return serializer.save()

    def create_company_correspondence(self, **data):
        payload = {
            "direction": "OUT", "status": "ISSUED", "subject": "Company correspondence",
            "recipient_company": self.client_company.id,
            **data,
        }
        if payload.get("direction") == "IN" and not payload.get("external_reference"):
            payload["external_reference"] = "TEST-EXTERNAL-REF"
        serializer = CorrespondenceSerializer(data=payload, context={"user": self.owner})
        assert serializer.is_valid(), serializer.errors
        return serializer.save()

    def make_approved_internal_revision(self, name="plan.txt"):
        document = Document(
            folder=self.folder, file_name=name, created_by=self.owner, document_origin=DocumentOrigin.INTERNAL,
        )
        document.file.save(name, SimpleUploadedFile(name, b"content"), save=False)
        document.save()
        revision = add_document_revision(document, SimpleUploadedFile(name, b"v1"), self.owner)
        workflow = start_workflow(revision, "TWO_STEP", self.owner, None, self.approver, self.owner)
        perform_action(workflow, self.owner, "SIGN", "sign")
        perform_action(workflow, self.approver, "APPROVE", "approve")
        revision.refresh_from_db()
        return revision

    def make_supplier_revision(self, name="supplier.txt"):
        document = Document(
            folder=self.folder, file_name=name, created_by=self.owner, document_origin=DocumentOrigin.SUPPLIER,
        )
        document.file.save(name, SimpleUploadedFile(name, b"content"), save=False)
        document.save()
        return add_document_revision(document, SimpleUploadedFile(name, b"v1"), self.owner)

    def create_transmittal_out(self, purpose="FOR_REVIEW", **data):
        payload = {
            "direction": "OUT", "status": "DRAFT", "project": self.project.id,
            "external_company": self.client_company.id, "subject": "Transmittal out subject",
            **data,
        }
        serializer = TransmittalSerializer(data=payload, context={"user": self.owner})
        assert serializer.is_valid(), serializer.errors
        transmittal = serializer.save()
        revision = self.make_approved_internal_revision(name=f"plan-{transmittal.pk}.txt")
        add_transmittal_item(transmittal, revision, purpose, self.owner)
        return finalize_transmittal(transmittal, self.owner)

    def create_transmittal_in(self, number="SUP-REF-0001", **data):
        payload = {
            "direction": "IN", "status": "DRAFT", "project": self.project.id,
            "external_company": self.supplier_company.id, "transmittal_number": number,
            "subject": "Transmittal in subject",
            **data,
        }
        serializer = TransmittalSerializer(data=payload, context={"user": self.owner})
        assert serializer.is_valid(), serializer.errors
        transmittal = serializer.save()
        revision = self.make_supplier_revision(name=f"supplier-{transmittal.pk}.txt")
        add_transmittal_item(transmittal, revision, None, self.owner)
        return finalize_transmittal(transmittal, self.owner)

    def grant_both(self, user=None, project=None):
        user = user or self.owner
        project = project or self.project
        grant_project_permission(
            user, project, self.company,
            ["documents.correspondence_view", "documents.transmittal_view"],
            f"Register Both Role {user.id}",
        )

    def grant_correspondence_only(self, user=None, project=None):
        user = user or self.owner
        project = project or self.project
        grant_project_permission(
            user, project, self.company, ["documents.correspondence_view"],
            f"Register Corr Role {user.id}",
        )

    def grant_transmittal_only(self, user=None, project=None):
        user = user or self.owner
        project = project or self.project
        grant_project_permission(
            user, project, self.company, ["documents.transmittal_view"],
            f"Register Trans Role {user.id}",
        )

    def call(self, user, params=None):
        factory = APIRequestFactory()
        request = factory.get("/correspondances", params or {})
        force_authenticate(request, user=user)
        # permission_classes=[]: same established test-only pattern as
        # test_correspondence.py/test_transmittal.py -- bypasses the global
        # IsAuthenticated/LicensePermission defaults (LicensePermission queries a
        # package_license table absent in the test DB); this view does its own explicit
        # documents.correspondence_view/transmittal_view authorization regardless.
        return CorrespondancesRegisterView.as_view(permission_classes=[])(request)


class CorrespondancesAuthorizationTests(CorrespondancesRegisterTestBase):
    def test_correspondence_only_permission(self):
        self.create_correspondence()
        self.create_transmittal_out()
        self.grant_correspondence_only()
        response = self.call(self.owner, {"project": self.project.id})
        self.assertEqual(response.status_code, 200)
        types = {row["entity_type"] for row in response.data["results"]}
        self.assertEqual(types, {"CORRESPONDENCE"})

    def test_transmittal_only_permission(self):
        self.create_correspondence()
        self.create_transmittal_out()
        self.grant_transmittal_only()
        response = self.call(self.owner, {"project": self.project.id})
        self.assertEqual(response.status_code, 200)
        types = {row["entity_type"] for row in response.data["results"]}
        self.assertEqual(types, {"TRANSMITTAL"})

    def test_both_permissions(self):
        self.create_correspondence()
        self.create_transmittal_out()
        self.grant_both()
        response = self.call(self.owner, {"project": self.project.id})
        self.assertEqual(response.status_code, 200)
        types = {row["entity_type"] for row in response.data["results"]}
        self.assertEqual(types, {"CORRESPONDENCE", "TRANSMITTAL"})

    def test_neither_permission_denied(self):
        self.create_correspondence()
        response = self.call(self.owner, {"project": self.project.id})
        self.assertEqual(response.status_code, 403)

    def test_company_context_excludes_transmittal_even_with_permission(self):
        self.create_company_correspondence()
        self.grant_both()  # grants at project scope; company-scope check below is separate
        grant_company_permission(
            self.owner, self.company,
            ["documents.correspondence_view", "documents.transmittal_view"],
            f"Register Company Both Role {self.owner.id}",
        )
        response = self.call(self.owner, {"company_scope": "true"})
        self.assertEqual(response.status_code, 200)
        types = {row["entity_type"] for row in response.data["results"]}
        self.assertEqual(types, {"CORRESPONDENCE"})

    def test_company_context_transmittal_type_returns_empty_not_denied(self):
        grant_company_permission(
            self.owner, self.company,
            ["documents.correspondence_view", "documents.transmittal_view"],
            f"Register Company Both Role 2 {self.owner.id}",
        )
        response = self.call(self.owner, {"company_scope": "true", "entity_type": "TRANSMITTAL"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)


class CorrespondancesProjectionTests(CorrespondancesRegisterTestBase):
    def setUp(self):
        super().setUp()
        self.grant_both()

    def _row_for(self, response, entity_type, direction):
        return next(
            r for r in response.data["results"]
            if r["entity_type"] == entity_type and r["direction"] == direction
        )

    def test_entity_type_and_direction_correct(self):
        self.create_correspondence(direction="OUT")
        self.create_transmittal_in()
        response = self.call(self.owner, {"project": self.project.id})
        types_directions = {(r["entity_type"], r["direction"]) for r in response.data["results"]}
        self.assertEqual(types_directions, {("CORRESPONDENCE", "OUT"), ("TRANSMITTAL", "IN")})

    def test_reference_correct(self):
        corr = self.create_correspondence()
        trans = self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id})
        corr_row = self._row_for(response, "CORRESPONDENCE", "OUT")
        trans_row = self._row_for(response, "TRANSMITTAL", "OUT")
        self.assertEqual(corr_row["reference"], corr.reference)
        self.assertEqual(trans_row["reference"], trans.transmittal_number)

    def test_project_projection_correct(self):
        self.create_correspondence()
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "CORRESPONDENCE", "OUT")
        self.assertEqual(row["project"], {"id": self.project.id, "name": self.project.project_name})

    def test_external_company_projection_correct(self):
        self.create_correspondence(direction="OUT")
        self.create_correspondence(direction="IN", sender_company=self.supplier_company.id)
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id})
        corr_out = self._row_for(response, "CORRESPONDENCE", "OUT")
        corr_in = self._row_for(response, "CORRESPONDENCE", "IN")
        trans_out = self._row_for(response, "TRANSMITTAL", "OUT")
        self.assertEqual(corr_out["company"]["id"], self.client_company.id)
        self.assertEqual(corr_in["company"]["id"], self.supplier_company.id)
        self.assertEqual(trans_out["company"]["id"], self.client_company.id)

    def test_business_date_correct(self):
        corr = self.create_correspondence(direction="OUT", issue_date="2026-02-01")
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "CORRESPONDENCE", "OUT")
        # response.data holds native Python objects here (the view was called directly,
        # never rendered through a real HTTP response) -- a real JSON response serializes
        # this the same way DRF always does for a DateTimeField.
        self.assertEqual(row["business_date"].date().isoformat(), "2026-02-01")

    def test_correspondence_response_status_present(self):
        self.create_correspondence(direction="IN", sender_company=self.client_company.id, response_required=True)
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "CORRESPONDENCE", "IN")
        self.assertEqual(row["response_status"], "TO_ANSWER")

    def test_transmittal_response_status_is_null(self):
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "TRANSMITTAL", "OUT")
        self.assertIsNone(row["response_status"])

    def test_transmittal_in_remains_backend_issued(self):
        transmittal = self.create_transmittal_in()
        self.assertEqual(transmittal.status, "ISSUED")  # backend domain model unchanged
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "TRANSMITTAL", "IN")
        self.assertEqual(row["lifecycle_status"], "ISSUED")  # UI-only "Received" is a frontend label

    # --- LOT 3D-3A TER: parent/child, Responsible, Due date projection -------------------

    def test_parent_correspondence_id_projected(self):
        parent = self.create_correspondence(direction="OUT")
        child = self.create_correspondence(
            direction="IN", sender_company=self.client_company.id,
            parent_correspondence=parent.id,
        )
        response = self.call(self.owner, {"project": self.project.id})
        parent_row = next(r for r in response.data["results"] if r["id"] == parent.id and r["entity_type"] == "CORRESPONDENCE")
        child_row = next(r for r in response.data["results"] if r["id"] == child.id and r["entity_type"] == "CORRESPONDENCE")
        self.assertIsNone(parent_row["parent_correspondence_id"])
        self.assertEqual(child_row["parent_correspondence_id"], parent.id)

    def test_transmittal_parent_correspondence_id_is_null(self):
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "TRANSMITTAL", "OUT")
        self.assertIsNone(row["parent_correspondence_id"])

    def test_responsible_projected(self):
        corr = self.create_correspondence(responsible=self.approver.id)
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "CORRESPONDENCE", "OUT")
        self.assertEqual(row["responsible"]["id"], self.approver.id)
        self.assertIn(self.approver.first_name, row["responsible"]["name"])

    def test_responsible_null_when_unset(self):
        self.create_correspondence()
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "CORRESPONDENCE", "OUT")
        self.assertIsNone(row["responsible"])

    def test_response_due_date_projected(self):
        self.create_correspondence(
            direction="IN", sender_company=self.client_company.id,
            response_required=True, response_due_date="2026-03-01",
        )
        response = self.call(self.owner, {"project": self.project.id})
        row = self._row_for(response, "CORRESPONDENCE", "IN")
        self.assertEqual(row["response_due_date"].date().isoformat(), "2026-03-01")


class CorrespondancesFilterTests(CorrespondancesRegisterTestBase):
    def setUp(self):
        super().setUp()
        self.grant_both()

    def test_project_filter(self):
        self.create_correspondence()
        response = self.call(self.owner, {"project": self.project.id})
        self.assertEqual(response.data["count"], 1)

    def test_company_filter(self):
        self.create_correspondence(recipient_company=self.client_company.id)
        self.create_transmittal_in()  # different (supplier) company
        response = self.call(self.owner, {"project": self.project.id, "company": self.client_company.id})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["entity_type"], "CORRESPONDENCE")

    def test_type_filter(self):
        self.create_correspondence()
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id, "entity_type": "TRANSMITTAL"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["entity_type"], "TRANSMITTAL")

    def test_direction_filter(self):
        self.create_correspondence(direction="OUT")
        self.create_correspondence(direction="IN", sender_company=self.client_company.id)
        response = self.call(self.owner, {"project": self.project.id, "direction": "IN"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["direction"], "IN")

    def test_lifecycle_status_filter(self):
        self.create_transmittal_out()  # ISSUED
        # A real DRAFT row (direct model creation -- the serializer forbids creating anything
        # other than DRAFT anyway, but adding items/finalizing would issue it).
        Transmittal.objects.create(
            direction="OUT", status="DRAFT", project=self.project,
            external_company=self.client_company, subject="still draft", created_by=self.owner,
        )
        response = self.call(self.owner, {"project": self.project.id, "status": "ISSUED"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["lifecycle_status"], "ISSUED")

    def test_correspondence_response_status_filter(self):
        self.create_correspondence(direction="IN", sender_company=self.client_company.id, response_required=True)
        self.create_correspondence(direction="OUT")  # NOT_REQUIRED
        response = self.call(self.owner, {"project": self.project.id, "status": "TO_ANSWER"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["response_status"], "TO_ANSWER")

    def test_search_reference(self):
        corr = self.create_correspondence()
        response = self.call(self.owner, {"project": self.project.id, "search": corr.reference})
        self.assertEqual(response.data["count"], 1)

    def test_search_subject(self):
        self.create_correspondence(subject="Unique Searchable Subject Xyz")
        response = self.call(self.owner, {"project": self.project.id, "search": "Searchable Subject"})
        self.assertEqual(response.data["count"], 1)

    # --- LOT 3D-3A TER: PENDING alias, contact, correspondence_type filters --------------

    def test_pending_status_alias_matches_any_open_response_state(self):
        self.create_correspondence(direction="IN", sender_company=self.client_company.id, response_required=True)  # TO_ANSWER
        self.create_correspondence(direction="OUT")  # NOT_REQUIRED
        response = self.call(self.owner, {"project": self.project.id, "status": "PENDING"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["response_status"], "TO_ANSWER")

    def test_pending_status_alias_excludes_overdue(self):
        self.create_correspondence(
            direction="IN", sender_company=self.client_company.id,
            response_required=True, response_due_date="2020-01-01",  # elapsed -> OVERDUE
        )
        response = self.call(self.owner, {"project": self.project.id, "status": "PENDING"})
        self.assertEqual(response.data["count"], 0)
        overdue_response = self.call(self.owner, {"project": self.project.id, "status": "OVERDUE"})
        self.assertEqual(overdue_response.data["count"], 1)

    def test_pending_status_alias_never_matches_transmittal(self):
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id, "status": "PENDING"})
        self.assertEqual(response.data["count"], 0)

    def test_contact_filter(self):
        contact = ContactCompany.objects.create(
            company_profile=self.company, company_name="Contact Filter Co", company_role="Client",
            contact_email="contact.filter@reg.invalid",
        )
        from project_management.models import ContactUser
        contact_user = ContactUser.objects.create(
            contact_company=self.client_company, name="Jane", surname="Doe", email="jane@reg.invalid",
        )
        self.create_correspondence(recipient_company=self.client_company.id, recipient_contact=contact_user.id)
        self.create_correspondence(recipient_company=self.client_company.id)  # no contact
        response = self.call(self.owner, {"project": self.project.id, "contact": contact_user.id})
        self.assertEqual(response.data["count"], 1)

    def test_contact_filter_excludes_transmittal(self):
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id, "contact": 999999})
        self.assertEqual(response.data["count"], 0)

    def test_correspondence_type_filter(self):
        letter = self.create_correspondence(correspondence_type="Letter")
        self.create_correspondence(correspondence_type="Notice")
        response = self.call(self.owner, {"project": self.project.id, "correspondence_type": "Letter"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["reference"], letter.reference)

    def test_correspondence_type_filter_excludes_transmittal(self):
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id, "correspondence_type": "Letter"})
        self.assertEqual(response.data["count"], 0)


class CorrespondancesPaginationOrderingTests(CorrespondancesRegisterTestBase):
    def setUp(self):
        super().setUp()
        self.grant_both()

    def test_correct_combined_count(self):
        self.create_correspondence()
        self.create_correspondence(direction="IN", sender_company=self.client_company.id)
        self.create_transmittal_out()
        self.create_transmittal_in()
        response = self.call(self.owner, {"project": self.project.id})
        self.assertEqual(response.data["count"], 4)
        self.assertEqual(len(response.data["results"]), 4)

    def test_global_ordering_by_reference(self):
        self.create_transmittal_out()  # TR-...
        self.create_correspondence()  # COR-...
        response = self.call(self.owner, {"project": self.project.id, "ordering": "reference"})
        refs = [r["reference"] for r in response.data["results"]]
        self.assertEqual(refs, sorted(refs))

    def test_pagination_across_mixed_entity_types(self):
        self.create_correspondence()
        self.create_correspondence(direction="IN", sender_company=self.client_company.id)
        self.create_transmittal_out()
        self.create_transmittal_in()
        response = self.call(self.owner, {"project": self.project.id, "page_size": 2, "page": 1})
        self.assertEqual(response.data["count"], 4)
        self.assertEqual(len(response.data["results"]), 2)
        page2 = self.call(self.owner, {"project": self.project.id, "page_size": 2, "page": 2})
        self.assertEqual(len(page2.data["results"]), 2)
        ids_page1 = {(r["entity_type"], r["id"]) for r in response.data["results"]}
        ids_page2 = {(r["entity_type"], r["id"]) for r in page2.data["results"]}
        self.assertEqual(ids_page1 & ids_page2, set())  # no overlap across pages

    def test_no_duplicate_rows(self):
        self.create_correspondence()
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id})
        keys = [(r["entity_type"], r["id"]) for r in response.data["results"]]
        self.assertEqual(len(keys), len(set(keys)))


class CorrespondancesIsolationTests(CorrespondancesRegisterTestBase):
    def setUp(self):
        super().setUp()
        self.grant_both()

    def test_no_cross_tenant_leakage(self):
        self.create_correspondence()
        self.create_transmittal_out()
        other_user = make_user("reg.cross.tenant@links-local.invalid")
        other_user.company = self.other_company
        other_user.save(update_fields=["company"])
        grant_project_permission(
            other_user, self.other_project, self.other_company,
            ["documents.correspondence_view", "documents.transmittal_view"],
            f"Register Other Role {other_user.id}",
        )
        response = self.call(other_user, {"project": self.other_project.id})
        self.assertEqual(response.data["count"], 0)

    def test_legacy_document_fields_untouched(self):
        transmittal = self.create_transmittal_out()
        document = transmittal.items.get().document_revision.document
        document.transmittal_category = "Legacy Value"
        document.external_review_status = "Legacy Status"
        document.save(update_fields=["transmittal_category", "external_review_status"])
        self.call(self.owner, {"project": self.project.id})
        document.refresh_from_db()
        self.assertEqual(document.transmittal_category, "Legacy Value")
        self.assertEqual(document.external_review_status, "Legacy Status")
