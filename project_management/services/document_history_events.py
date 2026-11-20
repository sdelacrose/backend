"""HISTORY-4: All Events -- a READ MODEL ONLY, assembled dynamically from the exact
authoritative records every specialized history already uses. No new persisted table, no
duplicated event source (section 3):

REVISION            -- DocumentRevision itself (its creation is a real lifecycle moment).
INTERNAL_WORKFLOW   -- DocumentWorkflowEvent (unchanged, L3A).
SUPPLIER_SURVEILLANCE -- SupplierSurveillanceEvent (unchanged, L3D).
CLIENT_DISTRIBUTION -- the exact ISSUED OUT TransmittalItem occurrences HISTORY-2's own
                       list_client_distribution_history already uses, enriched with the
                       authoritative issuing actor from TransmittalEvent (section 4D) where a
                       retransmission needs its own second timeline entry (section 12/15).

Every event stays traceable to its exact source row (source_type/source_id) and to the exact
DocumentRevision it belongs to -- never aggregated by Project, never mixed across Documents
(section 5).
"""

from project_management.models.document import DocumentOrigin
from project_management.models.document_revision import DocumentRevision
from project_management.models.document_workflow import DocumentWorkflowEvent
from project_management.models.supplier_surveillance_review import SupplierSurveillanceEvent
from project_management.models.transmittal import Transmittal, TransmittalEvent, TransmittalItem
from project_management.serializers.document_register import user_reference
from project_management.services.client_distribution_history import is_actual_client_recipient


def _source_revision_for(document, revision):
    """Section 8: INTERNAL -> revision_label, SUPPLIER/CLIENT -> external_revision -- the
    exact same dichotomy historyRules.ts's own getRevisionCellForOrigin already uses on the
    frontend; never persisted, derived only."""
    if document.document_origin == DocumentOrigin.INTERNAL:
        return revision.revision_label
    return revision.external_revision


# GED-WF.4.4-B: what a plain document reader (documents.view) must never receive on a client event.
_PROTECTED_KEYS = ("transmittal_reference", "transmittal_id", "comment", "reason")

# Business order of events sharing the very same instant (chronological: lower rank happens first).
_SEND_TYPES = ("SEND_SUCCEEDED", "SEND_FAILED")
_RANK_BY_TYPE = {"ISSUED": 2, "RETRANSMITTED": 2, "SEND_SUCCEEDED": 3, "SEND_FAILED": 3, "OPENED": 4, "RESENT": 4}
_RANK_BY_GROUP = {"REVISION": 0, "INTERNAL_WORKFLOW": 1, "SUPPLIER_SURVEILLANCE": 1}


def _event_rank(event):
    return _RANK_BY_TYPE.get(event["event_type"], _RANK_BY_GROUP.get(event["event_group"], 5))


def _mask_client_event(event):
    """GED-SEC.2: client distribution / client surveillance events keep their place in the timeline (type, date,
    revision, company, result) but lose the actor, the free comment, the Transmittal reference and identifier."""
    metadata = {k: v for k, v in event["metadata"].items() if k not in _PROTECTED_KEYS}
    return {**event, "actor": None, "metadata": metadata}


def _iso(value):
    return value.isoformat() if value else None


def _client_surveillance_summary(event, transmittal_links):
    """GED-WF.4.4-B: explicit whitelist of a ClientSurveillanceEvent.data (never the raw JSON). Summary fields only
    (type, due dates, A/B/C/D result and code, late/correction flags); the free reason and the Transmittal
    identifier are protected keys, removed for a plain reader by _mask_client_event."""
    data = event.data or {}
    kind = event.event_type
    out = {}
    if kind in ("OPENED", "RESENT"):
        out["due_at"] = data.get("due_at")
        if transmittal_links and data.get("transmittal"):
            out["transmittal_id"] = data["transmittal"]
        if data.get("transmittal_reference"):
            out["transmittal_reference"] = data["transmittal_reference"]
    elif kind == "DUE_DATE_CHANGED":
        out.update(previous_due_at=data.get("old"), due_at=data.get("new"), reason=data.get("reason") or "")
    elif kind in ("RETURN_RECORDED", "RETURN_CORRECTED", "RETURN_LATE"):
        out.update(
            category=data.get("category"), code=data.get("code"), late=bool(data.get("late")),
            corrects_previous=bool(data.get("supersedes")), qualified_later=bool(data.get("qualified_later")),
            via_incoming_transmittal=bool(data.get("in_line")),
        )
        if transmittal_links and data.get("in_transmittal"):
            out["transmittal_id"] = data["in_transmittal"]
    elif kind == "FU_OPENED":  # LOT C: the retained return, and whether the confirmation was automatic
        out.update(category=data.get("category"), code=data.get("code"), automatic=bool(data.get("automatic")), reconfirmed=bool(data.get("reconfirmed")))
    elif kind == "STATUS" and data.get("retained_return"):
        out["retained_result"] = {"category": data.get("category"), "code": data.get("code")}
    return out


def list_document_history_events(document, detail=True, transmittal_links=None):
    """Every authoritative event belonging to this exact Document, newest first (section 16).
    Avoids N+1 (section 25): exactly one bulk query per source, keyed by this Document's own
    revision ids -- never one query per revision or per event.

    GED-WF.4.4-B: `transmittal_links` (default: same as `detail`) tells whether the caller may open a Transmittal:
    only then is a Transmittal identifier part of the event, so the front end never has to guess a link.
    """
    if transmittal_links is None:
        transmittal_links = detail
    transmittal_links = bool(transmittal_links and detail)
    revisions = list(
        DocumentRevision.objects.filter(document=document).select_related("created_by")
    )
    revision_ids = [revision.id for revision in revisions]
    revisions_by_id = {revision.id: revision for revision in revisions}

    events = []

    # A. REVISION -- section 9: the real creation/upload/receipt moment of each stored
    # version, never inventing a label before one actually exists (a pre-approval INTERNAL
    # revision's source_revision is legitimately None here, exactly as DocumentRevisionHistory
    # already shows).
    for revision in revisions:
        events.append(
            {
                "id": f"revision-{revision.id}",
                "event_type": "REVISION_CREATED",
                "event_group": "REVISION",
                "occurred_at": revision.created_at,
                "actor": user_reference(revision.created_by),
                "document_revision_id": revision.id,
                "source_revision": _source_revision_for(document, revision),
                "source_type": "revision",
                "source_id": revision.id,
                "metadata": {
                    "file_name": revision.file_name,
                    "comment": revision.comment,
                },
            }
        )

    # B. INTERNAL_WORKFLOW -- section 10: exact existing DocumentWorkflowEvent stream, never
    # duplicated/rewritten. event_type is the real Action value (START/SIGN/VALIDATE/APPROVE/
    # REJECT/CHANGE_ACTOR) -- the frontend already has a translation for every one of these
    # (DocumentWorkflowCard/workflowRules.ts EVENT_KEYS), reused as-is.
    if revision_ids:
        workflow_events = DocumentWorkflowEvent.objects.filter(
            workflow__revision_id__in=revision_ids
        ).select_related("workflow", "actor", "previous_actor", "new_actor")
        for event in workflow_events:
            revision = revisions_by_id.get(event.workflow.revision_id)
            events.append(
                {
                    "id": f"workflow_event-{event.id}",
                    "event_type": event.action,
                    "event_group": "INTERNAL_WORKFLOW",
                    "occurred_at": event.created_at,
                    "actor": user_reference(event.actor),
                    "document_revision_id": event.workflow.revision_id,
                    "source_revision": (
                        _source_revision_for(document, revision) if revision else None
                    ),
                    "source_type": "workflow_event",
                    "source_id": event.id,
                    "metadata": {
                        "comment": event.comment,
                        "step": event.step,
                        "previous_actor": user_reference(event.previous_actor),
                        "new_actor": user_reference(event.new_actor),
                    },
                }
            )

        # C. SUPPLIER_SURVEILLANCE -- section 11: exact existing SupplierSurveillanceEvent
        # stream, never duplicated/rewritten. event_type is the real event_type value
        # (SURVEILLANCE_CREATED/REVIEWER_ASSIGNED/.../PDF_GENERATED) -- already translated by
        # HISTORY-3's own SupplierSurveillanceEventList, reused as-is.
        surveillance_events = SupplierSurveillanceEvent.objects.filter(
            surveillance__document_revision_id__in=revision_ids
        ).select_related("actor", "surveillance")
        for event in surveillance_events:
            revision = revisions_by_id.get(event.surveillance.document_revision_id)
            events.append(
                {
                    "id": f"surveillance_event-{event.id}",
                    "event_type": event.event_type,
                    "event_group": "SUPPLIER_SURVEILLANCE",
                    "occurred_at": event.created_at,
                    "actor": user_reference(event.actor),
                    "document_revision_id": event.surveillance.document_revision_id,
                    "source_revision": (
                        _source_revision_for(document, revision) if revision else None
                    ),
                    "source_type": "surveillance_event",
                    "source_id": event.id,
                    "metadata": {
                        "surveillance_reference": event.surveillance.reference,
                        **(event.metadata or {}),
                    },
                }
            )

        # D. CLIENT_DISTRIBUTION -- section 12/13/14/15: the exact same authoritative
        # evidence as HISTORY-2's list_client_distribution_history (OUT + ISSUED
        # TransmittalItem for one of THIS Document's revisions). A retransmission -- a second
        # issued occurrence of the same (document_revision, Client company) pair, whether via
        # a real ClientIssue or the historical NULL-ClientIssue fallback grouping -- is a REAL
        # additional timeline event, never collapsed (section 12/15). The issuing actor comes
        # from TransmittalEvent (ISSUED), the one existing audit record that actually carries
        # it -- Transmittal itself has no separate "issued_by" field.
        items = (
            TransmittalItem.objects.filter(
                document_revision_id__in=revision_ids,
                transmittal__direction=Transmittal.Direction.OUT,
                transmittal__status=Transmittal.Status.ISSUED,
            )
            .select_related(
                "transmittal",
                "transmittal__external_company",
                "document_revision",
                "client_issue",
                "client_issue__client_company",
            )
            .order_by("transmittal__issued_at", "id")
        )
        # HISTORY-4.1: "OUT + ISSUED" alone is not "to the Client" (checkpoint section 4) --
        # the SAME shared rule ClientDistributionHistory now uses, so the two read paths can
        # never diverge (section 6).
        client_items = [
            item
            for item in items
            if is_actual_client_recipient(document, item.transmittal.external_company_id)
        ]
        transmittal_ids = {item.transmittal_id for item in client_items}
        issued_actor_by_transmittal = {
            issued_event.transmittal_id: issued_event.actor
            for issued_event in TransmittalEvent.objects.filter(
                transmittal_id__in=transmittal_ids,
                event_type=TransmittalEvent.EventType.ISSUED,
            ).select_related("actor")
        }

        seen_groups = set()
        for item in client_items:
            transmittal = item.transmittal
            revision = item.document_revision
            client_issue = item.client_issue
            # Same grouping key as ClientDistributionHistory's own getGroupKey (frontend) --
            # a real ClientIssue groups by its own id; a historical NULL-ClientIssue item
            # falls back to the exact (revision, Client company) pair.
            group_key = (
                f"issue-{client_issue.id}"
                if client_issue
                else f"legacy-{revision.id}-{transmittal.external_company_id}"
            )
            is_retransmission = group_key in seen_groups
            seen_groups.add(group_key)
            client_company = (
                client_issue.client_company if client_issue else transmittal.external_company
            )
            events.append(
                {
                    "id": f"transmittal_item-{item.id}",
                    "event_type": "RETRANSMITTED" if is_retransmission else "ISSUED",
                    "event_group": "CLIENT_DISTRIBUTION",
                    "occurred_at": transmittal.issued_at,
                    "actor": user_reference(issued_actor_by_transmittal.get(transmittal.id)),
                    "document_revision_id": revision.id,
                    "source_revision": _source_revision_for(document, revision),
                    "source_type": "transmittal_item",
                    "source_id": item.id,
                    "metadata": {
                        "transmittal_reference": transmittal.transmittal_number,
                        "client_company_id": client_company.id if client_company else None,
                        "client_company_name": (
                            client_company.company_name if client_company else None
                        ),
                        "client_issue_id": item.client_issue_id,
                        "client_revision_label": (
                            client_issue.client_revision_label if client_issue else None
                        ),
                        "distribution_purpose": item.distribution_purpose,
                        **({"transmittal_id": transmittal.id} if transmittal_links else {}),
                    },
                }
            )

        # D2. GED-WF.4.4-B: the REAL send attempts of these Transmittals (SEND_SUCCEEDED / SEND_FAILED), only for
        # the items of THIS document (never another document of the same Transmittal). ISSUED alone is never a send.
        # The raw TransmittalEvent.metadata (recipients, subject, body, attachments, mailbox, provider id, raw error)
        # is never exposed: an explicit whitelist (attempt number, `uncertain`) is built instead.
        send_events_by_transmittal = {}
        attempts_by_transmittal = {}
        if transmittal_ids:
            for send_event in TransmittalEvent.objects.filter(
                transmittal_id__in=transmittal_ids, event_type__in=_SEND_TYPES + ("SEND_STARTED",)
            ).select_related("actor").order_by("id"):
                if send_event.event_type == "SEND_STARTED":
                    attempts_by_transmittal.setdefault(send_event.transmittal_id, []).append(send_event.id)
                else:
                    send_events_by_transmittal.setdefault(send_event.transmittal_id, []).append(send_event)
        for item in client_items:
            transmittal = item.transmittal
            revision = item.document_revision
            client_issue = item.client_issue
            client_company = client_issue.client_company if client_issue else transmittal.external_company
            for send_event in send_events_by_transmittal.get(transmittal.id, []):
                started = attempts_by_transmittal.get(transmittal.id, [])
                attempt_id = (send_event.metadata or {}).get("attempt")
                metadata = {
                    "transmittal_reference": transmittal.transmittal_number,
                    "client_company_id": client_company.id if client_company else None,
                    "client_company_name": client_company.company_name if client_company else None,
                    "client_issue_id": item.client_issue_id,
                    "client_revision_label": client_issue.client_revision_label if client_issue else None,
                    "distribution_purpose": item.distribution_purpose,
                    "attempt": started.index(attempt_id) + 1 if attempt_id in started else None,
                    **({"transmittal_id": transmittal.id} if transmittal_links else {}),
                }
                if send_event.event_type == "SEND_FAILED":
                    metadata["uncertain"] = bool((send_event.metadata or {}).get("uncertain"))
                events.append(
                    {
                        "id": f"transmittal_event-{send_event.id}-item-{item.id}",
                        "event_type": send_event.event_type,
                        "event_group": "CLIENT_DISTRIBUTION",
                        "occurred_at": send_event.created_at,
                        "actor": user_reference(send_event.actor),
                        "document_revision_id": revision.id,
                        "source_revision": _source_revision_for(document, revision),
                        "source_type": "transmittal_event",
                        "source_id": send_event.id,
                        "metadata": metadata,
                    }
                )

    # Explicit client follow-up events remain distinct from evidence of distribution. GED-WF.4.4-B: the real
    # event_type is the title (OPENED, RESENT, DUE_DATE_CHANGED, RETURN_*, STATUS); the surveillance status stays a
    # complementary `status` field.
    from project_management.models import ClientSurveillanceEvent, TransmittalClientReturnLine
    for event in ClientSurveillanceEvent.objects.filter(surveillance__client_issue__document_revision_id__in=revision_ids).select_related('actor', 'surveillance__client_issue__client_company'):
        issue = event.surveillance.client_issue
        revision = revisions_by_id.get(issue.document_revision_id)
        events.append({'id': f'client_surveillance_event-{event.pk}', 'event_type': event.event_type,
            'event_group': 'CLIENT_SURVEILLANCE', 'occurred_at': event.created_at, 'actor': user_reference(event.actor),
            'document_revision_id': issue.document_revision_id,
            'source_revision': _source_revision_for(document, revision) if revision else None,
            'source_type': 'client_surveillance_event', 'source_id': event.pk,
            'metadata': {'comment': event.comment, 'result': event.result, 'status': event.status,
                         'client_company_name': issue.client_company.company_name,
                         **_client_surveillance_summary(event, transmittal_links)}})

    # E. GED-WF.4.4-B: confirmed incoming (IN) client receptions of THIS document, from the received line itself
    # (never from the return, which is already the RETURN_RECORDED event). occurred_at is the BUSINESS reception
    # date (`received_at`), never the qualification date; the result is never shown here, only whether the return
    # is still to qualify. An unconfirmed (draft) IN is not a reception.
    if revision_ids:
        lines = list(
            TransmittalClientReturnLine.objects.filter(
                client_issue__document_revision_id__in=revision_ids, transmittal__status=Transmittal.Status.ISSUED,
            ).select_related("transmittal", "client_issue__client_company", "client_return")
        )
        received_event_by_transmittal = {
            received.transmittal_id: received
            for received in TransmittalEvent.objects.filter(
                transmittal_id__in={line.transmittal_id for line in lines}, event_type=TransmittalEvent.EventType.RECEIVED,
            ).select_related("actor").order_by("id")
        } if lines else {}
        for line in lines:
            issue = line.client_issue
            revision = revisions_by_id.get(issue.document_revision_id)
            received = received_event_by_transmittal.get(line.transmittal_id)
            qualified = line.client_return_id is not None
            events.append({
                "id": f"client_return_line-{line.id}",
                "event_type": "IN_RECEIVED_QUALIFIED" if qualified else "IN_RECEIVED_TO_QUALIFY",
                "event_group": "CLIENT_SURVEILLANCE",
                "occurred_at": line.received_at,
                "actor": user_reference(received.actor) if received else None,
                "document_revision_id": issue.document_revision_id,
                "source_revision": _source_revision_for(document, revision) if revision else None,
                "source_type": "client_return_line",
                "source_id": line.id,
                "metadata": {
                    "transmittal_reference": line.transmittal.transmittal_number,
                    "client_company_id": issue.client_company_id,
                    "client_company_name": issue.client_company.company_name,
                    "client_issue_id": issue.id,
                    "qualified": qualified,
                    "received_at": _iso(line.received_at),
                    "recorded_at": _iso(received.created_at) if received else None,
                    "qualified_at": _iso(line.client_return.recorded_at) if qualified else None,
                    **({"transmittal_id": line.transmittal_id} if transmittal_links else {}),
                },
            })

    if not detail:
        events = [_mask_client_event(e) if e["event_group"] in ("CLIENT_DISTRIBUTION", "CLIENT_SURVEILLANCE") else e for e in events]

    # Section 16: newest first, deterministic secondary order (never unstable DB ordering). Same instant:
    # business order (revision, workflow, issue, accepted send, surveillance opening, the rest).
    events.sort(
        key=lambda event: (
            event["occurred_at"],
            _event_rank(event),
            event["source_type"],
            event["source_id"],
            event["id"],
        ),
        reverse=True,
    )
    return events
