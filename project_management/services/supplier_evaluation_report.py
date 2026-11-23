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

from django.core.files.base import ContentFile
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
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
        "conclusion_text": "The supplier obtained {score} / 100, rated {rating}. {answered} of {total} criteria were scored.",
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
        "conclusion_text": "Le fournisseur obtient {score} / 100, apprécié {rating}. {answered} critères sur {total} ont été notés.",
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
        "conclusion_text": "Der Lieferant erreicht {score} / 100, eingestuft als {rating}. {answered} von {total} Kriterien wurden bewertet.",
        "EXCELLENT": "Ausgezeichnet", "GOOD": "Gut", "IMPROVEMENT_REQUIRED": "Verbesserung erforderlich",
        "UNSATISFACTORY": "Ungenügend", "yes": "Ja",
    },
}


def _name(user):
    return None if not user else ((user.full_name or "").strip() or user.email)


def _fmt_score(value):
    return None if value is None else f"{float(value):.1f}"


def _fmt_weight(value):
    return None if value in (None, "") else f"{float(value):g} %"


def _fmt_date(value):
    return value.strftime("%Y-%m-%d") if value else None


def _fmt_dt(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else None


def report_filename(evaluation):
    return f"{evaluation.reference} - Rapport.pdf"


def assert_reportable(evaluation):
    if evaluation.status not in FINAL_STATUSES:
        raise ValidationError({"detail": "The report is only available once the evaluation is approved or closed."})


def build_report_context(evaluation, language, generated_at):
    """Everything printed in the PDF, from the snapshot, the saved answers and the stored result only."""
    labels = LABELS[language]
    snapshot = evaluation.template_snapshot or {}
    answers = {r.criterion_id: r for r in evaluation.responses.all()}
    result = evaluation_scoring(evaluation)  # sections / synthesis from the frozen snapshot
    section_scores = {s["id"]: s for s in result["sections"]}
    # Approved / closed evaluations carry their stored result: that is the figure shown on screen.
    overall = evaluation.overall_score if evaluation.overall_score is not None else result["overall_score"]
    rating = evaluation.rating or result["rating"]

    sections, answered_total, criteria_total = [], 0, 0
    for section in snapshot.get("sections", []):
        rows = []
        for criterion in section.get("criteria", []):
            answer = answers.get(criterion["id"])
            criteria_total += 1
            if answer is None or (answer.score is None and not answer.not_applicable):
                shown = labels["unanswered"]
            elif answer.not_applicable:
                shown = labels["na"]
                answered_total += 1
            else:
                shown = f"{answer.score} / 5"
                answered_total += 1
            rows.append({
                "label": criterion.get("label", ""),
                "description": criterion.get("description", ""),
                "weight": _fmt_weight(criterion.get("weight")),
                "required": bool(criterion.get("required")),
                "critical": bool(criterion.get("critical")),
                "answer": shown,
                "comment": (answer.comment if answer else "") or "",
            })
        scored = section_scores.get(section["id"], {})
        sections.append({
            "title": section.get("title", ""), "weight": _fmt_weight(section.get("weight")),
            "score": _fmt_score(scored.get("score")), "rows": rows,
        })

    approval = (
        evaluation.events.filter(event_type=SupplierEvaluationEvent.EventType.APPROVED)
        .exclude(comment="").order_by("-created_at", "-id").first()
    )
    order, project = evaluation.purchase_order, evaluation.project
    score_text = _fmt_score(overall)
    conclusion = None
    if score_text is not None and rating:
        conclusion = labels["conclusion_text"].format(
            score=score_text, rating=labels[rating], answered=answered_total, total=criteria_total)
    return {
        "labels": labels,
        "language": language,
        "reference": evaluation.reference,
        "supplier": evaluation.supplier.company_name,
        "order": order.reference if order else None,
        "project": project.project_name if project else None,
        "type": labels[evaluation.evaluation_type],
        "period": (f"{_fmt_date(evaluation.period_start) or ''} → {_fmt_date(evaluation.period_end) or ''}"
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
        "submitted": f"{_fmt_dt(evaluation.submitted_at) or ''} {_name(evaluation.submitted_by) or ''}".strip() or None,
        "review_started": f"{_fmt_dt(evaluation.reviewed_at) or ''} {_name(evaluation.reviewed_by) or ''}".strip() or None,
        "approved": f"{_fmt_dt(evaluation.approved_at) or ''} {_name(evaluation.approved_by) or ''}".strip() or None,
        "closed": f"{_fmt_dt(evaluation.closed_at) or ''} {_name(evaluation.closed_by) or ''}".strip() or None,
        "generated_at": _fmt_dt(generated_at),
    }


def render_report_pdf(evaluation, language, generated_at):
    html = render_to_string("supplier_evaluation_report.html", build_report_context(evaluation, language, generated_at))
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


def generate_report(*, evaluation, user, language="en"):
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
    pdf = render_report_pdf(evaluation, language, generated_at)  # nothing persisted yet: a failure leaves no trace
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
