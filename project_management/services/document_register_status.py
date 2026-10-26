"""L3F: Document Register Control Status -- Internal / Supplier / Client, each independent.

A Document is never represented by one generic status trying to combine all three business
processes (L3F section 1). All three are DERIVED (never stored, never duplicated through
signals, L3F section 25/39): Internal from the authoritative DocumentWorkflow of the CURRENT
revision (services.document_workflow), Supplier from the authoritative SupplierSurveillance
attached to the CURRENT revision (services.supplier_surveillance*), Client from the Document's
existing transmittal_category metadata (services.transmittal_eligibility.
document_transmittal_purpose) gated by the relevant upstream status and actual OUT Transmittal
evidence for the exact CURRENT revision (models.transmittal).

Everything here is CURRENT-REVISION-ONLY (L3F section 3): an older revision's completed
workflow/surveillance/transmission never overrides what the CURRENT revision actually shows.
"current revision" is exactly services.document_workflow.get_current_revision's own definition
(latest by created_at/id) -- the same one already authoritative for revision_label/wf_status
elsewhere in this codebase; never redefined here.

KNOWN GAP (L3F section 8, reported rather than fabricated): the authoritative DocumentWorkflow
architecture (models.document_workflow.DocumentWorkflow.Status) has no COMMENTED outcome --
only DRAFT/VERIFY/APPROVE/APPROVED/REJECTED. InternalControlStatus.COMMENTED exists below purely
as reserved target vocabulary (L3F section 5's canonical list) and is NEVER produced by
compute_internal_control_status. Do not fabricate it from Document.status or any other
unrelated field.

KNOWN ARCHITECTURAL LIMIT (discovered during L3F, reported rather than silently worked around):
services.transmittal_eligibility.validate_item_eligibility currently rejects any non-INTERNAL
document from joining an OUT Transmittal ("Only internal-origin documents may be distributed
through an outgoing transmittal."). compute_client_control_status below is written generically
against the real TransmittalItem/Transmittal relationship (never a new boolean field) so it will
correctly pick up a SUPPLIER-origin revision's actual OUT transmission the moment some future
lot allows one to exist -- but under the CURRENT architecture a SUPPLIER-origin revision can
never actually acquire that evidence through the existing Transmittal creation flow, so its
Client Status can reach TO_ISSUE but never ISSUED/UNDER_REVIEW in practice today. This is a
pre-existing Transmittal-architecture restriction, intentionally not broadened here (L3F section
2/45 scope boundary).
"""

from django.db.models import Exists, OuterRef, Q, Subquery

from project_management.models.document import Document, DocumentOrigin
from project_management.models.document_workflow import DocumentWorkflow
from project_management.models.supplier_surveillance import SupplierSurveillance
from project_management.models.transmittal import Transmittal, TransmittalItem
from project_management.services.document_workflow import annotate_current_workflow
from project_management.services.transmittal_eligibility import (
    document_transmittal_purpose,
)

WFStatus = DocumentWorkflow.Status
SurveillanceStatus = SupplierSurveillance.Status
SurveillanceResult = SupplierSurveillance.Result
SupplierCategory = Document.SupplierTransmittalCategory


class InternalControlStatus:
    """L3F section 5. COMMENTED is reserved vocabulary only -- see module docstring gap note."""

    DRAFT = "DRAFT"
    IN_PROGRESS = "IN_PROGRESS"
    TO_REVIEW = "TO_REVIEW"
    TO_VERIFY = "TO_VERIFY"
    TO_APPROVE = "TO_APPROVE"
    APPROVED = "APPROVED"
    COMMENTED = "COMMENTED"
    REJECTED = "REJECTED"

    values = (DRAFT, IN_PROGRESS, TO_REVIEW, TO_VERIFY, TO_APPROVE, APPROVED, COMMENTED, REJECTED)


class SupplierControlStatus:
    """L3F/L3F.2 section 5. DRAFT: no physical main Supplier file/current revision yet (the
    Register entry exists ahead of it, L3F.2 section 4/6 -- same "entry != file" principle as
    Internal's own DRAFT, never a Supplier-specific uploaded boolean). Never confuse with
    SupplierSurveillance.status == DRAFT (Surveillance exists, execution not yet begun)."""

    DRAFT = "DRAFT"
    TO_MONITOR = "TO_MONITOR"
    IN_PROGRESS = "IN_PROGRESS"
    TO_CLOSE = "TO_CLOSE"
    REVIEWED = "REVIEWED"
    COMMENTED = "COMMENTED"
    REJECTED = "REJECTED"

    values = (DRAFT, TO_MONITOR, IN_PROGRESS, TO_CLOSE, REVIEWED, COMMENTED, REJECTED)


class ClientControlStatus:
    """L3F section 16/23. REVIEWED/COMMENTED/REJECTED are reserved for the future Client Review
    process (section 23) -- never produced by compute_client_control_status today."""

    TO_ISSUE = "TO_ISSUE"
    ISSUED = "ISSUED"
    UNDER_REVIEW = "UNDER_REVIEW"
    REVIEWED = "REVIEWED"
    COMMENTED = "COMMENTED"
    REJECTED = "REJECTED"

    values = (TO_ISSUE, ISSUED, UNDER_REVIEW, REVIEWED, COMMENTED, REJECTED)


# --- Annotation (N+1-safe: correlated subqueries only, L3F section 26) -------------------------


def annotate_register_control_status(queryset):
    """Adds, on top of services.document_workflow.annotate_current_workflow's own
    current_revision_id/wf_status: current_surveillance_status, current_surveillance_result,
    current_revision_sent_out. Idempotent, same guard pattern as annotate_current_workflow."""
    if "current_surveillance_status" in queryset.query.annotations:
        return queryset
    queryset = annotate_current_workflow(queryset)

    surveillance_of_current_revision = SupplierSurveillance.objects.filter(
        document_revision_id=OuterRef("current_revision_id")
    )
    queryset = queryset.annotate(
        current_surveillance_status=Subquery(
            surveillance_of_current_revision.values("status")[:1]
        ),
        current_surveillance_result=Subquery(
            surveillance_of_current_revision.values("result")[:1]
        ),
    )

    out_transmitted = TransmittalItem.objects.filter(
        document_revision_id=OuterRef("current_revision_id"),
        transmittal__direction=Transmittal.Direction.OUT,
        transmittal__status=Transmittal.Status.ISSUED,
    )
    queryset = queryset.annotate(current_revision_sent_out=Exists(out_transmitted))
    return queryset


# --- Per-row computation (reads annotated attributes when present, L3F section 26) -------------


def _ensure_control_annotations(document):
    """Single-object fallback (the detail/retrieve endpoint, never the list) -- same dual-path
    pattern already established by DocumentSerializer.get_revision_label/
    get_current_external_revision: prefer the list queryset's own annotations (hasattr short-
    circuits, zero extra queries), else resolve once here and cache onto the instance so a
    second compute_* call for the same object never re-queries."""
    if hasattr(document, "current_surveillance_status"):
        return
    from project_management.services.document_workflow import (
        get_current_revision,
        get_workflow_for_revision,
    )

    current_revision = get_current_revision(document)
    if not hasattr(document, "current_revision_id"):
        document.current_revision_id = current_revision.id if current_revision else None
    if not hasattr(document, "wf_status"):
        workflow = get_workflow_for_revision(current_revision) if current_revision else None
        document.wf_status = workflow.status if workflow else None

    surveillance = (
        SupplierSurveillance.objects.filter(document_revision=current_revision).first()
        if current_revision
        else None
    )
    document.current_surveillance_status = surveillance.status if surveillance else None
    document.current_surveillance_result = surveillance.result if surveillance else None

    document.current_revision_sent_out = bool(current_revision) and TransmittalItem.objects.filter(
        document_revision=current_revision,
        transmittal__direction=Transmittal.Direction.OUT,
        transmittal__status=Transmittal.Status.ISSUED,
    ).exists()


def compute_internal_control_status(document):
    """L3F section 5/6/7/8. None for a non-INTERNAL document ("—")."""
    if document.document_origin != DocumentOrigin.INTERNAL:
        return None
    _ensure_control_annotations(document)
    has_main_file = bool(getattr(document, "current_revision_id", None)) or bool(document.file)
    if not has_main_file:
        return InternalControlStatus.DRAFT
    wf_status = getattr(document, "wf_status", None)
    return {
        None: InternalControlStatus.IN_PROGRESS,
        WFStatus.DRAFT: InternalControlStatus.TO_REVIEW,
        WFStatus.VERIFY: InternalControlStatus.TO_VERIFY,
        WFStatus.APPROVE: InternalControlStatus.TO_APPROVE,
        WFStatus.APPROVED: InternalControlStatus.APPROVED,
        WFStatus.REJECTED: InternalControlStatus.REJECTED,
    }.get(wf_status, InternalControlStatus.IN_PROGRESS)


def compute_supplier_control_status(document):
    """L3F/L3F.2 section 4-15. None for a non-SUPPLIER document, or a SUPPLIER FOR_INFORMATION
    document (with a main file) and no (exceptional) Surveillance ("—").

    L3F.2 section 4/6/7: a Register entry without its physical main file/current revision yet
    is DRAFT regardless of category -- checked FIRST, exactly like Internal's own DRAFT (same
    has_main_file source, never a Supplier-specific uploaded boolean). Only once the file exists
    does the normal Supplier category/Surveillance evaluation apply.
    """
    if document.document_origin != DocumentOrigin.SUPPLIER:
        return None
    _ensure_control_annotations(document)
    has_main_file = bool(getattr(document, "current_revision_id", None)) or bool(document.file)
    if not has_main_file:
        return SupplierControlStatus.DRAFT
    surveillance_status = getattr(document, "current_surveillance_status", None)
    if surveillance_status is None:
        if document.supplier_transmittal_category == SupplierCategory.FOR_REVIEW:
            return SupplierControlStatus.TO_MONITOR
        return None
    if surveillance_status in (SurveillanceStatus.DRAFT, SurveillanceStatus.UNDER_REVIEW):
        return SupplierControlStatus.IN_PROGRESS
    if surveillance_status == SurveillanceStatus.READY_FOR_CLOSURE:
        return SupplierControlStatus.TO_CLOSE
    if surveillance_status == SurveillanceStatus.CLOSED:
        result = getattr(document, "current_surveillance_result", None)
        return {
            SurveillanceResult.REVIEWED: SupplierControlStatus.REVIEWED,
            SurveillanceResult.COMMENTED: SupplierControlStatus.COMMENTED,
            SurveillanceResult.REJECTED: SupplierControlStatus.REJECTED,
        }.get(result)
    return None


def compute_client_control_status(document, internal_status=None, supplier_status=None):
    """L3F section 16-24. None ("—") for CLIENT-origin, for a category not eligible for
    transmission (Not sent/Available/unset), or while the upstream gate is not yet satisfied.

    `internal_status`/`supplier_status` may be passed in (already computed) to avoid a second
    derivation pass when a caller already has them; otherwise computed here.
    """
    if document.document_origin == DocumentOrigin.CLIENT:
        return None
    _ensure_control_annotations(document)
    purpose = document_transmittal_purpose(document)
    if purpose is None:
        return None

    if document.document_origin == DocumentOrigin.INTERNAL:
        if internal_status is None:
            internal_status = compute_internal_control_status(document)
        gate_ok = internal_status == InternalControlStatus.APPROVED
    elif document.document_origin == DocumentOrigin.SUPPLIER:
        if supplier_status is None:
            supplier_status = compute_supplier_control_status(document)
        if supplier_status == SupplierControlStatus.DRAFT:
            # L3F.2 section 8: not physically distributable yet -- blocks the gate regardless
            # of category, even FOR_INFORMATION's own no-mandatory-Surveillance semantic below.
            gate_ok = False
        elif document.supplier_transmittal_category == SupplierCategory.FOR_INFORMATION:
            # L3F section 19: no mandatory Surveillance for FOR_INFORMATION -- the Supplier gate
            # is already considered satisfied by that existing, established semantic.
            gate_ok = True
        else:
            gate_ok = supplier_status == SupplierControlStatus.REVIEWED
    else:
        gate_ok = False

    if not gate_ok:
        return None

    sent_out = bool(getattr(document, "current_revision_sent_out", False))
    if not sent_out:
        return ClientControlStatus.TO_ISSUE
    return (
        ClientControlStatus.ISSUED
        if purpose == "FOR_INFORMATION"
        else ClientControlStatus.UNDER_REVIEW
    )


def register_control_statuses(document):
    """The three axes together, Client computed from the other two to avoid a double pass."""
    internal = compute_internal_control_status(document)
    supplier = compute_supplier_control_status(document)
    client = compute_client_control_status(document, internal, supplier)
    return {
        "internal_control_status": internal,
        "supplier_control_status": supplier,
        "client_control_status": client,
    }


# --- Server-side filters (L3F section 29) -------------------------------------------------------


def _as_values(value):
    """The MultiSelect filter bar sends one or several comma-separated values (same convention
    as document_type/discipline/status already use, L3F section 29); a plain single value (as
    the ticket's own filter examples show) still works."""
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v]
    if value is None:
        return []
    return [v for v in str(value).split(",") if v]


def _union_by_pk(queryset, single_value_filter, values):
    """Multiple selected values are OR'd together (matches ANY of them) -- same semantics as
    the existing CharInFilter fields, built as a pk union since each branch's own filter
    predicate differs structurally per value rather than being one shared field lookup."""
    if not values:
        return queryset.none()
    matched_ids = set()
    for value in values:
        matched_ids |= set(single_value_filter(queryset, value).values_list("pk", flat=True))
    return queryset.filter(pk__in=matched_ids)


def filter_by_internal_control_status(queryset, value):
    return _union_by_pk(queryset, _filter_by_one_internal_control_status, _as_values(value))


def _filter_by_one_internal_control_status(queryset, value):
    queryset = annotate_register_control_status(queryset).filter(
        document_origin=DocumentOrigin.INTERNAL
    )
    if value == InternalControlStatus.DRAFT:
        return queryset.filter(current_revision_id__isnull=True, file="")
    if value == InternalControlStatus.IN_PROGRESS:
        return queryset.filter(_has_main_file_q(), wf_status__isnull=True)
    wf_status = {
        InternalControlStatus.TO_REVIEW: WFStatus.DRAFT,
        InternalControlStatus.TO_VERIFY: WFStatus.VERIFY,
        InternalControlStatus.TO_APPROVE: WFStatus.APPROVE,
        InternalControlStatus.APPROVED: WFStatus.APPROVED,
        InternalControlStatus.REJECTED: WFStatus.REJECTED,
    }.get(value)
    if wf_status is None:
        # COMMENTED (reserved, never emitted) or an unknown value: matches nothing.
        return queryset.none()
    return queryset.filter(wf_status=wf_status)


def filter_by_supplier_control_status(queryset, value):
    return _union_by_pk(queryset, _filter_by_one_supplier_control_status, _as_values(value))


def _has_main_file_q():
    return Q(current_revision_id__isnull=False) | ~Q(file="")


def _filter_by_one_supplier_control_status(queryset, value):
    queryset = annotate_register_control_status(queryset).filter(
        document_origin=DocumentOrigin.SUPPLIER
    )
    if value == SupplierControlStatus.DRAFT:
        return queryset.filter(current_revision_id__isnull=True, file="")
    # Every other Supplier status requires the main file to exist (L3F.2 section 4/6).
    queryset = queryset.filter(_has_main_file_q())
    if value == SupplierControlStatus.TO_MONITOR:
        return queryset.filter(
            supplier_transmittal_category=SupplierCategory.FOR_REVIEW,
            current_surveillance_status__isnull=True,
        )
    if value == SupplierControlStatus.IN_PROGRESS:
        return queryset.filter(
            current_surveillance_status__in=[
                SurveillanceStatus.DRAFT, SurveillanceStatus.UNDER_REVIEW,
            ]
        )
    if value == SupplierControlStatus.TO_CLOSE:
        return queryset.filter(current_surveillance_status=SurveillanceStatus.READY_FOR_CLOSURE)
    result = {
        SupplierControlStatus.REVIEWED: SurveillanceResult.REVIEWED,
        SupplierControlStatus.COMMENTED: SurveillanceResult.COMMENTED,
        SupplierControlStatus.REJECTED: SurveillanceResult.REJECTED,
    }.get(value)
    if result is None:
        return queryset.none()
    return queryset.filter(
        current_surveillance_status=SurveillanceStatus.CLOSED,
        current_surveillance_result=result,
    )


def filter_by_client_control_status(queryset, value):
    return _union_by_pk(queryset, _filter_by_one_client_control_status, _as_values(value))


def _filter_by_one_client_control_status(queryset, value):
    queryset = annotate_register_control_status(queryset).exclude(
        document_origin=DocumentOrigin.CLIENT
    )
    eligible_category = Q(transmittal_category__iexact="for review") | Q(
        transmittal_category__iexact="for information"
    )
    queryset = queryset.filter(eligible_category)

    internal_gate = Q(document_origin=DocumentOrigin.INTERNAL, wf_status=WFStatus.APPROVED)
    supplier_gate = Q(document_origin=DocumentOrigin.SUPPLIER) & _has_main_file_q() & (
        # L3F.2 section 8: DRAFT (no main file) is excluded by the _has_main_file_q() clause
        # above regardless of category, even FOR_INFORMATION's own no-mandatory-Surveillance
        # semantic below.
        Q(supplier_transmittal_category=SupplierCategory.FOR_INFORMATION)
        | Q(
            current_surveillance_status=SurveillanceStatus.CLOSED,
            current_surveillance_result=SurveillanceResult.REVIEWED,
        )
    )
    queryset = queryset.filter(internal_gate | supplier_gate)

    if value == ClientControlStatus.TO_ISSUE:
        return queryset.filter(current_revision_sent_out=False)
    if value == ClientControlStatus.ISSUED:
        return queryset.filter(
            current_revision_sent_out=True, transmittal_category__iexact="for information"
        )
    if value == ClientControlStatus.UNDER_REVIEW:
        return queryset.filter(
            current_revision_sent_out=True, transmittal_category__iexact="for review"
        )
    # REVIEWED/COMMENTED/REJECTED: reserved for the future Client Review process, never emitted.
    return queryset.none()
