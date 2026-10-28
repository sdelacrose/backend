"""LOT L04 -- plain allowlist serialization for SST. Deliberately never a
DRF ModelSerializer over SSTCase directly: a case's `steps`/periods need
per-viewer shaping (is_full_view strips nothing here -- SST has only ONE
visibility tier, self or SST-habilitated, never a partial "your own period
only" tier like onboarding's responsible_user), and the payroll projection
needs its own strict allowlist, never this one reused by accident."""


def serialize_sst_period(period):
    lr = period.leave_request
    return {
        "id": period.id,
        "is_initial": period.is_initial,
        "start_date": lr.start_date,
        "end_date": lr.end_date,
        "status": lr.status,
        "cancelled_at": period.cancelled_at,
        "cancellation_reason": period.cancellation_reason,
        "created_at": period.created_at,
    }


def serialize_sst_event(event):
    return {
        "id": event.id,
        "event_type": event.event_type,
        "actor": getattr(event.actor, "full_name", None),
        "note": event.note,
        "created_at": event.created_at,
    }


def serialize_sst_document(document):
    latest_file = document.files.order_by("-version_number").first()
    return {
        "id": document.id,
        "title": document.title,
        "category_id": document.category_id,
        "created_at": document.created_at,
        "latest_file": (
            {"id": latest_file.id, "file_name": latest_file.file_name} if latest_file else None
        ),
    }


def serialize_sst_category_option(category):
    return {"id": category.id, "name": category.name}


def serialize_sst_case(case, *, include_events=False):
    """A viewer only ever reaches this (can_view_staff_sst_dossier: self or
    SST-habilitated) -- so `available_categories`/`documents` are safe to
    include here unconditionally, reusing that ONE gate rather than
    requiring the viewer to ALSO separately hold the general staff_
    management.view_documents permission just to pick a category or see
    their own case's pieces (a real gap found and fixed: the generic
    staff-document-categories/staff-documents endpoints are gated by that
    general permission, which an SST-only RH or the employee concerned may
    never hold)."""
    data = {
        "id": case.id,
        "staff_id": case.staff_id,
        "staff_name": getattr(case.staff.user, "full_name", None),
        "category": case.category,
        "status": case.status,
        "declared_by": getattr(case.declared_by, "full_name", None),
        "declared_at": case.declared_at,
        "workplace_adjustment_note": case.workplace_adjustment_note,
        "external_reference": case.external_reference,
        "closed_at": case.closed_at,
        "periods": [serialize_sst_period(p) for p in case.periods.order_by("created_at")],
        "documents": [serialize_sst_document(d) for d in case.documents.order_by("created_at")],
        "available_categories": [
            serialize_sst_category_option(c)
            for c in case.company_profile.staff_document_categories.filter(is_sst=True, is_active=True).order_by("name")
        ],
    }
    if include_events:
        data["events"] = [serialize_sst_event(e) for e in case.events.order_by("created_at")]
    return data


def serialize_payroll_projection_row(row):
    return {
        "sst_case_id": row["sst_case_id"],
        "staff_id": row["staff_id"],
        "staff_name": row["staff_name"],
        "category": row["category"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "status": row["status"],
    }
