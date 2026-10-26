"""MAIL-1.2A: Microsoft Graph correspondence sending -- the ONLY place this codebase calls
Microsoft Graph's sendMail. Reuses the existing OAuth token storage/refresh/encryption
(mailbox_oauth.py, microsoft_outlook.py) and the existing access-control rules
(mailbox_access.py); never a second Microsoft integration, never a second token store, never a
new permission scheme. project_management.services.correspondence_send is the only caller --
this module knows nothing about Correspondence itself.
"""

import base64
import hashlib
from urllib.parse import quote

import requests

from ecpmpapp.constants import MS_GRAPH_ME_URL, MS_GRAPH_SEND_MAIL_URL, MS_MAIL_SCOPES
from userapis.microsoft_outlook import refresh_outlook_access_token
from userapis.models import MailboxConnection
from userapis.recette_guard import RecetteMailBlocked, check_send_allowed, confirm_tenant_with_microsoft

# Microsoft Graph's sendMail request body hard limit is 4 MB total (JSON + base64 attachment
# content, which itself inflates the raw bytes by ~1.37x). This conservative, raw-bytes-before-
# encoding ceiling leaves headroom for the rest of the JSON payload -- a real send that exceeds
# it is blocked here with a clear message instead of a cryptic Graph 413/400.
MAX_GRAPH_ATTACHMENTS_BYTES = 3 * 1024 * 1024

# MAIL-1.2A-FINAL section 1: the single, shared marker that lets
# project_management.services.correspondence_send recognize an "uncertain" prior failure from
# CorrespondenceSendEvent.error_message alone (section 1 explicitly prefers reusing existing
# data over a new field/migration) -- defined here, where the string is actually produced, and
# imported by correspondence_send.py rather than duplicated as a second literal that could
# drift out of sync.
UNCERTAIN_DELIVERY_MARKER = "UNCERTAIN DELIVERY"


class GraphSendError(Exception):
    """Any failure actually attempting a Microsoft Graph send -- network, missing/invalid
    token, a failed identity check, or a non-202 Graph response. Always recorded by the caller
    as a FAILED CorrespondenceSendEvent (the exact same treatment the existing SMTP path
    already gives a provider failure), never raised as a pre-send eligibility error (that is
    SendBlocked, in project_management.services.correspondence_send, a separate and earlier
    concern).

    MAIL-1.2A-REVIEW section 3: `uncertain=True` marks the one real ambiguous case -- the
    sendMail request may have already reached and been accepted by Microsoft before the
    failure happened on our side (a read timeout or a dropped connection while waiting for the
    response), so Microsoft could have actually sent the email even though LINKS recorded
    FAILED. This is never surfaced as a stronger guarantee than that -- no idempotency key
    exists for this Graph call, so it is never auto-retried and never silently treated as
    "safe to resend" by this code; the flag only makes that uncertainty visible in
    event.error_message (the existing, unmodified audit field) instead of hiding it behind an
    ordinary-looking FAILED result indistinguishable from "nothing was sent"."""

    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def get_mail_access_token_for_sending(user):
    """Section 5/6: always forces a fresh, explicitly Mail-scoped token immediately before a
    real send attempt -- never trusts a cached token that might already be valid-but-narrower
    (Calendar/Contacts/Mail share one token pair per user; an unrelated Calendar-only refresh
    elsewhere can silently leave the stored access token without Mail.Send, see
    refresh_outlook_access_token's own docstring). Fail-closed: returns None on any failure --
    no stored credentials, no refresh token, or Microsoft itself rejects the refresh -- never a
    stale or wrongly-scoped token is handed to a caller."""
    if user is None or not hasattr(user, "outlook_credentials"):
        return None
    if not user.outlook_credentials.outlook_refresh_token:
        return None
    return refresh_outlook_access_token(user, scope=MS_MAIL_SCOPES)


def _graph_recipient(email):
    return {"emailAddress": {"address": email}}


def _graph_attachments(attachments):
    total_size = sum(a.file_size or 0 for a in attachments)
    if total_size > MAX_GRAPH_ATTACHMENTS_BYTES:
        raise GraphSendError(
            "Attachments are too large to send via Microsoft 365 "
            f"({total_size} bytes, limit {MAX_GRAPH_ATTACHMENTS_BYTES} bytes)."
        )
    payload = []
    for attachment in attachments:
        with attachment.file.open("rb") as handle:
            content = handle.read()
        # MAIL-1.4-A: the SHA-256 of exactly these bytes, which are the ones sent -- taken from the
        # read the send already makes (no second read), left on the instance for the caller.
        attachment.sent_sha256 = hashlib.sha256(content).hexdigest()
        payload.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment.file_name,
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
        )
    return payload


def _extract_graph_error(response):
    """Never includes the bearer token or any header -- only Graph's own error body/status,
    which is exactly the "erreur technique exploitable sans secret" section 8 asks for."""
    try:
        payload = response.json()
        error = payload.get("error") or {}
        return f"{error.get('code', 'GraphError')}: {error.get('message', response.text)}"
    except ValueError:
        return f"Microsoft Graph returned HTTP {response.status_code}"


def _verify_individual_sender_identity(access_token, mailbox_email):
    """MAIL-1.2A-REVIEW section 2: mirrors verify_shared_mailbox_access's own established
    pattern (mailbox_oauth.py -- a minimal, read-only Graph probe) rather than inventing a new
    verification scheme, applied to the gap that pattern never covered: an INDIVIDUAL
    MailboxConnection.email_address is a value someone typed/registered in LINKS, never
    verified against the Microsoft identity actually behind the connected OAuth token. Without
    this, a user could register email_address="someone-else@company.com" with
    owner_user=themselves, connect their OWN unrelated Microsoft account, and
    can_use_mailbox's ownership-only check would treat them as authorized to send "as" an
    address their token has no real relationship to -- Microsoft Graph's own tenant
    configuration is the only thing that would have caught this otherwise, and only
    sometimes. Returns True only when the token's own `mail` or `userPrincipalName` exactly
    matches (case-insensitive) -- any ambiguity fails closed (False)."""
    try:
        response = requests.get(
            MS_GRAPH_ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
        )
    except requests.RequestException:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    identity_emails = {
        (payload.get("mail") or "").strip().lower(),
        (payload.get("userPrincipalName") or "").strip().lower(),
    }
    identity_emails.discard("")
    return mailbox_email.strip().lower() in identity_emails


def send_via_graph(
    *, mailbox, sender_user, to, cc, bcc, subject, body, attachments, correlation_marker=""
):
    """MAIL-1.3C: `correlation_marker` is new and optional. Omitted, the request is byte-for-byte
    what it always was. Given, it is added as ONE custom Internet header (x-links-send) so the
    copy saved in Sent Items can later be recognised with certainty
    (userapis.mailbox_correlation); it changes nothing else about the send, its result handling or
    its uncertain-delivery rules.

    Section 5: `mailbox.email_address` is always the sendMail target -- never substituted,
    never guessed. For an individual mailbox, `sender_user` is its own owner (their own
    Microsoft identity, so this is equivalent to the identity behind /me/sendMail); for a
    shared mailbox, `sender_user` is the connected delegate. This function never decides
    whether that identity is actually allowed to send as/on behalf of `mailbox.email_address`
    -- it makes the one real call and reports exactly what Microsoft Graph answered. That is
    this lot's own explicit fail-closed rule: MailboxConnection.status=="active" and a
    MailboxUserGrant.can_send row are LINKS-side authorization only, never proof of a real
    Exchange Send As/Send on Behalf right -- Microsoft Graph is the sole, final authority, and
    a 403 here becomes a FAILED CorrespondenceSendEvent, never a silently-swapped sender and
    never a send reported as successful before Graph actually accepts it."""
    # LINKS-RECETTE-03: second layer (the first is in send_correspondence, before any event is
    # written). Runs before any token refresh or Graph call; a no-op outside recette.
    try:
        check_send_allowed(
            sender_address=mailbox.email_address, to=to, cc=cc, bcc=bcc, provider="graph",
        )
    except RecetteMailBlocked as error:
        raise GraphSendError(str(error)) from error

    access_token = get_mail_access_token_for_sending(sender_user)
    if not access_token:
        raise GraphSendError(
            "No valid Microsoft 365 access token is available for this mailbox's connected "
            "identity. Reconnect Microsoft 365 for this mailbox and try again."
        )
    try:
        confirm_tenant_with_microsoft(access_token)
    except RecetteMailBlocked as error:
        raise GraphSendError(str(error)) from error

    if mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
        # Section 2: shared mailboxes keep their existing delegation model unchanged (the
        # connected delegate's token is never claimed to BE the shared mailbox) -- only the
        # individual case ever had an unverified LINKS-side email_address claim to check.
        if not _verify_individual_sender_identity(access_token, mailbox.email_address):
            raise GraphSendError(
                "The connected Microsoft 365 account does not match this mailbox's own "
                "address. Reconnect the correct Microsoft account for this mailbox."
            )

    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [_graph_recipient(to)],
    }
    if cc:
        message["ccRecipients"] = [_graph_recipient(email) for email in cc]
    if bcc:
        message["bccRecipients"] = [_graph_recipient(email) for email in bcc]
    if attachments:
        message["attachments"] = _graph_attachments(attachments)
    if correlation_marker:
        # Graph only accepts custom headers whose name starts with "x-".
        message["internetMessageHeaders"] = [
            {"name": "x-links-send", "value": correlation_marker}
        ]

    url = MS_GRAPH_SEND_MAIL_URL.format(email=quote(mailbox.email_address, safe=""))
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"message": message, "saveToSentItems": "true"},
            timeout=20,
        )
    except requests.exceptions.ConnectTimeout as error:
        # Section 3: the TCP/TLS handshake itself never completed -- Microsoft never received
        # any part of this request. Provably safe: FAILED, not uncertain.
        raise GraphSendError(f"Could not reach Microsoft Graph: {error}") from error
    except requests.RequestException as error:
        # Section 3: every OTHER failure here (a read timeout waiting for Graph's response, a
        # connection dropped mid-request/response, etc.) cannot be told apart from "Microsoft
        # already received and is processing/has sent this message" -- fails closed on the
        # side of caution rather than assuming it is safe. No automatic retry is attempted
        # here or anywhere in this lot, and no idempotency guarantee is claimed: the message
        # text itself is the only mechanism available (section 6: no new field/migration) to
        # keep this visible to whoever next looks at event.error_message, instead of it
        # reading identically to an ordinary, safe-to-retry failure.
        raise GraphSendError(
            f"{UNCERTAIN_DELIVERY_MARKER} (Microsoft Graph may have already accepted this "
            f"message -- check the mailbox's Sent Items before resending): {error}",
            uncertain=True,
        ) from error

    if response.status_code != 202:
        raise GraphSendError(_extract_graph_error(response))

    # Section 8's own explicit warning: Microsoft Graph's sendMail returns 202 Accepted with an
    # EMPTY body on success -- there is no message id to read, ever. 202 only means Microsoft
    # accepted the request for processing, never that the recipient actually received it; never
    # invent an id, never present this as stronger proof than the SMTP path's own equally-empty
    # provider_message_id.
    return ""
