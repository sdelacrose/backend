"""Read-only forecasts. Recorded issue/surveillance deadlines remain authoritative."""
from datetime import timedelta


def day(value):
    return value.date() if hasattr(value, "hour") else value


def forecast_targets(*, origin, base, durations, three_step=False, decisions=None,
                     supplier_closed=None, supplier_due=None, supplier_exists=False,
                     supplier_information=False, category=None, already_sent=False):
    decisions = decisions or {}
    def add(value, field):
        return day(value) + timedelta(days=durations[field]) if value else None
    targets = {}
    if origin == "INTERNAL":
        targets["author"] = add(base, "author_preparation_effective_days")
        previous = decisions.get("author") or targets["author"]
        if three_step:
            targets["reviewer"] = add(previous, "internal_review_duration_effective_days")
            previous = decisions.get("reviewer") or targets["reviewer"]
        targets["approver"] = add(previous, "internal_approval_duration_effective_days")
        ready = decisions.get("approver") or targets["approver"]
    elif origin == "SUPPLIER":
        targets["supplier_review"] = supplier_due if supplier_exists else (
            None if supplier_information else add(base, "supplier_review_duration_effective_days"))
        ready = supplier_closed or (base if supplier_information else targets["supplier_review"])
    else:
        return targets
    purpose = (category or "").strip().lower()
    if purpose in ("for review", "for information") and not already_sent:
        targets["client_sent"] = add(ready, "client_issue_duration_effective_days")
        if purpose == "for review":
            targets["client_review"] = add(targets["client_sent"], "client_review_duration_effective_days")
    return targets


def document_timeline_targets(document):
    from project_management.services.document_type_review_duration import resolve_review_durations
    from project_management.services.document_workflow import get_workflow_for_revision
    from project_management.services.client_distribution_history import list_client_distribution_history
    from project_management.models import SupplierSurveillance
    project = document.folder.project
    if not project:
        return {"revision_id": None, "targets": {}}
    revision = document.revisions.order_by("-version_number", "-id").first()
    workflow = get_workflow_for_revision(revision) if revision else None
    if workflow and workflow.status in ("REJECTED", "CANCELLED"):
        return {"revision_id": revision.pk, "targets": {}}
    durations = resolve_review_durations(project=project, document_type=document.document_type)
    decisions = {}
    if workflow:
        for event in workflow.events.all():
            if event.action not in ("SIGN", "VALIDATE", "APPROVE"):
                continue
            role = {"AUTHOR_SIGN": "author", "VERIFY": "reviewer", "APPROVE": "approver"}.get(event.step)
            if role:
                decisions[role] = event.created_at
    surveillance = SupplierSurveillance.objects.filter(document_revision=revision).first() if revision and document.document_origin == "SUPPLIER" else None
    sent = revision and any(row["document_revision_id"] == revision.pk for row in list_client_distribution_history(document))
    targets = forecast_targets(origin=document.document_origin,
        base=(workflow.started_at or workflow.created_at) if workflow else (revision.created_at if revision else document.uploaded_at),
        durations=durations, three_step=workflow.workflow_type == "THREE_STEP" if workflow else bool(getattr(getattr(document, "control", None), "verifier_id", None)),
        decisions=decisions, supplier_closed=surveillance.closed_at if surveillance else None,
        supplier_due=surveillance.review_due_date if surveillance else None,
        supplier_exists=surveillance is not None,
        supplier_information=document.supplier_transmittal_category == "FOR_INFORMATION",
        category=document.transmittal_category, already_sent=bool(sent))
    return {"revision_id": revision.pk if revision else None, "targets": targets}
