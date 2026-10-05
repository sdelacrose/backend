"""L3G-1 + L3G-2: Supplier Surveillance final evidence -- metadata snapshot builder (L3G-1) and
PDF rendering/storage (L3G-2).

L3G-1 part: validates that a Surveillance is CLOSED and builds the immutable, identity-sensitive
evidence_metadata snapshot (L3G architecture checkpoint section 9/10/11, L3G-1 section 6) -- the
detailed Q&A stays authoritative in template_snapshot + SupplierSurveillanceResponse and is
deliberately never duplicated into evidence_metadata (section 11); it is instead read fresh, at
render time, directly from those same two already-immutable sources (L3G-2 section 13/27).

L3G-2 part: `generate_supplier_surveillance_evidence()` is the ONE generation path (section 4/6/
23) -- idempotent (existing Evidence is returned as-is, never regenerated/replaced, section 6),
and deliberately never wrapped in the SAME transaction as closure (section 3/5): rendering and
hashing happen first, outside any transaction (so a rendering failure leaves nothing behind to
clean up), and only the already-rendered bytes are persisted inside one small atomic block.

Everything here is server-derived from already-persisted, authoritative state (section 9) --
nothing is accepted as input from the frontend.
"""

import hashlib
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone
from weasyprint import HTML

from project_management.models.supplier_surveillance import SupplierSurveillance
from project_management.models.supplier_surveillance_evidence import (
    SupplierSurveillanceEvidence,
)
from project_management.models.supplier_surveillance_review import (
    SupplierSurveillanceEvent,
    SupplierSurveillanceResponse,
)
from project_management.services.supplier_surveillance_review import record_event

EventType = SupplierSurveillanceEvent.EventType
CheckValue = SupplierSurveillanceResponse.CheckValue
CHECK_VALUE_LABELS = dict(CheckValue.choices)


def assert_surveillance_closed(surveillance):
    """L3G-1 section 8: final evidence metadata may only be built for a CLOSED Surveillance."""
    if surveillance.status != SupplierSurveillance.Status.CLOSED:
        raise ValidationError(
            "Final evidence can only be built for a closed Supplier Surveillance."
        )


def _iso(value):
    """L3G-1: evidence_metadata is a plain JSONField (same as template_snapshot/
    approval_snapshot/evidence_snapshot elsewhere in this app, none of which carry a custom
    encoder) -- timestamps are frozen as ISO-8601 strings here, the same convention
    services.supplier_surveillance_template.build_template_snapshot already uses for its own
    `captured_at`, rather than storing a raw datetime the JSON backend cannot serialize."""
    return value.isoformat() if value is not None else None


def _actor_snapshot(user):
    """L3G-1 section 7: the smallest existing identity convention (same null-safety / display-
    name-fallback shape as serializers.document_register.user_reference), extended with email
    since evidence_metadata needs it. Never fails because an optional actor FK is null."""
    if user is None:
        return None
    return {
        "id": user.id,
        "name": (user.full_name or "").strip() or user.email,
        "email": user.email,
    }


def _reviewer_snapshot(assignment):
    return {
        "assignment_id": assignment.id,
        "reviewer": _actor_snapshot(assignment.reviewer),
        "status": assignment.status,
        "assigned_at": _iso(assignment.assigned_at),
        "started_at": _iso(assignment.started_at),
        "completed_at": _iso(assignment.completed_at),
    }


def build_evidence_metadata(surveillance):
    """L3G-1 section 6: the full immutable identity-sensitive snapshot.

    Reads exclusively from the Surveillance's own exact document_revision (never "latest
    revision", section 12) and its frozen template_snapshot (never the live Template, section
    10). Supports 1..N reviewer assignments (section 11/L3D).
    """
    assert_surveillance_closed(surveillance)

    revision = surveillance.document_revision
    document = revision.document
    project = surveillance.project
    snapshot = surveillance.template_snapshot or {}
    supplier_company = document.external_issuer_company

    return {
        "surveillance": {
            "id": surveillance.id,
            "reference": surveillance.reference,
            "created_at": _iso(surveillance.created_at),
            "closed_at": _iso(surveillance.closed_at),
            "result": surveillance.result,
            "new_revision_required": surveillance.new_revision_required,
            "closure_comment": surveillance.closure_comment,
        },
        "project": {
            "id": project.id if project else None,
            "name": project.project_name if project else None,
            "code": project.project_code if project else None,
        },
        "document": {
            "id": document.id,
            "document_revision_id": revision.id,
            "title": document.file_name,
            "document_type": document.document_type,
            "discipline": document.discipline,
            "supplier_transmittal_category": document.supplier_transmittal_category,
        },
        "supplier": {
            "company_id": supplier_company.id if supplier_company else None,
            "company_name": supplier_company.company_name if supplier_company else None,
            "supplier_reference": document.identification_number,
            "supplier_revision": revision.external_revision,
        },
        "template": {
            "template_id": snapshot.get("template_id"),
            "template_name": snapshot.get("template_name"),
            "template_version": snapshot.get("template_version"),
        },
        "actors": {
            "creator": _actor_snapshot(surveillance.created_by),
            "closer": _actor_snapshot(surveillance.closed_by),
            "reviewers": [
                _reviewer_snapshot(assignment)
                for assignment in surveillance.reviewer_assignments.all()
            ],
        },
    }


def build_evidence_filename(surveillance):
    """L3G-1 section 16: {surveillance.reference}_{supplier_reference}_{supplier_revision}.pdf,
    sanitized the same way the existing generated-PDF conventions already do (offer_pdf.py's own
    Path(...).name). Blank Supplier Ref/Revision are dropped rather than rendered literally
    (never redesigns Surveillance reference numbering, section 16)."""
    document = surveillance.document_revision.document
    parts = [surveillance.reference]
    if document.identification_number:
        parts.append(document.identification_number)
    if surveillance.document_revision.external_revision:
        parts.append(surveillance.document_revision.external_revision)
    return Path("_".join(parts) + ".pdf").name


# ---------------------------------------------------------------------------
# L3G-2: report rendering context (section 13/14/15/27/28/29).
# ---------------------------------------------------------------------------


def _response_display_value(item, response):
    """L3G-2 section 14/29: a CHECK item renders its human-readable label (never the raw enum
    value), a TEXT/TEXTAREA item renders its free text. A legitimately missing response (no
    answer row at all, or a blank text answer) renders a neutral em-dash -- never invented,
    never treated as a failure (section 14)."""
    if response is None:
        return "—"
    if item.get("type") == "CHECK":
        return CHECK_VALUE_LABELS.get(response.check_value, "—")
    text = (response.text_value or "").strip()
    return text or "—"


def _build_report_sections(surveillance):
    """L3G-2 section 13/15/27/28: the detailed Q&A, rendered directly from the frozen
    template_snapshot (never the live Template -- section 28, it may be edited or deleted after
    closure) merged with every Reviewer's persisted Responses (never evidence_metadata, which
    deliberately never duplicates the Q&A -- L3G-1 section 11). One row per Reviewer per Control
    Point (section 15) -- a single-reviewer Surveillance therefore renders exactly one row per
    item, with no visible duplication at all."""
    snapshot = surveillance.template_snapshot or {}
    assignments = list(
        surveillance.reviewer_assignments.select_related("reviewer").prefetch_related("responses")
    )
    reviewer_rows = [
        {
            "reviewer": _actor_snapshot(assignment.reviewer),
            "responses_by_item": {
                response.item_snapshot_id: response for response in assignment.responses.all()
            },
        }
        for assignment in assignments
    ]

    sections = []
    for section in snapshot.get("sections", []):
        items = []
        for item in section.get("items", []):
            item_id = item.get("id")
            reviewer_entries = [
                {
                    "reviewer_name": (
                        row["reviewer"]["name"] if row["reviewer"] else "—"
                    ),
                    "display_value": _response_display_value(
                        item, row["responses_by_item"].get(item_id)
                    ),
                }
                for row in reviewer_rows
            ]
            items.append(
                {
                    "label": item.get("label"),
                    "description": item.get("description"),
                    "required": item.get("required", False),
                    "reviewer_entries": reviewer_entries,
                }
            )
        sections.append(
            {
                "title": section.get("title"),
                "description": section.get("description"),
                "items": items,
            }
        )
    return sections


def _display_dt(value):
    """Human-readable timestamp for the printed report. Returns None (rendered as an em-dash by
    the template) for a null optional timestamp (section 29) -- never crashes."""
    return value.strftime("%d %b %Y, %H:%M") if value else None


def _build_reviewer_summary(surveillance):
    """L3G-2 section 12: one row per Reviewer with pretty-printed timestamps. Built from the
    live reviewer_assignments rows rather than by re-parsing evidence_metadata's own ISO
    strings -- the same underlying, already-immutable-once-CLOSED data (L3G-1 section 7/formerly
    _reviewer_snapshot), just not round-tripped through JSON a second time for display. Must
    NEVER mutate the `metadata` dict passed into build_evidence_report_context -- that exact
    object is what gets persisted verbatim as evidence_metadata."""
    assignments = surveillance.reviewer_assignments.select_related("reviewer")
    return [
        {
            "reviewer": _actor_snapshot(assignment.reviewer),
            "status": assignment.status,
            "assigned_at_display": _display_dt(assignment.assigned_at),
            "started_at_display": _display_dt(assignment.started_at),
            "completed_at_display": _display_dt(assignment.completed_at),
        }
        for assignment in assignments
    ]


def build_evidence_report_context(surveillance, metadata, *, generated_at):
    """L3G-2 section 9-18: the full render context for the report template. `metadata` is the
    already-built, frozen evidence_metadata (identity); `sections`/`reviewer_summary` are read
    fresh from template_snapshot + persisted Responses/Assignments (section 13) -- distinct
    sources, deliberately never merged into one JSON blob (L3G-1 section 11), and `metadata`
    itself is only ever read here, never mutated (it is the exact object persisted verbatim)."""
    return {
        "metadata": metadata,
        "sections": _build_report_sections(surveillance),
        "reviewer_summary": _build_reviewer_summary(surveillance),
        "generated_at_display": _display_dt(generated_at),
        "created_at_display": _display_dt(surveillance.created_at),
        "closed_at_display": _display_dt(surveillance.closed_at),
    }


def render_supplier_surveillance_report_html(surveillance, metadata, *, generated_at):
    return render_to_string(
        "supplier_surveillance_report.html",
        build_evidence_report_context(surveillance, metadata, generated_at=generated_at),
    )


def render_supplier_surveillance_report_pdf(surveillance, metadata, *, generated_at):
    html_content = render_supplier_surveillance_report_html(
        surveillance, metadata, generated_at=generated_at
    )
    return HTML(string=html_content).write_pdf()


# ---------------------------------------------------------------------------
# L3G-2: generation orchestration (section 4/5/6/23).
# ---------------------------------------------------------------------------


def generate_supplier_surveillance_evidence(*, surveillance, actor):
    """L3G-2 section 4: the ONE generation path, used both by the automatic post-closure attempt
    and by the manual retry endpoint (section 23) -- never a second path.

    Idempotent (section 6): an existing Evidence row is returned as-is, never regenerated,
    overwritten, or replaced. Atomicity (section 5): rendering + hashing happen first, outside
    any transaction (a rendering failure therefore leaves nothing to clean up); the PDF bytes are
    then written to storage, and ONLY the final Evidence row + PDF_GENERATED event are wrapped in
    one short transaction. If that transaction fails because a concurrent call already created
    the Evidence row first (IntegrityError on the OneToOne), the just-written, now-orphaned
    storage file is deleted and the winning call's Evidence row is returned instead -- never a
    second row, never a crash, never an orphaned file (section 5/6/32-F/G).
    """
    assert_surveillance_closed(surveillance)

    existing = SupplierSurveillanceEvidence.objects.filter(surveillance=surveillance).first()
    if existing is not None:
        return existing

    # Pure/read-only steps: nothing persisted yet, so a failure here (e.g. WeasyPrint raising)
    # leaves no partial state at all (section 33).
    metadata = build_evidence_metadata(surveillance)
    generated_at = timezone.now()
    pdf_bytes = render_supplier_surveillance_report_pdf(
        surveillance, metadata, generated_at=generated_at
    )
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    filename = build_evidence_filename(surveillance)

    evidence = SupplierSurveillanceEvidence(
        surveillance=surveillance,
        generated_by=actor,
        file_sha256=file_hash,
        evidence_metadata=metadata,
    )
    # L3G-1's own model field is `generated_at = DateTimeField(auto_now_add=True)` -- Django
    # overwrites this on every first save regardless of what is assigned here, so the value
    # actually persisted to the row is set independently, a few milliseconds after the one
    # embedded in the rendered PDF content above (L3G-2 section 19 -- reported as exact, by
    # design, non-material behavior rather than silently papered over; loosening auto_now_add to
    # make them byte-identical would need a migration, which section 39 says to stop and ask
    # about rather than add here).
    evidence.generated_at = generated_at

    # FieldFile.save(..., save=False) only writes to storage; it does not touch the database.
    evidence.file.save(filename, ContentFile(pdf_bytes), save=False)
    try:
        with transaction.atomic():
            evidence.save()
            record_event(
                surveillance,
                EventType.PDF_GENERATED,
                actor,
                metadata={
                    "evidence_id": evidence.id,
                    "file_sha256": file_hash,
                    "filename": filename,
                },
            )
    except IntegrityError:
        # A concurrent call (e.g. automatic generation racing a manual retry click) won the
        # OneToOne -- clean up our own now-orphaned file and return the winner's row, never a
        # second Evidence/event (section 5/6).
        evidence.file.delete(save=False)
        winner = SupplierSurveillanceEvidence.objects.filter(surveillance=surveillance).first()
        if winner is not None:
            return winner
        raise
    except Exception:
        # Any other DB failure during persistence: no misleading partial Evidence/event may
        # survive it (section 5/35) -- delete the orphaned file and propagate.
        evidence.file.delete(save=False)
        raise
    return evidence
