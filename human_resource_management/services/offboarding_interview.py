"""L05 -- "reutiliser les capacites existantes de calendrier pour les
entretiens" (spec 6.9, criterion 4): a `step_kind=interview` step gets a
plain `userapis.CalendarEvent` row linked to it once RH/the responsible
EXPLICITLY picks a start/end time -- never a silently auto-picked slot (no
availability-checking engine exists to reuse, and building one would be a
new capability, not a reuse of an existing one -- criterion 4 is therefore
PARTIAL: "entretien a planifier" -> "creneau reellement reserve" is built;
an automatic best-slot-finder is not, and is reported as such).

Deliberately does NOT call `userapis.microsoft_outlook.
create_outlook_calendar_event` -- that is a separate, already-existing
integration requiring the specific acting user's own live Outlook OAuth
token (today only ever invoked from `ai_tools/services.py`, never from the
plain CalendarEventViewSet create path either), with real failure modes
(token expired/not connected) this lot has no business silently swallowing.
Calling it here would be a NEW dependency, not a reuse of "les capacites
existantes" as literally asked -- the CalendarEvent row this function
creates is a REAL reservation inside LINKS ("creneau reellement reserve"),
but `outlook_event_id` stays null unless a company's own, separate Outlook
sync already covers it: never claimed as "invitation envoyee" by this
code."""
from django.contrib.contenttypes.models import ContentType

from userapis.models import CalendarEvent
from userapis.signals import notify_calendar_event_created

from human_resource_management.models.onboarding import OnboardingInstanceEvent, StepKind


class InterviewSchedulingError(Exception):
    pass


def schedule_interview(*, step_instance, start_date, end_date, actor):
    if step_instance.step_kind != StepKind.INTERVIEW:
        raise InterviewSchedulingError("Only an interview-kind step can be scheduled this way.")
    if end_date <= start_date:
        raise InterviewSchedulingError("The interview's end time must be after its start time.")

    staff = step_instance.instance.staff
    assignees = {staff.user_id}
    if step_instance.responsible_user_id:
        assignees.add(step_instance.responsible_user_id)

    event = CalendarEvent(
        company_profile=step_instance.instance.company_profile,
        name=step_instance.title,
        description="",
        is_private=True,
        start_date=start_date,
        end_date=end_date,
        user=actor,
        created_by=actor,
    )
    # Mirrors CalendarEventSerializer.create's own suppress-then-notify
    # sequence (userapis/serializers/calendar.py) so assignees are attached
    # BEFORE the one real "created" notification fires -- never an empty-
    # assignee notification followed by a second, duplicate one.
    event._suppress_create_notification = True
    event.save()
    event._bypass_assignee_notification = True
    event.assignees.set(assignees)
    delattr(event, "_bypass_assignee_notification")
    notify_calendar_event_created(event)

    step_instance.calendar_event = event
    step_instance.save(update_fields=["calendar_event"])
    OnboardingInstanceEvent.objects.create(
        instance=step_instance.instance, step_instance=step_instance,
        event_type=OnboardingInstanceEvent.EVENT_INTERVIEW_SCHEDULED, actor=actor,
        detail={"calendar_event_id": event.id, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
    )
    return event
