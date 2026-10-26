from datetime import date, timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, Permission, Role, RolePermission, User, UserRole
from project_management.models import (
    ContactCompany,
    ContactUser,
    Correspondence,
    CorrespondenceReferenceCounter,
    Document,
    Folder,
    Project,
)
from project_management.permissions import CorrespondencePermission
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.services.correspondence_reference import (
    generate_correspondence_reference,
)
from project_management.views.correspondence import CorrespondenceAttachmentViewSet, CorrespondenceViewSet
from project_management.test_document_revision import NoNotificationsMixin


def make_user(email, **extra):
    return User.objects.create_user(
        email=email, first_name="T", last_name="U", title="Mr",
        user_type="company_user", **extra,
    )


def grant_correspondence_permissions(user, company, actions=("view", "create", "update", "delete")):
    role, _ = Role.objects.get_or_create(
        name=f"Correspondence Test Role {user.id}", company=company, type="solution", scope="company",
        defaults={"is_default": False, "is_active": True, "created_by": user},
    )
    for action in actions:
        key = f"documents.correspondence_{action}"
        perm, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": "documents", "description": "test"},
        )
        RolePermission.objects.get_or_create(role=role, permission=perm)
    UserRole.objects.get_or_create(user=user, role=role, defaults={"company": company, "assigned_by": user})
    return role


class CorrespondenceTestBase(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.owner = make_user("corr.owner@links-local.invalid")
        self.company = CompanyProfile.objects.create(
            admin_user=self.owner, company_name="Correspondence Test Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="c@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.client_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Acme Client", company_role="Client",
            contact_email="acme@x.invalid",
        )
        self.client_contact = ContactUser.objects.create(
            company_profile=self.company, contact_company=self.client_company,
            name="Jane", surname="Doe", email="jane@acme.invalid",
        )
        self.orphan_contact = ContactUser.objects.create(
            company_profile=self.company, name="Orphan", surname="Contact", email="orphan@x.invalid",
        )
        self.project = Project.objects.create(
            project_name="Correspondence Test Project", user=self.owner, company_profile=self.company,
            contract_value=0, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
        )

    def create(self, **data):
        payload = {
            "direction": "OUT",
            "subject": "Test subject",
            "recipient_company": self.client_company.id,
            **data,
        }
        # LOT 3D-3D1 pre-flight B: a new IN now requires external_reference -- auto-filled here
        # so every existing IN-creating test (not itself testing that requirement) keeps working
        # unchanged; a test that wants to test the requirement passes external_reference itself
        # (including explicitly blank/omitted, via a raw serializer call, not this helper).
        if payload.get("direction") == "IN" and not payload.get("external_reference"):
            payload["external_reference"] = "TEST-EXTERNAL-REF"
        serializer = CorrespondenceSerializer(data=payload, context={"user": self.owner})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    def update(self, instance, **data):
        serializer = CorrespondenceSerializer(
            Correspondence.objects.get(pk=instance.pk), data=data, partial=True,
            context={"user": self.owner},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()


class CorrespondenceCreationTests(CorrespondenceTestBase):
    def test_in_create(self):
        # LOT 3D-3C0C section 6: an IN correspondence never gets an internally generated
        # reference -- it stays blank (the model's own default) unless/until the sender's own
        # external_reference is entered manually.
        correspondence = self.create(direction="IN", sender_company=self.client_company.id)
        self.assertEqual(correspondence.direction, "IN")
        self.assertEqual(correspondence.reference, "")

    def test_in_create_with_manual_external_reference(self):
        correspondence = self.create(
            direction="IN", sender_company=self.client_company.id,
            external_reference="ACME-LETTER-042",
        )
        self.assertEqual(correspondence.reference, "")
        self.assertEqual(correspondence.external_reference, "ACME-LETTER-042")
        self.assertEqual(correspondence.display_reference, "ACME-LETTER-042")

    def test_in_create_does_not_consume_a_reference_sequence(self):
        # The counter is untouched by an IN create -- the very next OUT still allocates 00001,
        # not 00002, proving IN never silently consumed a sequence number.
        self.create(direction="IN", sender_company=self.client_company.id)
        out_correspondence = self.create(direction="OUT")
        self.assertTrue(out_correspondence.reference.endswith("-COR-00001"))

    def test_out_create(self):
        correspondence = self.create(direction="OUT")
        self.assertEqual(correspondence.direction, "OUT")

    def test_company_scope_default(self):
        correspondence = self.create()
        self.assertIsNone(correspondence.project_id)
        self.assertEqual(correspondence.company_profile_id, self.company.id)
        self.assertIsNone(correspondence.freelancer_id)

    def test_project_scope(self):
        correspondence = self.create(project=self.project.id)
        self.assertEqual(correspondence.project_id, self.project.id)
        self.assertIsNone(correspondence.company_profile_id)

    def test_contact_must_belong_to_selected_company(self):
        serializer = CorrespondenceSerializer(
            data={
                "direction": "OUT", "subject": "x",
                "recipient_company": self.client_company.id,
                "recipient_contact": self.orphan_contact.id,  # belongs to no company
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("recipient_contact", serializer.errors)

    def test_uses_canonical_contact_company(self):
        correspondence = self.create(sender_contact=None)
        self.assertIsInstance(correspondence.recipient_company, ContactCompany)


class CorrespondenceReferenceTests(CorrespondenceTestBase):
    def test_reference_is_permanent(self):
        correspondence = self.create()
        original = correspondence.reference
        self.update(correspondence, subject="Changed subject")
        self.assertEqual(Correspondence.objects.get(pk=correspondence.pk).reference, original)

    def test_direction_cannot_change(self):
        correspondence = self.create(direction="OUT")
        with self.assertRaises(Exception):
            self.update(correspondence, direction="IN")

    def test_reference_cannot_be_forced_by_client(self):
        correspondence = self.create()
        self.update(correspondence, reference="HACKED-00001")
        self.assertNotEqual(Correspondence.objects.get(pk=correspondence.pk).reference, "HACKED-00001")

    def test_company_reference_format(self):
        # LOT 3D-3C0 section 9: aligned with the Project Document reference's own convention
        # (owner code leads, COR fills the type-code slot, sequence last) -- was
        # COR-<code>-00001 (COR leading) under the original LOT 3B format.
        correspondence = self.create()
        self.assertEqual(correspondence.reference, f"{self.company.company_code}-COR-00001")

    def test_project_reference_format(self):
        correspondence = self.create(project=self.project.id)
        self.project.refresh_from_db()
        self.assertEqual(correspondence.reference, f"{self.project.project_code}-COR-00001")

    def test_sequence_independent_from_document_counter(self):
        folder = Folder.objects.create(company_profile=self.company, name="Corr Test Folder")
        document = Document(folder=folder, file_name="d.txt", created_by=self.owner)
        document.file.save("d.txt", SimpleUploadedFile("d.txt", b"x"), save=False)
        document.save()
        before = CorrespondenceReferenceCounter.objects.filter(company_profile=self.company).count()
        self.create()
        self.create()
        self.assertEqual(
            CorrespondenceReferenceCounter.objects.filter(company_profile=self.company).count(), before + 1
        )
        # Document's own reference is untouched by correspondence numbering.
        self.assertFalse(document.full_document_number)

    def test_sequence_increments(self):
        first = self.create()
        second = self.create()
        self.assertNotEqual(first.reference, second.reference)
        self.assertTrue(second.reference.endswith("00002"))


class CorrespondenceResponseStatusTests(CorrespondenceTestBase):
    def test_response_not_required(self):
        correspondence = self.create(response_required=False)
        self.assertEqual(correspondence.response_status, Correspondence.ResponseStatus.NOT_REQUIRED)

    def test_in_required_starts_to_answer(self):
        correspondence = self.create(direction="IN", sender_company=self.client_company.id, response_required=True)
        self.assertEqual(correspondence.response_status, Correspondence.ResponseStatus.TO_ANSWER)

    def test_out_required_starts_awaiting_reply(self):
        correspondence = self.create(direction="OUT", response_required=True)
        self.assertEqual(correspondence.response_status, Correspondence.ResponseStatus.AWAITING_REPLY)

    def test_overdue_is_derived_not_persisted(self):
        correspondence = self.create(
            direction="OUT", response_required=True,
            response_due_date=date.today() - timedelta(days=1),
        )
        self.assertEqual(correspondence.response_status, Correspondence.ResponseStatus.AWAITING_REPLY)
        self.assertEqual(correspondence.effective_response_status, Correspondence.ResponseStatus.OVERDUE)
        # Never written to the DB by the mere passage of time.
        self.assertEqual(
            Correspondence.objects.get(pk=correspondence.pk).response_status,
            Correspondence.ResponseStatus.AWAITING_REPLY,
        )

    def test_not_overdue_once_answered(self):
        correspondence = self.create(direction="IN", sender_company=self.client_company.id, response_required=True)
        correspondence.response_status = Correspondence.ResponseStatus.ANSWERED
        correspondence.response_due_date = date.today() - timedelta(days=5)
        correspondence.save(update_fields=["response_status", "response_due_date"])
        self.assertFalse(correspondence.is_overdue)


class CorrespondenceResponseLinkingTests(CorrespondenceTestBase):
    def test_in_answered_by_issued_out_response(self):
        incoming = self.create(direction="IN", sender_company=self.client_company.id, response_required=True)
        outgoing = self.create(
            direction="OUT", parent_correspondence=incoming.id, status=Correspondence.Status.ISSUED,
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.response_status, Correspondence.ResponseStatus.ANSWERED)
        self.assertEqual(outgoing.parent_correspondence_id, incoming.id)

    def test_in_preparation_for_draft_response(self):
        incoming = self.create(direction="IN", sender_company=self.client_company.id, response_required=True)
        self.create(
            direction="OUT", parent_correspondence=incoming.id, status=Correspondence.Status.DRAFT,
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

    def test_in_preparation_finalizes_to_answered_once_issued(self):
        incoming = self.create(direction="IN", sender_company=self.client_company.id, response_required=True)
        draft = self.create(
            direction="OUT", parent_correspondence=incoming.id, status=Correspondence.Status.DRAFT,
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.response_status, Correspondence.ResponseStatus.IN_PREPARATION)
        self.update(draft, status=Correspondence.Status.ISSUED)
        incoming.refresh_from_db()
        self.assertEqual(incoming.response_status, Correspondence.ResponseStatus.ANSWERED)

    def test_issue_date_alone_does_not_answer_the_parent(self):
        """LOT 3B stabilization: issue_date is a plain business date only -- only `status`
        (DRAFT/ISSUED) drives the response lifecycle now."""
        incoming = self.create(direction="IN", sender_company=self.client_company.id, response_required=True)
        draft = self.create(
            direction="OUT", parent_correspondence=incoming.id, status=Correspondence.Status.DRAFT,
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.response_status, Correspondence.ResponseStatus.IN_PREPARATION)
        # Setting issue_date alone (no status change) must NOT answer the parent.
        self.update(draft, issue_date=date.today())
        incoming.refresh_from_db()
        self.assertEqual(incoming.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

    def test_issued_cannot_revert_to_draft(self):
        correspondence = self.create(direction="OUT", status=Correspondence.Status.ISSUED)
        with self.assertRaises(Exception):
            self.update(correspondence, status=Correspondence.Status.DRAFT)

    def test_in_cannot_be_draft(self):
        with self.assertRaises(Exception):
            self.create(direction="IN", sender_company=self.client_company.id, status=Correspondence.Status.DRAFT)

    def test_out_answered_by_in_response(self):
        outgoing = self.create(direction="OUT", response_required=True)
        self.create(direction="IN", sender_company=self.client_company.id, parent_correspondence=outgoing.id)
        outgoing.refresh_from_db()
        self.assertEqual(outgoing.response_status, Correspondence.ResponseStatus.ANSWERED)

    def test_in_to_in_rejected(self):
        first = self.create(direction="IN", sender_company=self.client_company.id, response_required=True)
        with self.assertRaises(Exception):
            self.create(direction="IN", sender_company=self.client_company.id, parent_correspondence=first.id)

    def test_out_to_out_rejected(self):
        first = self.create(direction="OUT", response_required=True)
        with self.assertRaises(Exception):
            self.create(direction="OUT", parent_correspondence=first.id)

    def test_cross_project_parent_rejected(self):
        other_project = Project.objects.create(
            project_name="Other Project", user=self.owner, company_profile=self.company,
            contract_value=0, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
        )
        parent = self.create(direction="IN", sender_company=self.client_company.id, project=self.project.id)
        with self.assertRaises(Exception):
            self.create(direction="OUT", project=other_project.id, parent_correspondence=parent.id)


class CorrespondenceAttachmentTests(CorrespondenceTestBase):
    def test_multiple_attachments(self):
        correspondence = self.create()
        factory = APIRequestFactory()
        view = CorrespondenceAttachmentViewSet.as_view({"post": "create"}, permission_classes=[])
        for name in ("a.pdf", "b.pdf"):
            request = factory.post(
                "/x/", {"file": SimpleUploadedFile(name, b"content")}, format="multipart"
            )
            force_authenticate(request, user=self.owner)
            with patch.object(CorrespondencePermission, "has_object_permission", return_value=True):
                response = view(request, correspondence_id=correspondence.id)
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(correspondence.attachments.count(), 2)

    def test_attachment_denied_for_unrelated_correspondence_without_permission(self):
        correspondence = self.create()
        factory = APIRequestFactory()
        view = CorrespondenceAttachmentViewSet.as_view({"post": "create"}, permission_classes=[])
        request = factory.post(
            "/x/", {"file": SimpleUploadedFile("a.pdf", b"content")}, format="multipart"
        )
        force_authenticate(request, user=self.owner)
        # DRF's dispatch() converts a raised PermissionDenied into a 403 response rather than
        # letting it propagate out of view(...): assert on the response, not on the exception.
        response = view(request, correspondence_id=correspondence.id)
        self.assertEqual(response.status_code, 403)


class CorrespondencePermissionTests(CorrespondenceTestBase):
    def call_list(self, user):
        factory = APIRequestFactory()
        request = factory.get("/correspondence")
        force_authenticate(request, user=user)
        view = CorrespondenceViewSet.as_view({"get": "list"})
        return view(request)

    def test_denied_without_permission(self):
        other_user = make_user("no.perm@links-local.invalid")
        other_user.company = self.company
        other_user.save(update_fields=["company"])
        response = self.call_list(other_user)
        self.assertEqual(response.status_code, 403)

    def test_allowed_with_permission(self):
        grant_correspondence_permissions(self.owner, self.company)
        response = self.call_list(self.owner)
        self.assertEqual(response.status_code, 200)


class CorrespondenceTypeTests(CorrespondenceTestBase):
    """LOT 3B stabilization section 2: configurable Correspondence Type reference data."""

    def test_default_types_available_with_empty_stored_config(self):
        from project_management.serializers.time_expense import get_valid_choices

        choices = get_valid_choices("correspondence_types", self.company.id, None, is_company=True)
        self.assertIn("Letter", choices)
        self.assertIn("Email", choices)

    def test_configured_type_accepted(self):
        correspondence = self.create(correspondence_type="Letter")
        self.assertEqual(correspondence.correspondence_type, "Letter")

    def test_invalid_type_rejected(self):
        serializer = CorrespondenceSerializer(
            data={
                "direction": "OUT", "subject": "x",
                "recipient_company": self.client_company.id,
                "correspondence_type": "Not A Real Type",
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("correspondence_type", serializer.errors)

    def test_configured_type_from_company_config_accepted(self):
        from models.models import CompanyConfig

        CompanyConfig.objects.update_or_create(
            company_profile=self.company, solution="Energy Consultant Solution",
            defaults={"correspondence_types": ["Letter", "Custom Type"]},
        )
        correspondence = self.create(correspondence_type="Custom Type")
        self.assertEqual(correspondence.correspondence_type, "Custom Type")


class LegacyDocumentFieldTests(CorrespondenceTestBase):
    def test_legacy_document_fields_untouched(self):
        folder = Folder.objects.create(company_profile=self.company, name="Legacy Field Folder")
        document = Document(folder=folder, file_name="legacy.txt", created_by=self.owner)
        document.file.save("legacy.txt", SimpleUploadedFile("legacy.txt", b"x"), save=False)
        document.save()
        self.create(direction="OUT")
        document.refresh_from_db()
        self.assertIsNone(document.transmittal_category)
        self.assertIsNone(document.external_review_status)
