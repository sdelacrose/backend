"""QUALITY Q7: supplier quality list and profile, computed from existing objects only.

No supplier table and no stored KPI: a supplier is a `ContactCompany` that receives real OUTGOING purchase orders of
the user's company, or that already has evaluations of that company (history stays reachable when the last order is
gone). Everything is scoped to the user's company, never to an identifier sent by the client.

Current Supplier Performance = the STORED score of the latest FINAL evaluation that is APPROVED or CLOSED. Its
reference date is `approved_at`, else the evaluated period end, else `created_at` (never `closed_at`). Historical
scores are never recalculated.
"""

from datetime import datetime, time, timezone as dt_timezone

from django.db.models import Count, Q

from project_management.models import ContactCompany, SupplierEvaluation
from project_management.services.supplier_evaluation import evaluable_orders
from project_management.services.supplier_evaluation_scoring import FINAL_STATUSES, compute_scoring

S = SupplierEvaluation.Status
OPEN_STATUSES = (S.DRAFT, S.IN_PROGRESS, S.SUBMITTED, S.UNDER_REVIEW, S.REJECTED)


def reference_date(evaluation):
    """approved_at, else the real business end of the evaluated period, else created_at."""
    if evaluation.approved_at:
        return evaluation.approved_at
    if evaluation.period_end:
        return datetime.combine(evaluation.period_end, time.min, tzinfo=dt_timezone.utc)
    return evaluation.created_at


def _sort_key(evaluation):
    return (reference_date(evaluation), evaluation.id)


def supplier_ids(user):
    """Suppliers of the user's company: receivers of its OUTGOING orders, plus anyone it already evaluated."""
    company_id = user.company_id
    from_orders = evaluable_orders(user).values_list("receiving_company_id", flat=True)
    from_evaluations = SupplierEvaluation.objects.filter(company_id=company_id).values_list("supplier_id", flat=True)
    return set(from_orders) | set(from_evaluations)


def suppliers_for_user(user):
    return ContactCompany.objects.filter(pk__in=supplier_ids(user))


def _scored_finals(user, ids):
    """FINAL + APPROVED/CLOSED evaluations, lightly loaded (one query for all the listed suppliers)."""
    return SupplierEvaluation.objects.filter(
        company_id=user.company_id, supplier_id__in=ids, evaluation_type=SupplierEvaluation.EvaluationType.FINAL,
        status__in=FINAL_STATUSES,
    ).only("id", "supplier_id", "reference", "overall_score", "rating", "approved_at", "period_end", "created_at")


def current_performance(evaluation):
    return None if evaluation is None else {
        "evaluation_id": evaluation.id,
        "reference": evaluation.reference,
        "date": reference_date(evaluation),
        "score": None if evaluation.overall_score is None else float(evaluation.overall_score),
        "rating": evaluation.rating,
    }


def list_suppliers(user, search=""):
    """Rows for the Quality suppliers list: one aggregate query + one query for the latest finals."""
    queryset = suppliers_for_user(user)
    search = (search or "").strip()
    if search:
        queryset = queryset.filter(company_name__icontains=search)
    mine = Q(supplier_evaluations__company_id=user.company_id)
    queryset = queryset.annotate(
        evaluations_open=Count("supplier_evaluations", filter=mine & Q(supplier_evaluations__status__in=OPEN_STATUSES), distinct=True),
        evaluations_partial=Count("supplier_evaluations", filter=mine & Q(supplier_evaluations__evaluation_type="PARTIAL"), distinct=True),
        evaluations_fnc=Count("supplier_evaluations", filter=mine & Q(supplier_evaluations__follows_fnc=True), distinct=True),
        reports_available=Count("supplier_evaluations", filter=mine & Q(supplier_evaluations__report__isnull=False), distinct=True),
    ).order_by("company_name", "id")
    return queryset


def latest_finals(user, ids):
    latest = {}
    for evaluation in _scored_finals(user, ids):
        best = latest.get(evaluation.supplier_id)
        if best is None or _sort_key(evaluation) > _sort_key(best):
            latest[evaluation.supplier_id] = evaluation
    return latest


def supplier_overview(user, supplier):
    """Counters of the profile, from a single aggregate over this company's evaluations of the supplier."""
    evaluations = SupplierEvaluation.objects.filter(company_id=user.company_id, supplier=supplier)
    return evaluations.aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(status__in=OPEN_STATUSES)),
        partial=Count("id", filter=Q(evaluation_type="PARTIAL")),
        final=Count("id", filter=Q(evaluation_type="FINAL")),
        fnc=Count("id", filter=Q(follows_fnc=True)),
        reports=Count("id", filter=Q(report__isnull=False)),
    )


def supplier_history(user, supplier):
    """Every evaluation of the supplier, newest first (one query, report presence through the join)."""
    return sorted(
        SupplierEvaluation.objects.filter(company_id=user.company_id, supplier=supplier)
        .select_related("purchase_order", "report"),
        key=_sort_key, reverse=True,
    )


def performance_points(history):
    """Score evolution of the approved / closed FINAL evaluations, oldest first. No trend label is invented."""
    finals = [e for e in history if e.evaluation_type == "FINAL" and e.status in FINAL_STATUSES]
    return [
        {"evaluation_id": e.id, "reference": e.reference, "date": reference_date(e),
         "score": None if e.overall_score is None else float(e.overall_score), "rating": e.rating}
        for e in sorted(finals, key=_sort_key)
    ]


def section_comparison(user, supplier):
    """Per-section scores of approved / closed FINAL evaluations that share the same historical model.

    Comparable = same template (and version) AND the very same ordered section titles in their own snapshots; any
    other evaluation lands in a different group, so a chart is never built on sections that do not match. Scores come
    from the scoring engine applied to each evaluation's own snapshot and saved answers (nothing is stored).
    """
    evaluations = (
        SupplierEvaluation.objects.filter(
            company_id=user.company_id, supplier=supplier, evaluation_type="FINAL", status__in=FINAL_STATUSES,
        ).prefetch_related("responses")
    )
    groups = {}
    for evaluation in sorted(evaluations, key=_sort_key):
        snapshot = evaluation.template_snapshot or {}
        titles = tuple(section.get("title", "") for section in snapshot.get("sections", []))
        if not titles:
            continue
        key = (evaluation.template_id, evaluation.template_version, snapshot.get("name", ""), titles)
        answers = {r.criterion_id: {"score": r.score, "not_applicable": r.not_applicable, "comment": r.comment}
                   for r in evaluation.responses.all()}
        result = compute_scoring(snapshot, answers)
        group = groups.setdefault(key, {
            "key": f"{evaluation.template_id or 'x'}-{evaluation.template_version or 0}-{len(groups)}",
            "template_name": snapshot.get("name", ""), "template_version": evaluation.template_version,
            "sections": list(titles), "series": [],
        })
        group["series"].append({
            "evaluation_id": evaluation.id, "reference": evaluation.reference, "date": reference_date(evaluation),
            "scores": [None if s["score"] is None else float(s["score"]) for s in result["sections"]],
        })
    ordered = sorted(groups.values(), key=lambda g: (-len(g["series"]), g["template_name"]))
    for group in ordered:
        group["comparable"] = len(group["series"]) >= 2
    return ordered
