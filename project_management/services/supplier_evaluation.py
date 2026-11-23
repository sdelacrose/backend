"""QUALITY Q3: create a Supplier Evaluation from a real outgoing purchase order, and save its answers.

Everything here trusts the authenticated user's company, never an identifier sent by the client: the
purchase order, its supplier, the template and the evaluation are all re-resolved inside that company.
Scoring (Q4) and the submission / validation workflow (Q5) are out of scope.
"""

import copy

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from project_management.models import (
    EvaluationReferenceCounter,
    EvaluationResponse,
    EvaluationTemplate,
    PurchaseOrder,
    SupplierEvaluation,
    SupplierEvaluationEvent,
)
from project_management.purchase_order_access import filter_purchase_orders_to_owner_scope
from project_management.models.supplier_evaluation_template import SCALE_MAX, SCALE_MIN
from project_management.services.supplier_evaluation_scoring import recalculate_evaluation_score
from project_management.services.supplier_evaluation_template import (
    build_template_snapshot,
    stored_template_weight_problem,
)

EDITABLE_STATUSES = (SupplierEvaluation.Status.DRAFT, SupplierEvaluation.Status.IN_PROGRESS)
REFERENCE_WIDTH = 5


# ---------------------------------------------------------------------------------------- purchase orders
def _supplier_in_company(company_id):
    return Q(receiving_company__company_profile_id=company_id) | Q(
        receiving_company__company_profile__isnull=True,
        receiving_company__user__company_id=company_id,
    )


def evaluable_orders(user):
    """Real OUTGOING purchase orders of the user's company whose receiving company is a supplier of it.
    Change notices are not orders. No end date is invented: only `po_date` is exposed."""
    company_id = getattr(user, "company_id", None)
    if not company_id:
        return PurchaseOrder.objects.none()
    scoped = filter_purchase_orders_to_owner_scope(PurchaseOrder.objects.all(), user)
    return PurchaseOrder.objects.filter(
        pk__in=scoped.values("pk"),
        po_type=PurchaseOrder.POType.OUTGOING,
        document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        receiving_company__isnull=False,
    ).filter(_supplier_in_company(company_id))


def orders_to_evaluate(user, search=""):
    queryset = (
        evaluable_orders(user)
        .select_related("receiving_company", "project")
        .annotate(
            evaluations_count=Count(
                "supplier_evaluations",
                filter=Q(supplier_evaluations__company_id=user.company_id),
                distinct=True,
            )
        )
    )
    search = (search or "").strip()
    if search:
        queryset = queryset.filter(
            Q(reference__icontains=search) | Q(receiving_company__company_name__icontains=search)
        )
    return queryset.order_by("-po_date", "-id")


# ------------------------------------------------------------------------------------------- references
def allocate_evaluation_reference(company_id, *, today=None):
    """EVAL-YYYYMM-NNNNN, unique per company. Must run inside a transaction (row lock on the counter)."""
    today = today or timezone.now().date()
    counter, _ = EvaluationReferenceCounter.objects.select_for_update().get_or_create(
        company_id=company_id
    )
    counter.last_sequence += 1
    counter.save(update_fields=["last_sequence", "updated_at"])
    return f"EVAL-{today:%Y%m}-{counter.last_sequence:0{REFERENCE_WIDTH}d}"


# ------------------------------------------------------------------------------------------- creation
def _clean_period(start, end):
    errors = {}
    if start and end and end < start:
        errors["period_end"] = "The end of the period cannot be before its start."
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def create_supplier_evaluation(
    *, user, purchase_order_id, template_id, evaluation_type,
    period_start=None, period_end=None, follows_fnc=False, fnc_reference="",
):
    if not getattr(user, "company_id", None):
        raise ValidationError({"detail": "A company account is required."})

    order = evaluable_orders(user).select_related("receiving_company", "project").filter(
        pk=purchase_order_id
    ).first()
    if order is None:
        raise ValidationError({"purchase_order": "This purchase order cannot be evaluated."})

    template = (
        EvaluationTemplate.objects.filter(company_id=user.company_id, pk=template_id)
        .prefetch_related("sections__criteria").first()
    )
    if template is None:
        raise ValidationError({"template": "This evaluation template does not exist."})
    if not template.is_active:
        raise ValidationError({"template": "This evaluation template is inactive."})
    if not any(section.criteria.all() for section in template.sections.all()):
        raise ValidationError({"template": "This evaluation template has no criterion."})
    weight_problem = stored_template_weight_problem(template)
    if weight_problem:  # templates saved before the Q4 weight rule must be corrected first
        raise ValidationError({"template": f"This evaluation template has invalid {weight_problem}"})

    if evaluation_type not in SupplierEvaluation.EvaluationType.values:
        raise ValidationError({"evaluation_type": "Choose a partial or a final evaluation."})
    _clean_period(period_start, period_end)
    fnc_reference = (fnc_reference or "").strip()
    if follows_fnc and not fnc_reference:
        raise ValidationError({"fnc_reference": "The FNC reference is required."})
    if not follows_fnc:
        fnc_reference = ""

    evaluation = SupplierEvaluation.objects.create(
        company_id=user.company_id,
        supplier=order.receiving_company,  # always derived from the order, never chosen
        purchase_order=order,
        project=order.project,
        template=template,
        template_version=template.version,
        # Frozen copy: later edits of the template never reach this evaluation.
        template_snapshot=copy.deepcopy(build_template_snapshot(template)),
        reference=allocate_evaluation_reference(user.company_id),
        status=SupplierEvaluation.Status.DRAFT,
        evaluation_type=evaluation_type,
        owner=user,
        created_by=user,
        period_start=period_start,
        period_end=period_end,
        follows_fnc=bool(follows_fnc),
        fnc_reference=fnc_reference,
    )
    SupplierEvaluationEvent.objects.create(
        evaluation=evaluation, event_type=SupplierEvaluationEvent.EventType.CREATED, actor=user,
    )
    return evaluation


# ------------------------------------------------------------------------------------------- answers
def snapshot_criteria(evaluation):
    snapshot = evaluation.template_snapshot or {}
    return {
        criterion["id"]: criterion
        for section in snapshot.get("sections", [])
        for criterion in section.get("criteria", [])
    }


def _threshold(snapshot, criterion):
    own = criterion.get("comment_required_below")
    return own if own is not None else snapshot.get("comment_required_below")


def _validate_answer(snapshot, criterion, answer):
    """Returns (score, not_applicable, comment) or raises ValueError with a message."""
    score = answer.get("score")
    not_applicable = bool(answer.get("not_applicable"))
    comment = (answer.get("comment") or "").strip()
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, int):
            raise ValueError("The score must be a whole number.")
        if not SCALE_MIN <= score <= SCALE_MAX:
            raise ValueError(f"The score must be between {SCALE_MIN} and {SCALE_MAX}.")
    if score is not None and not_applicable:
        raise ValueError("A criterion cannot have both a score and N/A.")
    if not_applicable and not criterion.get("allow_na"):
        raise ValueError("N/A is not allowed for this criterion.")
    threshold = _threshold(snapshot, criterion)
    if score is not None and threshold is not None and score < threshold and not comment:
        raise ValueError("A comment is required for a score below the threshold.")
    return score, not_applicable, comment


@transaction.atomic
def save_responses(*, evaluation, user, answers):
    """Save (create / update / clear) answers. All-or-nothing: one invalid answer saves nothing."""
    evaluation = SupplierEvaluation.objects.select_for_update().get(pk=evaluation.pk)
    if evaluation.owner_id != user.id:  # Q5: only the owner prepares the answers
        raise PermissionDenied("Only the owner of the evaluation can change its answers.")
    if evaluation.status not in EDITABLE_STATUSES:
        raise ValidationError({"detail": "This evaluation can no longer be changed."})
    if not isinstance(answers, list):
        raise ValidationError({"answers": "A list of answers is expected."})

    criteria = snapshot_criteria(evaluation)
    snapshot = evaluation.template_snapshot or {}
    errors, cleaned, seen = {}, [], set()
    for answer in answers:
        criterion_id = answer.get("criterion_id") if isinstance(answer, dict) else None
        if criterion_id not in criteria:  # only criteria of this evaluation's own snapshot
            errors[str(criterion_id)] = "This criterion does not belong to the evaluation."
            continue
        if criterion_id in seen:
            errors[str(criterion_id)] = "This criterion is answered twice."
            continue
        seen.add(criterion_id)
        try:
            cleaned.append((criterion_id, *_validate_answer(snapshot, criteria[criterion_id], answer)))
        except ValueError as error:
            errors[str(criterion_id)] = str(error)
    if errors:
        raise ValidationError({"answers": errors})

    for criterion_id, score, not_applicable, comment in cleaned:
        if score is None and not not_applicable and not comment:
            EvaluationResponse.objects.filter(evaluation=evaluation, criterion_id=criterion_id).delete()
            continue
        EvaluationResponse.objects.update_or_create(
            evaluation=evaluation, criterion_id=criterion_id,
            defaults={"score": score, "not_applicable": not_applicable, "comment": comment,
                      "answered_by": user},
        )
    if evaluation.status == SupplierEvaluation.Status.DRAFT and evaluation.responses.exists():
        evaluation.status = SupplierEvaluation.Status.IN_PROGRESS
        evaluation.save(update_fields=["status", "updated_at"])
        SupplierEvaluationEvent.objects.create(
            evaluation=evaluation, event_type=SupplierEvaluationEvent.EventType.STARTED, actor=user,
        )
    recalculate_evaluation_score(evaluation)
    return evaluation


def evaluation_progress(evaluation):
    criteria = snapshot_criteria(evaluation)
    answered_ids = {
        r.criterion_id
        for r in evaluation.responses.all()
        if r.score is not None or r.not_applicable
    }
    answered_ids &= set(criteria)
    required = {cid for cid, c in criteria.items() if c.get("required")}
    critical = {cid for cid, c in criteria.items() if c.get("critical")}
    return {
        "total": len(criteria),
        "answered": len(answered_ids),
        "required_total": len(required),
        "required_answered": len(required & answered_ids),
        "critical_total": len(critical),
        "critical_answered": len(critical & answered_ids),
    }
