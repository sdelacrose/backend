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

from project_management.models.document import DocumentOrigin
from project_management.models.transmittal import Transmittal, TransmittalItem
from project_management.services.transmittal_finalize import is_review_overdue


def _source_revision(document_revision):
    """Section 5: INTERNAL -> revision_label, SUPPLIER -> external_revision. Never persisted
    (architecture checkpoint section 11 -- derived only); CLIENT-origin never reaches here
    since OUT eligibility already excludes it (services.transmittal_eligibility, unchanged)."""
    document = document_revision.document
    if document.document_origin == DocumentOrigin.SUPPLIER:
        return document_revision.external_revision
    return document_revision.revision_label


def list_client_distribution_history(document):
    """Every actual ISSUED OUT distribution of this Document's revisions, oldest first (so a
    retransmission naturally sorts after its own first occurrence) -- a DRAFT item is never
    included (section 3: "ready to send != sent" extends to this read path too)."""
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
            "document_revision__document",
            "client_issue",
            "client_issue__client_company",
        )
        .order_by("transmittal__issued_at", "id")
    )

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
        results.append(
            {
                "transmittal_id": transmittal.pk,
                "transmittal_reference": transmittal.transmittal_number,
                "issued_at": transmittal.issued_at,
                "client_company_id": client_company.pk if client_company else None,
                "client_company_name": client_company.company_name if client_company else None,
                "document_revision_id": revision.pk,
                "source_revision": _source_revision(revision),
                "client_issue_id": item.client_issue_id,
                "client_revision_label": (
                    client_issue.client_revision_label if client_issue else None
                ),
                "distribution_purpose": item.distribution_purpose,
                "review_due_date": item.review_due_date,
                "is_overdue": is_review_overdue(item.review_due_date),
            }
        )
    return results
