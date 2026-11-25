"""QUALITY Q5: the one and only Supplier Evaluation validation workflow.

DRAFT -> IN_PROGRESS -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> CLOSED, with the correction loop
UNDER_REVIEW -> REJECTED -> IN_PROGRESS -> SUBMITTED. Nothing else is possible; CLOSED is final.

Every rule is enforced here (the interface only mirrors `allowed_actions`). Who may do what:
  * the owner prepares the answers, picks the reviewer, submits, resumes after a rejection;
  * the reviewer (chosen by the owner, always a different user) starts the review, approves or rejects;
  * the owner or the reviewer may close an approved evaluation.
Being a company administrator grants nothing here: only the user named as reviewer can review.
The score / rating never conditions a submission or an approval.
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from models.models import User
from project_management.models import SupplierEvaluation, SupplierEvaluationEvent
from project_management.services.supplier_evaluation import (
    EDITABLE_STATUSES,
    _threshold,
    snapshot_criteria,
)
from project_management.services.supplier_evaluation_scoring import recalculate_evaluation_score

S = SupplierEvaluation.Status
E = SupplierEvaluationEvent.EventType

# action -> (allowed source statuses, target status)
TRANSITIONS = {
    # A DRAFT has no answer yet, so submission_check never lets it through: it must be started first.
    "submit": (EDITABLE_STATUSES, S.SUBMITTED),
    "start_review": ((S.SUBMITTED,), S.UNDER_REVIEW),
    "reject": ((S.UNDER_REVIEW,), S.REJECTED),
    "resume": ((S.REJECTED,), S.IN_PROGRESS),
    "approve": ((S.UNDER_REVIEW,), S.APPROVED),
    "close": ((S.APPROVED,), S.CLOSED),
}
OWNER_ACTIONS = ("submit", "resume")
REVIEWER_ACTIONS = ("start_review", "reject", "approve")


def _can_manage(user):
    # Imported lazily: the views import this service.
    from project_management.views.quality import user_can_manage_evaluations

    return user_can_manage_evaluations(user)


def is_owner(evaluation, user):
    return evaluation.owner_id is not None and evaluation.owner_id == user.id


def is_reviewer(evaluation, user):
    # The reviewer must never be the owner: nobody approves their own work, whatever their rights.
    return (
        evaluation.reviewer_id is not None
        and evaluation.reviewer_id == user.id
        and evaluation.reviewer_id != evaluation.owner_id
    )


def eligible_reviewers(evaluation):
    """Active users of the evaluation's company who hold the Quality evaluation right, owner excluded."""
    users = User.objects.filter(company_id=evaluation.company_id, is_active=True)
    # Cheap pre-filter in SQL (any role granting the right); the exact check below stays authoritative.
    users = users.filter(
        user_company_roles__role__role_permissions__permission__key__in=["quality.evaluation_manage", "quality.full_access"]
    ).distinct()
    if evaluation.owner_id:
        users = users.exclude(pk=evaluation.owner_id)
    return [user for user in users.order_by("first_name", "last_name", "email") if _can_manage(user)]


def allowed_actions(evaluation, user):
    """What THIS user can do now (the interface shows exactly these buttons)."""
    if not _can_manage(user):
        return []
    status, actions = evaluation.status, []
    if is_owner(evaluation, user):
        if status in EDITABLE_STATUSES:
            actions += ["assign_reviewer", "submit"]
        if status == S.REJECTED:
            actions.append("resume")
    if is_reviewer(evaluation, user):
        if status == S.SUBMITTED:
            actions.append("start_review")
        if status == S.UNDER_REVIEW:
            actions += ["approve", "reject"]
    if status == S.APPROVED and (is_owner(evaluation, user) or is_reviewer(evaluation, user)):
        actions.append("close")
    return actions


def submission_check(evaluation):
    """What still blocks a submission. Pure data, shared by the detail payload and the submit action."""
    criteria = snapshot_criteria(evaluation)
    snapshot = evaluation.template_snapshot or {}
    answers = {r.criterion_id: r for r in evaluation.responses.all() if r.criterion_id in criteria}
    section_of = {
        criterion["id"]: section.get("title", "")
        for section in snapshot.get("sections", [])
        for criterion in section.get("criteria", [])
    }

    def item(cid):
        return {"criterion_id": cid, "label": criteria[cid]["label"], "section_title": section_of[cid]}

    def answered(cid):
        answer = answers.get(cid)
        return bool(answer) and (answer.score is not None or answer.not_applicable)

    missing_required = [item(c) for c, v in criteria.items() if v.get("required") and not answered(c)]
    missing_critical = [item(c) for c, v in criteria.items() if v.get("critical") and not answered(c)]
    missing_comments = []
    for cid, answer in answers.items():
        threshold = _threshold(snapshot, criteria[cid])
        if answer.score is not None and threshold is not None and answer.score < threshold \
                and not (answer.comment or "").strip():
            missing_comments.append(item(cid))
    reviewer_problem = None
    if evaluation.owner_id is None:
        reviewer_problem = "owner_missing"
    elif evaluation.reviewer_id is None:
        reviewer_problem = "reviewer_missing"
    elif evaluation.reviewer_id == evaluation.owner_id:
        reviewer_problem = "reviewer_is_owner"
    nothing_answered = not any(answered(c) for c in criteria)
    ready = not (nothing_answered or missing_required or missing_critical or missing_comments or reviewer_problem) \
        and evaluation.status in EDITABLE_STATUSES
    return {
        "ready": ready,
        "missing_required": missing_required,
        "missing_critical": missing_critical,
        "missing_comments": missing_comments,
        "nothing_answered": nothing_answered,
        "reviewer_problem": reviewer_problem,
        "answered": sum(1 for c in criteria if answered(c)),
        "total": len(criteria),
    }


def record_event(evaluation, event_type, actor, comment="", metadata=None):
    return SupplierEvaluationEvent.objects.create(
        evaluation=evaluation, event_type=event_type, actor=actor,
        comment=(comment or "").strip(), metadata=metadata or {},
    )


def _locked(evaluation):
    return SupplierEvaluation.objects.select_for_update().get(pk=evaluation.pk)


@transaction.atomic
def assign_reviewer(*, evaluation, user, reviewer_id):
    evaluation = _locked(evaluation)
    if not is_owner(evaluation, user):
        raise PermissionDenied("Only the owner of the evaluation can choose its reviewer.")
    if evaluation.status not in EDITABLE_STATUSES:
        raise ValidationError({"detail": "The reviewer can only be changed while the evaluation is being prepared."})
    reviewer = next((u for u in eligible_reviewers(evaluation) if u.id == reviewer_id), None) \
        if isinstance(reviewer_id, int) and not isinstance(reviewer_id, bool) else None
    if reviewer is None:
        raise ValidationError({"reviewer": "Choose a reviewer from your company, different from the owner."})
    evaluation.reviewer = reviewer
    evaluation.save(update_fields=["reviewer", "updated_at"])
    record_event(evaluation, E.REVIEWER_ASSIGNED, user, metadata={"reviewer_id": reviewer.id})
    return evaluation


def _require_actor(action, evaluation, user):
    if action in OWNER_ACTIONS:
        allowed = is_owner(evaluation, user)
        who = "the owner of the evaluation"
    elif action in REVIEWER_ACTIONS:
        allowed = is_reviewer(evaluation, user)
        who = "the reviewer of the evaluation"
    else:  # close
        allowed = is_owner(evaluation, user) or is_reviewer(evaluation, user)
        who = "the owner or the reviewer of the evaluation"
    if not allowed:
        raise PermissionDenied(f"Only {who} can do this.")


@transaction.atomic
def transition(*, action, evaluation, user, comment=""):
    """Run one workflow step. Everything is re-checked on the locked row: status, actor, completeness."""
    sources, target = TRANSITIONS[action]
    evaluation = _locked(evaluation)
    _require_actor(action, evaluation, user)
    if evaluation.status not in sources:
        raise ValidationError({"detail": "This action is not possible in the current status."})
    comment = (comment or "").strip()
    now = timezone.now()
    event, fields = None, ["status", "updated_at"]

    if action == "submit":
        check = submission_check(evaluation)
        if not check["ready"]:
            raise ValidationError({"detail": "The evaluation is not ready to be submitted.", "submission": check})
        recalculate_evaluation_score(evaluation)
        resubmission = evaluation.rejected_at is not None
        evaluation.submitted_at, evaluation.submitted_by = now, user
        fields += ["submitted_at", "submitted_by"]
        event = E.RESUBMITTED if resubmission else E.SUBMITTED
    elif action == "start_review":
        evaluation.reviewed_at, evaluation.reviewed_by = now, user
        fields += ["reviewed_at", "reviewed_by"]
        event = E.REVIEW_STARTED
    elif action == "reject":
        if not comment:
            raise ValidationError({"comment": "A reason is required to reject an evaluation."})
        evaluation.rejected_at, evaluation.rejected_by, evaluation.rejection_reason = now, user, comment
        fields += ["rejected_at", "rejected_by", "rejection_reason"]
        event = E.REJECTED
    elif action == "resume":
        event = E.RESUMED
    elif action == "approve":
        recalculate_evaluation_score(evaluation)  # the stored result is frozen from here on
        evaluation.approved_at, evaluation.approved_by = now, user
        fields += ["approved_at", "approved_by"]
        event = E.APPROVED
    else:  # close
        evaluation.closed_at, evaluation.closed_by = now, user
        fields += ["closed_at", "closed_by"]
        event = E.CLOSED

    evaluation.status = target
    evaluation.save(update_fields=fields)
    record_event(evaluation, event, user, comment)
    return evaluation
