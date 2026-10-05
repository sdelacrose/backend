from datetime import date

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, Permission, Role, RolePermission, User, UserRole
from project_management.models import (
    ContactCompany,
    ContactUser,
    Correspondence,
    CorrespondenceReferenceCounter,
    Document,
    DocumentOrigin,
    DocumentReferenceCounter,
    Folder,
    Project,
    ProjectTeamMembers,
    SupplierSurveillance,
    Transmittal,
    TransmittalEvent,
    TransmittalItem,
    TransmittalReferenceCounter,
)
from project_management.permissions import TransmittalPermission
from project_management.serializers.transmittal import TransmittalSerializer
from project_management.services.document_workflow import perform_action, start_workflow
from project_management.services.document_revision import add_document_revision
from project_management.services.transmittal_eligibility import validate_item_eligibility
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item, remove_transmittal_item
from project_management.services.transmittal_reference import generate_transmittal_number
from project_management.test_document_revision import NoNotificationsMixin
from project_management.views.transmittal import TransmittalViewSet


def make_user(email, **extra):
    return User.objects.create_user(
        email=email, first_name="T", last_name="U", title="Mr",
        user_type="company_user", **extra,
    )


def grant_transmittal_permissions(user, company, actions=("view", "create", "update", "issue", "delete")):
    """Company-scope grant: only the LIST check (`has_permission` for GET .../transmittal)
    reads company scope -- every object-level action now reads project scope instead, since
    Transmittal is project-only (LOT 3C amendment section 1). See
    `grant_transmittal_project_permissions` for that."""
    role, _ = Role.objects.get_or_create(
        name=f"Transmittal Test Role {user.id}", company=company, type="solution", scope="company",
        defaults={"is_default": False, "is_active": True, "created_by": user},
    )
    for action in actions:
        key = f"documents.transmittal_{action}"
        perm, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": "documents", "description": "test"},
        )
        RolePermission.objects.get_or_create(role=role, permission=perm)
    UserRole.objects.get_or_create(user=user, role=role, defaults={"company": company, "assigned_by": user})


def grant_transmittal_project_permissions(user, project, company, actions):
    """Project-scope grant (ProjectTeamMembers + a project-scope Role), matching the
    established pattern (see test_action_item_requirements.py) -- required for any
    TransmittalPermission.has_object_permission check, since obj.project is always set."""
    role, _ = Role.objects.get_or_create(
        name=f"Transmittal Project Test Role {user.id}", company=company, type="solution", scope="project",
        defaults={"is_default": False, "is_active": True, "created_by": user},
    )
    for action in actions:
        key = f"documents.transmittal_{action}"
        perm, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": "documents", "description": "test"},
        )
        RolePermission.objects.get_or_create(role=role, permission=perm)
    # update_or_create, not get_or_create: Project creation already auto-adds its owner as a
    # ProjectTeamMembers row (with no project_role) -- get_or_create would find that existing
    # row and silently keep its (permission-less) project_role, since defaults only apply on
    # creation.
    ProjectTeamMembers.objects.update_or_create(
        project=project, team_member=user,
        defaults={"role": "Editor", "project_role": role, "project_access": "RW"},
    )
    return role


def make_project(company, user, name="Transmittal Test Project"):
    return Project.objects.create(
        project_name=name, user=user, company_profile=company,
        contract_value=0, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
    )


class TransmittalTestBase(NoNotificationsMixin, TestCase):
    """LOT 3C amendment: every Transmittal is project-scoped. No company/library scope exists."""

    def setUp(self):
        self.silence_notifications()
        self.owner = make_user("trans.owner@links-local.invalid")
        self.approver = make_user("trans.approver@links-local.invalid")
        self.company = CompanyProfile.objects.create(
            admin_user=self.owner, company_name="Transmittal Test Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="t@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.approver.company = self.company
        self.approver.save(update_fields=["company"])
        self.client_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Acme Client", company_role="Client",
            contact_email="acme@x.invalid",
        )
        self.client_contact = ContactUser.objects.create(
            company_profile=self.company, contact_company=self.client_company,
            name="Jane", surname="Doe", email="jane@acme.invalid",
        )
        self.supplier_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Acme Supplier", company_role="Supplier",
            contact_email="supplier@x.invalid",
        )
        self.other_supplier_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Other Supplier", company_role="Supplier",
            contact_email="other.supplier@x.invalid",
        )
        self.orphan_contact = ContactUser.objects.create(
            company_profile=self.company, name="Orphan", surname="Contact", email="orphan@x.invalid",
        )
        self.project = make_project(self.company, self.owner, "Transmittal Test Project")
        self.other_project = make_project(self.company, self.owner, "Transmittal Other Project")
        self.folder = Folder.objects.create(project=self.project, name="Transmittal Project Folder")
        self.other_project_folder = Folder.objects.create(project=self.other_project, name="Other Project Folder")
        self.company_folder = Folder.objects.create(company_profile=self.company, name="Company Library Folder")

        self.other_owner = make_user("trans.other.owner@links-local.invalid")
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_owner, company_name="Other Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="other@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.other_tenant_project = make_project(self.other_company, self.other_owner, "Other Tenant Project")
        self.other_tenant_folder = Folder.objects.create(project=self.other_tenant_project, name="Other Tenant Folder")

    # --- document/revision fixtures -----------------------------------------------

    def make_document(self, folder=None, origin=DocumentOrigin.INTERNAL, name="plan.txt"):
        folder = folder or self.folder
        document = Document(
            folder=folder, file_name=name, created_by=self.owner, document_origin=origin,
        )
        document.file.save(name, SimpleUploadedFile(name, b"content"), save=False)
        document.save()
        return document

    def add_revision(self, document, content=b"v1"):
        return add_document_revision(document, SimpleUploadedFile("rev.txt", content), self.owner)

    def approve(self, revision):
        workflow = start_workflow(
            revision, "TWO_STEP", self.owner, None, self.approver, self.owner,
        )
        perform_action(workflow, self.owner, "SIGN", "author sign")
        perform_action(workflow, self.approver, "APPROVE", "approved")
        revision.refresh_from_db()
        return revision

    def make_approved_internal_revision(self, folder=None, transmittal_category="For review"):
        # LOT 3E-1C1 OUT eligibility amendment: an OUT-eligible revision now also needs its
        # Document classified "For review"/"For information" (transmittal_category), not just
        # internally approved -- default matches every existing caller's actual intent ("make me
        # a revision usable in an OUT transmittal"). Pass transmittal_category=None to build one
        # that is approved but NOT classified, for the new rule-2 rejection tests.
        document = self.make_document(folder=folder, origin=DocumentOrigin.INTERNAL)
        if transmittal_category is not None:
            document.transmittal_category = transmittal_category
            document.save(update_fields=["transmittal_category"])
        revision = self.add_revision(document)
        return self.approve(revision)

    def make_supplier_revision(self, folder=None, category=None, client_category=None):
        document = self.make_document(folder=folder, origin=DocumentOrigin.SUPPLIER, name="supplier.txt")
        if category is not None:
            document.supplier_transmittal_category = category
        if client_category is not None:
            document.transmittal_category = client_category
        if category is not None or client_category is not None:
            document.save(update_fields=["supplier_transmittal_category", "transmittal_category"])
        return self.add_revision(document, content=b"supplier-v1")

    def make_client_revision(self, folder=None):
        document = self.make_document(folder=folder, origin=DocumentOrigin.CLIENT, name="client.txt")
        return self.add_revision(document, content=b"client-v1")

    # L3F.1: the Supplier OUT gate only ever reads SupplierSurveillance.status/.result (never
    # writes) -- a plain ORM row is enough here, the full Reviewer/Closure service machinery
    # (L3B-E, deliberately untouched by this lot) is exercised elsewhere.
    def make_surveillance(self, revision, status, result=None):
        return SupplierSurveillance.objects.create(
            project=self.project,
            document_revision=revision,
            reference=f"SR-TEST-{revision.id}",
            status=status,
            result=result,
            created_by=self.owner,
        )

    # --- transmittal helpers ---------------------------------------------------

    def create_out_transmittal(self, **data):
        payload = {
            "direction": "OUT", "status": "DRAFT", "project": self.project.id,
            "external_company": self.client_company.id,
            **data,
        }
        serializer = TransmittalSerializer(data=payload, context={"user": self.owner})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    # Kept as `create_transmittal` for readability in tests that only care about "some OUT
    # transmittal" (most eligibility/finalization/immutability tests).
    create_transmittal = create_out_transmittal

    def create_in_transmittal(self, transmittal_number="SUP-TR-2026-0001", **data):
        payload = {
            "direction": "IN", "status": "DRAFT", "project": self.project.id,
            "external_company": self.supplier_company.id, "transmittal_number": transmittal_number,
            **data,
        }
        serializer = TransmittalSerializer(data=payload, context={"user": self.owner})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    def update_transmittal(self, instance, **data):
        serializer = TransmittalSerializer(
            Transmittal.objects.get(pk=instance.pk), data=data, partial=True,
            context={"user": self.owner},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()


class TransmittalNumberingTests(TransmittalTestBase):
    """OUT numbering only -- IN never allocates from TransmittalReferenceCounter."""

    def test_out_number_format_uses_project_code(self):
        transmittal = self.create_out_transmittal()
        self.project.refresh_from_db()
        self.assertTrue(self.project.project_code)
        self.assertEqual(transmittal.transmittal_number, f"TR-{self.project.project_code}-00001")

    def test_out_sequence_increments_per_project(self):
        first = self.create_out_transmittal()
        second = self.create_out_transmittal()
        self.assertTrue(first.transmittal_number.endswith("-00001"))
        self.assertTrue(second.transmittal_number.endswith("-00002"))

    def test_out_sequence_independent_across_projects(self):
        self.create_out_transmittal()  # project #1 -> ...00001
        other = self.create_out_transmittal(project=self.other_project.id)
        self.assertTrue(other.transmittal_number.endswith("-00001"))

    def test_out_counter_independent_from_document_counter(self):
        generate_transmittal_number(project=self.project)
        self.assertEqual(DocumentReferenceCounter.objects.count(), 0)

    def test_out_counter_independent_from_correspondence_counter(self):
        generate_transmittal_number(project=self.project)
        self.assertEqual(CorrespondenceReferenceCounter.objects.count(), 0)
        self.assertEqual(Correspondence.objects.count(), 0)

    def test_out_number_is_permanent_via_api(self):
        transmittal = self.create_out_transmittal()
        original = transmittal.transmittal_number
        self.update_transmittal(transmittal, transmittal_number="TR-HACKED-00001")
        transmittal.refresh_from_db()
        self.assertEqual(transmittal.transmittal_number, original)

    def test_out_number_is_permanent_at_model_layer(self):
        transmittal = self.create_out_transmittal()
        transmittal.transmittal_number = "TR-HACKED-00001"
        with self.assertRaises(ValidationError):
            transmittal.save()

    def test_project_is_immutable_after_creation(self):
        transmittal = self.create_out_transmittal()
        locked = Transmittal.objects.get(pk=transmittal.pk)
        locked.project = self.other_project
        with self.assertRaises(ValidationError):
            locked.save()


class TransmittalCreationTests(TransmittalTestBase):
    def test_project_required_for_out(self):
        serializer = TransmittalSerializer(
            data={"direction": "OUT", "status": "DRAFT", "external_company": self.client_company.id},
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("project", serializer.errors)

    def test_project_required_for_in(self):
        serializer = TransmittalSerializer(
            data={
                "direction": "IN", "status": "DRAFT", "external_company": self.supplier_company.id,
                "transmittal_number": "SUP-1",
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("project", serializer.errors)

    def test_out_cannot_set_transmittal_number_manually(self):
        serializer = TransmittalSerializer(
            data={
                "direction": "OUT", "status": "DRAFT", "project": self.project.id,
                "external_company": self.client_company.id, "transmittal_number": "TR-CUSTOM-00001",
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("transmittal_number", serializer.errors)

    def test_in_requires_supplier_reference(self):
        serializer = TransmittalSerializer(
            data={
                "direction": "IN", "status": "DRAFT", "project": self.project.id,
                "external_company": self.supplier_company.id,
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("transmittal_number", serializer.errors)

    def test_in_requires_external_company(self):
        serializer = TransmittalSerializer(
            data={
                "direction": "IN", "status": "DRAFT", "project": self.project.id,
                "transmittal_number": "SUP-1",
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("external_company", serializer.errors)

    def test_in_preserves_supplier_reference_exactly(self):
        transmittal = self.create_in_transmittal(transmittal_number="SIE-TM-2026-0048")
        self.assertEqual(transmittal.transmittal_number, "SIE-TM-2026-0048")

    def test_in_does_not_consume_out_counter(self):
        self.create_in_transmittal()
        self.assertEqual(TransmittalReferenceCounter.objects.count(), 0)
        # A subsequent OUT transmittal in the same project must still start at 00001.
        out = self.create_out_transmittal()
        self.assertTrue(out.transmittal_number.endswith("-00001"))

    def test_cannot_create_directly_as_issued(self):
        for payload in (
            {"direction": "OUT", "status": "ISSUED", "project": self.project.id, "external_company": self.client_company.id},
            {"direction": "IN", "status": "ISSUED", "project": self.project.id, "external_company": self.supplier_company.id, "transmittal_number": "SUP-2"},
        ):
            with self.subTest(direction=payload["direction"]):
                serializer = TransmittalSerializer(data=payload, context={"user": self.owner})
                self.assertFalse(serializer.is_valid())
                self.assertIn("status", serializer.errors)

    def test_draft_mutability_of_header_fields(self):
        transmittal = self.create_out_transmittal(subject="Original")
        updated = self.update_transmittal(transmittal, subject="Updated")
        self.assertEqual(updated.subject, "Updated")

    def test_direction_cannot_change(self):
        transmittal = self.create_out_transmittal()
        locked = Transmittal.objects.get(pk=transmittal.pk)
        locked.direction = "IN"
        with self.assertRaises(ValidationError):
            locked.save()

    def test_external_contact_must_belong_to_external_company(self):
        serializer = TransmittalSerializer(
            data={
                "direction": "OUT", "status": "DRAFT", "project": self.project.id,
                "external_company": self.client_company.id,
                "external_contacts": [self.orphan_contact.id],
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("external_contacts", serializer.errors)

    def test_external_company_must_belong_to_own_tenant(self):
        foreign_company = ContactCompany.objects.create(
            company_profile=self.other_company, company_name="Foreign Client",
            contact_email="foreign@x.invalid",
        )
        serializer = TransmittalSerializer(
            data={
                "direction": "OUT", "status": "DRAFT", "project": self.project.id,
                "external_company": foreign_company.id,
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("external_company", serializer.errors)


class TransmittalInUniquenessTests(TransmittalTestBase):
    """LOT 3C amendment section 5: (project, external Supplier company, supplier reference)
    is the uniqueness key -- never a global unique constraint on the reference alone."""

    def test_duplicate_project_supplier_reference_rejected(self):
        self.create_in_transmittal(transmittal_number="SUP-DUP-1")
        serializer = TransmittalSerializer(
            data={
                "direction": "IN", "status": "DRAFT", "project": self.project.id,
                "external_company": self.supplier_company.id, "transmittal_number": "SUP-DUP-1",
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("transmittal_number", serializer.errors)

    def test_same_reference_allowed_for_another_project(self):
        self.create_in_transmittal(transmittal_number="SUP-DUP-2")
        other = self.create_in_transmittal(transmittal_number="SUP-DUP-2", project=self.other_project.id)
        self.assertEqual(other.transmittal_number, "SUP-DUP-2")

    def test_same_reference_allowed_for_another_supplier(self):
        self.create_in_transmittal(transmittal_number="SUP-DUP-3")
        other = self.create_in_transmittal(
            transmittal_number="SUP-DUP-3", external_company=self.other_supplier_company.id,
        )
        self.assertEqual(other.transmittal_number, "SUP-DUP-3")

    def test_db_constraint_backstops_duplicate_in_reference(self):
        first = self.create_in_transmittal(transmittal_number="SUP-DUP-4")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Transmittal.objects.create(
                    direction="IN", status="DRAFT", project=self.project,
                    external_company=self.supplier_company, transmittal_number="SUP-DUP-4",
                    created_by=self.owner,
                )

    def test_supplier_reference_immutable_after_creation(self):
        transmittal = self.create_in_transmittal(transmittal_number="SUP-IMMUTABLE-1")
        locked = Transmittal.objects.get(pk=transmittal.pk)
        locked.transmittal_number = "SUP-CHANGED"
        with self.assertRaises(ValidationError):
            locked.save()


class TransmittalEligibilityTests(TransmittalTestBase):
    def test_out_internal_approved_revision_accepted(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(item.document_revision_id, revision.id)

    def test_out_rejects_unapproved_internal_revision(self):
        transmittal = self.create_transmittal()
        document = self.make_document(origin=DocumentOrigin.INTERNAL)
        revision = self.add_revision(document)  # never approved: no revision_label
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    # --- LOT 3E-1C1 OUT eligibility amendment: rule 2 (Document.transmittal_category) ---------
    # Canonical field: Document.transmittal_category (free-text, project/company-configured from
    # GC_TRANSMITTAL_CATEGORIES -- "Not sent" / "Available" / "For review" / "For information").
    # Section 22 test matrix A-I.

    def test_out_accepts_approved_for_review_document(self):  # A
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision(transmittal_category="For review")
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(item.document_revision_id, revision.id)

    def test_out_accepts_approved_for_information_document(self):  # B
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision(transmittal_category="For information")
        item = add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        self.assertEqual(item.document_revision_id, revision.id)

    def test_out_rejects_approved_not_sent_document(self):  # C
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision(transmittal_category="Not sent")
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_approved_available_document(self):
        # "Available" is deliberately NOT an eligibility condition -- it is excluded exactly
        # like "Not sent" and unset, never given special treatment.
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision(transmittal_category="Available")
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_approved_unclassified_document(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision(transmittal_category=None)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_unapproved_for_review_document(self):  # D
        transmittal = self.create_transmittal()
        document = self.make_document(origin=DocumentOrigin.INTERNAL)
        document.transmittal_category = "For review"
        document.save(update_fields=["transmittal_category"])
        revision = self.add_revision(document)  # never approved: no revision_label
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_unapproved_for_information_document(self):  # E
        transmittal = self.create_transmittal()
        document = self.make_document(origin=DocumentOrigin.INTERNAL)
        document.transmittal_category = "For information"
        document.save(update_fields=["transmittal_category"])
        revision = self.add_revision(document)  # never approved: no revision_label
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)

    def test_out_rejects_client_origin_document(self):
        transmittal = self.create_transmittal()
        revision = self.make_client_revision()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    # --- L3F.1: Supplier OUT eligibility (section 5/6/8/9/10) -- a SUPPLIER-origin revision is
    # now ALSO eligible for OUT distribution, gated by its own exact-revision SupplierSurveillance
    # (never "any Supplier revision", the original, now-superseded absolute rule this single test
    # used to assert). Section 22 test matrix B.

    def test_out_rejects_supplier_with_no_surveillance_at_all(self):  # B.3
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_supplier_surveillance_draft(self):  # B.4
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(revision, SupplierSurveillance.Status.DRAFT)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_supplier_surveillance_under_review(self):  # B.5
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(revision, SupplierSurveillance.Status.UNDER_REVIEW)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_supplier_surveillance_ready_for_closure(self):  # B.6
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(revision, SupplierSurveillance.Status.READY_FOR_CLOSURE)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_supplier_closed_commented(self):  # B.7
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(
            revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.COMMENTED,
        )
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_supplier_closed_rejected(self):  # B.8
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(
            revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REJECTED,
        )
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_accepts_supplier_closed_reviewed(self):  # B.9
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(
            revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED,
        )
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(item.document_revision_id, revision.id)

    def test_out_accepts_supplier_for_information_without_surveillance(self):  # C.10
        # Established L3B/L3D/L3F semantic: FOR_INFORMATION carries no mandatory Surveillance --
        # the Supplier gate is satisfied without one.
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(
            category="FOR_INFORMATION", client_category="For information",
        )
        item = add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        self.assertEqual(item.document_revision_id, revision.id)

    def test_out_rejects_supplier_reviewed_but_client_purpose_not_sent(self):  # F.14
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="Not sent")
        self.make_surveillance(
            revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED,
        )
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_rejects_supplier_reviewed_but_client_purpose_missing(self):
        transmittal = self.create_transmittal()
        revision = self.make_supplier_revision(category="FOR_REVIEW")  # no client_category
        self.make_surveillance(
            revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED,
        )
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_out_exact_revision_rev_b_under_review_not_eligible_despite_rev_a_reviewed(self):  # D.11
        document = self.make_document(origin=DocumentOrigin.SUPPLIER, name="exact-rev.txt")
        document.supplier_transmittal_category = "FOR_REVIEW"
        document.transmittal_category = "For review"
        document.save(update_fields=["supplier_transmittal_category", "transmittal_category"])
        revision_a = self.add_revision(document, content=b"rev-a")
        self.make_surveillance(
            revision_a, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED,
        )
        revision_b = self.add_revision(document, content=b"rev-b")
        self.make_surveillance(revision_b, SupplierSurveillance.Status.UNDER_REVIEW)

        transmittal = self.create_transmittal()
        # Rev A (its own Surveillance is REVIEWED) remains eligible on its own exact terms.
        item = add_transmittal_item(transmittal, revision_a, "FOR_REVIEW", self.owner)
        self.assertEqual(item.document_revision_id, revision_a.id)
        # Rev B must NOT inherit Rev A's REVIEWED result -- its own Surveillance is UNDER_REVIEW.
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision_b, "FOR_REVIEW", self.owner)

    def test_out_crafted_request_still_rejected_server_side(self):  # section 18
        # Simulates a client bypassing any frontend picker filtering -- the service itself must
        # still reject every non-eligible Supplier state.
        transmittal = self.create_transmittal()
        for status, result in (
            (SupplierSurveillance.Status.UNDER_REVIEW, None),
            (SupplierSurveillance.Status.READY_FOR_CLOSURE, None),
            (SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.COMMENTED),
            (SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REJECTED),
        ):
            revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
            self.make_surveillance(revision, status, result)
            with self.assertRaises(ValidationError):
                add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        client_revision = self.make_client_revision()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, client_revision, "FOR_REVIEW", self.owner)

    def test_in_accepts_supplier_origin_document(self):
        transmittal = self.create_in_transmittal()
        revision = self.make_supplier_revision()
        item = add_transmittal_item(transmittal, revision, None, self.owner)
        self.assertEqual(item.document_revision_id, revision.id)

    def test_in_rejects_internal_origin_document(self):
        transmittal = Transmittal(direction="IN", project=self.project, external_company=self.supplier_company)
        revision = self.make_approved_internal_revision()
        with self.assertRaises(ValidationError):
            validate_item_eligibility(transmittal, revision)

    def test_duplicate_revision_rejected(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)

    def test_mixed_distribution_purposes(self):
        transmittal = self.create_transmittal()
        rev_a = self.make_approved_internal_revision()
        rev_b = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, rev_a, "FOR_REVIEW", self.owner)
        add_transmittal_item(transmittal, rev_b, "FOR_INFORMATION", self.owner)
        purposes = set(transmittal.items.values_list("distribution_purpose", flat=True))
        self.assertEqual(purposes, {"FOR_REVIEW", "FOR_INFORMATION"})

    def test_cross_project_rejected(self):
        transmittal = self.create_transmittal(project=self.project.id)
        revision = self.make_approved_internal_revision(folder=self.other_project_folder)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_company_library_document_rejected(self):
        # Transmittal is project-only now: a company-library document (no project) is never
        # eligible, regardless of origin/approval.
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision(folder=self.company_folder)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_cross_company_tenant_rejected(self):
        transmittal = self.create_transmittal()
        document = Document(
            folder=self.other_tenant_folder, file_name="x.txt", created_by=self.owner,
            document_origin=DocumentOrigin.INTERNAL,
        )
        document.file.save("x.txt", SimpleUploadedFile("x.txt", b"x"), save=False)
        document.save()
        revision = add_document_revision(document, SimpleUploadedFile("x.txt", b"x2"), self.owner)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)


class TransmittalDistributionPurposeTests(TransmittalTestBase):
    """LOT 3C amendment section 7: FOR_REVIEW/FOR_INFORMATION is required for OUT, but
    nullable/not-applicable for IN -- never a forced false value."""

    def test_out_item_requires_distribution_purpose(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, None, self.owner)

    def test_out_item_rejects_invalid_purpose(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "NOT_A_REAL_PURPOSE", self.owner)

    def test_in_item_allows_null_distribution_purpose(self):
        transmittal = self.create_in_transmittal()
        revision = self.make_supplier_revision()
        item = add_transmittal_item(transmittal, revision, None, self.owner)
        self.assertIsNone(item.distribution_purpose)

    def test_in_item_allows_blank_distribution_purpose(self):
        transmittal = self.create_in_transmittal()
        revision = self.make_supplier_revision()
        item = add_transmittal_item(transmittal, revision, "", self.owner)
        self.assertIsNone(item.distribution_purpose)

    def test_in_item_rejects_invalid_purpose_if_given(self):
        transmittal = self.create_in_transmittal()
        revision = self.make_supplier_revision()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "NOT_A_REAL_PURPOSE", self.owner)


class TransmittalItemMutabilityTests(TransmittalTestBase):
    def test_item_removed_while_draft(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        remove_transmittal_item(item, self.owner)
        self.assertFalse(TransmittalItem.objects.filter(pk=item.pk).exists())

    def test_multiple_items(self):
        transmittal = self.create_transmittal()
        for _ in range(3):
            revision = self.make_approved_internal_revision()
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        self.assertEqual(transmittal.items.count(), 3)


class TransmittalFinalizationTests(TransmittalTestBase):
    def test_finalize_requires_at_least_one_item(self):
        transmittal = self.create_transmittal()
        with self.assertRaises(ValidationError):
            finalize_transmittal(transmittal, self.owner)

    def test_finalize_out_success(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        self.assertEqual(finalized.status, "ISSUED")
        self.assertIsNotNone(finalized.issued_at)
        self.assertIsNone(finalized.received_at)

    def test_finalize_in_success(self):
        transmittal = self.create_in_transmittal()
        revision = self.make_supplier_revision()
        add_transmittal_item(transmittal, revision, None, self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        self.assertEqual(finalized.status, "ISSUED")
        self.assertIsNotNone(finalized.received_at)
        self.assertIsNone(finalized.issued_at)
        self.assertEqual(
            TransmittalEvent.objects.filter(transmittal=finalized, event_type="RECEIVED").count(), 1,
        )

    def test_finalize_freezes_item_evidence_snapshot(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertIsNotNone(item.evidence_snapshot)
        self.assertEqual(item.evidence_snapshot["revision"]["revision_label"], revision.revision_label)
        self.assertIsNotNone(item.evidence_snapshot["revision"]["file_sha256"])
        self.assertEqual(item.evidence_snapshot["distribution_purpose"], "FOR_REVIEW")

    def test_finalize_freezes_header_evidence_snapshot(self):
        transmittal = self.create_transmittal(external_contacts=[self.client_contact.id])
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        self.assertEqual(finalized.evidence_snapshot["external_company"]["name"], self.client_company.company_name)
        self.assertEqual(len(finalized.evidence_snapshot["external_contacts"]), 1)

    def test_finalize_creates_issued_event(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)
        self.assertEqual(
            TransmittalEvent.objects.filter(transmittal=transmittal, event_type="ISSUED").count(), 1,
        )

    def test_finalize_revalidates_and_rolls_back(self):
        transmittal = self.create_transmittal()
        good = self.make_approved_internal_revision()
        bad = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, good, "FOR_REVIEW", self.owner)
        add_transmittal_item(transmittal, bad, "FOR_INFORMATION", self.owner)
        # Simulate the (otherwise impossible) case of an item becoming ineligible after being
        # added, to prove finalize re-validates every item and rolls back atomically.
        Document.objects.filter(pk=bad.document_id).update(document_origin=DocumentOrigin.CLIENT)
        with self.assertRaises(ValidationError):
            finalize_transmittal(transmittal, self.owner)
        transmittal.refresh_from_db()
        self.assertEqual(transmittal.status, "DRAFT")
        self.assertIsNone(transmittal.items.get(document_revision=good).evidence_snapshot)
        self.assertEqual(TransmittalEvent.objects.filter(transmittal=transmittal, event_type="ISSUED").count(), 0)

    def test_finalize_requires_valid_external_company(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        Transmittal.objects.filter(pk=transmittal.pk).update(external_company=None)
        transmittal.refresh_from_db()
        with self.assertRaises(ValidationError):
            finalize_transmittal(transmittal, self.owner)

    def test_finalize_does_not_alter_internal_approval_evidence(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        label_before = revision.revision_label
        snapshot_before = revision.approval_snapshot
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)
        revision.refresh_from_db()
        self.assertEqual(revision.revision_label, label_before)
        self.assertEqual(revision.approval_snapshot, snapshot_before)


class TransmittalImmutabilityTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        self.transmittal = self.create_transmittal(external_contacts=[self.client_contact.id])
        self.revision = self.make_approved_internal_revision()
        add_transmittal_item(self.transmittal, self.revision, "FOR_REVIEW", self.owner)
        self.finalized = finalize_transmittal(self.transmittal, self.owner)

    def _rejected_update(self, **data):
        serializer = TransmittalSerializer(
            Transmittal.objects.get(pk=self.finalized.pk), data=data, partial=True,
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        return serializer

    def test_issued_cannot_revert_to_draft(self):
        self._rejected_update(status="DRAFT")

    def test_issued_header_fields_are_frozen(self):
        self._rejected_update(subject="Changed after issue")

    def test_issued_is_immutable_at_the_model_layer_too(self):
        locked = Transmittal.objects.get(pk=self.finalized.pk)
        locked.subject = "Changed via model directly"
        with self.assertRaises(ValidationError):
            locked.save()

    def test_issued_transmittal_cannot_be_deleted(self):
        with self.assertRaises(ValidationError):
            self.finalized.delete()

    def test_draft_transmittal_can_be_deleted(self):
        draft = self.create_transmittal()
        draft.delete()
        self.assertFalse(Transmittal.objects.filter(pk=draft.pk).exists())

    def test_no_item_add_after_finalization(self):
        other_revision = self.make_approved_internal_revision()
        with self.assertRaises(ValidationError):
            add_transmittal_item(self.finalized, other_revision, "FOR_INFORMATION", self.owner)

    def test_no_item_remove_after_finalization(self):
        item = self.finalized.items.get()
        with self.assertRaises(ValidationError):
            remove_transmittal_item(item, self.owner)

    def test_item_model_delete_blocked_after_finalization(self):
        item = self.finalized.items.get()
        with self.assertRaises(ValidationError):
            item.delete()

    def test_external_contacts_cannot_mutate_after_finalization(self):
        with self.assertRaises(ValidationError):
            self.finalized.external_contacts.add(self.orphan_contact)
        with self.assertRaises(ValidationError):
            self.finalized.external_contacts.remove(self.client_contact)
        with self.assertRaises(ValidationError):
            self.finalized.external_contacts.clear()


class TransmittalEventTests(TransmittalTestBase):
    def test_created_event_recorded(self):
        transmittal = self.create_transmittal()
        self.assertEqual(
            TransmittalEvent.objects.filter(transmittal=transmittal, event_type="CREATED").count(), 1,
        )

    def test_item_added_and_removed_events_recorded(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        remove_transmittal_item(item, self.owner)
        self.assertEqual(
            TransmittalEvent.objects.filter(transmittal=transmittal, event_type="ITEM_ADDED").count(), 1,
        )
        self.assertEqual(
            TransmittalEvent.objects.filter(transmittal=transmittal, event_type="ITEM_REMOVED").count(), 1,
        )

    def test_events_are_not_editable_or_deletable(self):
        transmittal = self.create_transmittal()
        event = TransmittalEvent.objects.filter(transmittal=transmittal).get()
        event.metadata = {"tampered": True}
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()


class TransmittalPermissionTests(TransmittalTestBase):
    def call_list(self, user):
        factory = APIRequestFactory()
        request = factory.get("/transmittal")
        force_authenticate(request, user=user)
        view = TransmittalViewSet.as_view({"get": "list"})
        return view(request)

    def test_denied_without_permission(self):
        other_user = make_user("no.perm.transmittal@links-local.invalid")
        other_user.company = self.company
        other_user.save(update_fields=["company"])
        response = self.call_list(other_user)
        self.assertEqual(response.status_code, 403)

    def test_allowed_with_permission(self):
        grant_transmittal_permissions(self.owner, self.company)
        response = self.call_list(self.owner)
        self.assertEqual(response.status_code, 200)

    def test_issue_permission_is_distinct_from_update(self):
        # Object-level checks read PROJECT scope (obj.project is always set now).
        grant_transmittal_project_permissions(
            self.owner, self.project, self.company, actions=("view", "create", "update", "delete"),
        )
        transmittal = self.create_transmittal()
        permission = TransmittalPermission("issue")
        factory = APIRequestFactory()
        request = factory.post(f"/transmittal/{transmittal.id}/issue")
        request.user = self.owner
        self.assertFalse(permission.has_object_permission(request, None, transmittal))
        grant_transmittal_project_permissions(self.owner, self.project, self.company, actions=("issue",))
        self.assertTrue(permission.has_object_permission(request, None, transmittal))


class LegacyFieldAndIsolationTests(TransmittalTestBase):
    def test_legacy_document_fields_untouched(self):
        document = self.make_document()
        # LOT 3E-1C1 OUT eligibility amendment: transmittal_category now gates OUT eligibility
        # (rule 2), so this fixture must use an eligible value to reach finalize -- the
        # assertion below is unchanged: finalize must never rewrite it.
        document.transmittal_category = "For review"
        document.external_review_status = "Legacy Status"
        document.save(update_fields=["transmittal_category", "external_review_status"])
        transmittal = self.create_transmittal()
        revision = self.approve(self.add_revision(document))
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)
        document.refresh_from_db()
        self.assertEqual(document.transmittal_category, "For review")
        self.assertEqual(document.external_review_status, "Legacy Status")

    def test_correspondence_data_and_counters_untouched(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)
        self.assertEqual(Correspondence.objects.count(), 0)
        self.assertEqual(CorrespondenceReferenceCounter.objects.count(), 0)


class TransmittalDetailSerializationTests(TransmittalTestBase):
    """LOT 3E-1B live defect: a live browser hit GET /transmittal/<id> returned 500
    (AttributeError: 'TransmittalSerializer' object has no attribute 'get_project_name'),
    traced to a stale, non-reloaded dev server process -- .data was never actually exercised end
    -to-end by a real test (the frontend's own tests only source-scan for the field
    declaration). This renders the serializer for real, the same way DRF does on every request,
    so a genuine SerializerMethodField/get_<field> name mismatch (unlike the stale-process cause
    here) would fail immediately."""

    def test_transmittal_detail_serializes_without_error_and_exposes_human_readable_fields(self):
        transmittal = self.create_transmittal(external_contacts=[self.client_contact.id])
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)
        transmittal.refresh_from_db()

        data = TransmittalSerializer(transmittal).data  # must not raise AttributeError

        self.assertEqual(data["project_name"], self.project.project_name)
        self.assertEqual(data["external_company_name"], self.client_company.company_name)
        self.assertEqual(
            data["external_contacts_detail"],
            [{"id": self.client_contact.id, "name": "Jane Doe"}],
        )
        item = data["items"][0]
        self.assertEqual(item["document_revision_detail"]["document"]["id"], revision.document_id)
        self.assertEqual(item["document_revision_detail"]["revision"]["id"], revision.id)

    def test_in_transmittal_detail_serializes_without_error(self):
        transmittal = self.create_in_transmittal()
        revision = self.make_supplier_revision()
        add_transmittal_item(transmittal, revision, None, self.owner)
        finalize_transmittal(transmittal, self.owner)
        transmittal.refresh_from_db()

        data = TransmittalSerializer(transmittal).data  # must not raise AttributeError

        self.assertEqual(data["external_company_name"], self.supplier_company.company_name)
        item = data["items"][0]
        self.assertEqual(item["document_revision_detail"]["revision"]["id"], revision.id)
