"""L3B: Supplier Surveillance -- on-demand creation only.

The authoritative creation path for a SupplierSurveillance. Deliberately the smallest possible
service: no reviewers, no events, no template instantiation, no PDF, no Action Items, no
notifications (all deferred to later lots, per L3A/L3B). Creation is explicitly ON DEMAND --
never triggered by Document creation, Revision creation or Transmittal reception (L3B section
19); nothing in this module is wired to a signal.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from project_management.models import Document, DocumentOrigin, DocumentRevision
from project_management.models.supplier_surveillance import (
    SupplierSurveillance,
    SupplierSurveillanceReferenceCounter,
)
from project_management.services.document_type_review_duration import (
    resolve_review_durations,
)
from project_management.services.project_codes import propose_unique_project_code
from project_management.services.supplier_surveillance_template import (
    assert_template_usable_for_surveillance,
    build_template_snapshot,
    resolve_company_for_project,
)

SEQUENCE_WIDTH = 5


def resolve_review_duration_snapshot(*, project, document_revision):
    """L3H-2 section 2/3/4/6/9: the frozen (duration_days, due_date) pair for a NEW Supplier
    Surveillance, or (None, None) when no mandatory deadline applies.

    FOR_REVIEW only (section 9) -- a FOR_INFORMATION document's exceptional/manual Surveillance
    never gets a mandatory deadline, preserving "FOR_INFORMATION creates no automatic
    operational obligation" exactly as the L3H architecture checkpoint already established for
    the Register's own derivation. Computed ONCE from the L3H-1 effective
    supplier_review_duration_days (never duplicating its null-fallback-to-15 logic) and
    document_revision.created_at (never SupplierSurveillance.created_at -- the deadline must
    not depend on when a user happens to click "Create Surveillance", section 3).
    """
    document = document_revision.document
    if document.supplier_transmittal_category != Document.SupplierTransmittalCategory.FOR_REVIEW:
        return None, None
    effective = resolve_review_durations(project=project, document_type=document.document_type)
    duration_days = effective["supplier_review_duration_effective_days"]
    due_date = document_revision.created_at.date() + timedelta(days=duration_days)
    return duration_days, due_date


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
        review_duration_days, review_due_date = resolve_review_duration_snapshot(
            project=project, document_revision=locked_revision,
        )
        instance = SupplierSurveillance.objects.create(
            project=project,
            document_revision=locked_revision,
            reference=reference,
            created_by=actor,
            template_snapshot=snapshot,
            # L3H-2: frozen once, here, at creation -- never recomputed later even if the
            # Project/Document Type configuration subsequently changes (section 6/7). NULL/NULL
            # for a FOR_INFORMATION exceptional Surveillance (section 9).
            review_duration_days=review_duration_days,
            review_due_date=review_due_date,
            # status defaults to DRAFT; result/new_revision_required stay NULL -- L3B/L3C never
            # populate them (closure remains L3E).
        )
        # L3D section 21: record creation for every NEW Surveillance from here on -- L3B rows
        # created before this lot are never backfilled.
        from project_management.services.supplier_surveillance_review import record_event
        from project_management.models.supplier_surveillance_review import SupplierSurveillanceEvent

        record_event(instance, SupplierSurveillanceEvent.EventType.SURVEILLANCE_CREATED, actor)
        return instance


def get_supplier_surveillance_for_revision(document_revision):
    """The Surveillance of `document_revision`, or None. Read-only, no side effects."""
    return SupplierSurveillance.objects.filter(document_revision=document_revision).first()


# L3H-2 section 10: all three non-CLOSED statuses remain overdue-eligible -- the contractual/
# internal review process is not yet fully closed in any of them.
OVERDUE_ELIGIBLE_STATUSES = (
    SupplierSurveillance.Status.DRAFT,
    SupplierSurveillance.Status.UNDER_REVIEW,
    SupplierSurveillance.Status.READY_FOR_CLOSURE,
)


def compute_is_overdue(surveillance):
    """L3H-2 section 10/11/12: a DERIVED condition -- never persisted, never folded into
    `status`. False whenever review_due_date is null (historical row or FOR_INFORMATION
    exceptional Surveillance, section 12) or the Surveillance is CLOSED (section 10). A due
    date of exactly today is NOT YET overdue (section 11) -- only strictly in the past is.
    """
    if surveillance.review_due_date is None:
        return False
    if surveillance.status not in OVERDUE_ELIGIBLE_STATUSES:
        return False
    return surveillance.review_due_date < timezone.localdate()
