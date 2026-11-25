"""QUALITY Q4: the one and only Supplier Evaluation scoring engine.

Pure functions over an evaluation's own frozen snapshot and its answers -- the live template is never read,
and the frontend implements no formula: it only displays what this module returns.

Rules
  * score s (1..5) -> s * 20 %.  N/A and "no answer" are excluded from the calculation (never counted as 0).
  * Section score: weighted mean of the scored criteria when EVERY criterion of the section has a weight in the
    snapshot (weights renormalised on the criteria actually scored), otherwise the simple mean.
    No scored criterion -> None.
  * Overall score: same principle over the sections that have a score (weights renormalised on them when every
    section of the snapshot is weighted, otherwise simple mean). No scored section -> overall None, rating None.
  * Rounding: exact Decimal arithmetic, ONE final rounding (half up) to one decimal for display and storage.
  * Rating is derived from that rounded score, i.e. from the value the user sees and that is stored:
    >= 85 Excellent, >= 70 Good, >= 50 Improvement Required, < 50 Unsatisfactory.
The weight rule (all absent, or all present and summing to 100) is enforced when a template is saved; a legacy
snapshot with partial weights simply falls back to the simple mean.
"""

from decimal import ROUND_HALF_UP, Decimal

from project_management.models import SupplierEvaluation

ONE_DECIMAL = Decimal("0.1")
POINTS_PER_SCORE = Decimal("20")

RATING_EXCELLENT = "EXCELLENT"
RATING_GOOD = "GOOD"
RATING_IMPROVEMENT_REQUIRED = "IMPROVEMENT_REQUIRED"
RATING_UNSATISFACTORY = "UNSATISFACTORY"
RATINGS = (RATING_EXCELLENT, RATING_GOOD, RATING_IMPROVEMENT_REQUIRED, RATING_UNSATISFACTORY)

STRENGTH_SCORES = (5,)
WEAK_SCORES = (1, 2)
FINAL_STATUSES = (SupplierEvaluation.Status.APPROVED, SupplierEvaluation.Status.CLOSED)


def round_score(value):
    return None if value is None else value.quantize(ONE_DECIMAL, rounding=ROUND_HALF_UP)


def rating_for(score):
    """`score`: the ROUNDED overall score (Decimal) or None."""
    if score is None:
        return None
    if score >= 85:
        return RATING_EXCELLENT
    if score >= 70:
        return RATING_GOOD
    if score >= 50:
        return RATING_IMPROVEMENT_REQUIRED
    return RATING_UNSATISFACTORY


def _mean(values_and_weights, weighted):
    """values_and_weights: [(value, weight)]; weights are renormalised on the items given."""
    if not values_and_weights:
        return None
    if weighted:
        total = sum(weight for _, weight in values_and_weights)
        return sum(value * weight for value, weight in values_and_weights) / total
    return sum(value for value, _ in values_and_weights) / len(values_and_weights)


def _weight(raw):
    return None if raw in (None, "") else Decimal(str(raw))


def _all_weighted(items):
    weights = [_weight(item.get("weight")) for item in items]
    return bool(weights) and all(w is not None and w > 0 for w in weights)


def compute_scoring(snapshot, answers):
    """`answers`: {criterion_id: {"score": int|None, "not_applicable": bool, "comment": str}}.
    Returns a plain dict (Decimals are rounded to one decimal)."""
    snapshot = snapshot or {}
    sections_out, strengths, improvements, critical_points, required_remaining = [], [], [], [], []
    scored_sections = []  # (unrounded section score, section snapshot)

    for section in snapshot.get("sections", []):
        criteria = section.get("criteria", [])
        weighted = _all_weighted(criteria)
        scored = []
        answered = 0
        for criterion in criteria:
            answer = answers.get(criterion["id"]) or {}
            score = answer.get("score")
            is_answered = score is not None or bool(answer.get("not_applicable"))
            answered += is_answered
            if criterion.get("required") and not is_answered:
                required_remaining.append({
                    "criterion_id": criterion["id"], "label": criterion["label"],
                    "section_title": section["title"], "critical": bool(criterion.get("critical")),
                })
            if score is None:  # N/A or no answer: excluded, never a zero
                continue
            scored.append((Decimal(score) * POINTS_PER_SCORE, _weight(criterion.get("weight")) if weighted else None))
            item = {
                "criterion_id": criterion["id"], "label": criterion["label"],
                "section_title": section["title"], "score": score, "comment": answer.get("comment") or "",
            }
            if score in STRENGTH_SCORES:
                strengths.append(item)
            if score in WEAK_SCORES:
                improvements.append(item)
                if criterion.get("critical"):
                    critical_points.append(item)
        value = _mean(scored, weighted)
        sections_out.append({
            "id": section["id"], "title": section["title"],
            "weight": section.get("weight"), "score": round_score(value),
            "scored": len(scored), "answered": answered, "total": len(criteria),
        })
        if value is not None:
            scored_sections.append((value, section))

    sections_weighted = _all_weighted(snapshot.get("sections", []))
    overall = _mean(
        [(value, _weight(section.get("weight")) if sections_weighted else None)
         for value, section in scored_sections],
        sections_weighted,
    )
    overall = round_score(overall)
    return {
        "overall_score": overall,
        "rating": rating_for(overall),
        "sections": sections_out,
        "sections_weighted": sections_weighted,
        "strengths": strengths,
        "improvements": improvements,
        "critical_points": critical_points,
        "required_remaining": required_remaining,
    }


def evaluation_scoring(evaluation):
    """Scoring of an evaluation from its snapshot and stored responses (read-only)."""
    answers = {
        r.criterion_id: {"score": r.score, "not_applicable": r.not_applicable, "comment": r.comment}
        for r in evaluation.responses.all()
    }
    return compute_scoring(evaluation.template_snapshot, answers)


def recalculate_evaluation_score(evaluation):
    """Store overall_score / rating. Approved and closed evaluations are never recalculated (their stored
    result is part of the record); the caller decides when this runs (every authorised answers save)."""
    if evaluation.status in FINAL_STATUSES:
        return evaluation
    result = evaluation_scoring(evaluation)
    evaluation.overall_score = result["overall_score"]
    evaluation.rating = result["rating"]
    evaluation.save(update_fields=["overall_score", "rating", "updated_at"])
    return evaluation
