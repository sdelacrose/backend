"""OPTION B1 / LOT 4: APPLICABILITY per client company, read from the existing data (no stored status, no migration).

"Applicable" is a property of ONE client company and ONE logical version: it is `ClientSurveillanceFollowUp.applicable_at`, set by the verified
send of the BPE (code A, or B after its internal validation) to THAT client. It is never global: another client company may still be in
review, and a newer logical version (a later, explicit revision) does NOT inherit the applicability of an older one -- the older one stays
listed as `superseded`. Approved (internal) and "in client review" stay distinct states.

The version a follow-up makes applicable is the one of its DERIVED file (the technical BPE of the same index for code A, the new revision for
B / C / D), not the one the client reviewed. For the CURRENT logical version and each client company concerned (it received that version, or a
follow-up leads to it):
- APPLICABLE            the BPE (or B file) was really sent to this client: `applicable_at`;
- FOLLOWUP_IN_PROGRESS  the client answered, the documentary follow-up (file, validation, distribution) is not finished;
- IN_CLIENT_REVIEW      a client surveillance of that version is still open (no result retained yet);
- DISTRIBUTED           sent, no client review to wait for (information only).
Read only: nothing is written.
"""
from project_management.models import ClientIssue, ClientSurveillance, ClientSurveillanceFollowUp, DocumentRevision
from project_management.services.client_distribution_history import is_actual_client_recipient
from project_management.services.document_logical_version import get_current_logical_revision, logical_info

APPLICABLE = "APPLICABLE"
FOLLOWUP_IN_PROGRESS = "FOLLOWUP_IN_PROGRESS"
IN_CLIENT_REVIEW = "IN_CLIENT_REVIEW"
DISTRIBUTED = "DISTRIBUTED"


def client_applicability(document, include_transmittal=True):
    """{"current": [...], "superseded": [...]} -- one entry per client company, see the module docstring."""
    revisions = list(DocumentRevision.objects.filter(document=document))
    current = get_current_logical_revision(document)
    if current is None:
        return {"current": [], "superseded": []}
    info = logical_info(revisions)
    labels = {r.pk: r.revision_label for r in revisions}

    def logical_of(revision_id):
        return info.get(revision_id, (None, None))

    issues = [i for i in ClientIssue.objects.filter(document_revision__document=document).select_related("client_company")
              if is_actual_client_recipient(document, i.client_company_id)]
    companies = {i.client_company_id: i.client_company for i in issues}
    surveillances = {s.client_issue_id: s for s in ClientSurveillance.objects.filter(client_issue__in=[i.pk for i in issues])}
    followups = list(ClientSurveillanceFollowUp.objects.filter(document=document).select_related(
        "surveillance__client_issue", "closing_send_event__transmittal", "client_return"))

    def followup_logical(followup):
        revision_id = followup.derived_revision_id or followup.surveillance.client_issue.document_revision_id
        return logical_of(revision_id)

    def followup_entry(followup):
        logical_id, number = followup_logical(followup)
        entry = {
            "followup_id": followup.pk, "category": followup.client_return.category, "code": followup.client_return.code_snapshot,
            "applicable_at": followup.applicable_at, "derived_revision_id": followup.derived_revision_id,
            "logical_revision_id": logical_id, "logical_version_number": number, "revision_label": labels.get(logical_id),
        }
        if include_transmittal and followup.closing_send_event_id:
            entry["transmittal_reference"] = followup.closing_send_event.transmittal.transmittal_number
        return entry

    # the companies concerned by the CURRENT version: those that received it, and those whose follow-up leads to it
    concerned = {}
    for issue in issues:
        if logical_of(issue.document_revision_id)[0] == current.pk:
            concerned.setdefault(issue.client_company_id, []).append(issue)
    for followup in followups:
        if followup_logical(followup)[0] == current.pk and followup.client_company_id in companies:
            concerned.setdefault(followup.client_company_id, [])

    current_result = []
    for company_id in sorted(concerned, key=lambda cid: companies[cid].company_name or ""):
        company_followups = [f for f in followups if f.client_company_id == company_id and followup_logical(f)[0] == current.pk]
        applicable = sorted((f for f in company_followups if f.applicable_at), key=lambda f: f.applicable_at)
        base = {"client_company_id": company_id, "client_company_name": companies[company_id].company_name,
                "logical_revision_id": current.pk, "logical_version_number": logical_of(current.pk)[1], "revision_label": current.revision_label}
        if applicable:
            current_result.append({**base, "state": APPLICABLE, **{k: v for k, v in followup_entry(applicable[-1]).items() if k not in base}})
        elif any(f.closed_at is None for f in company_followups):
            current_result.append({**base, "state": FOLLOWUP_IN_PROGRESS, "applicable_at": None})
        elif any(surveillances.get(i.pk) is not None and surveillances[i.pk].status != ClientSurveillance.Status.CLOSED for i in concerned[company_id]):
            current_result.append({**base, "state": IN_CLIENT_REVIEW, "applicable_at": None})
        else:
            current_result.append({**base, "state": DISTRIBUTED, "applicable_at": None})

    superseded = [
        {"client_company_id": f.client_company_id, "client_company_name": companies[f.client_company_id].company_name if f.client_company_id in companies else None,
         **followup_entry(f)}
        for f in followups if f.applicable_at and followup_logical(f)[0] != current.pk
    ]
    superseded.sort(key=lambda e: (e["applicable_at"], e["followup_id"]), reverse=True)
    return {"current": current_result, "superseded": superseded}
