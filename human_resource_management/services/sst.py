"""LOT L04 -- SST business logic. The one write path for SSTCase/
SSTCasePeriod/SSTCaseEvent, and the only code that ever creates/corrects/
cancels a `LeaveRequest` on behalf of the SST module.

Why this never goes through `LeaveRequestSerializer`/`LeaveRequestViewSet`:
an SST period is recorded by RH as an administrative FACT ("RH enregistre
l'arret"), never submitted for a line manager's approval -- there is no
pending/review step to reuse. Creating the LeaveRequest directly, already
`status='approved'`, also sidesteps `notify_on_leave_request_save`'s
reviewer-notification branch entirely (it only fires for a newly-created
PENDING request, see signals.py) -- avoiding, by construction rather than by
patching that shared signal, the confidentiality leak a 'pending' SST row
would otherwise cause (a team manager holding the ordinary
`leave_request.review` right would be notified and see it in their review
queue). `LeaveRequestViewSet`'s own queryset separately excludes every
`is_sst_managed` row outright (see views/leave.py), so this module owns the
entire read/write surface for these rows.

Overlap/concurrency: reuses `_requests_conflict` (serializers/leave.py) and
the SAME per-employee `select_for_update()` lock already used by
`LeaveRequestSerializer.update()` -- never a second, parallel concurrency
mechanism.

RH13: `check_late_decision_signal` is called explicitly after any save that
changes an SST LeaveRequest's dates/status (create/correct/cancel) --
bypassing the serializer means it would otherwise never fire for these rows,
silently missing the late-decision detection RH13 already provides for
every other leave request.
"""
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from ecpmpapp.constants import StatusType
from ecpmpapp.utils import create_notification_with_recipients, get_actor_name
from models.models import CompanyConfig, LeaveType, Staff, User

from human_resource_management.models.leave import LeaveRequest
from human_resource_management.models.sst import SSTCase, SSTCaseEvent, SSTCasePeriod
from human_resource_management.permissions import can_manage_sst_case, has_sst_payroll_attestation_permission
from human_resource_management.serializers.leave import _requests_conflict
from human_resource_management.services.leave_absence_signal import check_late_decision_signal
from human_resource_management.services.staff_documents import create_staff_document


class SSTError(Exception):
    """Raised for any refused SST operation -- callers (views) turn this
    into a 400/403 as appropriate, never a raw 500."""


# A company-scope LeaveType is lazily find-or-created per category, the
# first time it's actually needed -- no manual setup screen required before
# SST can be used. `consumes_balance=False` always (ticket's own explicit
# non-deduction requirement for every SST category, not only sick/maternity
# -- a work accident or a declared incident never consumes paid leave
# either); `is_sst_managed=True` always (excluded from the classic form,
# see views.leave_type.applicable and LeaveRequestSerializer.validate).
_CATEGORY_LEAVE_TYPE_KEYS = {
    SSTCase.CATEGORY_SICK_LEAVE: ("sst-sick-leave", "SST -- Maladie"),
    SSTCase.CATEGORY_LONG_TERM_SICK_LEAVE: ("sst-long-term-sick-leave", "SST -- Maladie longue duree"),
    SSTCase.CATEGORY_MATERNITY: ("sst-maternity", "SST -- Maternite"),
    SSTCase.CATEGORY_WORK_ACCIDENT: ("sst-work-accident", "SST -- Accident du travail"),
    SSTCase.CATEGORY_INCIDENT_NO_HARM: ("sst-incident-no-harm", "SST -- Incident sans dommage"),
}


def _resolve_sst_leave_type(company_profile, category):
    """Find-or-create this company's own LeaveType row for `category`.

    JUDGMENT CALL (documented, not hidden): a LeaveType is structurally tied
    to one `CompanyConfig` (one per licensed solution -- see RH13's
    solution-isolation work), but SST is a standalone Profile-level feature
    with no solution context of its own. This picks the company's FIRST
    CompanyConfig, whichever solution that happens to be -- correct for the
    common single-solution company, a known simplification for a company
    licensed on several solutions at once (flagged in the closure report,
    never silently assumed correct)."""
    company_config = CompanyConfig.objects.filter(company_profile=company_profile).order_by("id").first()
    if company_config is None:
        raise SSTError("This company has no configuration yet; cannot resolve an SST leave type.")
    key, name = _CATEGORY_LEAVE_TYPE_KEYS[category]
    leave_type, _created = LeaveType.objects.get_or_create(
        company_config=company_config, key=key,
        defaults=dict(
            name=name,
            is_active=True,
            deduction_mode=LeaveType.DEDUCTION_CALENDAR_DAYS,
            allow_half_day=False,
            requires_supporting_document=False,
            consumes_balance=False,
            is_sst_managed=True,
        ),
    )
    return leave_type


def _log(case, event_type, actor, note=""):
    return SSTCaseEvent.objects.create(case=case, event_type=event_type, actor=actor, note=note)


def _sst_recipients(company_profile_id, *, exclude_user_id=None):
    """Same query shape as human_resource_management.utils.get_leave_
    reviewers, swapped to the SST permission keys -- never the general
    leave-reviewer resolution (that's exactly the leak this module exists
    to avoid)."""
    qs = User.objects.filter(
        company_id=company_profile_id,
        is_active=True,
        is_deleted=False,
        user_company_roles__role__company_id=company_profile_id,
        user_company_roles__role__scope='company',
        user_company_roles__role__type='solution',
        user_company_roles__role__is_active=True,
        user_company_roles__role__role_permissions__permission__key__in=(
            'staff_management.manage_sst_cases', 'staff_management.view_sst_dossier',
        ),
    ).distinct()
    if exclude_user_id:
        qs = qs.exclude(id=exclude_user_id)
    return qs


def _notify_sst_habilitated(case, message, *, actor):
    recipients = set(_sst_recipients(case.company_profile_id, exclude_user_id=getattr(actor, "id", None)))
    if not recipients:
        return
    transaction.on_commit(lambda: create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(SSTCase),
        object_id=case.id,
        content_object=case,
        notification_type='sst_case',
        action_type='update',
        recipients=recipients,
    ))


def _notify_employee(case, message):
    employee_user = case.staff.user
    transaction.on_commit(lambda: create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(SSTCase),
        object_id=case.id,
        content_object=case,
        notification_type='sst_case',
        action_type='update',
        recipients={employee_user},
    ))


def declare_case(*, staff, category, actor, external_reference=""):
    """Declaration by the employee concerned, for themselves, OR by an
    SST-habilitated RH, for any staff of her own company -- both explicit
    ticket requirements. Never auto-creates a period/LeaveRequest (the
    ticket's own instruction: declaration alone must never imply an
    absence)."""
    is_self = actor.id == staff.user_id
    if not (is_self or can_manage_sst_case(actor, staff)):
        raise SSTError("You do not have permission to declare an SST case for this staff member.")
    if category not in _CATEGORY_LEAVE_TYPE_KEYS:
        raise SSTError("Unknown SST category.")
    with transaction.atomic():
        case = SSTCase.objects.create(
            staff=staff, company_profile=staff.user.company, category=category,
            declared_by=actor, external_reference=external_reference or "",
        )
        _log(case, SSTCaseEvent.EVENT_DECLARED, actor)
    _notify_sst_habilitated(
        case, f"{get_actor_name(actor)} declared a new SST case for {staff.user.full_name}.", actor=actor,
    )
    return case


def _create_sst_period(case, start_date, end_date, *, actor, is_initial, event_type, event_note):
    """Shared by record_period (first period) and extend_period
    (prolongation) -- always a NEW LeaveRequest/SSTCasePeriod, never a
    rewrite of an earlier one's dates (see correct_period for that)."""
    if end_date < start_date:
        raise SSTError("End date cannot be before start date.")
    staff = case.staff
    with transaction.atomic():
        # Same per-employee lock LeaveRequestSerializer.update() already
        # uses -- a concurrent SST/classic operation for this SAME employee
        # is fully serialized, never merely checked-then-raced.
        Staff.objects.select_for_update().get(pk=staff.id)
        existing = LeaveRequest.objects.filter(employee=staff, status=StatusType.APPROVED.value)
        for other in existing:
            if _requests_conflict(start_date, end_date, False, None, other.start_date, other.end_date, other.is_half_day, other.half_day_period):
                raise SSTError(
                    f"This period overlaps an existing approved absence (#{other.id}, "
                    f"{other.start_date}..{other.end_date})."
                )
        leave_type = _resolve_sst_leave_type(staff.user.company, case.category)
        leave_request = LeaveRequest.objects.create(
            employee=staff, type=leave_type.name, leave_type=leave_type,
            start_date=start_date, end_date=end_date,
            status=StatusType.APPROVED.value,
            decided_at=case.declared_at, decided_by=actor,
        )
        period = SSTCasePeriod.objects.create(
            case=case, leave_request=leave_request, is_initial=is_initial, created_by=actor,
        )
        _log(case, event_type, actor, event_note)
    # Outside the transaction (reads already-committed state), same
    # convention as the serializer's own call site.
    check_late_decision_signal(leave_request, event_type=StatusType.APPROVED.value, actor=actor)
    return period


def record_period(*, case, start_date, end_date, actor):
    """Only an SST-habilitated RH records the administrative period -- the
    ticket's own instruction ("seule la RH habilitee enregistre les periodes
    administratives confirmees"), never the employee themselves even for
    their own case."""
    if not can_manage_sst_case(actor, case.staff):
        raise SSTError("You do not have permission to record a period for this SST case.")
    # A brand-new, contiguous period every time -- a prolongation is simply
    # a second (or third...) call to this SAME function, is_initial auto-
    # detected, never an in-place edit of an earlier period (see
    # correct_period for that distinct operation).
    is_initial = not case.periods.filter(is_initial=True).exists()
    period = _create_sst_period(
        case, start_date, end_date, actor=actor, is_initial=is_initial,
        event_type=SSTCaseEvent.EVENT_PERIOD_RECORDED if is_initial else SSTCaseEvent.EVENT_PERIOD_EXTENDED,
        event_note=f"{start_date}..{end_date}",
    )
    action_label = "recorded an SST period for" if is_initial else "extended the SST case for"
    _notify_sst_habilitated(case, f"{get_actor_name(actor)} {action_label} {case.staff.user.full_name}.", actor=actor)
    _notify_employee(case, "A period was recorded for your SST case." if is_initial else "Your SST case was extended.")
    return period


def correct_period(*, period, start_date, end_date, actor, reason=""):
    """A TARGETED correction of an EXISTING period's own dates -- never a
    blanket lift of LeaveRequestSerializer's "already reviewed" lock (that
    lock is never even reached: this writes the LeaveRequest row directly).
    Re-validates the overlap guard excluding the period's own request."""
    case = period.case
    if not can_manage_sst_case(actor, case.staff):
        raise SSTError("You do not have permission to correct this SST case.")
    if period.cancelled_at is not None:
        raise SSTError("Cannot correct a cancelled period.")
    if end_date < start_date:
        raise SSTError("End date cannot be before start date.")
    staff = case.staff
    leave_request = period.leave_request
    old_range = f"{leave_request.start_date}..{leave_request.end_date}"
    with transaction.atomic():
        Staff.objects.select_for_update().get(pk=staff.id)
        existing = LeaveRequest.objects.filter(
            employee=staff, status=StatusType.APPROVED.value,
        ).exclude(pk=leave_request.pk)
        for other in existing:
            if _requests_conflict(start_date, end_date, False, None, other.start_date, other.end_date, other.is_half_day, other.half_day_period):
                raise SSTError(
                    f"This correction overlaps another approved absence (#{other.id}, "
                    f"{other.start_date}..{other.end_date})."
                )
        leave_request.start_date = start_date
        leave_request.end_date = end_date
        leave_request.save(update_fields=["start_date", "end_date"])
        _log(
            case, SSTCaseEvent.EVENT_PERIOD_CORRECTED, actor,
            f"{old_range} -> {start_date}..{end_date}" + (f" ({reason})" if reason else ""),
        )
    check_late_decision_signal(leave_request, event_type=StatusType.APPROVED.value, actor=actor)
    _notify_employee(case, "A period of your SST case was corrected.")
    return period


def cancel_period(*, period, actor, reason=""):
    """Voids a period -- reuses the existing `rejected` status (the closest
    semantic fit already on LeaveRequest.LEAVE_STATUSES, no new status
    invented) so every existing "approved-only" reader (calendar,
    availability, RH13 accrual impact) stops counting it as an absence for
    free, with no changes needed there. The SSTCasePeriod itself keeps its
    OWN cancelled_at/by/reason -- a separate, SST-specific audit trail,
    never only the generic LeaveRequest one."""
    case = period.case
    if not can_manage_sst_case(actor, case.staff):
        raise SSTError("You do not have permission to cancel this period.")
    if period.cancelled_at is not None:
        raise SSTError("This period is already cancelled.")
    from django.utils import timezone

    leave_request = period.leave_request
    with transaction.atomic():
        Staff.objects.select_for_update().get(pk=case.staff_id)
        leave_request.status = StatusType.REJECTED.value
        leave_request.decided_at = timezone.now()
        leave_request.decided_by = actor
        leave_request.save(update_fields=["status", "decided_at", "decided_by"])
        period.cancelled_at = timezone.now()
        period.cancelled_by = actor
        period.cancellation_reason = reason or ""
        period.save(update_fields=["cancelled_at", "cancelled_by", "cancellation_reason"])
        _log(case, SSTCaseEvent.EVENT_PERIOD_CANCELLED, actor, reason)
    check_late_decision_signal(leave_request, event_type=StatusType.REJECTED.value, actor=actor)
    _notify_employee(case, "A period of your SST case was cancelled.")
    return period


def record_decision(*, case, note, actor):
    """"Suites donnees, notamment une adaptation de poste" -- a plain
    administrative note, never a diagnosis/medical field."""
    if not can_manage_sst_case(actor, case.staff):
        raise SSTError("You do not have permission to record a decision for this SST case.")
    with transaction.atomic():
        case.workplace_adjustment_note = note
        case.save(update_fields=["workplace_adjustment_note"])
        _log(case, SSTCaseEvent.EVENT_DECISION_RECORDED, actor, note)
    _notify_employee(case, "A decision was recorded for your SST case.")
    return case


def close_case(*, case, actor):
    if not can_manage_sst_case(actor, case.staff):
        raise SSTError("You do not have permission to close this SST case.")
    if case.status == SSTCase.STATUS_CLOSED:
        raise SSTError("This case is already closed.")
    from django.utils import timezone

    with transaction.atomic():
        case.status = SSTCase.STATUS_CLOSED
        case.closed_at = timezone.now()
        case.closed_by = actor
        case.save(update_fields=["status", "closed_at", "closed_by"])
        _log(case, SSTCaseEvent.EVENT_CLOSED, actor)
    return case


def log_document_added(*, case, actor, title):
    """Called by the StaffDocument create view right after a document is
    linked to this case (sst_case set) -- see views.staff_documents."""
    _log(case, SSTCaseEvent.EVENT_DOCUMENT_ADDED, actor, title)
    _notify_sst_habilitated(
        case, f"{get_actor_name(actor)} added a document to the SST case for {case.staff.user.full_name}.",
        actor=actor,
    )
    _notify_employee(case, "A document was added to your SST case.")


# --- Payroll: a NARROW, allowlisted projection -- never the dossier/documents ---

def fetch_payroll_projection(*, company_profile, actor):
    """Dates and administrative nature ONLY -- never reason/notes/documents.
    Gated by has_sst_payroll_view_permission at the VIEW layer (same
    convention as every other service function here); `actor` is accepted
    for signature symmetry/future auditing, not consulted a second time."""
    rows = []
    for case in SSTCase.objects.filter(company_profile=company_profile).select_related("staff__user"):
        periods = case.periods.exclude(cancelled_at__isnull=False).select_related("leave_request")
        for period in periods:
            rows.append({
                "sst_case_id": case.id,
                "staff_id": case.staff_id,
                "staff_name": case.staff.user.full_name,
                "category": case.category,
                "start_date": period.leave_request.start_date,
                "end_date": period.leave_request.end_date,
                "status": case.status,
            })
    return rows


def deposit_payroll_attestation(*, case, actor, uploaded_file, category):
    """Payroll's own write path: deposit the salary attestation as an
    ordinary SST StaffDocument (is_sst=True), visible to the employee and
    SST-habilitated RH afterwards -- payroll itself never regains read
    access through this call (deposit-only, per the ticket's explicit
    separate-permissions instruction)."""
    if not has_sst_payroll_attestation_permission(actor):
        raise SSTError("You do not have permission to deposit a payroll attestation.")
    if not category.is_sst:
        raise SSTError("The attestation category must be an SST category.")
    from human_resource_management.services.staff_documents import create_staff_document_file

    with transaction.atomic():
        document = create_staff_document(
            staff=case.staff, company_profile=case.company_profile, category=category,
            title="Attestation de salaire", actor=actor, sst_case=case,
        )
        create_staff_document_file(staff_document=document, actor=actor, uploaded_file=uploaded_file)
        _log(case, SSTCaseEvent.EVENT_PAYROLL_ATTESTATION_DEPOSITED, actor)
    _notify_employee(case, "Your salary attestation is now available.")
    return document
