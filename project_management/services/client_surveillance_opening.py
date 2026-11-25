"""GED-WF.4.2: automatic opening of the client surveillance after a really successful OUT send.

A surveillance is opened ONLY from a SEND_SUCCEEDED event of an OUT transmittal, for each item whose
distribution purpose is exactly FOR_REVIEW and that already has its ClientIssue (set at issuing). Never
from a draft, an issued-only transmittal, SEND_STARTED, SEND_FAILED (certain or uncertain) or a
FOR_INFORMATION item. SMTP/Graph success proves the transport accepted the message, not the delivery.

Identity is unchanged: one ClientIssue (revision x client company) has at most one ClientSurveillance
(OneToOne). A later send of the same revision to the same client reuses it and only adds a RESENT event:
the current due date is kept (even if edited by hand) and a CLOSED surveillance is never reopened.

Idempotent: the same event can be processed any number of times (opening_send_event is unique, RESENT
events carry the send event id). Rows are locked (Transmittal, then ClientIssue) so two simultaneous
processings serialize. The e-mail is never sent again from here; a failure here never undoes the send.
"""
import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceEvent, Transmittal, TransmittalEvent, TransmittalItem,
)

logger = logging.getLogger(__name__)

STATUS = ClientSurveillance.Status
EVENT = ClientSurveillanceEvent.EventType


def iso(value):
    return value.isoformat() if value else None


def resolve_duration_days(item):
    """Frozen item duration first, then the effective project/document-type configuration; None when
    nothing usable exists (never invented). Returns (days, anomaly)."""
    if item.review_duration_days:
        return item.review_duration_days, None
    try:
        from project_management.services.document_type_review_duration import resolve_review_durations
        document = item.document_revision.document
        days = resolve_review_durations(
            project=item.transmittal.project, document_type=document.document_type,
        )["client_review_duration_effective_days"]
    except Exception:
        logger.exception("Client review duration could not be resolved for item %s", item.pk)
        return None, "duration_unresolved"
    return (days, None) if isinstance(days, int) and days > 0 else (None, "duration_unresolved")


def compute_due_at(send_event, days):
    return send_event.created_at + timedelta(days=days) if days else None


def _eligible_items(send_event):
    """The FOR_REVIEW items of the sent OUT transmittal -> (item, reason_if_skipped)."""
    from project_management.services.client_distribution_history import is_actual_client_recipient

    transmittal = send_event.transmittal
    items = transmittal.items.select_related(
        "document_revision__document", "client_issue", "transmittal__project",
    ).order_by("id")
    out = []
    for item in items:
        if item.distribution_purpose != TransmittalItem.DistributionPurpose.FOR_REVIEW:
            continue  # FOR_INFORMATION (or no purpose): never expected, never touched
        if item.client_issue_id is None:
            out.append((item, "no_client_issue"))
        elif not is_actual_client_recipient(item.document_revision.document, transmittal.external_company_id):
            out.append((item, "recipient_is_not_the_client"))
        else:
            out.append((item, None))
    return out


def _is_sendable_event(send_event):
    return (
        send_event is not None
        and send_event.event_type == TransmittalEvent.EventType.SEND_SUCCEEDED
        and send_event.transmittal.direction == Transmittal.Direction.OUT
    )


def plan_openings(send_event):
    """Read-only: what processing this event would do, per eligible item (used by the simulation)."""
    if not _is_sendable_event(send_event):
        return []
    rows = []
    for item, skipped in _eligible_items(send_event):
        row = {"item": item.pk, "document_revision": item.document_revision_id, "client_issue": item.client_issue_id}
        if skipped:
            rows.append({**row, "action": "SKIP", "anomalies": [skipped]})
            continue
        days, anomaly = resolve_duration_days(item)
        existing = ClientSurveillance.objects.filter(client_issue_id=item.client_issue_id).first()
        if existing is None or existing.opening_send_event_id is None and existing.status != STATUS.CLOSED:
            action = "OPEN"
        elif existing.opening_send_event_id == send_event.pk or existing.events.filter(
            event_type=EVENT.RESENT, data__send_event=send_event.pk
        ).exists():
            action = "ALREADY_DONE"
        else:
            action = "RESEND"
        # a resend/replay keeps the CURRENT due date of the existing surveillance (never recomputed)
        due_at = compute_due_at(send_event, days) if action == "OPEN" else (existing.due_at if existing else None)
        rows.append({
            **row, "action": action, "existing_surveillance": existing.pk if existing else None,
            "review_duration_days": days if action == "OPEN" else existing.review_duration_days, "due_at": iso(due_at),
            "anomalies": [anomaly] if anomaly else [],
        })
    return rows


def _event(surveillance, actor, event_type, data, status=None):
    return ClientSurveillanceEvent.objects.create(
        surveillance=surveillance, actor=actor, status=status or surveillance.status,
        result=surveillance.result, event_type=event_type, data=data,
    )


def _base_data(send_event, item):
    transmittal = send_event.transmittal
    return {
        "send_event": send_event.pk, "transmittal": transmittal.pk,
        "transmittal_reference": transmittal.transmittal_number, "item": item.pk,
        "document_revision": item.document_revision_id, "sent_at": iso(send_event.created_at),
    }


@transaction.atomic
def open_client_surveillances(send_event):
    """Apply the plan under locks. Returns the list of (action, surveillance_id) actually performed."""
    if not _is_sendable_event(send_event):
        return []
    Transmittal.objects.select_for_update().get(pk=send_event.transmittal_id)
    done = []
    for item, skipped in _eligible_items(send_event):
        if skipped:
            logger.warning("Client surveillance not opened for item %s: %s", item.pk, skipped)
            continue
        ClientIssue.objects.select_for_update().get(pk=item.client_issue_id)
        existing = ClientSurveillance.objects.select_for_update().filter(client_issue_id=item.client_issue_id).first()
        data = _base_data(send_event, item)
        if existing is None or (existing.opening_send_event_id is None and existing.status != STATUS.CLOSED):
            days, anomaly = resolve_duration_days(item)
            due_at = compute_due_at(send_event, days)
            if anomaly:
                logger.error("Client surveillance for item %s opened without due date: %s", item.pk, anomaly)
                data["anomaly"] = anomaly
            fields = {
                "opening_transmittal_item": item, "opening_send_event": send_event,
                "review_duration_days": days, "due_at": due_at, "opened_at": send_event.created_at,
            }
            if existing is None:
                surveillance = ClientSurveillance.objects.create(
                    client_issue_id=item.client_issue_id, created_by=send_event.actor, status=STATUS.UNDER_REVIEW, **fields)
            else:  # created by hand before any send, not yet opened by one: adopt it, keep its history
                surveillance = existing
                for name, value in fields.items():
                    setattr(surveillance, name, value)
                if surveillance.status == STATUS.DRAFT:
                    surveillance.status = STATUS.UNDER_REVIEW
                surveillance.save()
            _event(surveillance, send_event.actor, EVENT.OPENED, {**data, "review_duration_days": days, "due_at": iso(due_at)})
            done.append(("OPEN", surveillance.pk))
        elif existing.opening_send_event_id == send_event.pk or existing.events.filter(
            event_type=EVENT.RESENT, data__send_event=send_event.pk
        ).exists():
            continue  # replay of an event already processed
        else:
            if existing.status == STATUS.CLOSED:
                data.update({"closed": True, "needs_business_review": True})
                logger.warning("Surveillance %s is closed and the revision was sent again: business review needed", existing.pk)
            _event(existing, send_event.actor, EVENT.RESENT, {**data, "due_at": iso(existing.due_at)})
            done.append(("RESEND", existing.pk))
    return done


def open_client_surveillances_safely(send_event):
    """Run after SEND_SUCCEEDED is durably recorded, in its own atomic block: any failure is logged and
    swallowed, so a sent message is never reported as unsent. Missing surveillances are picked up by
    the idempotent reconciliation (`open_client_surveillances` again, or the simulation command)."""
    try:
        return open_client_surveillances(send_event)
    except Exception:
        logger.exception("Client surveillance opening failed for send event %s; the message stays sent", send_event.pk)
        return None


def change_due_date(surveillance, new_due_at, reason, actor):
    """Manual, traced change of the effective due date (the caller has already checked the dedicated
    right). The immutable event keeps the old and new instants, the user and the reason."""
    if new_due_at is None or timezone.is_naive(new_due_at):
        raise ValidationError({"due_at": "A timezone-aware date and time is required."})
    reason = (reason or "").strip()
    if len(reason) > 500:
        raise ValidationError({"reason": "The reason is limited to 500 characters."})
    with transaction.atomic():
        locked = ClientSurveillance.objects.select_for_update().get(pk=surveillance.pk)
        if locked.status == STATUS.CLOSED:
            raise ValidationError("A closed client surveillance cannot be changed.")
        old = locked.due_at
        if old == new_due_at:
            raise ValidationError({"due_at": "The due date is unchanged."})
        locked.due_at = new_due_at
        locked.save(update_fields=["due_at", "updated_at"])
        _event(locked, actor, EVENT.DUE_DATE_CHANGED, {"old": iso(old), "new": iso(new_due_at), "reason": reason})
    return locked


def can_manage_client_surveillance(user, document):
    """documents.client_surveillance_manage (or full_access) in the document's own project --
    documents.update and transmittal_issue never suffice."""
    if not user or not user.is_authenticated or not user.is_active or document is None:
        return False
    from project_management.permissions import DocumentPermission

    checker = DocumentPermission(action="client_surveillance_manage", scope="mixed")
    if checker.is_company_document(document):
        scope, context = "company", {}
    else:
        scope = "project"
        context = checker._get_context(request=None, view=None, scope="project", obj=document)
    return bool(
        checker._check_permission(user, "documents", "client_surveillance_manage", scope, context)
        or checker._check_permission(user, "documents", "full_access", scope, context)
    )


def can_process_client_return(user, document):
    """SURVEILLANCE A/B/C/D LOT 2: recording, qualifying, retaining a client return and providing the file that answers
    it is document work: documents.update in the document's own scope (project or company), nothing more. The
    administrative operations (manual closure with a result, due date) keep documents.client_surveillance_manage."""
    if not user or not user.is_authenticated or not user.is_active or document is None:
        return False
    from project_management.services.document_register import can_update_document

    return bool(can_update_document(user, document))


def can_view_client_surveillance_detail(user, document):
    """GED-SEC.2: the detailed side of a client surveillance (comments, source references, actors, detailed
    events, Transmittal references, annotated files) needs, on top of the read access to the document,
    documents.client_surveillance_manage (or full_access) OR documents.transmittal_view in the document's own
    project. documents.view alone only gives the summary (status, A/B/C/D result, code, due date, revision).
    No new right: both permissions already exist. A Transmittal is project-only, so a company document is
    only opened by the surveillance right."""
    if can_manage_client_surveillance(user, document):
        return True
    # LOT 2 (R8): whoever may process a return (documents.update, same scope) sees the details that processing needs.
    if can_process_client_return(user, document):
        return True
    return can_open_transmittal(user, document)


def can_open_transmittal(user, document):
    """GED-WF.4.4-B: a Transmittal identifier (hence a link to its drawer) is only given to who really holds
    documents.transmittal_view in the document's project: client_surveillance_manage alone does not open it."""
    if not user or not user.is_authenticated or not user.is_active or document is None:
        return False
    from project_management.permissions import DocumentPermission, TransmittalPermission

    if DocumentPermission(action="view", scope="mixed").is_company_document(document):
        return False
    project = document.folder.project if document.folder_id else None
    if project is None:
        return False
    return bool(TransmittalPermission("view")._check_action_or_full_access(user, "transmittal_view", "project", {"project": project}))
