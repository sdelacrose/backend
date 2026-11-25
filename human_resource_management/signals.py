from functools import partial

from django.core.exceptions import FieldDoesNotExist
from django.db import transaction
from django.utils.text import capfirst
from django.db.models.signals import post_save, pre_delete, m2m_changed
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from models.models import StaffConfiguration, Staff
from ecpmpapp.constants import (
    CUSTOM_LABELS_STAFF_OBSERVATION,
    StatusType,
)
from human_resource_management.models import StaffObservation, LeaveRequest
from human_resource_management.utils import (
    get_leave_reviewers,
    send_request_notification_email,
    send_request_status_email,
)
from ecpmpapp.middleware import get_current_user
from ecpmpapp.utils import (
    create_notification_with_recipients, 
    get_actor_name, get_validate_recipients
)
def label_for_staff_observation(field_name):
    """Return a human‑friendly label for a given StaffObservation field."""
    if field_name in CUSTOM_LABELS_STAFF_OBSERVATION:
        return CUSTOM_LABELS_STAFF_OBSERVATION[field_name]
    try:
        field = StaffObservation._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

@receiver(post_save, sender=LeaveRequest)
def notify_on_leave_request_save(sender, instance, created, **kwargs):
    requester = get_current_user()
    actor_name = get_actor_name(requester)

    if created:
        if instance.status != StatusType.PENDING.value:
            return

        employee_user = instance.employee.user
        reviewers = set(get_leave_reviewers(employee_user))
        recipients = get_validate_recipients(reviewers, employee_user)
        if not recipients:
            return

        create_notification_with_recipients(
            message=f'{get_actor_name(employee_user)} created a leave request',
            content_type=ContentType.objects.get_for_model(LeaveRequest),
            object_id=instance.pk,
            content_object=instance,
            notification_type='leave_request',
            action_type='create',
            recipients=recipients,
        )
        for reviewer in recipients:
            transaction.on_commit(
                partial(
                    send_request_notification_email,
                    employee_user,
                    reviewer,
                    instance,
                ),
                robust=True,
            )
        return
    else:
        changed = instance.tracker.changed() or []
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        message_for_employee = ""
        if meaningful == ['is_working_from_home']:
            val = 'Yes' if instance.is_working_from_home else 'No'
            message_for_employee = (
                f"Your work from home request was updated by {actor_name} to (field: {val})."
            )
        elif meaningful == ['status']:
            if instance.status == "approved":
                message_for_employee = (
                    f"Your leave request was approved by {actor_name}."
                )
            elif instance.status == "rejected":
                message_for_employee = (
                    f"Your leave request was rejected by {actor_name}."
                )
            else:
                message_for_employee = (
                    f"Your leave request was updated by {actor_name} to (field: {instance.status})."
                )
        else:
            message_for_employee = f"Your leave request was updated by {actor_name}."
        if instance.employee and instance.employee.user:
            recipients = {instance.employee.user}
            validate_recipients = get_validate_recipients(recipients, requester)
            create_notification_with_recipients(
                message=message_for_employee,
                content_type=ContentType.objects.get_for_model(LeaveRequest),
                object_id=instance.pk,
                content_object=instance,
                notification_type='leave_request',
                action_type='update',
                recipients=validate_recipients,
            )
            if (
                meaningful == ['status']
                and instance.status in {
                    StatusType.APPROVED.value,
                    StatusType.REJECTED.value,
                }
            ):
                reviewer = requester if getattr(requester, 'is_authenticated', False) else None
                transaction.on_commit(
                    partial(
                        send_request_status_email,
                        instance.employee.user,
                        reviewer,
                        instance,
                        instance.status,
                    ),
                    robust=True,
                )

@receiver(pre_delete, sender=LeaveRequest)
def notify_on_leave_request_delete(sender, instance, **kwargs):
    requester = get_current_user()
    actor_name = get_actor_name(requester)

    message_for_employee = f"Your leave request was deleted by {actor_name}."
    if instance.employee and instance.employee.user:
        recipients = {instance.employee.user}
        validate_recipients = get_validate_recipients(recipients, requester)
        create_notification_with_recipients(
            message=message_for_employee,
            content_type=ContentType.objects.get_for_model(LeaveRequest),
            object_id=instance.pk,
            content_object=instance,
            notification_type='leave_request',
            action_type='delete',
            recipients= validate_recipients
        )

@receiver(post_save, sender=StaffConfiguration)
def update_related_staff_profiles_on_default_management_change(sender, instance, created, **kwargs):
    # L00-B2A / DEC-52: "Default Management" (StaffConfiguration) is labeled
    # to the admin as the defaults for FUTURE recruitments -- it is NOT a
    # per-employee record. This receiver used to mass-overwrite every
    # EXISTING Staff of the company (annual_leaves_allowed, wfh_leaves_allowed,
    # weekly_hours, hourly_rate, salary) on every save of that form, silently
    # erasing real, already-confirmed individual situations (confirmed via a
    # real endpoint call, see lot_l00b2a_default_management_tests.py). DEC-52
    # keeps existing dossiers and any already-programmed individual change
    # untouched; a defaults change only ever affects the StaffConfiguration
    # row itself, read directly by whatever future onboarding flow needs it.
    # No current code path applies these defaults to a newly created Staff
    # either (Staff creation via the API is disabled; the one provisioning
    # management command hardcodes its own values and never reads this model)
    # -- that gap is unchanged by this fix, not something invented here.
    return


# AI-2054: real-time workload re-evaluation on the Project Management writes
# that can change a staff member's occupancy -- attached at the MODEL level
# (ActionItem save/delete/assignees-changed, StaffQuotiteHistory save) so
# every PM write path (direct save, the bulk-update service, serializers,
# admin) is covered by this ONE shared receiver each, without touching any
# Project Management file. Dispatches a lightweight, isolated Celery task
# (never inline) from `transaction.on_commit`, so a slow/failing evaluation
# can never block or roll back the PM write itself, and only ever runs
# AFTER that write has actually committed.
from project_management.models import ActionItem
from human_resource_management.models.leave_entitlement import StaffQuotiteHistory
from human_resource_management.services.workload import (
    resolve_action_item_company_profile_id,
    resolve_action_item_solution,
)


def _dispatch_workload_reevaluation(staff, company_profile_id, solution):
    if not staff or not company_profile_id or not solution:
        return
    from human_resource_management.models.workload import StaffWorkloadAlertConfig
    config = StaffWorkloadAlertConfig.objects.filter(
        company_config__company_profile_id=company_profile_id,
        company_config__solution=solution,
        is_active=True,
    ).first()
    if config is None:
        return

    def _enqueue(staff_id=staff.id, config_id=config.id):
        from human_resource_management.tasks import evaluate_staff_workload_for_signal_task
        evaluate_staff_workload_for_signal_task.delay(staff_id, config_id)

    transaction.on_commit(_enqueue)


def _effective_assignee_users(instance):
    assignees = list(instance.assignees.all())
    if instance.assignee_id and instance.assignee_id not in {u.id for u in assignees}:
        assignees.append(instance.assignee)
    return assignees


@receiver(post_save, sender=ActionItem)
def reevaluate_workload_on_action_item_save(sender, instance, created, **kwargs):
    # Covers budget/dates/status/hierarchy changes -- the engine itself
    # (services.workload) re-derives everything fresh on each evaluation,
    # so a single dispatch per current assignee is always sufficient.
    company_profile_id = resolve_action_item_company_profile_id(instance)
    solution = resolve_action_item_solution(instance)
    for user in _effective_assignee_users(instance):
        staff = getattr(user, "staff", None)
        if staff:
            _dispatch_workload_reevaluation(staff, company_profile_id, solution)


@receiver(pre_delete, sender=ActionItem)
def reevaluate_workload_on_action_item_delete(sender, instance, **kwargs):
    # pre_delete (not post_delete): the M2M/FK chain is still readable here.
    company_profile_id = resolve_action_item_company_profile_id(instance)
    solution = resolve_action_item_solution(instance)
    for user in _effective_assignee_users(instance):
        staff = getattr(user, "staff", None)
        if staff:
            _dispatch_workload_reevaluation(staff, company_profile_id, solution)


@receiver(m2m_changed, sender=ActionItem.assignees.through)
def reevaluate_workload_on_action_item_assignees_changed(sender, instance, action, pk_set, **kwargs):
    if action not in {"post_add", "post_remove"}:
        return
    company_profile_id = resolve_action_item_company_profile_id(instance)
    solution = resolve_action_item_solution(instance)
    for user_id in (pk_set or set()):
        staff = Staff.objects.filter(user_id=user_id).select_related("user").first()
        if staff:
            _dispatch_workload_reevaluation(staff, company_profile_id, solution)


@receiver(post_save, sender=StaffQuotiteHistory)
def reevaluate_workload_on_quotite_change(sender, instance, created, **kwargs):
    # A changed contractual situation changes CAPACITY, not PM data -- the
    # solution isn't known from this side, so every active config of this
    # staff member's own company is targeted (narrow: a handful of
    # solutions per company at most).
    staff = instance.staff
    company_profile_id = getattr(staff.user, "company_id", None)
    if not company_profile_id:
        return
    from human_resource_management.models.workload import StaffWorkloadAlertConfig
    for config in StaffWorkloadAlertConfig.objects.filter(
        company_config__company_profile_id=company_profile_id, is_active=True,
    ):
        def _enqueue(staff_id=staff.id, config_id=config.id):
            from human_resource_management.tasks import evaluate_staff_workload_for_signal_task
            evaluate_staff_workload_for_signal_task.delay(staff_id, config_id)
        transaction.on_commit(_enqueue)
