"""QUALITY Q6: PDF report of an approved / closed Supplier Evaluation, filed as a real LINKS Document.

* Content comes ONLY from the evaluation's own frozen snapshot, its saved answers and its stored result: the live
  template is never read, and nothing is recomputed from it.
* The PDF is rendered with WeasyPrint (already used by the Supplier Surveillance evidence) from a Django template.
* One logical Document per evaluation (`SupplierEvaluationReport`); every regeneration is a new DocumentRevision of
  that same Document, created by the existing `add_document_revision` service (no second document system).
* Generation never writes to the evaluation: status, score and workflow fields stay as they are.
* Rendering happens before any write. The Document, the link and the revision are created in ONE transaction
  holding a lock on the evaluation row, so a failure leaves nothing behind and concurrent first generations can
  never create two Documents.
"""

from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.core.files.base import ContentFile
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import dateformat, timezone, translation
from rest_framework.exceptions import ValidationError
from weasyprint import HTML

from project_management.models import (
    Document,
    Folder,
    SupplierEvaluation,
    SupplierEvaluationEvent,
    SupplierEvaluationReport,
)
from project_management.services.document_revision import add_document_revision
from project_management.services.supplier_evaluation_scoring import FINAL_STATUSES, evaluation_scoring

REPORT_FOLDER_NAME = "Quality Reports"
REPORT_DOCUMENT_TYPE = "Supplier Evaluation Report"
LANGUAGES = ("en", "fr", "de")

LABELS = {
    "en": {
        "title": "Supplier evaluation report", "reference": "Evaluation", "supplier": "Supplier", "order": "Purchase order",
        "project": "Project", "period": "Evaluated period", "type": "Type", "PARTIAL": "Partial", "FINAL": "Final",
        "fnc": "FNC reference", "none": "-", "result": "Result", "score": "Overall score", "rating": "Rating",
        "sections": "Scores by section", "details": "Criteria", "criterion": "Criterion", "weight": "Weight",
        "answer": "Score", "comment": "Comment", "na": "N/A", "unanswered": "Not answered", "not_scored": "Not scored",
        "required": "required", "critical": "critical", "strengths": "Strengths", "improvements": "Areas for improvement",
        "critical_points": "Critical points", "no_items": "None", "conclusion": "Conclusion", "workflow": "Validation",
        "owner": "Owner", "reviewer": "Reviewer", "submitted": "Submitted", "review_started": "Review started",
        "approved": "Approved", "closed": "Closed", "generated": "Generated", "page": "Page", "of": "of",
        "template": "Questionnaire", "reviewer_comment": "Reviewer comment",
        "conclusion_text": "The supplier obtained {score} / 100, rated {rating}.",
        "conclusion_counts": "Out of {total} criteria: {scored} scored, {na} N/A, {unanswered} not answered.",
        "section": "Section", "section_score": "Score", "step": "Step", "actor": "Actor", "datetime": "Date and time",
        "timezone": "Time zone",
        "EXCELLENT": "Excellent", "GOOD": "Good", "IMPROVEMENT_REQUIRED": "Improvement required",
        "UNSATISFACTORY": "Unsatisfactory", "yes": "Yes",
    },
    "fr": {
        "title": "Rapport d'évaluation fournisseur", "reference": "Évaluation", "supplier": "Fournisseur",
        "order": "Commande", "project": "Projet", "period": "Période évaluée", "type": "Type", "PARTIAL": "Partielle",
        "FINAL": "Finale", "fnc": "Référence FNC", "none": "-", "result": "Résultat", "score": "Score global",
        "rating": "Appréciation", "sections": "Scores par section", "details": "Critères", "criterion": "Critère",
        "weight": "Poids", "answer": "Note", "comment": "Commentaire", "na": "N/A", "unanswered": "Sans réponse",
        "not_scored": "Non noté", "required": "obligatoire", "critical": "critique", "strengths": "Points forts",
        "improvements": "Axes d'amélioration", "critical_points": "Points critiques", "no_items": "Aucun",
        "conclusion": "Conclusion", "workflow": "Validation", "owner": "Responsable", "reviewer": "Relecteur",
        "submitted": "Soumise", "review_started": "Revue démarrée", "approved": "Approuvée", "closed": "Clôturée",
        "generated": "Généré", "page": "Page", "of": "sur", "template": "Questionnaire",
        "reviewer_comment": "Commentaire du relecteur",
        "conclusion_text": "Le fournisseur obtient {score} / 100, apprécié {rating}.",
        "conclusion_counts": "Sur {total} critères : {scored} noté(s), {na} N/A, {unanswered} sans réponse.",
        "section": "Section", "section_score": "Score", "step": "Étape", "actor": "Acteur", "datetime": "Date et heure",
        "timezone": "Fuseau horaire",
        "EXCELLENT": "Excellent", "GOOD": "Bon", "IMPROVEMENT_REQUIRED": "À améliorer",
        "UNSATISFACTORY": "Insatisfaisant", "yes": "Oui",
    },
    "de": {
        "title": "Lieferantenbewertungsbericht", "reference": "Bewertung", "supplier": "Lieferant",
        "order": "Bestellung", "project": "Projekt", "period": "Bewerteter Zeitraum", "type": "Typ",
        "PARTIAL": "Teilbewertung", "FINAL": "Endbewertung", "fnc": "FNC-Referenz", "none": "-",
        "result": "Ergebnis", "score": "Gesamtpunktzahl", "rating": "Einstufung", "sections": "Punktzahl je Abschnitt",
        "details": "Kriterien", "criterion": "Kriterium", "weight": "Gewicht", "answer": "Note",
        "comment": "Kommentar", "na": "N/A", "unanswered": "Nicht beantwortet", "not_scored": "Nicht bewertet",
        "required": "Pflicht", "critical": "kritisch", "strengths": "Stärken", "improvements": "Verbesserungsbereiche",
        "critical_points": "Kritische Punkte", "no_items": "Keine", "conclusion": "Fazit", "workflow": "Freigabe",
        "owner": "Verantwortlicher", "reviewer": "Prüfer", "submitted": "Eingereicht", "review_started": "Prüfung gestartet",
        "approved": "Genehmigt", "closed": "Abgeschlossen", "generated": "Erstellt", "page": "Seite", "of": "von",
        "template": "Fragebogen", "reviewer_comment": "Kommentar des Prüfers",
        "conclusion_text": "Der Lieferant erreicht {score} / 100, eingestuft als {rating}.",
        "conclusion_counts": "Von {total} Kriterien: {scored} bewertet, {na} N/A, {unanswered} nicht beantwortet.",
        "section": "Abschnitt", "section_score": "Punktzahl", "step": "Schritt", "actor": "Akteur", "datetime": "Datum und Uhrzeit",
        "timezone": "Zeitzone",
        "EXCELLENT": "Ausgezeichnet", "GOOD": "Gut", "IMPROVEMENT_REQUIRED": "Verbesserung erforderlich",
        "UNSATISFACTORY": "Ungenügend", "yes": "Ja",
    },
}


def _name(user):
    return None if not user else ((user.full_name or "").strip() or user.email)


def _localized(text, language):
    """Decimal comma for French and German; the digits themselves are the engine's, untouched."""
    return text.replace(".", ",") if language in ("fr", "de") else text


def _fmt_score(value, language="en"):
    return None if value is None else _localized(f"{float(value):.1f}", language)


def _fmt_weight(value, language="en"):
    return None if value in (None, "") else f"{_localized(f'{float(value):g}', language)} %"


def _fmt_date(value, language="en"):
    if not value:
        return None
    with translation.override(language):
        return dateformat.format(value, "j F Y")


def report_timezone(name):
    """The zone the reader asked for (their browser's IANA zone, sent with the request). LINKS stores no per-user
    time zone and its only company zone (`accrual_timezone`) belongs to leave accrual, so nothing is guessed: an
    absent or unknown name prints in UTC, and the zone is always written next to the time."""
    try:
        return ZoneInfo(name), name
    except Exception:  # noqa: BLE001 - unknown / empty / malformed names all fall back the same way
        return dt_timezone.utc, "UTC"


def _fmt_dt(value, language, tz):
    if not value:
        return None
    local = value.astimezone(tz)
    offset = local.strftime("%z")
    with translation.override(language):
        text = dateformat.format(local, "j F Y, H:i")
    return f"{text} (UTC{offset[:3]}:{offset[3:]})"


def report_filename(evaluation):
    return f"{evaluation.reference} - Rapport.pdf"


def assert_reportable(evaluation):
    if evaluation.status not in FINAL_STATUSES:
        raise ValidationError({"detail": "The report is only available once the evaluation is approved or closed."})


def build_report_context(evaluation, language, generated_at, timezone_name=None):
    """Everything printed in the PDF, from the snapshot, the saved answers and the stored result only."""
    labels = LABELS[language]
    tz, tz_name = report_timezone(timezone_name)
    snapshot = evaluation.template_snapshot or {}
    answers = {r.criterion_id: r for r in evaluation.responses.all()}
    result = evaluation_scoring(evaluation)  # sections / synthesis from the frozen snapshot
    section_scores = {s["id"]: s for s in result["sections"]}
    # Approved / closed evaluations carry their stored result: that is the figure shown on screen.
    overall = evaluation.overall_score if evaluation.overall_score is not None else result["overall_score"]
    rating = evaluation.rating or result["rating"]

    sections, scored_total, na_total, criteria_total = [], 0, 0, 0
    for section in snapshot.get("sections", []):
        rows = []
        for criterion in section.get("criteria", []):
            answer = answers.get(criterion["id"])
            criteria_total += 1
            if answer is None or (answer.score is None and not answer.not_applicable):
                shown = labels["unanswered"]
            elif answer.not_applicable:
                shown = labels["na"]
                na_total += 1
            else:
                shown = f"{answer.score} / 5"
                scored_total += 1
            rows.append({
                "label": criterion.get("label", ""),
                "description": criterion.get("description", ""),
                "weight": _fmt_weight(criterion.get("weight"), language),
                "required": bool(criterion.get("required")),
                "critical": bool(criterion.get("critical")),
                "answer": shown,
                "comment": (answer.comment if answer else "") or "",
            })
        scored = section_scores.get(section["id"], {})
        sections.append({
            "title": section.get("title", ""), "weight": _fmt_weight(section.get("weight"), language),
            "score": _fmt_score(scored.get("score"), language), "rows": rows,
        })

    approval = (
        evaluation.events.filter(event_type=SupplierEvaluationEvent.EventType.APPROVED)
        .exclude(comment="").order_by("-created_at", "-id").first()
    )
    order, project = evaluation.purchase_order, evaluation.project
    score_text = _fmt_score(overall, language)
    conclusion = None
    if score_text is not None and rating:
        conclusion = labels["conclusion_text"].format(score=score_text, rating=labels[rating])
    counts = labels["conclusion_counts"].format(
        total=criteria_total, scored=scored_total, na=na_total, unanswered=criteria_total - scored_total - na_total)
    steps = [
        {"step": labels[key], "actor": _name(actor), "when": _fmt_dt(when, language, tz)}
        for key, actor, when in (
            ("submitted", evaluation.submitted_by, evaluation.submitted_at),
            ("review_started", evaluation.reviewed_by, evaluation.reviewed_at),
            ("approved", evaluation.approved_by, evaluation.approved_at),
            ("closed", evaluation.closed_by, evaluation.closed_at),
        )
        if when
    ]
    return {
        "labels": labels,
        "language": language,
        "reference": evaluation.reference,
        "supplier": evaluation.supplier.company_name,
        "order": order.reference if order else None,
        "project": project.project_name if project else None,
        "type": labels[evaluation.evaluation_type],
        "period": (f"{_fmt_date(evaluation.period_start, language) or ''} → {_fmt_date(evaluation.period_end, language) or ''}"
                   if evaluation.period_start or evaluation.period_end else None),
        "fnc_reference": evaluation.fnc_reference if evaluation.follows_fnc else None,
        "template": f"{snapshot.get('name', '')} v{evaluation.template_version or ''}".strip(),
        "score": score_text,
        "rating": labels[rating] if rating else None,
        "rating_code": rating,
        "sections": sections,
        "strengths": result["strengths"],
        "improvements": result["improvements"],
        "critical_points": result["critical_points"],
        "conclusion": conclusion,
        "reviewer_comment": approval.comment if approval else None,
        "owner": _name(evaluation.owner),
        "reviewer": _name(evaluation.reviewer),
        "steps": steps,
        "counts": counts,
        "timezone": tz_name,
        "generated_at": _fmt_dt(generated_at, language, tz),
    }


def render_report_pdf(evaluation, language, generated_at, timezone_name=None):
    html = render_to_string(
        "supplier_evaluation_report.html", build_report_context(evaluation, language, generated_at, timezone_name))
    return HTML(string=html).write_pdf()


def quality_reports_folder(company_id):
    """The company folder holding every report, created on first use under the company root folder."""
    from models.models import CompanyProfile

    company = CompanyProfile.objects.get(pk=company_id)
    root, _ = Folder.objects.get_or_create(
        company_profile=company, parent=None, name=f"{company.id}_company_root_folder",
    )
    folder, _ = Folder.objects.get_or_create(
        company_profile=company, parent=root, name=REPORT_FOLDER_NAME,
    )
    return folder


def generate_report(*, evaluation, user, language="en", timezone_name=None):
    """Create the evaluation report Document (first call) or add a revision to it (next calls).
    Returns (link, revision, created)."""
    language = language if language in LANGUAGES else "en"
    evaluation = (
        SupplierEvaluation.objects.select_related("supplier", "purchase_order", "project", "owner", "reviewer",
                                                  "submitted_by", "reviewed_by", "approved_by", "closed_by")
        .get(pk=evaluation.pk)
    )
    assert_reportable(evaluation)

    generated_at = timezone.now()
    pdf = render_report_pdf(evaluation, language, generated_at, timezone_name)  # nothing persisted yet: a failure leaves no trace
    filename = report_filename(evaluation)

    with transaction.atomic():
        # Serialises concurrent generations of this evaluation (never writes to it).
        locked = SupplierEvaluation.objects.select_for_update().only("id", "status", "company_id").get(pk=evaluation.pk)
        assert_reportable(locked)
        link = SupplierEvaluationReport.objects.select_related("document").filter(evaluation_id=locked.pk).first()
        created = link is None
        if created:
            document = Document.objects.create(
                folder=quality_reports_folder(locked.company_id),
                author=user, assignee=user, created_by=user,
                file_name=filename, document_type=REPORT_DOCUMENT_TYPE,
                document_origin="INTERNAL",
                description=f"{evaluation.reference} - {evaluation.supplier.company_name}",
            )
            link = SupplierEvaluationReport.objects.create(evaluation=locked, document=document, created_by=user)
        revision = add_document_revision(
            link.document, ContentFile(pdf, name=filename), user,
            revision_type="minor",
            comment=f"Quality report generated ({evaluation.status})",
            allow_identical=True,  # an explicit regeneration is never refused as a duplicate
        )
    return link, revision, created
