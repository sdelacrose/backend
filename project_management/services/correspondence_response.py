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


# CORR-STATUS-01: an OUT Correspondence that requires a reply has NO response status until it has
# actually been sent/issued. The column is a non-nullable CharField with no "empty" choice, so
# "no status yet" is the empty string (shown as a dash by every consumer) -- never NOT_REQUIRED
# (that would claim no reply is needed) and never AWAITING_REPLY (nobody is waiting yet).
NO_RESPONSE_STATUS = ""


def initial_response_status(*, direction, response_required, status=None):
    """The response_status a Correspondence gets at creation, before any response is linked.

    `status` is the Correspondence's own lifecycle value. For an OUT it decides whether a reply
    is already being awaited: only an ISSUED OUT (really sent, or recorded as already issued) is.
    A DRAFT OUT -- including a validated, "ready to send" one -- is not waiting for anything yet.
    """
    Status = _status_choices()
    if not response_required:
        return Status.NOT_REQUIRED
    if direction == "IN":
        return Status.TO_ANSWER
    if status == _lifecycle_choices().ISSUED:
        return Status.AWAITING_REPLY
    return NO_RESPONSE_STATUS


def mark_awaiting_reply_once_issued(correspondence):
    """CORR-STATUS-01: the one transition into "waiting for a reply". Called only after an OUT
    has REALLY become ISSUED (a confirmed send, or an explicit manual issue) -- never on
    validation, signature, a failed or an uncertain send. A no-op unless the OUT requires a reply
    and still has no response status, so a resend (or an older row that already carries one)
    is never rewritten. Reuses the one recompute rule, so a reply already linked beforehand
    makes it ANSWERED rather than AWAITING_REPLY."""
    if (
        correspondence.direction == "OUT"
        and correspondence.response_required
        and correspondence.status == _lifecycle_choices().ISSUED
        and correspondence.response_status == NO_RESPONSE_STATUS
    ):
        recompute_parent_response_status_from_responses(correspondence)


def validate_parent_correspondence(parent, child_direction, child_scope, *, exclude_child_id=None):
    """child_scope: a dict of the child's (project_id, company_profile_id, freelancer_id).

    exclude_child_id: the child's own id when this is an UPDATE of an existing response that
    still carries the same parent_correspondence (e.g. editing a DRAFT reply's subject) -- it
    must never be counted against itself by the active-reply check below.
    """
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
    # LOT 3D-3C0C section 10: a Correspondence cannot start a SECOND response while one is
    # already in progress -- mirrors the UI gating (View mode's Reply button, Actions menu's
    # Reply item), enforced here too so a client bypassing the UI is still rejected. DRAFT is
    # the only "in progress" lifecycle status (an IN response can never be DRAFT --
    # validate_status_for_direction already rejects that), so this only ever actually blocks
    # exactly the case the ticket describes: an IN parent that already has an OUT child still
    # being prepared. exclude_child_id keeps this from blocking a save of that same draft reply.
    Lifecycle = _lifecycle_choices()
    in_progress = parent.responses.filter(status=Lifecycle.DRAFT)
    if exclude_child_id is not None:
        in_progress = in_progress.exclude(pk=exclude_child_id)
    if in_progress.exists():
        raise ValidationError(
            {
                "parent_correspondence": (
                    "This correspondence already has a response in progress -- it cannot "
                    "start another one until that response is issued or deleted."
                )
            }
        )


def _candidate_status_for_child(parent_direction, child):
    """What response_status a single linked `child` implies for a parent of `parent_direction`,
    on its own -- the one place this per-child mapping lives, reused by both apply_response_link
    (create/update: only ever moves a pending parent forward) and
    recompute_parent_response_status_from_responses (delete: recomputes from whichever
    responses still remain, section LOT 3D-3C0C). Returns None for a direction combination that
    should never occur (validate_parent_correspondence already rejects it at save time).
    """
    Status = _status_choices()
    Lifecycle = _lifecycle_choices()
    if parent_direction == "IN" and child.direction == "OUT":
        # A linked OUT response's own lifecycle decides this -- DRAFT is a prepared response
        # (parent IN_PREPARATION), ISSUED actually answers the IN Correspondence (parent
        # ANSWERED). issue_date is a plain business date only and is never consulted here.
        return Status.ANSWERED if child.status == Lifecycle.ISSUED else Status.IN_PREPARATION
    if parent_direction == "OUT" and child.direction == "IN":
        # An IN is always ISSUED (DRAFT IN is rejected at creation): it unconditionally
        # answers an OUT that required a response.
        return Status.ANSWERED
    return None


# Only the two non-terminal candidates _candidate_status_for_child ever returns need a rank --
# ANSWERED is strictly more advanced than IN_PREPARATION (section 3: with several remaining
# responses, the most advanced one determines the parent).
def _status_rank(status):
    Status = _status_choices()
    return {Status.IN_PREPARATION: 1, Status.ANSWERED: 2}.get(status, 0)


def apply_response_link(child):
    """Once `child` (freshly created or updated) has a parent_correspondence, move the
    parent's response_status forward. Never touches the child's own status, never rewrites an
    already-ANSWERED parent, and never applies to a parent that never required a response.
    """
    parent = child.parent_correspondence
    if parent is None:
        return
    if not parent.response_required or parent.response_status not in parent.PENDING_STATUSES:
        return

    new_status = _candidate_status_for_child(parent.direction, child)
    if new_status is None:
        return

    if new_status != parent.response_status:
        parent.response_status = new_status
        parent.save(update_fields=["response_status", "updated_at"])


def recompute_parent_response_status_from_responses(parent):
    """LOT 3D-3C0C: after a child Correspondence is DELETED, recompute `parent`'s
    response_status from whichever linked responses still remain -- unlike apply_response_link
    (create/update), this can move the status backwards (e.g. ANSWERED -> IN_PREPARATION, or
    back to the original TO_ANSWER/AWAITING_REPLY once no response is left at all), since the
    response that had advanced it may be exactly the one just deleted.

    Reuses _candidate_status_for_child (the same per-child mapping apply_response_link uses)
    and initial_response_status (the same "no response yet" rule creation already uses) --
    no separate, duplicated set of status rules.

    Never touches a parent that doesn't require a response (stays NOT_REQUIRED). OVERDUE is
    still never written here -- it stays a read-time derivation
    (Correspondence.effective_response_status), exactly like everywhere else in this module.
    """
    if not parent.response_required:
        return

    remaining = list(parent.responses.all())
    candidates = [
        status
        for status in (
            _candidate_status_for_child(parent.direction, child) for child in remaining
        )
        if status is not None
    ]
    if candidates:
        new_status = max(candidates, key=_status_rank)
    else:
        # No response left at all: back to the exact same starting state a fresh Correspondence
        # with this direction/response_required would get.
        new_status = initial_response_status(
            direction=parent.direction, response_required=parent.response_required,
            status=parent.status,
        )

    if new_status != parent.response_status:
        parent.response_status = new_status
        parent.save(update_fields=["response_status", "updated_at"])
