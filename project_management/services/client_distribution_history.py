"""HISTORY-2: Client Distribution History -- a minimal, read-only traceability view answering
"has this Document actually been distributed to the Client, and through which exact
Transmittal(s)?" (NOT the final Internal/Supplier/Client/All Events history architecture --
see the HISTORY-2 report; that remains future lots).

Authoritative evidence stays exactly what LOT 3C/L3H-3/HISTORY-1 already established: an OUT
Transmittal actually ISSUED, carrying this Document's exact DocumentRevision as a
TransmittalItem (section 3). ClientIssue (HISTORY-1) is surfaced alongside it -- never instead
of it -- as the Client-facing identity; a historical item with client_issue=NULL (issued before
HISTORY-1 existed) legitimately still appears (section 11): its Client company/source
revision/dates come straight from the Transmittal/DocumentRevision themselves, and its Client
Revision is reported as None, never fabricated.
"""

from django.utils import timezone

from project_management.models.client_surveillance import ClientSurveillance
from project_management.models.document import DocumentOrigin
from project_management.models.transmittal import Transmittal, TransmittalEvent, TransmittalItem


def _source_revision(document_revision):
    """Section 5: INTERNAL -> revision_label, SUPPLIER -> external_revision. Never persisted
    (architecture checkpoint section 11 -- derived only); CLIENT-origin never reaches here
    since OUT eligibility already excludes it (services.transmittal_eligibility, unchanged)."""
    document = document_revision.document
    if document.document_origin == DocumentOrigin.SUPPLIER:
        return document_revision.external_revision
    # B1 / LOT 2: a technical BPE version has no label of its own: it shows the index inherited from its source revision
    if document_revision.revision_label is None and document_revision.index_revision_id:
        return document_revision.index_revision.revision_label
    return document_revision.revision_label


def is_actual_client_recipient(document, external_company_id):
    """HISTORY-4.1: "OUT + ISSUED" alone does not mean "to the Client" -- direction and
    recipient are separate concepts (checkpoint section 4). A genuine Client distribution's
    recipient must never be the exact company that issued this SUPPLIER-origin Document to us
    in the first place (Document.external_issuer_company): sending a revision back to that
    same company is a Supplier-directed OUT (e.g. the future comment-return flow), never a
    Client distribution. This is the ONE shared rule -- Client Distribution History and All
    Events CLIENT_DISTRIBUTION both call this, never two divergent definitions (section 6).

    Deliberately never a company_role/company_name check (section 7): company_role is free,
    tenant-configurable text (project_management/serializers/contact.py's own
    get_allowed_company_roles_for_request, validated against a per-company configurable list,
    never a fixed Client/Supplier enum) -- never authoritative for this. Works unchanged for
    a historical client_issue=NULL row too (section 8): it reads straight from
    Document/Transmittal, never ClientIssue.
    """
    issuer_id = document.external_issuer_company_id
    return issuer_id is None or issuer_id != external_company_id


def actual_client_recipient_q():
    """GED-WF.4.4-A: the SQL form of is_actual_client_recipient, on a TransmittalItem queryset -- the recipient
    of the OUT Transmittal is not the company that issued this document to us (a null issuer keeps the item).
    The ONE place where the rule exists in SQL, next to its Python form above."""
    from django.db.models import F, Q

    return ~Q(document_revision__document__external_issuer_company_id=F("transmittal__external_company_id"))


def list_client_distribution_history(document):
    """Every actual ISSUED OUT distribution of this Document's revisions, oldest first (so a
    retransmission naturally sorts after its own first occurrence) -- a DRAFT item is never
    included (section 3: "ready to send != sent" extends to this read path too), and nor is an
    OUT item whose recipient is not actually acting as the Client for this exact Document
    (is_actual_client_recipient, HISTORY-4.1)."""
    items = (
        TransmittalItem.objects.filter(
            document_revision__document=document,
            transmittal__direction=Transmittal.Direction.OUT,
            transmittal__status=Transmittal.Status.ISSUED,
        )
        .select_related(
            "transmittal",
            "transmittal__external_company",
            "document_revision",
            "document_revision__index_revision",
            "document_revision__document",
            "client_issue",
            "client_issue__client_company",
        )
        .order_by("transmittal__issued_at", "id")
    )

    items = [i for i in items if is_actual_client_recipient(document, i.transmittal.external_company_id)]
    send_info = _send_info_by_transmittal({i.transmittal_id for i in items})
    surveillance_by_issue = _surveillance_by_issue({i.client_issue_id for i in items if i.client_issue_id})
    seen_groups = set()

    results = []
    for item in items:
        transmittal = item.transmittal
        revision = item.document_revision
        client_issue = item.client_issue
        # A historical, pre-HISTORY-1 item (client_issue=NULL) still reports the real Client
        # company it was actually issued to -- straight from the Transmittal itself, section 11.
        client_company = (
            client_issue.client_company if client_issue else transmittal.external_company
        )
        group_key = f"issue-{item.client_issue_id}" if client_issue else f"legacy-{revision.pk}-{transmittal.external_company_id}"
        is_resend = group_key in seen_groups  # a second ISSUED occurrence of the same revision/client, never a mail retry
        seen_groups.add(group_key)
        sent = send_info.get(transmittal.pk, {})
        surveillance = surveillance_by_issue.get(item.client_issue_id)
        due_at = surveillance.due_at if surveillance else None
        due_active = bool(surveillance and due_at and surveillance.status != ClientSurveillance.Status.CLOSED)
        results.append(
            {
                "send_state": sent.get("state", SEND_STATE_NOT_SENT),
                "sent_at": sent.get("accepted_at"),
                "send_failed_at": sent.get("failed_at"),
                "send_uncertain": sent.get("uncertain", False),
                "send_attempts": sent.get("attempts", 0),
                "is_resend": is_resend,
                "surveillance_status": surveillance.status if surveillance else None,
                "due_at": due_at,
                "due_active": due_active,
                "transmittal_id": transmittal.pk,
                "transmittal_reference": transmittal.transmittal_number,
                "issued_at": transmittal.issued_at,
                "client_company_id": client_company.pk if client_company else None,
                "client_company_name": client_company.company_name if client_company else None,
                "document_revision_id": revision.pk,
                "source_revision": _source_revision(revision),
                # B1 / LOT 2: the PREL / BPE state of the physical file that was sent, and whether it is a technical BPE version
                "issue_state": revision.issue_state,
                "is_technical": revision.index_revision_id is not None,
                "client_issue_id": item.client_issue_id,
                "client_revision_label": (
                    client_issue.client_revision_label if client_issue else None
                ),
                "distribution_purpose": item.distribution_purpose,
                # GED-WF.4.4-B (D4): the item's date is only what was asked at issue time. The effective due date is
                # ClientSurveillance.due_at (`due_at`); a delay is only active on an open surveillance.
                "review_due_date": item.review_due_date,
                "is_overdue": bool(due_active and due_at < timezone.now()),
            }
        )
    return results


SEND_STATE_NOT_SENT = "NOT_SENT"
SEND_STATE_FAILED = "SEND_FAILED"
SEND_STATE_ACCEPTED = "SEND_ACCEPTED"


def _send_info_by_transmittal(transmittal_ids):
    """GED-WF.4.4-B: what the recorded send attempts say about each Transmittal -- one bulk query. A Transmittal
    only ISSUED has no send (NOT_SENT); SEND_SUCCEEDED is a transport acceptance, never a delivery proof; an
    uncertain failure is never turned into a success. Raw TransmittalEvent.metadata never leaves this function."""
    info = {}
    if not transmittal_ids:
        return info
    for event in TransmittalEvent.objects.filter(
        transmittal_id__in=transmittal_ids, event_type__in=("SEND_STARTED", "SEND_SUCCEEDED", "SEND_FAILED")
    ).order_by("id"):
        row = info.setdefault(event.transmittal_id, {"state": SEND_STATE_NOT_SENT, "attempts": 0})
        if event.event_type == "SEND_STARTED":
            row["attempts"] += 1
        elif event.event_type == "SEND_SUCCEEDED":
            row["state"] = SEND_STATE_ACCEPTED
            row["accepted_at"] = row.get("accepted_at") or event.created_at
        elif row["state"] != SEND_STATE_ACCEPTED:
            row["state"] = SEND_STATE_FAILED
            row["failed_at"] = event.created_at
            row["uncertain"] = bool((event.metadata or {}).get("uncertain"))
    return info


def _surveillance_by_issue(issue_ids):
    if not issue_ids:
        return {}
    return {s.client_issue_id: s for s in ClientSurveillance.objects.filter(client_issue_id__in=issue_ids)}


def mask_distribution_row(row):
    """GED-SEC.2: a plain document reader keeps the distribution summary (when, to which company, which revision,
    purpose, review due date) but not the Transmittal reference/id, which belong to the Transmittal detail."""
    return {**row, "transmittal_id": None, "transmittal_reference": None}
