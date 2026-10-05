"""LOT 3C: the single atomic action that turns a DRAFT Transmittal into ISSUED evidence.

For OUT this is "issue"; for IN this is "record as received" -- one generic implementation
(materially identical), with the resulting TransmittalEvent naming ISSUED vs RECEIVED so API
semantics stay understandable (LOT 3C section 11).

On any failure, the whole transaction rolls back: nothing is half-frozen.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from project_management.models import ContactCompany, Transmittal, TransmittalEvent
from project_management.services.document_type_review_duration import (
    resolve_review_durations,
)
from project_management.services.transmittal_eligibility import (
    build_item_evidence_snapshot,
    validate_item_eligibility,
)


def resolve_client_review_duration_snapshot(*, project, document_revision, issued_at):
    """L3H-3 section 5/6/7/8: the frozen (duration_days, due_date) pair for ONE OUT
    TransmittalItem being issued now, FOR_REVIEW only (section 10/11) -- (None, None) for
    FOR_INFORMATION. Computed from the L3H-1 effective client_review_duration_days (never
    duplicating its null-fallback-to-15 logic) and THIS exact Transmittal's own issued_at
    (never Document/DocumentRevision/TransmittalItem creation dates, section 3) -- plain
    calendar days (section 8).
    """
    document = document_revision.document
    duration_days = resolve_review_durations(
        project=project, document_type=document.document_type,
    )["client_review_duration_effective_days"]
    due_date = issued_at.date() + timedelta(days=duration_days)
    return duration_days, due_date


def _external_snapshot(transmittal):
    company = transmittal.external_company
    return {
        "schema_version": 1,
        "external_company": (
            {"id": company.pk, "name": company.company_name} if company else None
        ),
        "external_contacts": [
            {"id": contact.pk, "name": f"{contact.name} {contact.surname}".strip()}
            for contact in transmittal.external_contacts.all()
        ],
        "subject": transmittal.subject,
        "message": transmittal.message,
    }


@transaction.atomic
def finalize_transmittal(transmittal, actor):
    locked = Transmittal.objects.select_for_update().get(pk=transmittal.pk)
    if locked.status != Transmittal.Status.DRAFT:
        raise ValidationError("This transmittal is already finalized.")

    # 1/2. Revalidate scope and external company/contact relationships.
    if locked.external_company_id is None:
        raise ValidationError({"external_company": "An external company is required."})
    if not ContactCompany.objects.filter(pk=locked.external_company_id).exists():
        raise ValidationError({"external_company": "The external company no longer exists."})
    for contact in locked.external_contacts.all():
        if contact.contact_company_id != locked.external_company_id:
            raise ValidationError(
                {"external_contacts": "A selected contact no longer belongs to the external company."}
            )

    # 3. At least one item.
    items = list(locked.items.select_related("document_revision", "document_revision__document"))
    if not items:
        raise ValidationError("At least one item is required to finalize a transmittal.")

    # Captured once, up front, so it is the EXACT SAME instant used for Transmittal.issued_at
    # below and for every item's L3H-3 Client Review due-date snapshot (section 3/7) -- never
    # two slightly different "now"s for one finalization.
    now = timezone.now()

    # 4/12. Revalidate every item's eligibility and freeze its evidence.
    for item in items:
        validate_item_eligibility(locked, item.document_revision)
        item.evidence_snapshot = build_item_evidence_snapshot(
            item.document_revision, item.distribution_purpose
        )
        update_fields = ["evidence_snapshot"]
        # L3H-3 section 7/10/11: OUT + FOR_REVIEW only -- never for IN, never for
        # FOR_INFORMATION. A DRAFT item that never reaches this point never gets a snapshot.
        if (
            locked.direction == Transmittal.Direction.OUT
            and item.distribution_purpose == item.DistributionPurpose.FOR_REVIEW
        ):
            duration_days, due_date = resolve_client_review_duration_snapshot(
                project=locked.project, document_revision=item.document_revision, issued_at=now,
            )
            item.review_duration_days = duration_days
            item.review_due_date = due_date
            update_fields += ["review_duration_days", "review_due_date"]
        item.save(update_fields=update_fields)

    # 5/6/7. Freeze header evidence, set the timestamp, set status.
    locked.evidence_snapshot = _external_snapshot(locked)
    if locked.direction == Transmittal.Direction.OUT:
        locked.issued_at = now
        event_type = TransmittalEvent.EventType.ISSUED
    else:
        locked.received_at = now
        event_type = TransmittalEvent.EventType.RECEIVED
    locked.status = Transmittal.Status.ISSUED
    locked.save(update_fields=["evidence_snapshot", "issued_at", "received_at", "status"])

    # 8. Immutable finalization event.
    TransmittalEvent.objects.create(transmittal=locked, event_type=event_type, actor=actor)
    return locked
