"""L3D: Supplier Surveillance Reviewers & Execution -- the authoritative service layer.

Nothing here ever reads from or writes to a live SupplierSurveillanceTemplate/Section/Item --
execution is driven exclusively by SupplierSurveillance.template_snapshot (L3C's frozen copy).
Completely separate from DocumentWorkflow (L3A/L3B/L3C section 2): no Author/Verifier/Approver
concept, no shared actor model, no shared audit trail.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from project_management.models import SupplierSurveillance
from project_management.models.supplier_surveillance_review import (
    SupplierSurveillanceEvent,
    SupplierSurveillanceReviewerAssignment,
    SupplierSurveillanceResponse,
)
from project_management.services.document_register import get_allowed_owner_queryset

Status = SupplierSurveillanceReviewerAssignment.Status
EventType = SupplierSurveillanceEvent.EventType
CheckValue = SupplierSurveillanceResponse.CheckValue


def record_event(surveillance, event_type, actor, *, assignment=None, metadata=None):
    return SupplierSurveillanceEvent.objects.create(
        surveillance=surveillance,
        event_type=event_type,
        actor=actor,
        assignment=assignment,
        metadata=metadata or {},
    )


def assert_snapshot_executable(surveillance):
    """L3D section 27: a null-snapshot (L3B legacy) row must never crash -- it simply has
    nothing to execute yet."""
    if not surveillance.template_snapshot:
        raise ValidationError(
            "This surveillance has no template snapshot and cannot be executed."
        )


def snapshot_items(surveillance):
    """Flat list of every Item dict across every Section of the frozen snapshot, or None if
    there is no snapshot at all (L3D section 27)."""
    snapshot = surveillance.template_snapshot
    if not snapshot:
        return None
    return [item for section in snapshot.get("sections", []) for item in section.get("items", [])]


def _item_by_snapshot_id(items, item_snapshot_id):
    for item in items:
        if item.get("id") == item_snapshot_id:
            return item
    return None


def assign_reviewer(*, surveillance, reviewer, actor):
    """L3D section 5/6/8/18/19: eligible internal User, no duplicate, blocked once the
    Surveillance is READY_FOR_CLOSURE."""
    assert_snapshot_executable(surveillance)
    if surveillance.status == SupplierSurveillance.Status.READY_FOR_CLOSURE:
        raise ValidationError(
            "Reviewers cannot be changed once the Surveillance is ready for closure."
        )
    allowed = get_allowed_owner_queryset(actor)
    if not allowed.filter(pk=reviewer.pk).exists():
        raise ValidationError("This user is not eligible to be a Surveillance Reviewer.")
    if SupplierSurveillanceReviewerAssignment.objects.filter(
        surveillance=surveillance, reviewer=reviewer
    ).exists():
        raise ValidationError("This user is already a Reviewer of this Surveillance.")

    with transaction.atomic():
        assignment = SupplierSurveillanceReviewerAssignment.objects.create(
            surveillance=surveillance, reviewer=reviewer, assigned_by=actor,
        )
        record_event(
            surveillance,
            EventType.REVIEWER_ASSIGNED,
            actor,
            assignment=assignment,
            metadata={"reviewer_user_id": reviewer.id},
        )
    return assignment


def remove_reviewer(*, assignment, actor):
    """L3D section 18/19: only while ASSIGNED and untouched, never once work has started,
    never once the Surveillance is READY_FOR_CLOSURE."""
    surveillance = assignment.surveillance
    if surveillance.status == SupplierSurveillance.Status.READY_FOR_CLOSURE:
        raise ValidationError(
            "Reviewers cannot be changed once the Surveillance is ready for closure."
        )
    if assignment.status != Status.ASSIGNED or assignment.responses.exists():
        raise ValidationError(
            "This Reviewer has already started their review and cannot be removed."
        )
    with transaction.atomic():
        reviewer_id = assignment.reviewer_id
        record_event(
            surveillance,
            EventType.REVIEWER_REMOVED,
            actor,
            metadata={"reviewer_user_id": reviewer_id},
        )
        assignment.delete()


def assert_can_respond(assignment, user):
    """L3D section 23/16: strictly the assignment's own reviewer, never another user (an admin
    or documents.update holder included) -- and never once completed (read-only evidence)."""
    if assignment.reviewer_id != user.id:
        raise PermissionDenied("You can only answer your own Surveillance review.")
    if assignment.status == Status.COMPLETED:
        raise ValidationError("This review is completed and its responses are read-only.")


def _clean_answer(item, answer):
    """L3D section 26: validate strictly against the frozen snapshot Item, never the
    client-submitted item_type/metadata. Returns (item_type, check_value, text_value)."""
    item_snapshot_id = item["id"]
    item_type = item.get("type")
    raw_check = answer.get("check_value") or None
    raw_text = answer.get("text_value") or ""

    if item_type == "CHECK":
        if raw_text.strip():
            raise ValidationError(
                f"Item {item_snapshot_id} is a CHECK item and does not accept a text value."
            )
        if raw_check is not None and raw_check not in CheckValue.values:
            raise ValidationError(f"Invalid CHECK value for item {item_snapshot_id}.")
        return item_type, raw_check, ""
    if item_type in ("TEXT", "TEXTAREA"):
        if raw_check is not None:
            raise ValidationError(
                f"Item {item_snapshot_id} is a {item_type} item and does not accept a CHECK value."
            )
        return item_type, None, raw_text
    # A malformed/unknown snapshot item type should never happen (L3C only ever writes
    # CHECK/TEXT/TEXTAREA) -- defensive only.
    raise ValidationError(f"Unsupported item type for item {item_snapshot_id}.")


def save_responses(*, assignment, actor, answers):
    """`answers`: an iterable of {item_snapshot_id, check_value?, text_value?}. Every answer is
    validated against the snapshot before anything is written (L3D section 11/26); one call
    upserts every changed item in one transaction (section 33 -- no per-keystroke writes)."""
    assert_can_respond(assignment, actor)
    surveillance = assignment.surveillance
    items = snapshot_items(surveillance)
    if items is None:
        raise ValidationError(
            "This surveillance has no template snapshot and cannot be executed."
        )

    cleaned = []
    for answer in answers:
        item_snapshot_id = answer.get("item_snapshot_id")
        item = _item_by_snapshot_id(items, item_snapshot_id)
        if item is None:
            raise ValidationError(f"Unknown snapshot item: {item_snapshot_id}.")
        item_type, check_value, text_value = _clean_answer(item, answer)
        cleaned.append((item_snapshot_id, item_type, check_value, text_value))

    if not cleaned:
        return assignment

    with transaction.atomic():
        is_first_response = (
            assignment.status == Status.ASSIGNED and not assignment.responses.exists()
        )
        for item_snapshot_id, item_type, check_value, text_value in cleaned:
            SupplierSurveillanceResponse.objects.update_or_create(
                assignment=assignment,
                item_snapshot_id=item_snapshot_id,
                defaults={
                    "item_type": item_type,
                    "check_value": check_value,
                    "text_value": text_value,
                },
            )
        if is_first_response:
            assignment.status = Status.IN_PROGRESS
            assignment.started_at = timezone.now()
            assignment.save(update_fields=["status", "started_at", "updated_at"])
            record_event(surveillance, EventType.REVIEW_STARTED, actor, assignment=assignment)
        if surveillance.status == SupplierSurveillance.Status.DRAFT:
            surveillance.status = SupplierSurveillance.Status.UNDER_REVIEW
            surveillance.save(update_fields=["status", "updated_at"])
        record_event(
            surveillance,
            EventType.RESPONSE_UPDATED,
            actor,
            assignment=assignment,
            metadata={"item_snapshot_ids": [c[0] for c in cleaned]},
        )
    return assignment


def missing_required_item_labels(assignment):
    """L3D section 14: completion validation reads ONLY the frozen snapshot's own `required`
    flag, never the live Template."""
    items = snapshot_items(assignment.surveillance) or []
    required = [item for item in items if item.get("required")]
    responses = {r.item_snapshot_id: r for r in assignment.responses.all()}
    missing = []
    for item in required:
        response = responses.get(item["id"])
        if item.get("type") == "CHECK":
            if not response or response.check_value not in CheckValue.values:
                missing.append(item.get("label"))
        else:
            if not response or not (response.text_value or "").strip():
                missing.append(item.get("label"))
    return missing


def complete_review(*, assignment, actor):
    """L3D section 15/16/24: explicit action, only by the assigned Reviewer themselves, blocked
    if any required Item is missing a valid answer."""
    if assignment.reviewer_id != actor.id:
        raise PermissionDenied("You can only complete your own Surveillance review.")
    if assignment.status == Status.COMPLETED:
        raise ValidationError("This review is already completed.")
    missing = missing_required_item_labels(assignment)
    if missing:
        raise ValidationError({"required": missing})

    surveillance = assignment.surveillance
    with transaction.atomic():
        assignment.status = Status.COMPLETED
        assignment.completed_at = timezone.now()
        assignment.save(update_fields=["status", "completed_at", "updated_at"])
        record_event(surveillance, EventType.REVIEW_COMPLETED, actor, assignment=assignment)

        # L3D section 17/36: READY_FOR_CLOSURE only once every CURRENT assignment is COMPLETED,
        # and only when there is at least one -- never with zero reviewers.
        assignments = list(surveillance.reviewer_assignments.all())
        all_completed = bool(assignments) and all(a.status == Status.COMPLETED for a in assignments)
        if all_completed and surveillance.status == SupplierSurveillance.Status.UNDER_REVIEW:
            surveillance.status = SupplierSurveillance.Status.READY_FOR_CLOSURE
            surveillance.save(update_fields=["status", "updated_at"])
            record_event(surveillance, EventType.READY_FOR_CLOSURE, actor)
    return assignment


def compute_progress(assignment):
    """L3D section 34: required answered / required total when required Items exist, else all
    answered / all total. A COMPLETED assignment always reports 100."""
    if assignment.status == Status.COMPLETED:
        return 100
    items = snapshot_items(assignment.surveillance) or []
    required = [item for item in items if item.get("required")]
    pool = required if required else items
    if not pool:
        return 0
    responses = {r.item_snapshot_id: r for r in assignment.responses.all()}
    answered = 0
    for item in pool:
        response = responses.get(item["id"])
        if not response:
            continue
        if item.get("type") == "CHECK":
            if response.check_value in CheckValue.values:
                answered += 1
        elif (response.text_value or "").strip():
            answered += 1
    return round(100 * answered / len(pool))
