"""LOT 3C: controlled add/remove of TransmittalItem rows while a Transmittal is still DRAFT.

Both operations lock the parent Transmittal row first, so a concurrent finalize (or a second
add/remove) cannot race past the DRAFT check (LOT 3C section 10/14).
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models import Transmittal, TransmittalEvent, TransmittalItem
from project_management.services.transmittal_eligibility import validate_item_eligibility


@transaction.atomic
def add_transmittal_item(transmittal, document_revision, distribution_purpose, actor):
    locked = Transmittal.objects.select_for_update().get(pk=transmittal.pk)
    if locked.status != Transmittal.Status.DRAFT:
        raise ValidationError("Items can only be added while the transmittal is a draft.")
    if locked.direction == Transmittal.Direction.OUT:
        # Unchanged: FOR_REVIEW/FOR_INFORMATION is the document's distribution purpose, still
        # required for an outgoing item.
        if distribution_purpose not in TransmittalItem.DistributionPurpose.values:
            raise ValidationError(
                {"distribution_purpose": "A valid distribution purpose is required for an outgoing item."}
            )
    else:
        # LOT 3C amendment section 7: FOR_REVIEW/FOR_INFORMATION has no business meaning for an
        # incoming Supplier item -- nullable/not-applicable rather than a forced false value.
        if distribution_purpose and distribution_purpose not in TransmittalItem.DistributionPurpose.values:
            raise ValidationError({"distribution_purpose": "Invalid distribution purpose."})
        distribution_purpose = distribution_purpose or None
    validate_item_eligibility(locked, document_revision)
    if TransmittalItem.objects.filter(
        transmittal=locked, document_revision=document_revision
    ).exists():
        raise ValidationError("This document revision is already in this transmittal.")

    item = TransmittalItem.objects.create(
        transmittal=locked,
        document_revision=document_revision,
        distribution_purpose=distribution_purpose,
    )
    TransmittalEvent.objects.create(
        transmittal=locked, event_type=TransmittalEvent.EventType.ITEM_ADDED, actor=actor,
        metadata={"item_id": item.pk, "document_revision_id": document_revision.pk},
    )
    return item


@transaction.atomic
def remove_transmittal_item(item, actor):
    locked = Transmittal.objects.select_for_update().get(pk=item.transmittal_id)
    if locked.status != Transmittal.Status.DRAFT:
        raise ValidationError("Items can only be removed while the transmittal is a draft.")
    item_id = item.pk
    document_revision_id = item.document_revision_id
    item.delete()
    TransmittalEvent.objects.create(
        transmittal=locked, event_type=TransmittalEvent.EventType.ITEM_REMOVED, actor=actor,
        metadata={"item_id": item_id, "document_revision_id": document_revision_id},
    )
