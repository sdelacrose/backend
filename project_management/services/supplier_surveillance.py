"""L3B: Supplier Surveillance -- on-demand creation only.

The authoritative creation path for a SupplierSurveillance. Deliberately the smallest possible
service: no reviewers, no events, no template instantiation, no PDF, no Action Items, no
notifications (all deferred to later lots, per L3A/L3B). Creation is explicitly ON DEMAND --
never triggered by Document creation, Revision creation or Transmittal reception (L3B section
19); nothing in this module is wired to a signal.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models import DocumentOrigin, DocumentRevision
from project_management.models.supplier_surveillance import (
    SupplierSurveillance,
    SupplierSurveillanceReferenceCounter,
)
from project_management.services.project_codes import propose_unique_project_code
from project_management.services.supplier_surveillance_template import (
    assert_template_usable_for_surveillance,
    build_template_snapshot,
    resolve_company_for_project,
)

SEQUENCE_WIDTH = 5


def assert_supplier_origin(document):
    """Only a SUPPLIER-origin document can have a Supplier Surveillance (server-side rule).

    Deliberately does NOT also require supplier_transmittal_category == FOR_REVIEW (L3B
    section 10): FOR_INFORMATION must remain able to receive an exceptional/manual Surveillance
    later -- only the frontend's normal entry point is FOR_REVIEW-only.
    """
    if document.document_origin != DocumentOrigin.SUPPLIER:
        raise ValidationError(
            "Supplier Surveillance is only available for a Supplier-origin document."
        )


def resolve_project_for_surveillance(document):
    """The owning Project of `document`, or None for a Company-library document.

    Supplier Surveillance is Project-only (L3A section 21): SUPPLIER origin is already
    structurally impossible on a Company folder (test_a_company_document_cannot_be_created_as_
    supplier), so this is a defensive check, not the primary enforcement.
    """
    folder = document.folder
    return folder.project if folder else None


@transaction.atomic
def generate_supplier_surveillance_reference(*, project):
    """The next permanent Supplier Surveillance reference for `project`.

    V1 temporary format (5-digit sequence, PER PROJECT), deliberately mirroring
    services.transmittal_reference.generate_transmittal_number -- same reuse of the project's
    own canonical project_code (never a newly-derived identifier), same SEQUENCE_WIDTH:
        SR-<PROJECT_CODE>-00001
    The business numbering format remains explicitly deferred (L3A section 10): isolated here
    so it can change later without touching the model or the creation service.
    """
    if not project.project_code:
        project.project_code = propose_unique_project_code(project)
        project.save(update_fields=["project_code"])

    counter, _ = SupplierSurveillanceReferenceCounter.objects.select_for_update().get_or_create(
        project=project
    )
    counter.last_sequence += 1
    counter.save(update_fields=["last_sequence", "updated_at"])

    reference = f"SR-{project.project_code}-{str(counter.last_sequence).zfill(SEQUENCE_WIDTH)}"

    # Runtime protection on top of the locked counter, mirroring Document's/Transmittal's own
    # post-allocation existence check.
    if SupplierSurveillance.objects.filter(reference=reference).exists():
        raise RuntimeError("Could not allocate a unique Supplier Surveillance reference.")
    return reference


def create_supplier_surveillance(*, document_revision, actor, template):
    """Create the (one and only) Supplier Surveillance of `document_revision`, capturing an
    immutable snapshot of `template` (L3C section 14).

    Atomic: locks the revision row first (mirrors services.document_workflow.start_workflow's
    own lock-then-check pattern) so two concurrent requests for the same revision can never both
    pass the duplicate check and both allocate a reference. The Template itself is read (never
    written) here -- its own rows stay exactly as they were; only a frozen copy is stored.
    """
    document = document_revision.document
    assert_supplier_origin(document)
    project = resolve_project_for_surveillance(document)
    if project is None:
        raise ValidationError(
            "Supplier Surveillance is only available for a Project Document (Livrable)."
        )
    company_id = resolve_company_for_project(project)
    assert_template_usable_for_surveillance(template, company_id=company_id)

    with transaction.atomic():
        locked_revision = DocumentRevision.objects.select_for_update().get(
            pk=document_revision.pk
        )
        if SupplierSurveillance.objects.filter(document_revision=locked_revision).exists():
            raise ValidationError(
                "This Supplier Revision already has a Supplier Surveillance."
            )
        reference = generate_supplier_surveillance_reference(project=project)
        snapshot = build_template_snapshot(template)
        return SupplierSurveillance.objects.create(
            project=project,
            document_revision=locked_revision,
            reference=reference,
            created_by=actor,
            template_snapshot=snapshot,
            # status defaults to DRAFT; result/new_revision_required stay NULL -- L3B/L3C never
            # populate them (closure remains L3E).
        )


def get_supplier_surveillance_for_revision(document_revision):
    """The Surveillance of `document_revision`, or None. Read-only, no side effects."""
    return SupplierSurveillance.objects.filter(document_revision=document_revision).first()
