from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.utils.text import capfirst
from django.dispatch import receiver
from django.db.models.signals import post_save, post_init, pre_save, pre_delete, m2m_changed
from django.contrib.contenttypes.models import ContentType

from models.models import UserOutlookCredentials

from ecpmpapp.constants import CUSTOM_LABELS_CALENDAR_EVENT
from userapis.models import CalendarEvent
from userapis.token_encryption import decrypt_token, encrypt_token
from ecpmpapp.middleware import get_current_user
from ecpmpapp.utils import (
    create_notification_with_recipients,
    format_notification_message, get_actor_name,
    get_validate_recipients
)


# SECURITY-MAIL-0C: read-side-only decryption for the Microsoft OAuth tokens stored on
# UserOutlookCredentials (admin-server-models -- this signal attaches the behaviour from here
# without touching that model's own file). Deliberately post_init, not pre_save: this lot does
# not enable write-side encryption (SECURITY-MAIL-0D's job) -- every existing read site
# (user.outlook_credentials.outlook_access_token, enumerated in the SECURITY-MAIL-0A report)
# keeps working completely unchanged, transparently seeing plaintext in memory whether the
# stored value is a legacy plaintext token or an "encv1:" ciphertext.
#
# Known, accepted limitation (no current code path hits it -- verified by search before this
# lot): a queryset built with .only()/.defer()/.values()/.values_list() on this model bypasses
# normal field population differently -- .values()/.values_list() skip model instantiation
# entirely (post_init never fires, decryption never applied), and .only()/.defer() would force
# an extra per-field DB fetch the moment this receiver touches the deferred attribute. Neither
# is used anywhere on this model today; revisit this receiver first if that ever changes.
#
# A value that carries the encryption prefix but cannot be decrypted (wrong/rotated-out key,
# corrupted ciphertext, or no key configured at all) raises TokenCryptoError here -- by design
# (the ticket's own "impérative" rule), never silently treated as a usable plaintext token.
# That exception propagates out of every call site that touches user.outlook_credentials,
# including a bare hasattr(user, "outlook_credentials") (hasattr only swallows AttributeError
# in Python 3, so this is an intentional loud failure, not an accidental one).
@receiver(post_init, sender=UserOutlookCredentials)
def decrypt_outlook_tokens_on_load(sender, instance, **kwargs):
    instance.outlook_access_token = decrypt_token(instance.outlook_access_token)
    instance.outlook_refresh_token = decrypt_token(instance.outlook_refresh_token)


# SECURITY-MAIL-0D: write-side encryption, gated entirely on MS_TOKEN_ENCRYPTION_WRITE_ENABLED
# (default False, not set by this lot -- a no-op pair until an operator explicitly flips it
# alongside a valid MS_TOKEN_ENCRYPTION_KEYS).
#
# Why pre_save + post_save, not pre_save alone: Django's save machinery has no hook to supply
# "a different value for the SQL write only" without changing the model's own Field class
# (forbidden -- admin-server-models is never modified). The only lever pre_save gives a signal
# receiver is mutating instance.<attr> directly, and whatever pre_save leaves there is exactly
# what gets written AND exactly what every existing consumer would see if it read the instance
# again afterward. So encrypt_outlook_tokens_before_write stashes each field's real plaintext
# in a private, never-persisted instance attribute and overwrites the public attribute with
# ciphertext (which Django then writes); restore_outlook_tokens_after_write puts the stashed
# plaintext straight back once the write has actually happened. Both signals fire for every
# save() path that goes through Django's normal save machinery -- .save(), .save(update_fields
# =[...]), and .objects.create() (which calls .save() internally) -- covering every one of the
# SECURITY-MAIL-0A-enumerated call sites, both for a freshly constructed instance and one
# already loaded via user.outlook_credentials.
#
# update_fields is honoured precisely: only a token field actually being written is
# encrypted/restored -- a save(update_fields=["outlook_token_expires"]) (hypothetical; no
# current caller does this) leaves the token fields completely untouched, in or out.
#
# Known, accepted limitation (same class as SECURITY-MAIL-0C's .only()/.defer() note, and
# explicitly flagged by the ticket itself, section 6): a write that bypasses Django's signal
# dispatch entirely -- QuerySet.update(), bulk_update(), bulk_create() -- would store a
# plaintext value even with the flag enabled, since pre_save never fires for those. Verified
# (SECURITY-MAIL-0A/0B/0C) that no existing code calls any of these on UserOutlookCredentials;
# this is a structural ceiling of the "no model change" constraint, not an oversight, and must
# be re-checked before any future code introduces one of those calls on this model.
#
# SECURITY-MAIL-0E: the encrypt step itself (encrypt_token(), e.g. a missing/invalid key) can
# no longer leave a PARTIALLY-mutated instance on failure -- every field's ciphertext is
# computed into a local dict first, and the instance is only ever mutated once all of them
# succeeded (see encrypt_outlook_tokens_before_write's own comment). The remaining, narrower,
# accepted residual risk: if the actual SQL write itself raises AFTER a successful encrypt
# step (e.g. an IntegrityError unrelated to encryption), post_save never fires and the
# in-memory instance is left holding ciphertext in its token attributes. This is fail-safe,
# not a data leak -- a later consumer reading that ciphertext as if it were a bearer token
# gets an immediate 401 from Microsoft Graph, never a usable credential -- and no current
# caller continues using an instance after its own save() raised. See
# SECURITY-MAIL-0E_READY_FOR_VALIDATION's own report for the full analysis of why a more
# complete guarantee is not achievable from signal receivers alone without either modifying
# UserOutlookCredentials itself (forbidden) or wrapping callers' save() calls individually
# (out of this lot's minimal-change scope).
_PENDING_PLAINTEXT_ATTR = "_security_mail_0d_pending_plaintext"
_OUTLOOK_TOKEN_FIELDS = ("outlook_access_token", "outlook_refresh_token")


@receiver(pre_save, sender=UserOutlookCredentials)
def encrypt_outlook_tokens_before_write(sender, instance, update_fields=None, **kwargs):
    if not getattr(settings, "MS_TOKEN_ENCRYPTION_WRITE_ENABLED", False):
        return
    fields_to_write = _OUTLOOK_TOKEN_FIELDS
    if update_fields is not None:
        fields_to_write = [f for f in _OUTLOOK_TOKEN_FIELDS if f in update_fields]
    if not fields_to_write:
        return
    # SECURITY-MAIL-0E fix: compute every field's ciphertext in a local dict BEFORE mutating
    # the instance at all. encrypt_token() itself refuses (TokenCryptoError) a non-empty value
    # when no valid key is configured -- the original 0D version called setattr() inside the
    # same loop that calls encrypt_token(), so a failure on the SECOND field (e.g.
    # outlook_refresh_token) left the FIRST field (outlook_access_token) already overwritten
    # with ciphertext on the instance, even though pre_save itself was about to raise and no
    # SQL would ever be attempted -- a real "durably corrupted in-memory instance" bug with no
    # SQL failure involved at all. Now either every field's plaintext is captured and every
    # ciphertext computed successfully before any setattr happens, or the instance is left
    # completely untouched.
    pending_plaintext = {}
    ciphertext_by_field = {}
    for field in fields_to_write:
        plaintext = getattr(instance, field)
        pending_plaintext[field] = plaintext
        # May raise -- nothing has been written to `instance` yet at this point, for this
        # field or any earlier one in this loop.
        ciphertext_by_field[field] = encrypt_token(plaintext)
    for field, ciphertext in ciphertext_by_field.items():
        setattr(instance, field, ciphertext)
    setattr(instance, _PENDING_PLAINTEXT_ATTR, pending_plaintext)


@receiver(post_save, sender=UserOutlookCredentials)
def restore_outlook_plaintext_after_write(sender, instance, **kwargs):
    pending_plaintext = getattr(instance, _PENDING_PLAINTEXT_ATTR, None)
    if not pending_plaintext:
        return
    for field, plaintext in pending_plaintext.items():
        setattr(instance, field, plaintext)
    delattr(instance, _PENDING_PLAINTEXT_ATTR)

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
