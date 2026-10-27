"""L03/L05 -- due-date reminders for onboarding steps and document validity,
reusing the AI-1685 dedup/retry/stop-at-resolution shape (see
human_resource_management/models/hr_reminders.py's module docstring for the
scoping note: no separate configurable Rule layer in this lot, same ledger
machinery as LeaveReminderDelivery/leave_reminders.py otherwise)."""
from datetime import date, timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from ecpmpapp.utils import create_notification_with_recipients
from human_resource_management.models.hr_reminders import (
    OnboardingStepReminderDelivery, ReminderChannel, ReminderStatus, StaffDocumentValidityReminderDelivery,
)
from human_resource_management.models.onboarding import OnboardingStepInstance
from human_resource_management.models.staff_documents import StaffDocumentFile, StaffDocumentFileStatusEvent
from human_resource_management.services.staff_documents import resolve_file_status

ONBOARDING_STEP_REMINDER_ANTICIPATION_DAYS = 3
DOCUMENT_VALIDITY_REMINDER_ANTICIPATION_DAYS = 30


def notify_step_assigned(*, step_instance, actor):
    """One-time, in-app-only notification (safe by construction -- never an
    external send) fired right after the assigning transaction commits --
    see views/onboarding.py, which wraps the call in
    transaction.on_commit(...)."""
    recipient = step_instance.responsible_user
    if recipient is None or recipient.id == getattr(actor, "id", None):
        return None
    content_type = ContentType.objects.get_for_model(step_instance)
    return create_notification_with_recipients(
        message=f"You have been assigned the onboarding step \"{step_instance.title}\" (due {step_instance.due_date}).",
        content_type=content_type, object_id=step_instance.id, content_object=step_instance,
        notification_type="onboarding_step_assigned", recipients={recipient}, action_type="create",
    )


def notify_document_requested(*, staff_document, actor):
    """One-time, in-app-only notification to the collaborator that a piece
    has been requested of them -- never includes any existing document's
    content, only the category name."""
    if not staff_document.staff_id:
        return None
    recipient = staff_document.staff.user
    if recipient is None or recipient.id == getattr(actor, "id", None):
        return None
    content_type = ContentType.objects.get_for_model(staff_document)
    return create_notification_with_recipients(
        message=f"A document is expected from you: \"{staff_document.category.name}\".",
        content_type=content_type, object_id=staff_document.id, content_object=staff_document,
        notification_type="staff_document_requested", recipients={recipient}, action_type="create",
    )


def _claim_delivery(model, lookup, *, manual):
    with transaction.atomic():
        delivery, created = model.objects.get_or_create(**lookup, defaults={"triggered_manually": manual})
        if created:
            return delivery
        delivery = model.objects.select_for_update().get(pk=delivery.pk)
        if delivery.status != ReminderStatus.FAILED:
            return None
        delivery.status = ReminderStatus.PENDING
        delivery.last_error = ""
        delivery.save(update_fields=["status", "last_error"])
        return delivery


def _deliver(delivery, send_fn):
    try:
        send_fn()
        delivery.status = ReminderStatus.SENT
        delivery.sent_at = timezone.now()
        delivery.save(update_fields=["status", "sent_at"])
    except Exception as exc:  # noqa: BLE001 -- recorded, never silently swallowed, never retried blindly
        delivery.status = ReminderStatus.FAILED
        delivery.last_error = str(exc)
        delivery.save(update_fields=["status", "last_error"])


def process_onboarding_step_reminders(*, manual=False, only_step_instance=None):
    """Live re-derivation on every call, never a cached "already resolved"
    flag: a step that is no longer PENDING (completed/cancelled since the
    last run) or whose responsible changed is simply excluded from THIS
    run's actionable set -- there is nothing to "stop", because nothing
    stale is ever read."""
    today = date.today()
    window_end = today + timedelta(days=ONBOARDING_STEP_REMINDER_ANTICIPATION_DAYS)
    qs = OnboardingStepInstance.objects.filter(status=OnboardingStepInstance.STATUS_PENDING, due_date__lte=window_end)
    if only_step_instance is not None:
        qs = qs.filter(pk=only_step_instance.pk)
    sent, failed, ignored = 0, 0, 0
    for step in qs.select_related("responsible_user", "instance"):
        recipient = step.responsible_user
        if recipient is None:
            ignored += 1
            continue
        # Live permission re-check at send time: the recipient must still be
        # a member of the SAME company as the instance (never trusted from
        # when the step was assigned).
        if getattr(recipient, "company_id", None) != step.instance.company_profile_id:
            ignored += 1
            continue
        delivery = _claim_delivery(
            OnboardingStepReminderDelivery,
            {"step_instance": step, "recipient": recipient, "channel": ReminderChannel.INTERNAL},
            manual=manual,
        )
        if delivery is None:
            continue
        content_type = ContentType.objects.get_for_model(step)

        def _send(step=step, recipient=recipient, content_type=content_type):
            result = create_notification_with_recipients(
                message=f"Reminder: the onboarding step \"{step.title}\" is due {step.due_date}.",
                content_type=content_type, object_id=step.id, content_object=step,
                notification_type="onboarding_step_due_soon", recipients={recipient}, action_type="update",
                bypass_preference_check=True,
            )
            if result is None:
                raise RuntimeError("Notification was not created (no eligible recipient).")

        _deliver(delivery, _send)
        if delivery.status == ReminderStatus.SENT:
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "ignored": ignored}


def process_staff_document_validity_reminders(*, manual=False, only_file=None):
    today = date.today()
    window_end = today + timedelta(days=DOCUMENT_VALIDITY_REMINDER_ANTICIPATION_DAYS)
    qs = StaffDocumentFile.objects.filter(valid_until__isnull=False, valid_until__lte=window_end, valid_until__gte=today)
    if only_file is not None:
        qs = qs.filter(pk=only_file.pk)
    sent, failed, ignored = 0, 0, 0
    for file in qs.select_related("staff_document__staff__user"):
        # Live resolution: a file superseded by a newer version, or no
        # longer ACCEPTED-worthy, is simply not excluded here -- validity
        # expiry concerns EVERY version that still carries a future date;
        # what stops the reminder is the file's own valid_until passing (it
        # naturally falls out of the qs filter above) or the document being
        # renewed with a NEW file (a NEW, unrelated delivery row, by design
        # -- see StaffDocumentValidityReminderDelivery's own docstring).
        if resolve_file_status(file) != StaffDocumentFileStatusEvent.STATUS_ACCEPTED:
            ignored += 1
            continue
        staff = file.staff_document.staff
        if staff is None or staff.user is None:
            ignored += 1
            continue
        recipient = staff.user
        delivery = _claim_delivery(
            StaffDocumentValidityReminderDelivery,
            {"file": file, "recipient": recipient, "channel": ReminderChannel.INTERNAL},
            manual=manual,
        )
        if delivery is None:
            continue
        content_type = ContentType.objects.get_for_model(file)

        def _send(file=file, recipient=recipient, content_type=content_type):
            result = create_notification_with_recipients(
                message=f"A document (\"{file.staff_document.title}\") expires on {file.valid_until}.",
                content_type=content_type, object_id=file.id, content_object=file,
                notification_type="staff_document_validity_expiring", recipients={recipient}, action_type="update",
                bypass_preference_check=True,
            )
            if result is None:
                raise RuntimeError("Notification was not created (no eligible recipient).")

        _deliver(delivery, _send)
        if delivery.status == ReminderStatus.SENT:
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "ignored": ignored}
