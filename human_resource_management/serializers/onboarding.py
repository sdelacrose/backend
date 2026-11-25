"""L05 -- plain allowlist serialization. Step-level visibility filtering
(employee sees only visible_to_employee steps; a plain responsible sees only
their own steps) happens in serialize_instance, driven by the SAME
permission functions used server-side for writes -- never a second,
divergent notion of "who can see what"."""
from human_resource_management.models.onboarding import OnboardingInstance
from human_resource_management.permissions import _view_all_permission_for_kind, is_company_admin_for_staff


def serialize_template_step(step):
    return {
        "id": step.id,
        "order": step.order,
        "title": step.title,
        "step_kind": step.step_kind,
        "responsible_role": step.responsible_role,
        "default_responsible_user_id": step.default_responsible_user_id,
        "due_offset_days": step.due_offset_days,
        "expected_category_id": step.expected_category_id,
        "visible_to_employee": step.visible_to_employee,
        "is_required": step.is_required,
    }


def serialize_template(template, *, include_steps=True):
    data = {
        "id": template.id,
        "name": template.name,
        "process_kind": template.process_kind,
        "version": template.version,
        "is_active": template.is_active,
    }
    if include_steps:
        data["steps"] = [serialize_template_step(s) for s in template.steps.order_by("order")]
    return data


def serialize_calendar_event(event):
    if event is None:
        return None
    return {
        "id": event.id,
        "start_date": event.start_date,
        "end_date": event.end_date,
        # Always null unless a company's own, separate Outlook sync has
        # populated it -- never claimed as "invitation sent" by this lot
        # (see services.offboarding_interview's module docstring).
        "outlook_event_id": event.outlook_event_id,
    }


def serialize_step_instance(step):
    return {
        "id": step.id,
        "order": step.order,
        "title": step.title,
        "step_kind": step.step_kind,
        "responsible_user": {"id": step.responsible_user_id, "name": getattr(step.responsible_user, "full_name", None)} if step.responsible_user_id else None,
        "due_date": step.due_date,
        "expected_category_id": step.expected_category_id,
        "calendar_event": serialize_calendar_event(step.calendar_event) if step.calendar_event_id else None,
        "visible_to_employee": step.visible_to_employee,
        "is_required": step.is_required,
        "status": step.status,
        "completed_at": step.completed_at,
    }


def serialize_instance(instance, viewer):
    """`steps` is filtered per-viewer: the full RH/Company-Admin view sees
    every step; the employee sees only their own instance's
    visible_to_employee steps; any other viewer sees only steps they are
    the CURRENT responsible_user of."""
    is_full_view = _view_all_permission_for_kind(instance.process_kind)(viewer) or is_company_admin_for_staff(viewer, instance.staff)
    is_employee = viewer.id == instance.staff.user_id

    steps_qs = instance.step_instances.order_by("order")
    if is_full_view:
        steps = list(steps_qs)
    elif is_employee:
        steps = [s for s in steps_qs if s.visible_to_employee]
    else:
        steps = [s for s in steps_qs if s.responsible_user_id == viewer.id]

    from human_resource_management.services.onboarding import compute_progress

    data = {
        "id": instance.id,
        "staff_id": instance.staff_id,
        "template_id": instance.template_id,
        "process_kind": instance.process_kind,
        # JSON key kept as "arrival_date" for continuity with the
        # already-shipped onboarding frontend -- it holds the departure
        # date for an offboarding instance (process_kind=offboarding).
        "arrival_date": instance.anchor_date,
        "status": instance.status,
        "source": instance.source,
        "triggered_at": instance.triggered_at,
        "cancelled_at": instance.cancelled_at,
        "completed_at": instance.completed_at,
        "steps": [serialize_step_instance(s) for s in steps],
    }
    if is_full_view:
        data["progress"] = compute_progress(instance)
    return data


def serialize_event(event):
    return {
        "id": event.id,
        "event_type": event.event_type,
        "step_instance_id": event.step_instance_id,
        "actor": {"id": event.actor_id, "name": getattr(event.actor, "full_name", None)} if event.actor_id else None,
        "detail": event.detail,
        "created_at": event.created_at,
    }
