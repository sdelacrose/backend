from django.core.exceptions import FieldDoesNotExist
from django.utils.text import capfirst
from django.dispatch import receiver
from django.db.models.signals import post_save, pre_delete, m2m_changed
from django.contrib.contenttypes.models import ContentType

from ecpmpapp.constants import CUSTOM_LABELS_CALENDAR_EVENT
from userapis.models import CalendarEvent
from ecpmpapp.middleware import get_current_user
from ecpmpapp.utils import (
    create_notification_with_recipients, 
    format_notification_message, get_actor_name,
    get_validate_recipients
)

def get_calendar_event_recipients(calendar_event):
    """
    Get all recipients for calendar event notifications.
    
    Args:
        calendar_event: CalendarEvent instance
    
    Returns:
        set: Set of User instances to notify
    """
    recipients = set()
    
    # Add assignees if any
    if calendar_event.assignees.exists():
        assignees_list = list(calendar_event.assignees.all())
        print("assignees_list --> ", assignees_list, flush=True)
        recipients.update(assignees_list)

    print("recipients after assignees --> ", recipients, flush=True)
    
    # Add freelancer if different from assignees
    if calendar_event.freelancer and calendar_event.freelancer not in recipients:
        recipients.add(calendar_event.freelancer)
    
    # Add company admin if company profile exists
    if calendar_event.company_profile and calendar_event.company_profile.admin_user:
        recipients.add(calendar_event.company_profile.admin_user)
    
    return recipients

def label_for_calendar_event(field_name):
    """Return a human-friendly label for a given CalendarEvent field."""
    if field_name in CUSTOM_LABELS_CALENDAR_EVENT:
        return CUSTOM_LABELS_CALENDAR_EVENT[field_name]
    try:
        field = CalendarEvent._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()


def calendar_event_object_label(event):
    """Label for notification text; includes project name when event is project-scoped."""
    label = f'Calendar Event "{event.name}"'
    project = getattr(event, 'project', None)
    project_name = getattr(project, 'project_name', None) if project else None
    if project_name:
        label = f'{label} for project "{project_name}"'
    return label


def notify_calendar_event_created(instance):
    """Send create notification after assignees (and other M2M) are set."""
    recipients = get_calendar_event_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)

    message = format_notification_message(
        'create',
        calendar_event_object_label(instance),
        actor_name
    )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(CalendarEvent),
        object_id=instance.pk,
        content_object=instance,
        notification_type='calendar_event',
        action_type='create',
        recipients=validated_recipients,
        project=instance.project if instance.project else None,
    )


@receiver(post_save, sender=CalendarEvent)
def notify_on_calendar_event_save(sender, instance, created, **kwargs):
    # Serializer create suppresses this and notifies after assignees are set
    # to avoid empty-assignee create + skipped M2M assignee notifies.
    if created:
        if getattr(instance, '_suppress_create_notification', False):
            return
        notify_calendar_event_created(instance)
    else:
        # For updates, only notify if there are meaningful changes (excluding assignees)
        # assume you have a tracker to detect changed fields
        changed_fields = instance.tracker.changed()
        meaningful = [f for f in changed_fields if f not in ['updated_at', 'assignees']]
        if not meaningful:
            return
        labels = ", ".join(label_for_calendar_event(f) for f in meaningful)
        
        recipients = get_calendar_event_recipients(instance)
        requester = get_current_user()
        actor_name = get_actor_name(requester)
        validated_recipients = get_validate_recipients(recipients, requester)
        
        message = format_notification_message(
            'update',
            calendar_event_object_label(instance),
            actor_name,
            field_changes=[labels]
        )

        create_notification_with_recipients(
            message=message,
            content_type=ContentType.objects.get_for_model(CalendarEvent),
            object_id=instance.pk,
            content_object=instance,
            notification_type='calendar_event',
            action_type='update',
            recipients=validated_recipients,
            project=instance.project if instance.project else None,
        )

@receiver(m2m_changed, sender=CalendarEvent.assignees.through)
def notify_on_calendar_event_assignees_changed(sender, instance, action, pk_set, **kwargs):
    """
    Handle notifications when assignees are added/removed from calendar events.
    """
    # print("=== M2M SIGNAL TRIGGERED ===", flush=True)
    # print("action --> ", action, flush=True)
    # print("instance --> ", instance, flush=True)
    # print("pk_set --> ", pk_set, flush=True)
    
    if action in ["post_add", "post_remove"]:
        # Skip notification if this is during initial creation (bypass flag is set)
        if getattr(instance, '_bypass_assignee_notification', False):
            return
        recipients = get_calendar_event_recipients(instance)
        requester = get_current_user()
        actor_name = get_actor_name(requester)
        validate_recipients = get_validate_recipients(recipients, requester)
        object_label = calendar_event_object_label(instance)

        if action == "post_add":
            message = format_notification_message(
                'update',
                object_label,
                actor_name,
                field_changes=['Assignees']
            )
        else:  # post_remove
            message = format_notification_message(
                'update',
                object_label,
                actor_name,
                field_changes=['Assignees']
            )

        create_notification_with_recipients(
            message=message,
            content_type=ContentType.objects.get_for_model(CalendarEvent),
            object_id=instance.pk,
            content_object=instance,
            notification_type='calendar_event',
            action_type='update',
            recipients=validate_recipients,
            project=instance.project if instance.project else None,
        )

@receiver(pre_delete, sender=CalendarEvent)
def notify_on_calendar_event_delete(sender, instance, **kwargs):
    recipients = get_calendar_event_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_removed_recipients = get_validate_recipients(recipients, requester)

    message = format_notification_message(
        'delete',
        calendar_event_object_label(instance),
        actor_name
    )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(CalendarEvent),
        object_id=instance.pk,
        content_object=instance,
        notification_type='calendar_event',
        action_type='delete',
        recipients=validated_removed_recipients,
        project=instance.project if instance.project else None,
    )
