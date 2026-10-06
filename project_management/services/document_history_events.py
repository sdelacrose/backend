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


def list_document_history_events(document):
    """Every authoritative event belonging to this exact Document, newest first (section 16).
    Avoids N+1 (section 25): exactly one bulk query per source, keyed by this Document's own
    revision ids -- never one query per revision or per event.
    """
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
                    },
                }
            )

    # Section 16: newest first, deterministic secondary order (never unstable DB ordering).
    events.sort(
        key=lambda event: (
            event["occurred_at"],
            event["source_type"],
            event["source_id"],
        ),
        reverse=True,
    )
    return events
