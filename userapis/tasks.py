import logging

from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.utils.timezone import now

from ecpmpapp.utils import create_notification_with_recipients
from userapis.models import CalendarEvent
from userapis.signals import calendar_event_object_label

logger = logging.getLogger(__name__)


def _calendar_reminder_recipients(event):
    """Match email reminder audience: assignees + owner."""
    recipients = set()
    if event.assignees.exists():
        recipients.update(event.assignees.all())

    owner = getattr(event, 'user', None) or getattr(event, 'created_by', None)
    if owner:
        recipients.add(owner)

    if event.freelancer:
        recipients.add(event.freelancer)

    if event.company_profile and event.company_profile.admin_user:
        recipients.add(event.company_profile.admin_user)

    return recipients


@shared_task(
    bind=True,
    name='userapis.send_calendar_event_reminder_notification',
    max_retries=2,
    default_retry_delay=30,
)
def send_calendar_event_reminder_notification(
    self, event_id, minutes, expected_start_iso=None
):
    """
    Create an in-app notification bell reminder for a calendar event.
    Stale tasks (event deleted/rescheduled) are no-ops.
    """
    try:
        event = (
            CalendarEvent.objects.filter(pk=event_id)
            .prefetch_related('assignees')
            .select_related(
                'user', 'created_by', 'freelancer', 'company_profile', 'project'
            )
            .first()
        )
        if not event:
            logger.info('Calendar reminder skipped; event %s missing', event_id)
            return

        if expected_start_iso and event.start_date.isoformat() != expected_start_iso:
            logger.info(
                'Calendar reminder skipped; event %s start changed', event_id
            )
            return

        minutes_until_start = (event.start_date - now()).total_seconds() / 60.0
        # Deliver from reminder time until the event starts.
        # (Late Celery pickup still notifies; too-early / post-start are skipped.)
        if minutes_until_start <= 0:
            logger.info(
                'Calendar reminder skipped; event %s already started', event_id
            )
            return
        if minutes_until_start > float(minutes) + 3:
            logger.info(
                'Calendar reminder skipped; event %s too early for %s-min '
                'reminder (%.1f min left)',
                event_id,
                minutes,
                minutes_until_start,
            )
            return

        recipients = _calendar_reminder_recipients(event)
        if not recipients:
            return

        message = (
            f'Reminder: {calendar_event_object_label(event)} '
            f'starts in {minutes} minutes.'
        )

        create_notification_with_recipients(
            message=message,
            content_type=ContentType.objects.get_for_model(CalendarEvent),
            object_id=event.pk,
            content_object=event,
            notification_type='calendar_event',
            action_type='update',
            recipients=recipients,
            project=event.project if event.project else None,
            # Email reminders go to participants without preference gating;
            # keep the bell in sync for assignees/owner.
            bypass_preference_check=True,
        )
    except Exception as exc:
        logger.exception(
            'Failed in-app calendar reminder for event %s: %s', event_id, exc
        )
        raise self.retry(exc=exc)
