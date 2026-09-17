from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.core.exceptions import FieldDoesNotExist
from django.utils.text import capfirst
from django.contrib.contenttypes.models import ContentType

from ecpmpapp.constants import CUSTOM_LABELS_OPPORTUNITY
from companyprofileapis.models import Opportunity
from ecpmpapp.middleware import get_current_user
from ecpmpapp.utils import (
    create_notification_with_recipients, 
    format_notification_message, get_actor_name
)

def get_opportunity_recipients(opportunity):
    """
    Get all recipients for opportunity notifications.
    
    Args:
        opportunity: Opportunity instance
    
    Returns:
        set: Set of User instances to notify
    """
    recipients = set()
    
    # print(f"=== DEBUGGING OPPORTUNITY RECIPIENTS ===", flush=True)
    # print(f"Opportunity ID: {opportunity.id}", flush=True)
    # print(f"Opportunity Title: {opportunity.title}", flush=True)
    # print(f"Freelancer: {opportunity.freelancer}", flush=True)
    # print(f"Created By: {opportunity.created_by}", flush=True)
    # print(f"Approver: {opportunity.approver}", flush=True)
    # print(f"Company Profile: {opportunity.company_profile}", flush=True)
    
    # Add freelancer if assigned
    if opportunity.freelancer:
        recipients.add(opportunity.freelancer)
    
    # Add creator if different from freelancer
    if opportunity.created_by and opportunity.created_by != opportunity.freelancer:
        recipients.add(opportunity.created_by)
    
    # Add approver if different from creator and freelancer
    if opportunity.approver and opportunity.approver not in recipients:
        recipients.add(opportunity.approver)
    
    # Add company admin if company profile exists
    if opportunity.company_profile and opportunity.company_profile.admin_user:
        recipients.add(opportunity.company_profile.admin_user)
    
    return recipients

def label_for_opportunity(field_name):
    """Return a human‑friendly label for a given Opportunity field."""
    if field_name in CUSTOM_LABELS_OPPORTUNITY:
        return CUSTOM_LABELS_OPPORTUNITY[field_name]
    try:
        field = Opportunity._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

@receiver(post_save, sender=Opportunity)
def notify_on_opportunity_save(sender, instance, created, **kwargs):
    print("=== OPPORTUNITY SAVE SIGNAL TRIGGERED ===", flush=True)

    recipients = get_opportunity_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)

    if created:
        action_type = 'create'
        message = format_notification_message(
            'create',
            f'Opportunity "{instance.title}"',
            actor_name
        )
    else:
        action_type = 'update'
        changed_fields = getattr(instance, 'tracker', None) and instance.tracker.changed() or []
        meaningful = [f for f in changed_fields if f != 'updated_at']
        if not meaningful:
            return
        labels = ", ".join(label_for_opportunity(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Opportunity "{instance.title}"',
            actor_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Opportunity),
        object_id=instance.pk,
        content_object=instance,
        action_type=action_type,
        notification_type='opportunity',
        recipients=recipients,
    )

@receiver(pre_delete, sender=Opportunity)
def notify_on_opportunity_delete(sender, instance, **kwargs):
    recipients = get_opportunity_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)

    message = format_notification_message(
        'delete',
        f'Opportunity "{instance.title}"',
        actor_name
    )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Opportunity),
        object_id=instance.pk,
        content_object=instance,
        notification_type='delete',
        recipients=recipients,
    )
