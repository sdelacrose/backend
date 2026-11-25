"""GED-WF.4.4-A: client RESULT and client DUE DATE of the document register (read only, nothing stored).

Source of truth, never duplicated: the ClientSurveillance rows of the CURRENT revision (one per ClientIssue = revision x
client company), only for the companies that really act as the client (client_distribution_history.
is_actual_client_recipient). A result is the one RETAINED at closure (ClientSurveillance.result, code and labels frozen on
the retained return, never recomputed from the current configuration); a return received on a surveillance that is not
closed is only offered as a provisional, clearly separate value; a late return (recorded after the closure) is only flagged,
never the retained result. A legacy REVIEWED result is reported as such, never as an acceptance. The due date is
ClientSurveillance.due_at only (never TransmittalItem.review_due_date) and a CLOSED surveillance is never overdue.

When several client companies received the same revision there is ONE ENTRY PER CLIENT -- no result is picked for them.
Summary data only (GED-SEC.2): no comment, actor, file, download link or Transmittal reference is ever returned here.
"""
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.utils import timezone

from project_management.models import ClientSurveillance, ClientSurveillanceEvent, ClientSurveillanceReturn
from project_management.models.document import DocumentOrigin
from project_management.services.client_distribution_history import is_actual_client_recipient
from project_management.services.document_register_status import annotate_register_control_status

STATUS = ClientSurveillance.Status
RETAINED_CATEGORIES = ("ACCEPTED", "ACCEPTED_WITH_COMMENTS", "COMMENTED", "REJECTED")
LEGACY_REVIEWED = "LEGACY_REVIEWED"  # a closed surveillance whose result is the pre-4.3 REVIEWED: to be qualified
NO_RESULT = "NONE"  # under surveillance, no retained result (yet)
DUE_FILTERS = ("overdue", "upcoming")


def _values(value):
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v]
    return [v for v in str(value or "").split(",") if v]


def _surveillances_of_current_revision():
    """ClientSurveillance of the current revision of the outer Document, for the real client companies only."""
    # B1 / LOT 4: the surveillances of the CURRENT LOGICAL version (its revision and its technical BPE versions)
    return ClientSurveillance.objects.filter(
        Q(client_issue__document_revision_id=OuterRef("logical_revision_id"))
        | Q(client_issue__document_revision__index_revision_id=OuterRef("logical_revision_id")),
    ).exclude(client_issue__document_revision__document__external_issuer_company_id=F("client_issue__client_company_id"))


def _has(**conditions):
    return Q(Exists(_surveillances_of_current_revision().filter(**conditions)))


# --- Server-side filters -------------------------------------------------------------------------


def filter_by_client_result(queryset, value):
    """Stable categories (not the project code), LEGACY_REVIEWED, or NONE. Several values are OR'd."""
    queryset = annotate_register_control_status(queryset)
    condition = Q()
    for item in _values(value):
        if item in RETAINED_CATEGORIES:
            condition |= _has(status=STATUS.CLOSED, result=item)
        elif item == LEGACY_REVIEWED:
            condition |= _has(status=STATUS.CLOSED, result=ClientSurveillance.Result.REVIEWED)
        elif item == NO_RESULT:
            # at least one client of the current revision has no retained (nor legacy) result yet
            undecided = _surveillances_of_current_revision().exclude(
                status=STATUS.CLOSED, result__in=(*RETAINED_CATEGORIES, ClientSurveillance.Result.REVIEWED))
            condition |= Q(Exists(undecided))
    return queryset.filter(condition) if condition else queryset.none()


def _open_has(**conditions):
    return Q(Exists(_surveillances_of_current_revision().exclude(status=STATUS.CLOSED).filter(**conditions)))


def filter_by_client_due(queryset, value):
    """overdue: an OPEN surveillance past its due_at. upcoming: an open surveillance not yet due. Closed never counts."""
    queryset = annotate_register_control_status(queryset)
    now = timezone.now()
    condition = Q()
    for item in _values(value):
        if item == "overdue":
            condition |= _open_has(due_at__lt=now)
        elif item == "upcoming":
            condition |= _open_has(due_at__gte=now)
    return queryset.filter(condition) if condition else queryset.none()


def annotate_client_sort_keys(queryset):
    """client_due_at (earliest due date of an open surveillance) and client_result (retained category, A..D order)."""
    if "client_due_at" in queryset.query.annotations:
        return queryset
    queryset = annotate_register_control_status(queryset)
    open_surveillances = _surveillances_of_current_revision().exclude(status=STATUS.CLOSED).exclude(due_at__isnull=True)
    retained = _surveillances_of_current_revision().filter(status=STATUS.CLOSED, result__in=RETAINED_CATEGORIES)
    return queryset.annotate(
        client_due_at=Subquery(open_surveillances.order_by("due_at").values("due_at")[:1]),
        client_result=Subquery(retained.order_by("client_issue__client_company_id").values("result")[:1]),
    )


# --- Per-row summary (one query per kind for the whole page, never per document) -----------------


def _code(record):
    return {
        "category": record.category, "code": record.code_snapshot or None,
        "label_en": record.label_en_snapshot, "label_fr": record.label_fr_snapshot, "label_de": record.label_de_snapshot,
    }


def build_client_register_map(documents):
    """{document_id: summary} for a page of Documents annotated with current_revision_id; absent = no surveillance."""
    # B1 / LOT 4: keyed by the CURRENT LOGICAL revision (fallback: the latest row); its technical BPE versions belong to it
    revision_to_document = {
        (getattr(doc, "logical_revision_id", None) or doc.current_revision_id): doc for doc in documents
        if getattr(doc, "current_revision_id", None) and doc.document_origin != DocumentOrigin.CLIENT
    }
    if not revision_to_document:
        return {}
    surveillances = list(
        ClientSurveillance.objects.filter(
            Q(client_issue__document_revision_id__in=revision_to_document)
            | Q(client_issue__document_revision__index_revision_id__in=revision_to_document))
        .select_related("client_issue__client_company", "client_issue__document_revision__document")
    )
    surveillances = [
        s for s in surveillances
        if is_actual_client_recipient(s.client_issue.document_revision.document, s.client_issue.client_company_id)
    ]
    if not surveillances:
        return {}
    ids = [s.pk for s in surveillances]
    issue_ids = [s.client_issue_id for s in surveillances]

    retained_ids = {}
    for event in ClientSurveillanceEvent.objects.filter(
        surveillance_id__in=ids, status=STATUS.CLOSED, event_type=ClientSurveillanceEvent.EventType.STATUS, data__isnull=False,
    ).order_by("pk").only("surveillance_id", "data"):
        retained_ids[event.surveillance_id] = (event.data or {}).get("retained_return")
    # the live returns (not replaced by a correction) of those issues: provisional value and late flag
    live_returns = list(
        ClientSurveillanceReturn.objects.filter(client_issue_id__in=issue_ids)
        .exclude(Exists(ClientSurveillanceReturn.objects.filter(supersedes_id=OuterRef("pk"))))
        .only("client_issue_id", "category", "code_snapshot", "label_en_snapshot", "label_fr_snapshot",
              "label_de_snapshot", "received_at", "recorded_at")
    )
    retained_records = {
        r.pk: r for r in ClientSurveillanceReturn.objects.filter(pk__in=[v for v in retained_ids.values() if v]).only(
            "category", "code_snapshot", "label_en_snapshot", "label_fr_snapshot", "label_de_snapshot")
    }
    by_issue = {}
    for record in live_returns:
        by_issue.setdefault(record.client_issue_id, []).append(record)

    now = timezone.now()
    entries = {}
    for s in surveillances:
        closed = s.status == STATUS.CLOSED
        records = by_issue.get(s.client_issue_id, [])
        result = None
        if closed and s.result == ClientSurveillance.Result.REVIEWED:
            result = {"category": None, "legacy_reviewed": True}
        elif closed and s.result in RETAINED_CATEGORIES:
            retained = retained_records.get(retained_ids.get(s.pk))
            frozen = _code(retained) if retained is not None and retained.category == s.result else {"category": s.result}
            result = {**frozen, "category": s.result, "legacy_reviewed": False}
        provisional = None
        if not closed:
            categorised = sorted((r for r in records if r.category), key=lambda r: (r.received_at, r.recorded_at))
            if categorised:
                provisional = {**_code(categorised[-1]), "received_at": categorised[-1].received_at}
        entries.setdefault(s.client_issue.document_revision.document_id, []).append({
            "client_issue_id": s.client_issue_id,
            "client_company_id": s.client_issue.client_company_id,
            "client_company_name": s.client_issue.client_company.company_name,
            "status": s.status,
            "due_at": s.due_at,
            "overdue": bool(not closed and s.due_at and s.due_at < now),
            "result": result,
            "provisional": provisional,
            "late_return": bool(closed and s.closed_at and any(r.recorded_at > s.closed_at for r in records)),
        })
    return {document_id: summarize(rows) for document_id, rows in entries.items()}


def summarize(rows):
    rows = sorted(rows, key=lambda r: (r["client_company_name"] or "", r["client_issue_id"]))
    open_due = sorted(r["due_at"] for r in rows if r["status"] != STATUS.CLOSED and r["due_at"])
    closed_due = sorted(r["due_at"] for r in rows if r["status"] == STATUS.CLOSED and r["due_at"])
    return {
        "client_count": len(rows),
        "results": rows,
        "due_at": (open_due or closed_due or [None])[0],
        "due_active": bool(open_due),
        "overdue": any(r["overdue"] for r in rows),
    }


def client_register_for_document(document):
    """Single-object fallback (detail endpoint): the same builder over one document."""
    from project_management.services.document_register_status import _ensure_control_annotations

    _ensure_control_annotations(document)
    return build_client_register_map([document]).get(document.pk)
