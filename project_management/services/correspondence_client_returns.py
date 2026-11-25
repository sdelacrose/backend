"""SURVEILLANCE IN LOT B: ONE source proof (an IN correspondence) for SEVERAL documents.

From an existing IN correspondence the user identifies the documents concerned, gives each an A/B/C/D result and
comments, and saves every return in ONE operation. Nothing is recreated: the correspondence, its .eml and its
attachments stay what they are (each return only references the correspondence, as `source_correspondence`), and no
document / revision / distribution is ever created -- only returns on distributions (ClientIssue) that already exist.

Per line the rules are exactly the ones of the single-document path (`record_client_return`, Lot A confirmations
included): project of the correspondence, readable document, documents.update on it, distribution of that very
document, document not withdrawn, sender company / type confirmations, return not earlier than the send. The whole
operation is ONE transaction: if any line is invalid nothing is written and every faulty line is reported by name, so
the user corrects it and submits again; a line already linked to this proof is never duplicated (second click safe).
"""
from datetime import datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from project_management.models import ClientIssue, ClientSurveillanceFollowUp, ClientSurveillanceReturn
from project_management.services.client_surveillance_followup import SAME_INDEX, next_step
from project_management.services.client_distribution_history import list_client_distribution_history
from project_management.services.client_surveillance_opening import can_process_client_return
from project_management.services.client_surveillance_return import PROOF_TYPE, record_client_return
from project_management.services.document_withdrawal import is_withdrawn
from project_management.services.transmittal_client_return import match_criteria, match_row

MAX_RESULTS = 50
MAX_LINES = 100


class _Rollback(Exception):
    pass


def _document(issue):
    return issue.document_revision.document


def proof_flags(correspondence, issue):
    """What differs between the proof and the distribution: the two things that need an explicit confirmation."""
    return {"same_company": correspondence.sender_company_id == issue.client_company_id,
            "is_surveillance": (correspondence.correspondence_type or "").lower() == PROOF_TYPE}


def default_received_at(correspondence):
    """The return date a proof suggests. A correspondence only keeps a reception DAY (no reliable time): it is never turned
    into 00:00 (that would put a return received the same day as the send before the send). The suggestion is the end of
    that day, never in the future -- the latest instant the return can have been received, marked as an estimate by
    `received_time_known` -- else now. The user can always set an explicit time."""
    now = timezone.now()
    day = correspondence.received_date
    if day is None or day >= timezone.localdate():
        return now
    end = timezone.make_aware(datetime.combine(day, datetime.max.time().replace(microsecond=0)))
    return min(end, now)


def proof_candidates(correspondence, actor, accessible_documents, reference="", revision=""):
    """Distributions the proof may concern. Several are returned as such -- nothing is chosen here. Only documents the
    user can read, of the correspondence's project, never withdrawn. Without any criterion only the distributions
    sent to the sender company are listed (the other companies appear when the user searches)."""
    wanted = match_criteria(reference, revision)
    searching = bool(wanted["reference"] or wanted["revision"])
    issues = ClientIssue.objects.filter(
        document_revision__document__folder__project_id=correspondence.project_id,
        document_revision__document__withdrawn_at__isnull=True,
        document_revision__document_id__in=accessible_documents.order_by().values("pk"),
    ).select_related("document_revision__document__folder", "client_company").order_by("-pk")
    if not searching:
        if not correspondence.sender_company_id:
            return []
        issues = issues.filter(client_company_id=correspondence.sender_company_id)
    linked = {r.client_issue_id: r for r in ClientSurveillanceReturn.objects.filter(
        source_correspondence=correspondence, supersedes__isnull=True)}
    rows = []
    for issue in issues:
        row = match_row(issue, wanted)
        if row is None:
            continue
        flags = proof_flags(correspondence, issue)
        existing = linked.get(issue.pk)
        row.update(flags, client_company_id=issue.client_company_id, already_linked=existing is not None,
                   linked_category=existing.category if existing else None, linked_return_id=existing.pk if existing else None,
                   can_process=can_process_client_return(actor, _document(issue)))
        rows.append(row)
    rows.sort(key=lambda r: (not r["same_company"], -r["client_issue_id"]))
    return rows[:MAX_RESULTS]


def _retention(followup, reason):
    """LOT C: what the user sees per line -- retained in the same action (with the next documentary step) or why the
    explicit confirmation is kept."""
    if followup is None:
        return {"retained": False, "retention_reason": reason, "followup_id": None, "next_step": None}
    step = next_step(followup)
    return {"retained": True, "retention_reason": None, "followup_id": followup.pk, "next_step": step["code"], "next_step_detail": step["detail"],
            "expected_state": followup.expected_state, "same_index": followup.client_return.category in SAME_INDEX}


def _error(line, code, detail, document=None):
    return {"client_issue_id": line.get("client_issue_id"), "document_id": document.pk if document else None,
            "reference": (document.full_document_number or document.identification_number or document.file_name) if document else None,
            "code": code, "detail": detail}


def _translate(error):
    """(business code, text) of a Django ValidationError: the explicit code when the service gave one, else the field."""
    data = error.message_dict if hasattr(error, "message_dict") else {"detail": error.messages}
    code = (data.get("code") or [None])[0]
    field = next((k for k in data if k != "code"), "detail")
    return code or {"received_at": "invalid_received_at", "category": "category_required", "comment": "comment_required",
                    "send_event": "send_event_required"}.get(field, "invalid_line"), " ".join(data.get(field, [""]))


def record_proof_returns(correspondence, actor, lines, *, received_at, source_channel, accessible_documents,
                         can_view_correspondence=None, confirm_other_company=False, confirm_other_type=False, auto_retain=True):
    """Record the returns of every line from ONE proof, in ONE transaction. Returns (results, errors): when `errors`
    is not empty nothing has been written. A line is {client_issue_id, category, comment, send_event?}."""
    if not lines:
        raise ValidationError({"lines": "Select at least one document.", "code": "no_line"})
    if len(lines) > MAX_LINES:
        raise ValidationError({"lines": f"At most {MAX_LINES} documents at once.", "code": "too_many_lines"})
    ids = [line.get("client_issue_id") for line in lines]
    results, errors = [], []
    try:
        with transaction.atomic():
            _process(correspondence, actor, lines, ids, received_at, source_channel, accessible_documents, can_view_correspondence,
                     confirm_other_company, confirm_other_type, results, errors, auto_retain)
            if errors:
                raise _Rollback  # no silent partial treatment: nothing of this submit stays
    except _Rollback:
        results = []
    return results, errors


def _process(correspondence, actor, lines, ids, received_at, source_channel, accessible_documents, can_view_correspondence,
             confirm_other_company, confirm_other_type, results, errors, auto_retain=True):
    # lock the distributions in a stable order: two simultaneous submits wait for each other, the second one then sees the first one's links
    issues = {i.pk: i for i in ClientIssue.objects.select_for_update(of=("self",)).filter(
        pk__in=[i for i in ids if isinstance(i, int)]).select_related(
        "document_revision__document__folder", "client_company").order_by("pk")}
    accessible = set(accessible_documents.order_by().filter(
        pk__in=[_document(i).pk for i in issues.values()]).values_list("pk", flat=True))
    linked = {r.client_issue_id: r for r in ClientSurveillanceReturn.objects.filter(
        source_correspondence=correspondence, supersedes__isnull=True, client_issue_id__in=list(issues))}
    seen = set()
    for line in lines:
        issue = issues.get(line.get("client_issue_id"))
        # never reveal what the user may not open: unknown, another project, unreadable or never distributed -> one generic answer
        if issue is None or _document(issue).folder.project_id != correspondence.project_id or _document(issue).pk not in accessible                 or issue.pk not in {row["client_issue_id"] for row in list_client_distribution_history(_document(issue))}:
            errors.append(_error(line, "not_available", "This document is not available for this proof."))
            continue
        document = _document(issue)
        if issue.pk in seen:
            errors.append(_error(line, "duplicate_line", "This document appears twice in the selection.", document))
            continue
        seen.add(issue.pk)
        if is_withdrawn(document):
            errors.append(_error(line, "document_withdrawn", "This document has been withdrawn.", document))
            continue
        if not can_process_client_return(actor, document):
            errors.append(_error(line, "permission_denied", "You do not have permission to record a client return on this document.", document))
            continue
        reference = document.full_document_number or document.identification_number or document.file_name
        existing = linked.get(issue.pk)
        if existing is not None:
            # second click / resubmission: the same return is never duplicated; another result must be corrected on the document
            if (line.get("category") or None) != existing.category:
                errors.append(_error(line, "already_linked", "This proof is already linked to this document with another result: correct it from the document.", document))
            else:
                followup = ClientSurveillanceFollowUp.objects.filter(client_return=existing).first()
                results.append({"client_issue_id": issue.pk, "document_id": document.pk, "reference": reference,
                                "return_id": existing.pk, "category": existing.category, "created": False, **_retention(followup, None)})
            continue
        try:
            with transaction.atomic():
                record = record_client_return(
                    issue, actor, category=line.get("category") or None, received_at=received_at, source_channel=source_channel,
                    comment=line.get("comment") or "", send_event_id=line.get("send_event") or None,
                    source_correspondence_id=correspondence.pk, can_view_correspondence=can_view_correspondence,
                    confirm_other_company=confirm_other_company, confirm_other_type=confirm_other_type, auto_retain=auto_retain)
        except ValidationError as error:
            code, detail = _translate(error)
            errors.append(_error(line, code, detail, document))
            continue
        except PermissionDenied:
            errors.append(_error(line, "permission_denied", "You cannot view this correspondence.", document))
            continue
        results.append({"client_issue_id": issue.pk, "document_id": document.pk, "reference": reference,
                        "return_id": record.pk, "category": record.category, "created": True,
                        **_retention((getattr(record, "retention", None) or {}).get("followup"), (getattr(record, "retention", None) or {}).get("reason"))})
