"""L05 -- template preview/trigger/reassignment/cancellation/closure for
onboarding instances. Two versioning precedents combined here (see the
models/onboarding.py module docstring): an immutable JSON snapshot captured
at trigger time (project_management.models.supplier_surveillance_template's
pattern) and a partial UniqueConstraint guaranteeing "one ACTIVE instance at
a time, not one forever" (project_management.models.document_workflow's
pattern)."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from human_resource_management.models.onboarding import (
    OnboardingInstance, OnboardingInstanceEvent, OnboardingStepInstance, OnboardingTemplateStep,
)
from human_resource_management.models.staff_documents import StaffDocumentFileStatusEvent
from human_resource_management.services.staff_documents import resolve_file_status


class OnboardingError(Exception):
    """A confirmed business-rule violation (distinct from a permission
    refusal, which callers check beforehand)."""


def resolve_step_responsible(staff, template_step, *, override_user=None):
    """Resolves the CURRENT responsible for one template step -- "aucun
    destinataire devine": returns None (never a guessed fallback) if the
    role cannot be resolved and no override was supplied."""
    if override_user is not None:
        return override_user
    if template_step.responsible_role == OnboardingTemplateStep.ROLE_SUPERVISOR:
        return staff.supervisor.user if staff.supervisor_id and staff.supervisor.user_id else None
    if template_step.responsible_role == OnboardingTemplateStep.ROLE_COMPANY_ADMIN:
        company = getattr(staff.user, "company", None)
        return company.admin_user if company and company.admin_user_id else None
    # ROLE_NAMED
    return template_step.default_responsible_user


def preview_trigger(*, staff, template, anchor_date, overrides=None):
    """Resolves every step's responsible + due_date WITHOUT creating
    anything -- "apercu des responsables et echeances avant activation".
    `overrides` is a dict of {template_step_id: User} letting RH supply a
    responsible for a step whose role could not auto-resolve. `anchor_date`
    is the employee's arrival (onboarding) or departure (offboarding) date
    -- `template.process_kind` decides which, this function itself stays
    agnostic (it only adds a signed offset to whatever date it is given)."""
    overrides = overrides or {}
    rows = []
    for template_step in template.steps.all().order_by("order"):
        override_user = overrides.get(template_step.id)
        responsible = resolve_step_responsible(staff, template_step, override_user=override_user)
        rows.append({
            "template_step_id": template_step.id,
            "order": template_step.order,
            "title": template_step.title,
            "step_kind": template_step.step_kind,
            "responsible_role": template_step.responsible_role,
            "responsible_user_id": responsible.id if responsible else None,
            "due_date": anchor_date + timedelta(days=template_step.due_offset_days),
            "expected_category_id": template_step.expected_category_id,
            "visible_to_employee": template_step.visible_to_employee,
            "is_required": template_step.is_required,
            "unresolved": responsible is None,
        })
    return rows


def _build_snapshot(template, rows):
    return {
        "template_id": template.id,
        "template_name": template.name,
        "template_version": template.version,
        "process_kind": template.process_kind,
        "captured_at": timezone.now().isoformat(),
        "steps": [
            {
                "order": r["order"], "title": r["title"], "step_kind": r["step_kind"],
                "responsible_role": r["responsible_role"],
                "responsible_user_id": r["responsible_user_id"], "due_date": r["due_date"].isoformat(),
                "expected_category_id": r["expected_category_id"], "visible_to_employee": r["visible_to_employee"],
                "is_required": r["is_required"],
            }
            for r in rows
        ],
    }


def trigger_onboarding(*, staff, template, anchor_date, actor, overrides=None, source=OnboardingInstance.SOURCE_MANUAL, trigger_key=None):
    """Activation is refused outright ("signale avant activation ; aucune
    activation si une responsabilite requise reste non resolue") if ANY
    REQUIRED step's responsible could not be resolved. The actual "one
    active instance at a time (per staff AND process_kind)" guarantee is the
    partial UniqueConstraint on OnboardingInstance -- this function surfaces
    that race as a friendly, IDEMPOTENT response (the already-active
    instance, never a raw IntegrityError, never a duplicate) rather than an
    unhandled 500, exactly like L01-B1's provisioning race handling.

    `process_kind` is NEVER passed independently -- it is always
    `template.process_kind`, so a trigger call's kind is entirely determined
    by which template it names (one engine, two kinds, never a mismatch).
    `source`/`trigger_key` are set by callers: the manual (RH-click) path
    leaves both at their defaults; the automatic attribution job
    (services.offboarding_automation) passes source=SOURCE_AUTOMATIC and
    trigger_key=<anchor_date.isoformat()> -- see OnboardingInstance's own
    docstring for why that single field is enough to prevent the job from
    recreating an instance it (or a human) already cancelled for the SAME
    departure event, without a separate suppression ledger."""
    process_kind = template.process_kind
    rows = preview_trigger(staff=staff, template=template, anchor_date=anchor_date, overrides=overrides)
    unresolved_required = [r for r in rows if r["unresolved"] and r["is_required"]]
    if unresolved_required:
        raise OnboardingError(
            "The following required steps have no resolved responsible and no override was supplied: "
            + ", ".join(r["title"] for r in unresolved_required)
        )

    existing_active = OnboardingInstance.objects.filter(staff=staff, process_kind=process_kind, status=OnboardingInstance.STATUS_ACTIVE).first()
    if existing_active is not None:
        return existing_active, False

    snapshot = _build_snapshot(template, rows)
    try:
        with transaction.atomic():
            instance = OnboardingInstance.objects.create(
                staff=staff, company_profile=staff.user.company, process_kind=process_kind, template=template,
                template_snapshot=snapshot, anchor_date=anchor_date, triggered_by=actor,
                source=source, trigger_key=trigger_key,
            )
            for r in rows:
                OnboardingStepInstance.objects.create(
                    instance=instance, order=r["order"], title=r["title"], step_kind=r["step_kind"],
                    responsible_user_id=r["responsible_user_id"], due_date=r["due_date"],
                    expected_category_id=r["expected_category_id"], visible_to_employee=r["visible_to_employee"],
                    is_required=r["is_required"],
                )
            OnboardingInstanceEvent.objects.create(
                instance=instance, event_type=OnboardingInstanceEvent.EVENT_TRIGGERED, actor=actor,
                detail={"template_id": template.id, "anchor_date": anchor_date.isoformat(), "source": source},
            )
        return instance, True
    except IntegrityError:
        # Lost a race against a concurrent trigger for the SAME staff+kind --
        # the other call won (manual vs automatic, or two manual calls, or
        # two automatic passes -- the same guarantee covers all of them);
        # returning its (now-committed) active instance is exactly as
        # successful an outcome for this one.
        existing_active = OnboardingInstance.objects.get(staff=staff, process_kind=process_kind, status=OnboardingInstance.STATUS_ACTIVE)
        return existing_active, False


def cancel_onboarding(*, instance, actor, reason=""):
    if instance.status != OnboardingInstance.STATUS_ACTIVE:
        raise OnboardingError("Only an active onboarding instance can be cancelled.")
    with transaction.atomic():
        instance.status = OnboardingInstance.STATUS_CANCELLED
        instance.cancelled_by = actor
        instance.cancelled_at = timezone.now()
        instance.save(update_fields=["status", "cancelled_by", "cancelled_at"])
        instance.step_instances.filter(status=OnboardingStepInstance.STATUS_PENDING).update(
            status=OnboardingStepInstance.STATUS_CANCELLED,
        )
        OnboardingInstanceEvent.objects.create(
            instance=instance, event_type=OnboardingInstanceEvent.EVENT_CANCELLED, actor=actor, detail={"reason": reason},
        )
    return instance


def reassign_step(*, step_instance, new_responsible_user, actor):
    """Access to this step is always computed LIVE from `responsible_user`'s
    CURRENT value (see permissions.can_act_on_onboarding_step) -- updating it
    here is itself the "retrait effectif de l'acces operationnel de
    l'ancien responsable", nothing further to revoke separately."""
    old_responsible_id = step_instance.responsible_user_id
    step_instance.responsible_user = new_responsible_user
    step_instance.save(update_fields=["responsible_user"])
    OnboardingInstanceEvent.objects.create(
        instance=step_instance.instance, step_instance=step_instance,
        event_type=OnboardingInstanceEvent.EVENT_STEP_REASSIGNED, actor=actor,
        detail={"from_user_id": old_responsible_id, "to_user_id": new_responsible_user.id if new_responsible_user else None},
    )
    return step_instance


def _step_requirement_satisfied(step_instance):
    """"Une piece seulement deposee ne satisfait pas une exigence demandant
    une piece acceptee": a step with an expected_category is only
    satisfiable once the STAFF's latest file in that category reaches
    ACCEPTED -- never merely SUBMITTED/VERIFIED."""
    if step_instance.expected_category_id is None:
        return True
    staff = step_instance.instance.staff
    document = staff.documents.filter(category_id=step_instance.expected_category_id).order_by("-id").first()
    if document is None:
        return False
    latest_file = document.files.order_by("-version_number").first()
    if latest_file is None:
        return False
    return resolve_file_status(latest_file) == StaffDocumentFileStatusEvent.STATUS_ACCEPTED


def complete_step(*, step_instance, actor):
    if step_instance.status != OnboardingStepInstance.STATUS_PENDING:
        raise OnboardingError("This step is not pending.")
    if not _step_requirement_satisfied(step_instance):
        raise OnboardingError("This step's expected document has not reached 'accepted' status yet.")
    step_instance.status = OnboardingStepInstance.STATUS_DONE
    step_instance.completed_at = timezone.now()
    step_instance.save(update_fields=["status", "completed_at"])
    OnboardingInstanceEvent.objects.create(
        instance=step_instance.instance, step_instance=step_instance,
        event_type=OnboardingInstanceEvent.EVENT_STEP_COMPLETED, actor=actor,
    )
    return step_instance


def compute_progress(instance):
    required = list(instance.step_instances.filter(is_required=True))
    if not required:
        return {"required_total": 0, "required_done": 0, "complete": True}
    done = sum(1 for s in required if s.status == OnboardingStepInstance.STATUS_DONE)
    return {"required_total": len(required), "required_done": done, "complete": done == len(required)}


def close_onboarding(*, instance, actor):
    """"Aucune cloture silencieuse avec etapes obligatoires inachevees" --
    this is the ONLY path that can set status=completed, and it is always an
    explicit action, never an automatic side effect of finishing the last
    step."""
    if instance.status != OnboardingInstance.STATUS_ACTIVE:
        raise OnboardingError("Only an active onboarding instance can be closed.")
    progress = compute_progress(instance)
    if not progress["complete"]:
        raise OnboardingError(
            f"{progress['required_done']}/{progress['required_total']} required steps are done -- cannot close yet."
        )
    instance.status = OnboardingInstance.STATUS_COMPLETED
    instance.completed_at = timezone.now()
    instance.save(update_fields=["status", "completed_at"])
    OnboardingInstanceEvent.objects.create(
        instance=instance, event_type=OnboardingInstanceEvent.EVENT_COMPLETED, actor=actor,
    )
    return instance


def preview_arrival_date_change(*, instance, new_anchor_date):
    """"Proposer un apercu des changements avant application" -- never
    silently recomputes anything. Works identically for onboarding
    (arrival) and offboarding (departure) instances -- only PENDING steps
    shift; DONE/CANCELLED ones are never touched (no step already completed
    is ever rewritten, and no duplicate reminder can result since a step
    that already had a reminder delivery keeps its own due_date-keyed row --
    a changed due_date is simply a NEW occurrence for the reminder engine,
    never a silent re-send of the old one)."""
    offset = (new_anchor_date - instance.anchor_date).days
    rows = []
    for step in instance.step_instances.filter(status=OnboardingStepInstance.STATUS_PENDING).order_by("order"):
        rows.append({"step_instance_id": step.id, "title": step.title, "old_due_date": step.due_date, "new_due_date": step.due_date + timedelta(days=offset)})
    return rows


def apply_arrival_date_change(*, instance, new_anchor_date, actor):
    rows = preview_arrival_date_change(instance=instance, new_anchor_date=new_anchor_date)
    with transaction.atomic():
        old_anchor_date = instance.anchor_date
        instance.anchor_date = new_anchor_date
        instance.save(update_fields=["anchor_date"])
        for row in rows:
            OnboardingStepInstance.objects.filter(pk=row["step_instance_id"]).update(due_date=row["new_due_date"])
        OnboardingInstanceEvent.objects.create(
            instance=instance, event_type=OnboardingInstanceEvent.EVENT_ARRIVAL_DATE_CHANGED, actor=actor,
            detail={"from": old_anchor_date.isoformat(), "to": new_anchor_date.isoformat(), "steps_shifted": len(rows)},
        )
    return rows
