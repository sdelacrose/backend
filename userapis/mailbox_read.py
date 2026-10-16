"""MAIL-1.3B: the READ-ONLY Microsoft Graph client for the messages of a MailboxConnection.

A reading foundation only: nothing here syncs, schedules, imports or exposes anything -- later
lots build on it. It reuses, never duplicates:
  - the OAuth connection and encrypted token storage (mailbox_oauth.py / UserOutlookCredentials)
    and the existing refresh (microsoft_outlook.refresh_outlook_access_token, Mail scopes);
  - the mailbox rules (mailbox_access.can_use_mailbox, read side) -- the caller must name the
    acting user and is re-authorized on EVERY reader construction, before any Graph request;
  - the Mail scopes already consented (no scope is added or widened).

Hard rules, enforced here:
  - GET only. No message is ever marked read, moved, deleted or modified remotely.
  - Metadata only: `$select` never asks for a body, body preview or attachment; whatever else
    Graph might return is dropped by normalize_message().
  - An INDIVIDUAL mailbox is read through "/me" of its owner, after checking that the Microsoft
    account behind the token really is the mailbox's address; a SHARED mailbox through
    "/users/{address}" with its delegate's token (Exchange decides the real access).
  - Errors carry a code and a fixed generic message -- never a Graph body, address, subject,
    token or URL (those can contain personal data).
  - A failure never changes MailboxConnection.status (a transient error must not deactivate a
    mailbox); recording it is the job of MailboxSyncState (userapis.mailbox_messages).
"""

import copy
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta, timezone as dt_timezone
from urllib.parse import quote

import requests
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ecpmpapp.constants import MS_GRAPH_MAIL_BASE_URL, MS_GRAPH_ME_URL, MS_MAIL_SCOPES
from models.models import User, UserOutlookCredentials
from userapis.mailbox_access import can_use_mailbox
from userapis.microsoft_outlook import refresh_outlook_access_token
from userapis.models import MailboxConnection

logger = logging.getLogger(__name__)

# The only folders a reader may list ("dossier autorisé"): incoming mail and sent mail (needed to
# recognise a thread). Anything else -- Drafts, Deleted Items, custom folders -- is refused.
ALLOWED_FOLDERS = ("inbox", "sentitems")
MAX_PAGE_SIZE = 100
MAX_RECIPIENTS = 50

# Everything that is ever requested about a message: identifiers, people, subject, date, flag.
# Deliberately no body / bodyPreview / attachments.
MESSAGE_SELECT = (
    "id,internetMessageId,conversationId,receivedDateTime,from,toRecipients,ccRecipients,"
    "subject,hasAttachments"
)
IDENTIFICATION_SELECT = "internetMessageId,conversationId,internetMessageHeaders"
# MAIL-1.3D-C2-B1: the ONLY place a body is ever requested -- one explicitly selected message, for
# a preview the user asked for. Every other read keeps MESSAGE_SELECT (metadata only).
CONTENT_SELECT = MESSAGE_SELECT + ",parentFolderId,body"
CONTENT_PREFER = 'IdType="ImmutableId", outlook.body-content-type="text"'
# Attachment METADATA only: never contentBytes.
ATTACHMENT_SELECT = "id,name,contentType,size,isInline"
MAX_ATTACHMENTS_LISTED = 100
MAX_ATTACHMENT_PAGES = 3
ATTACHMENT_KINDS = {
    "#microsoft.graph.fileAttachment": "file",
    "#microsoft.graph.itemAttachment": "item",
    "#microsoft.graph.referenceAttachment": "reference",
}
# The only headers ever kept from internetMessageHeaders (used to recognise a reply, plus the
# LINKS send marker that identifies a message LINKS itself sent -- MAIL-1.3C).
THREADING_HEADERS = ("message-id", "in-reply-to", "references", "x-links-send")

# Fixed, generic messages: safe to surface to a client.
ERROR_MESSAGES = {
    "not_authorized": "You are not authorized to read this mailbox.",
    "mailbox_inactive": "This mailbox is not active.",
    "unsupported_provider": "Reading is only available for Microsoft 365 mailboxes.",
    "token_unavailable": "No Microsoft 365 connection is available for this mailbox.",
    "token_refresh_failed": "The Microsoft 365 connection could not be renewed.",
    "token_rejected": "Microsoft 365 rejected the connection; it must be authorized again.",
    "identity_mismatch": "The connected Microsoft account does not match this mailbox.",
    "access_denied": "Microsoft 365 denied access to this mailbox.",
    "not_found": "The requested item was not found.",
    "rate_limited": "Microsoft 365 is limiting requests; try again later.",
    "temporary": "Microsoft 365 is temporarily unreachable; try again later.",
    "invalid_response": "Microsoft 365 returned an unexpected response.",
    "request_rejected": "Microsoft 365 rejected the request.",
    "invalid_request": "The request is not allowed.",
    "sync_state_expired": "The incremental read position expired; a bounded re-read is needed.",
    "content_too_large": "The requested content is larger than the allowed size.",
}
RETRYABLE_CODES = {"token_refresh_failed", "rate_limited", "temporary"}


class MailboxReadError(Exception):
    """Any refusal or failure reading a mailbox. `code` is stable (see ERROR_MESSAGES);
    `retryable` tells a future scheduler whether trying again later can help;
    `retry_after` is Graph's own Retry-After (seconds) when it gave one."""

    def __init__(self, code, *, retry_after=None):
        super().__init__(ERROR_MESSAGES[code])
        self.code = code
        self.retryable = code in RETRYABLE_CODES
        self.retry_after = retry_after


@dataclass(frozen=True)
class GraphMessageMeta:
    graph_id: str
    folder: str = ""
    internet_message_id: str = ""
    conversation_id: str = ""
    received_at: object = None
    sender_address: str = ""
    sender_name: str = ""
    to_addresses: tuple = ()
    cc_addresses: tuple = ()
    subject: str = ""
    has_attachments: bool = False


@dataclass(frozen=True)
class MessagePage:
    messages: list = field(default_factory=list)
    next_link: str = ""


@dataclass(frozen=True)
class DeltaPage:
    """One page of an incremental (delta) read of a folder: the changed messages' metadata, the
    Graph ids of items reported as removed (deleted or moved out), and either the link to the next
    page or -- on the last page -- the opaque `delta_link` to store for the next run."""
    messages: list = field(default_factory=list)
    removed_ids: list = field(default_factory=list)
    next_link: str = ""
    delta_link: str = ""


@dataclass(frozen=True)
class GraphAttachmentMeta:
    """What Graph says about one attachment -- never its content."""
    graph_id: str
    name: str = ""
    content_type: str = ""
    size: object = None            # declared by Graph; never trusted on its own
    kind: str = "unknown"          # file | item | reference | unknown
    is_inline: bool = False


@dataclass(frozen=True)
class AttachmentListing:
    attachments: list = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class MessageContent:
    """One message's metadata and its body as PLAIN TEXT (never HTML)."""
    meta: object
    body_text: str = ""


@dataclass(frozen=True)
class ReplyIdentification:
    internet_message_id: str = ""
    conversation_id: str = ""
    in_reply_to: str = ""
    references: tuple = ()
    send_marker: str = ""


# --- Normalisation (what is kept of a Graph message) -----------------------------------------

def _address(recipient):
    email = (recipient or {}).get("emailAddress") or {}
    return str(email.get("address") or "").strip().lower()[:254]


def _addresses(recipients):
    seen = []
    for recipient in recipients or []:
        address = _address(recipient)
        if address and address not in seen:
            seen.append(address)
        if len(seen) >= MAX_RECIPIENTS:
            break
    return tuple(seen)


def _received_at(value):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def normalize_message(raw, folder=""):
    """A Graph message dict -> GraphMessageMeta, or None when it has no usable Graph id. Missing
    or null fields become empty values (internetMessageId can legitimately be absent); any extra
    field Graph returned (a body, say) is dropped here and never reaches storage."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    sender = (raw.get("from") or {}).get("emailAddress") or {}
    return GraphMessageMeta(
        graph_id=str(raw["id"])[:512],
        folder=folder,
        internet_message_id=str(raw.get("internetMessageId") or "").strip()[:998],
        conversation_id=str(raw.get("conversationId") or "")[:512],
        received_at=_received_at(raw.get("receivedDateTime")),
        sender_address=_address({"emailAddress": sender}),
        sender_name=str(sender.get("name") or "")[:255],
        to_addresses=_addresses(raw.get("toRecipients")),
        cc_addresses=_addresses(raw.get("ccRecipients")),
        subject=str(raw.get("subject") or "")[:500],
        has_attachments=bool(raw.get("hasAttachments")),
    )


_BLOCK_TAGS = ("p", "div", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre")


def html_to_text(markup):
    """Plain text of an HTML body, for the case where Graph answers HTML although plain text was
    asked for. Scripts, styles and every tag are dropped; nothing is fetched or executed (lxml
    parses, it never loads the resources a message references)."""
    import re

    from lxml import html as lxml_html

    if not markup or not markup.strip():
        return ""
    try:
        document = lxml_html.fromstring(markup)
    except Exception:
        return re.sub(r"<[^>]*>", "", markup)
    for element in list(document.iter("script", "style", "head", "title")):
        element.drop_tree()
    for element in document.iter():
        if not isinstance(element.tag, str):
            continue
        if element.tag == "br" or element.tag in _BLOCK_TAGS:
            element.tail = "\n" + (element.tail or "")
    return re.sub(r"\n{3,}", "\n\n", document.text_content()).strip()


def _normalize_attachment(raw):
    """A Graph attachment dict -> GraphAttachmentMeta, or None without a usable id. Whatever else
    Graph returned (contentBytes, say) is dropped here."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    size = raw.get("size")
    return GraphAttachmentMeta(
        graph_id=str(raw["id"])[:512],
        name=str(raw.get("name") or "")[:255],
        content_type=str(raw.get("contentType") or "")[:255],
        size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        kind=ATTACHMENT_KINDS.get(str(raw.get("@odata.type") or ""), "unknown"),
        is_inline=bool(raw.get("isInline")),
    )


# --- Authorization -----------------------------------------------------------------------------

def authorize_mailbox_read(actor, mailbox, *, project=None):
    """The explicit authorization context of every read: `actor` must be allowed to READ this
    mailbox right now (owner of an individual one / `can_read` grant on a shared one), belong to
    its company, and -- in a project context -- the mailbox must be assigned to that project.
    Raises MailboxReadError otherwise. Always answers "not_authorized" to someone with no right
    at all, so the existence or state of a mailbox is never revealed to a stranger."""
    if actor is None or mailbox is None:
        raise MailboxReadError("not_authorized")
    if getattr(actor, "company_id", None) != mailbox.company_profile_id:
        raise MailboxReadError("not_authorized")
    if can_use_mailbox(actor, mailbox, for_sending=False, project=project):
        pass
    elif mailbox.status != MailboxConnection.Status.ACTIVE:
        # Only someone who WOULD be allowed if the mailbox were active learns it is inactive.
        probe = copy.copy(mailbox)
        probe.status = MailboxConnection.Status.ACTIVE
        if can_use_mailbox(actor, probe, for_sending=False, project=project):
            raise MailboxReadError("mailbox_inactive")
        raise MailboxReadError("not_authorized")
    else:
        raise MailboxReadError("not_authorized")
    if mailbox.provider != MailboxConnection.Provider.MICROSOFT:
        raise MailboxReadError("unsupported_provider")


def _token_holder(mailbox):
    """Whose Microsoft token reads this mailbox: the owner for an individual mailbox, the
    delegate (connected_by) for a shared one."""
    if mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
        return mailbox.owner_user
    return mailbox.connected_by


# --- The reader -----------------------------------------------------------------------------

class MailboxGraphReader:
    """Read-only access to ONE authorized mailbox. Constructing it authorizes `actor`; every
    method then issues only Graph GETs. Use one instance per job: it keeps one access token."""

    def __init__(self, mailbox, *, actor, project=None, timeout=15):
        authorize_mailbox_read(actor, mailbox, project=project)
        holder = _token_holder(mailbox)
        if holder is None:
            raise MailboxReadError("token_unavailable")
        self.mailbox = mailbox
        self._holder_id = holder.pk
        self._timeout = timeout
        self._token = None
        self._token_is_fresh = False
        self._identity_verified = False

    # -- URLs
    @property
    def _context_url(self):
        if self.mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
            return f"{MS_GRAPH_MAIL_BASE_URL}/me"
        return f"{MS_GRAPH_MAIL_BASE_URL}/users/{quote(self.mailbox.email_address, safe='')}"

    def _own_page_url(self, url):
        """A next-page link is followed only if it stays inside THIS mailbox's own context --
        never an arbitrary URL (the bearer token must not be sent anywhere else)."""
        if not isinstance(url, str) or not url.startswith(self._context_url + "/"):
            raise MailboxReadError("invalid_request")
        return url

    def _own_delta_url(self, url):
        """A delta next/delta link (or a stored cursor) is followed only if it stays inside THIS
        mailbox's own context and is a messages/delta URL of Graph -- never an arbitrary URL.
        Graph may spell a user context `/users/{address}` or `/users('{address}')`."""
        if not isinstance(url, str) or len(url) > 4096:
            raise MailboxReadError("invalid_request")
        if self.mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
            prefixes = [f"{MS_GRAPH_MAIL_BASE_URL}/me/"]
        else:
            address = self.mailbox.email_address
            prefixes = [
                f"{self._context_url}/",
                f"{MS_GRAPH_MAIL_BASE_URL}/users('{address}')/",
                f"{MS_GRAPH_MAIL_BASE_URL}/users('{quote(address, safe='@')}')/",
            ]
        if not any(url.startswith(prefix) for prefix in prefixes) or "/messages/delta" not in url:
            raise MailboxReadError("invalid_request")
        return url

    # -- token (one controlled refresh, serialized per user)
    def _access_token(self, *, force_refresh=False):
        if self._token and not force_refresh:
            return self._token
        with transaction.atomic():
            # Row lock: two readers of the same user never refresh at the same time.
            credentials = (
                UserOutlookCredentials.objects.select_for_update()
                .filter(user_id=self._holder_id)
                .first()
            )
            if credentials is None or not credentials.outlook_refresh_token:
                raise MailboxReadError("token_unavailable")
            expires = credentials.outlook_token_expires
            if (
                not force_refresh
                and credentials.outlook_access_token
                and expires
                and timezone.now() < expires - timedelta(seconds=60)
            ):
                self._token, self._token_is_fresh = credentials.outlook_access_token, False
                return self._token
            token = refresh_outlook_access_token(
                User.objects.get(pk=self._holder_id), scope=MS_MAIL_SCOPES
            )
        if not token:
            raise MailboxReadError("token_refresh_failed")
        self._token, self._token_is_fresh = token, True
        return token

    # -- GET
    def _get_json(self, url, params=None, *, _retried=False, extra_headers=None):
        token = self._access_token()
        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    # Stable ids across folder moves (what a later de-duplication needs).
                    "Prefer": 'IdType="ImmutableId"',
                    **(extra_headers or {}),
                },
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise MailboxReadError("temporary")

        status = response.status_code
        if status in (401, 403) and not self._token_is_fresh and not _retried:
            # A cached token may predate the Mail scopes (Calendar refreshes can narrow it):
            # one controlled renewal, then one retry -- never a loop.
            self._access_token(force_refresh=True)
            return self._get_json(url, params, _retried=True, extra_headers=extra_headers)
        if status == 200:
            try:
                payload = response.json()
            except ValueError:
                raise MailboxReadError("invalid_response")
            if not isinstance(payload, dict):
                raise MailboxReadError("invalid_response")
            return payload
        # Only the status is logged: never the URL (it carries a mailbox address) or a body.
        logger.warning(
            "Mailbox read failed mailbox_id=%s http_status=%s", self.mailbox.pk, status
        )
        if status == 401:
            raise MailboxReadError("token_rejected")
        if status == 403:
            raise MailboxReadError("access_denied")
        if status == 404:
            raise MailboxReadError("not_found")
        if status == 410:
            # Graph's answer to an expired delta token (syncStateNotFound / resyncRequired).
            raise MailboxReadError("sync_state_expired")
        if status == 429:
            try:
                retry_after = int(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                retry_after = None
            raise MailboxReadError("rate_limited", retry_after=retry_after)
        if status >= 500:
            raise MailboxReadError("temporary")
        raise MailboxReadError("request_rejected")

    def _ensure_identity(self):
        """Individual mailbox only: the account behind the token must BE this mailbox's address,
        otherwise "/me" would read someone else's mail into this mailbox (same rule the send path
        applies before sending). Checked once per reader. Shared mailboxes are read by address
        and Exchange decides the access."""
        if self._identity_verified:
            return
        if self.mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
            payload = self._get_json(MS_GRAPH_ME_URL)
            identities = {
                str(payload.get("mail") or "").strip().lower(),
                str(payload.get("userPrincipalName") or "").strip().lower(),
            }
            identities.discard("")
            if self.mailbox.email_address.strip().lower() not in identities:
                raise MailboxReadError("identity_mismatch")
        self._identity_verified = True

    @staticmethod
    def _message_id(message_id):
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 512:
            raise MailboxReadError("invalid_request")
        return quote(message_id.strip(), safe="")

    # -- public read methods
    def list_messages(
        self, folder="inbox", *, top=50, received_since=None, received_until=None,
        subject=None, conversation_id=None, page_url=None,
        sender=None, subject_contains=None, skip=None,
    ):
        """One page of message METADATA from an allowed folder, newest first. `page_url` is the
        previous page's `next_link`. Never marks anything read.

        MAIL-1.3C: `received_until`, `subject` and `conversation_id` (exact matches) narrow the read
        on the Graph side, so a targeted lookup (one sent message, one thread) never lists the
        rest of the folder.

        MAIL-1.3D-C1 (a user's own explicit search): `sender` (an exact address),
        `subject_contains` and `skip` (an offset into the ordered result) narrow and page the read
        the same way. Graph requires the ordered property (`receivedDateTime`) to lead the filter,
        so a search always carries a `received_since` bound."""
        folder = str(folder or "").strip().lower()
        if folder not in ALLOWED_FOLDERS:
            raise MailboxReadError("invalid_request")
        self._ensure_identity()
        if page_url:
            payload = self._get_json(self._own_page_url(page_url))
        else:
            params = {
                "$select": MESSAGE_SELECT,
                "$top": max(1, min(int(top), MAX_PAGE_SIZE)),
                "$orderby": "receivedDateTime desc",
            }
            def _utc(value):
                if timezone.is_naive(value):
                    value = timezone.make_aware(value, dt_timezone.utc)
                return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # receivedDateTime first: Graph requires the ordered property to lead the filter.
            conditions = []
            if received_since is not None:
                conditions.append(f"receivedDateTime ge {_utc(received_since)}")
            if received_until is not None:
                conditions.append(f"receivedDateTime le {_utc(received_until)}")
            if subject is not None:
                escaped = str(subject).replace("'", "''")
                conditions.append(f"subject eq '{escaped}'")
            if conversation_id:
                escaped = str(conversation_id).replace("'", "''")
                conditions.append(f"conversationId eq '{escaped}'")
            if sender:
                escaped = str(sender).replace("'", "''")
                conditions.append(f"from/emailAddress/address eq '{escaped}'")
            if subject_contains:
                escaped = str(subject_contains).replace("'", "''")
                conditions.append(f"contains(subject,'{escaped}')")
            if skip:
                params["$skip"] = max(0, int(skip))
            if conditions:
                params["$filter"] = " and ".join(conditions)
            payload = self._get_json(f"{self._context_url}/mailFolders/{folder}/messages", params)
        items = payload.get("value")
        if not isinstance(items, list):
            raise MailboxReadError("invalid_response")
        messages = [m for m in (normalize_message(raw, folder) for raw in items) if m is not None]
        next_link = payload.get("@odata.nextLink") or ""
        if next_link:
            next_link = self._own_page_url(next_link)
        return MessagePage(messages=messages, next_link=next_link)

    def delta_messages(self, folder="inbox", *, since=None, page_url=None, page_size=50):
        """One page of an INCREMENTAL read of `folder` (Graph delta), metadata only.

        First call: pass `since` (a datetime) -- Graph is asked, server-side, for
        `receivedDateTime ge since` (the one filter delta supports) so only the bounded recent
        period is enumerated. Following calls: pass the previous page's `next_link`, or the stored
        `delta_link` of an earlier completed run as `page_url`. Items Graph reports as removed come
        back as ids only. Nothing is ever marked read, moved or changed remotely.

        There is NO conversation or subject filter here: delta cannot narrow by them, which is why
        this is only ever used on SHARED mailboxes (never a personal one)."""
        folder = str(folder or "").strip().lower()
        if folder not in ALLOWED_FOLDERS:
            raise MailboxReadError("invalid_request")
        self._ensure_identity()
        headers = {"Prefer": f'IdType="ImmutableId", odata.maxpagesize={max(1, min(int(page_size), MAX_PAGE_SIZE))}'}
        if page_url:
            payload = self._get_json(self._own_delta_url(page_url), extra_headers=headers)
        else:
            if since is None:
                # An unbounded first enumeration of a folder is never allowed.
                raise MailboxReadError("invalid_request")
            if timezone.is_naive(since):
                since = timezone.make_aware(since, dt_timezone.utc)
            params = {
                "$select": MESSAGE_SELECT,
                "$filter": f"receivedDateTime ge {since.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
                "$orderby": "receivedDateTime desc",
            }
            payload = self._get_json(
                f"{self._context_url}/mailFolders/{folder}/messages/delta", params,
                extra_headers=headers,
            )
        items = payload.get("value")
        if not isinstance(items, list):
            raise MailboxReadError("invalid_response")
        messages, removed = [], []
        for raw in items:
            if isinstance(raw, dict) and "@removed" in raw:
                if raw.get("id"):
                    removed.append(str(raw["id"])[:512])
                continue
            meta = normalize_message(raw, folder)
            if meta is not None:
                messages.append(meta)
        next_link = payload.get("@odata.nextLink") or ""
        delta_link = payload.get("@odata.deltaLink") or ""
        if next_link:
            next_link = self._own_delta_url(next_link)
        if delta_link:
            delta_link = self._own_delta_url(delta_link)
        return DeltaPage(messages=messages, removed_ids=removed, next_link=next_link, delta_link=delta_link)

    def get_message(self, message_id):
        """Metadata of one message (a precise lookup)."""
        message_id = self._message_id(message_id)
        self._ensure_identity()
        payload = self._get_json(
            f"{self._context_url}/messages/{message_id}", {"$select": MESSAGE_SELECT}
        )
        message = normalize_message(payload)
        if message is None:
            raise MailboxReadError("invalid_response")
        return message

    def get_message_in_folder(self, message_id, folder):
        """Metadata of ONE explicitly selected message, checked to still be in `folder`
        (MAIL-1.3D-C2-A: an import re-verifies that the message exists and has not been moved).
        Two GETs -- the folder's id, then the message with its parentFolderId -- and nothing else:
        no body, no attachment, no listing, nothing marked read. A deleted or moved message is
        "not_found"."""
        folder = str(folder or "").strip().lower()
        if folder not in ALLOWED_FOLDERS:
            raise MailboxReadError("invalid_request")
        message_id = self._message_id(message_id)
        self._ensure_identity()
        folder_payload = self._get_json(f"{self._context_url}/mailFolders/{folder}", {"$select": "id"})
        payload = self._get_json(
            f"{self._context_url}/messages/{message_id}",
            {"$select": MESSAGE_SELECT + ",parentFolderId"},
        )
        folder_id = folder_payload.get("id")
        if not folder_id or payload.get("parentFolderId") != folder_id:
            raise MailboxReadError("not_found")
        message = normalize_message(payload, folder)
        if message is None:
            raise MailboxReadError("invalid_response")
        return message

    def get_message_content(self, message_id, folder):
        """MAIL-1.3D-C2-B1: ONE explicitly selected message -- its metadata and its body as plain
        text -- checked to still be in `folder`. Graph is asked for a text body; should it answer
        HTML anyway, it is converted here, so HTML never leaves this method. Two GETs (the folder's
        id, the message); nothing is marked read, changed or downloaded."""
        folder = str(folder or "").strip().lower()
        if folder not in ALLOWED_FOLDERS:
            raise MailboxReadError("invalid_request")
        message_id = self._message_id(message_id)
        self._ensure_identity()
        folder_payload = self._get_json(f"{self._context_url}/mailFolders/{folder}", {"$select": "id"})
        payload = self._get_json(
            f"{self._context_url}/messages/{message_id}", {"$select": CONTENT_SELECT},
            extra_headers={"Prefer": CONTENT_PREFER},
        )
        folder_id = folder_payload.get("id")
        if not folder_id or payload.get("parentFolderId") != folder_id:
            raise MailboxReadError("not_found")
        meta = normalize_message(payload, folder)
        body = payload.get("body")
        if meta is None or not (body is None or isinstance(body, dict)):
            raise MailboxReadError("invalid_response")
        content = (body or {}).get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise MailboxReadError("invalid_response")
        if str((body or {}).get("contentType") or "text").lower() != "text":
            content = html_to_text(content)
        return MessageContent(meta=meta, body_text=content.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", ""))

    def list_attachments(self, message_id, *, limit=MAX_ATTACHMENTS_LISTED):
        """The METADATA of a message's attachments (id, name, type, declared size, kind, inline
        flag) -- never their content: `$select` leaves out contentBytes. Bounded: at most `limit`
        items and MAX_ATTACHMENT_PAGES pages; `truncated` says something was left out."""
        message_id = self._message_id(message_id)
        self._ensure_identity()
        payload = self._get_json(
            f"{self._context_url}/messages/{message_id}/attachments", {"$select": ATTACHMENT_SELECT}
        )
        attachments, truncated, pages = [], False, 1
        while True:
            items = payload.get("value")
            if not isinstance(items, list):
                raise MailboxReadError("invalid_response")
            for raw in items:
                if len(attachments) >= limit:
                    truncated = True
                    break
                item = _normalize_attachment(raw)
                if item is not None:
                    attachments.append(item)
            next_link = payload.get("@odata.nextLink") or ""
            if truncated or not next_link:
                break
            if pages >= MAX_ATTACHMENT_PAGES:
                truncated = True
                break
            payload = self._get_json(self._own_page_url(next_link))
            pages += 1
        return AttachmentListing(attachments=attachments, truncated=truncated)

    def _error_for_status(self, status, response):
        """The MailboxReadError a non-200 answer to a download stands for (status only is logged)."""
        logger.warning("Mailbox download failed mailbox_id=%s http_status=%s", self.mailbox.pk, status)
        if status == 401:
            return MailboxReadError("token_rejected")
        if status == 403:
            return MailboxReadError("access_denied")
        if status == 404:
            return MailboxReadError("not_found")
        if status == 429:
            try:
                retry_after = int(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                retry_after = None
            return MailboxReadError("rate_limited", retry_after=retry_after)
        if status >= 500:
            return MailboxReadError("temporary")
        return MailboxReadError("request_rejected")

    def _stream_to(self, url, fh, max_bytes, *, _retried=False):
        """GET `url` and write the answer to the open binary file `fh`, never more than
        `max_bytes` (MailboxReadError "content_too_large" the moment it would exceed it -- nothing is
        truncated and kept). Returns (size, sha256 hex) of what was REALLY received."""
        token = self._access_token()
        try:
            response = requests.get(
                url, stream=True, timeout=self._timeout,
                headers={"Authorization": f"Bearer {token}", "Accept": "*/*", "Prefer": 'IdType="ImmutableId"'},
            )
        except requests.RequestException:
            raise MailboxReadError("temporary")
        try:
            status = response.status_code
            if status in (401, 403) and not self._token_is_fresh and not _retried:
                response.close()
                self._access_token(force_refresh=True)
                return self._stream_to(url, fh, max_bytes, _retried=True)
            if status != 200:
                raise self._error_for_status(status, response)
            digest, size = hashlib.sha256(), 0
            try:
                for chunk in response.iter_content(65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise MailboxReadError("content_too_large")
                    fh.write(chunk)
                    digest.update(chunk)
            except requests.RequestException:
                raise MailboxReadError("temporary")
            return size, digest.hexdigest()
        finally:
            response.close()

    def download_message_mime(self, message_id, fh, max_bytes):
        """MAIL-1.3D-C2-B3: the ORIGINAL message (MIME, the future .eml) of one explicitly selected
        message, streamed into `fh` within `max_bytes`. Read-only; nothing is marked or changed."""
        message_id = self._message_id(message_id)
        self._ensure_identity()
        return self._stream_to(f"{self._context_url}/messages/{message_id}/$value", fh, max_bytes)

    def download_attachment(self, message_id, attachment_id, fh, max_bytes):
        """MAIL-1.3D-C2-B3: the raw content of ONE explicitly selected file attachment, streamed
        into `fh` within `max_bytes`."""
        message_id = self._message_id(message_id)
        attachment_id = self._message_id(attachment_id)
        self._ensure_identity()
        return self._stream_to(
            f"{self._context_url}/messages/{message_id}/attachments/{attachment_id}/$value", fh, max_bytes
        )

    def get_reply_identification(self, message_id):
        """What is needed to recognise a reply: the message's own Internet id, its conversation
        and -- from the Internet headers, whitelisted to Message-ID / In-Reply-To / References --
        the ids of the messages it answers. No other header is kept."""
        message_id = self._message_id(message_id)
        self._ensure_identity()
        payload = self._get_json(
            f"{self._context_url}/messages/{message_id}", {"$select": IDENTIFICATION_SELECT}
        )
        headers = {}
        for header in payload.get("internetMessageHeaders") or []:
            name = str((header or {}).get("name") or "").strip().lower()
            if name in THREADING_HEADERS and name not in headers:
                headers[name] = str((header or {}).get("value") or "").strip()
        return ReplyIdentification(
            internet_message_id=(
                str(payload.get("internetMessageId") or "").strip()[:998]
                or headers.get("message-id", "")[:998]
            ),
            conversation_id=str(payload.get("conversationId") or "")[:512],
            in_reply_to=headers.get("in-reply-to", "")[:998],
            references=tuple(headers.get("references", "").split())[:50],
            send_marker=headers.get("x-links-send", "")[:200],
        )
