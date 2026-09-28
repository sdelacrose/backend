"""LOT 3C: the single atomic action that turns a DRAFT Transmittal into ISSUED evidence.

For OUT this is "issue"; for IN this is "record as received" -- one generic implementation
(materially identical), with the resulting TransmittalEvent naming ISSUED vs RECEIVED so API
semantics stay understandable (LOT 3C section 11).

On any failure, the whole transaction rolls back: nothing is half-frozen.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from project_management.models import ContactCompany, Transmittal, TransmittalEvent
from project_management.services.transmittal_eligibility import (
    build_item_evidence_snapshot,
    validate_item_eligibility,
)


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

    # 4/12. Revalidate every item's eligibility and freeze its evidence.
    for item in items:
        validate_item_eligibility(locked, item.document_revision)
        item.evidence_snapshot = build_item_evidence_snapshot(
            item.document_revision, item.distribution_purpose
        )
        item.save(update_fields=["evidence_snapshot"])

    # 5/6/7. Freeze header evidence, set the timestamp, set status.
    now = timezone.now()
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
