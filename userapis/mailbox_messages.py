"""MAIL-1.3B: storage of message METADATA read from a mailbox, who may see it, and the per-mailbox
sync state. No Graph call here (see mailbox_read.py), no endpoint, nothing scheduled.

Privacy model (the rule every later lot must keep): a stored message belongs to exactly one
mailbox and is visible only to people who may READ THAT MAILBOX right now -- the owner of an
individual mailbox, a user holding `can_read` on a shared one -- inside their own company, and,
in a project context, only if the mailbox is assigned to that project. Holding a mailbox right
never opens LINKS correspondences, and holding a correspondence right never opens a mailbox's
messages. The same Internet message present in two mailboxes is two independent rows with their
own rights: nothing is merged or shared on the strength of `internetMessageId` alone.

MAIL-1.3D-A: a PERSONAL mailbox exposes only the messages linked to a LINKS correspondence (the
sent copies and the confirmed replies); the rest of that mailbox is never stored by the sync and,
should such a row ever exist, is never visible. A SHARED mailbox's rows form its review queue and
are visible to everyone who may read that mailbox.

Three different rights, never merged (MAIL-1.3D-A2):
  1. that a reply EXISTS for a correspondence -- a fact about the correspondence, not carried by any
     serializer today; exposing it, if ever wanted, is a separate decision;
  2. the right to see a stored message's METADATA -- exactly the right to read its mailbox
     (`messages_visible_to` / `can_read_stored_message`); correspondence rights never open it;
  3. the right to IMPORT it into LINKS -- a later lot; it will require (2) AND the correspondence
     create right, and nothing here grants it.
"""

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from userapis.mailbox_access import can_use_mailbox
from userapis.models import MailboxConnection, MailboxMessage, MailboxSyncState

# Only these are ever refreshed on an already stored row; state and correspondence (decisions
# taken later by a person or by a matching lot) are never overwritten by a re-read.
_FILL_IF_EMPTY = (
    "internet_message_id", "conversation_id", "sender_address", "sender_name", "subject",
)


def store_message_metadata(mailbox, meta):
    """Idempotently store one GraphMessageMeta for `mailbox`. Returns (row, created).

    The same Graph item, or the same Internet message (same internetMessageId, e.g. Inbox and
    Sent Items copies) already stored for THIS mailbox is updated in place, never duplicated; a
    missing internetMessageId only means the Graph id is the sole key. The row keeps its first
    folder and Graph id, and its processing state / correspondence are never touched."""
    if not meta.graph_id:
        raise ValueError("A message needs a Graph id to be stored.")
    for attempt in range(2):
        try:
            with transaction.atomic():
                row = MailboxMessage.objects.select_for_update().filter(
                    mailbox=mailbox, graph_message_id=meta.graph_id
                ).first()
                if row is None and meta.internet_message_id:
                    row = MailboxMessage.objects.select_for_update().filter(
                        mailbox=mailbox, internet_message_id=meta.internet_message_id
                    ).first()
                if row is not None:
                    changed = []
                    for name in _FILL_IF_EMPTY:
                        incoming = getattr(meta, name)
                        if incoming and not getattr(row, name):
                            setattr(row, name, incoming)
                            changed.append(name)
                    if meta.received_at and row.received_at is None:
                        row.received_at = meta.received_at
                        changed.append("received_at")
                    if meta.has_attachments and not row.has_attachments:
                        row.has_attachments = True
                        changed.append("has_attachments")
                    if changed:
                        row.save(update_fields=changed + ["updated_at"])
                    return row, False
                row = MailboxMessage.objects.create(
                    mailbox=mailbox,
                    company_profile_id=mailbox.company_profile_id,
                    folder=meta.folder,
                    graph_message_id=meta.graph_id,
                    internet_message_id=meta.internet_message_id,
                    conversation_id=meta.conversation_id,
                    sender_address=meta.sender_address,
                    sender_name=meta.sender_name,
                    to_addresses=list(meta.to_addresses),
                    cc_addresses=list(meta.cc_addresses),
                    subject=meta.subject,
                    received_at=meta.received_at,
                    has_attachments=meta.has_attachments,
                )
                return row, True
        except IntegrityError:
            # A concurrent writer stored it between our lookup and insert: look again.
            if attempt == 1:
                raise


def readable_mailboxes(user, *, project=None):
    """Mailboxes `user` may READ right now -- the queryset form of
    can_use_mailbox(for_sending=False): same company, ACTIVE, owner of an individual mailbox or
    a `can_read` grant on a shared one, and (project context) assigned to that project."""
    if user is None or not getattr(user, "company_id", None):
        return MailboxConnection.objects.none()
    mailboxes = MailboxConnection.objects.filter(
        company_profile_id=user.company_id, status=MailboxConnection.Status.ACTIVE
    ).filter(
        Q(connection_type=MailboxConnection.ConnectionType.INDIVIDUAL, owner_user=user)
        | Q(
            connection_type=MailboxConnection.ConnectionType.SHARED,
            user_grants__user=user,
            user_grants__can_read=True,
        )
    )
    if project is not None:
        mailboxes = mailboxes.filter(project_assignments__project=project)
    return mailboxes.distinct()


def messages_visible_to(user, *, project=None):
    """The stored messages `user` may see: only those of mailboxes they may read (see the module
    docstring). An explicit user is required -- there is no unscoped accessor."""
    return MailboxMessage.objects.filter(
        mailbox__in=readable_mailboxes(user, project=project),
        company_profile_id=getattr(user, "company_id", None),
    ).filter(
        Q(mailbox__connection_type=MailboxConnection.ConnectionType.SHARED)
        | Q(correspondence__isnull=False)
    )


def can_read_stored_message(user, message, *, project=None):
    """Single-message form of messages_visible_to, via the very same mailbox rule."""
    if user is None or message is None:
        return False
    if getattr(user, "company_id", None) != message.company_profile_id:
        return False
    if (
        message.mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL
        and message.correspondence_id is None
    ):
        return False
    return can_use_mailbox(user, message.mailbox, for_sending=False, project=project)


# --- sync state (technical only) -----------------------------------------------------------

def get_sync_state(mailbox):
    state, _ = MailboxSyncState.objects.get_or_create(mailbox=mailbox)
    return state


def record_sync_success(mailbox, *, cursor=None):
    """A read completed: remember where it stopped and clear the last error. Never touches
    MailboxConnection.status."""
    state = get_sync_state(mailbox)
    if cursor is not None:
        state.cursor = cursor
    state.last_synced_at = timezone.now()
    state.last_error_code = ""
    state.last_error_at = None
    state.save()
    return state


def record_sync_error(mailbox, error):
    """A read failed: keep only the short error CODE (a MailboxReadError's `code`) -- never the
    message text, an address or a Graph body -- and the time. The mailbox's own status is NEVER
    changed here, whatever the error: a transient failure must not deactivate a mailbox, and a
    permanent one is a decision for a later lot / a person."""
    state = get_sync_state(mailbox)
    state.last_error_code = str(getattr(error, "code", "") or "error")[:40]
    state.last_error_at = timezone.now()
    state.save()
    return state
