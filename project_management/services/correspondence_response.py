"""LOT 3B: Correspondence response-status lifecycle.

response_status is communication response tracking ONLY -- never a Livrable external-review
result and never a Transmittal concept (LOT 3A-BIS section 11/12). It is driven by two
events only: (1) initial creation (from response_required + direction), and (2) a response
Correspondence being linked via parent_correspondence. OVERDUE is never written here (see
Correspondence.effective_response_status): it is a read-time derivation.
"""

from django.core.exceptions import ValidationError

ResponseStatus = None  # set lazily below to avoid a circular import at module load time
LifecycleStatus = None


def _status_choices():
    global ResponseStatus
    if ResponseStatus is None:
        from project_management.models import Correspondence

        ResponseStatus = Correspondence.ResponseStatus
    return ResponseStatus


def _lifecycle_choices():
    global LifecycleStatus
    if LifecycleStatus is None:
        from project_management.models import Correspondence

        LifecycleStatus = Correspondence.Status
    return LifecycleStatus


def validate_status_for_direction(direction, status):
    """LOT 3B stabilization section 1: recording an IN means it has already been received --
    a DRAFT IN has no business value and is rejected."""
    Lifecycle = _lifecycle_choices()
    if direction == "IN" and status == Lifecycle.DRAFT:
        raise ValidationError(
            {"status": "An incoming correspondence cannot be a draft: it has already been received."}
        )


def initial_response_status(*, direction, response_required):
    """The response_status a Correspondence gets at creation, before any response is linked."""
    Status = _status_choices()
    if not response_required:
        return Status.NOT_REQUIRED
    if direction == "IN":
        return Status.TO_ANSWER
    return Status.AWAITING_REPLY


def validate_parent_correspondence(parent, child_direction, child_scope):
    """child_scope: a dict of the child's (project_id, company_profile_id, freelancer_id)."""
    if parent is None:
        return
    if parent.direction == child_direction:
        raise ValidationError(
            {
                "parent_correspondence": (
                    "A response must have the opposite direction of the Correspondence it "
                    "answers (IN cannot answer IN, OUT cannot answer OUT)."
                )
            }
        )
    parent_scope = {
        "project_id": parent.project_id,
        "company_profile_id": parent.company_profile_id,
        "freelancer_id": parent.freelancer_id,
    }
    if parent_scope != child_scope:
        raise ValidationError(
            {
                "parent_correspondence": (
                    "A response must belong to the exact same project/company as the "
                    "Correspondence it answers."
                )
            }
        )


def apply_response_link(child):
    """Once `child` (freshly created or updated) has a parent_correspondence, move the
    parent's response_status forward. Never touches the child's own status, never rewrites an
    already-ANSWERED parent, and never applies to a parent that never required a response.
    """
    parent = child.parent_correspondence
    if parent is None:
        return
    Status = _status_choices()
    if not parent.response_required or parent.response_status not in parent.PENDING_STATUSES:
        return

    Lifecycle = _lifecycle_choices()
    if parent.direction == "IN" and child.direction == "OUT":
        # LOT 3B stabilization: a linked OUT response's own lifecycle decides this -- DRAFT is
        # a prepared response (parent IN_PREPARATION), ISSUED actually answers the IN
        # Correspondence (parent ANSWERED). issue_date is a plain business date only and is
        # never consulted here.
        new_status = Status.ANSWERED if child.status == Lifecycle.ISSUED else Status.IN_PREPARATION
    elif parent.direction == "OUT" and child.direction == "IN":
        # An IN is always ISSUED (DRAFT IN is rejected at creation): it unconditionally
        # answers an OUT that required a response.
        new_status = Status.ANSWERED
    else:
        return

    if new_status != parent.response_status:
        parent.response_status = new_status
        parent.save(update_fields=["response_status", "updated_at"])
