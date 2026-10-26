"""HISTORY-1: ClientIssue -- minimal foundation service.

ClientIssue is the identity of "this exact source DocumentRevision, as distributed to this
exact Client company" -- survives retransmission (same revision + same Client company always
resolves to the SAME ClientIssue row), and is distinct per Client company when the same
revision is legitimately issued to more than one Client (L3H-History architecture checkpoint
section 4/14).

Created/reused ONLY at successful OUT finalization (services.transmittal_finalize), never at
draft creation, never for IN -- see that module's own call site.
"""

from project_management.models.client_issue import ClientIssue


def get_or_create_client_issue(*, document_revision, client_company, actor):
    """Idempotent per (document_revision, client_company): the DB-level UniqueConstraint is
    the actual source of truth: get_or_create never overwrites an existing row (so a reused
    Issue's own created_by/client_revision_label are left exactly as they were -- see HISTORY-1
    section 17, write-once preparation).
    """
    issue, _created = ClientIssue.objects.get_or_create(
        document_revision=document_revision,
        client_company=client_company,
        defaults={"created_by": actor},
    )
    return issue
