from functools import partial

from django.core.exceptions import FieldDoesNotExist
from django.db import transaction
from django.utils.text import capfirst
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from models.models import CompanyProfile,  StaffConfiguration, Staff
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
    if created:
        return
    else:
        company_profile = CompanyProfile.objects.get(id=instance.company_profile.id)
        company_users = company_profile.users.all()
        staff_profiles = Staff.objects.filter(user__in=company_users)
        for staff_profile in staff_profiles:
            staff_profile.annual_leaves_allowed = instance.number_of_leaves
            staff_profile.wfh_leaves_allowed = instance.number_of_wfh_leaves
            staff_profile.weekly_hours = instance.number_of_hours
            staff_profile.hourly_rate = instance.hourly_rate
            staff_profile.salary = instance.base_salary
            staff_profile.save()
