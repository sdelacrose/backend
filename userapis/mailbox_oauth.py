"""MAIL-1.1C2: Microsoft 365 Mail OAuth for MailboxConnection -- individual and Company-
administered shared mailboxes. No sending, no receiving, no synchronization (out of this
lot's scope entirely); this module's only job is turning a completed OAuth consent into a
correctly-scoped, correctly-encrypted UserOutlookCredentials and the right MailboxConnection
status transition.

Reuses the EXISTING Microsoft OAuth app registration, authorize/token endpoints, and
UserOutlookCredentials storage (admin-server-models, never modified) -- never a second,
parallel Microsoft integration. The authorize/token calls below intentionally mirror
userapis/microsoft_outlook.py's own get_access_token shape rather than reusing it directly,
because that function hardcodes MS_OUTLOOK_CALENDAR_SCOPES; this flow must request the UNION
of scopes (MS_MAIL_SCOPES, see ecpmpapp/constants.py) so the single resulting token keeps
working for Calendar/Contacts too.
"""

import uuid
from datetime import timedelta
from urllib.parse import quote, urlencode

import requests
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from models.models import UserOutlookCredentials
from ecpmpapp.constants import (
    MS_GRAPH_USER_MAILFOLDER_URL,
    MS_MAIL_SCOPES,
    MS_OAUTH_AUTHORIZE_URL,
    MS_OAUTH_TOKEN_URL,
)
from userapis.mailbox_access import can_administer_mailbox
from userapis.models import MailboxConnection, MailOAuthState
from userapis.recette_guard import (
    RecetteMailBlocked,
    check_mailbox_address,
    confirm_tenant_with_microsoft,
    recette_mode,
)
from userapis.token_encryption import TokenCryptoError, encrypt_token

STATE_PREFIX = "mail"


class MailOAuthError(Exception):
    """Any condition that must stop the flow before (or instead of) a MailboxConnection
    status change -- always surfaced as a clear, specific message, never a 500."""


def build_mail_oauth_url(user, mailbox):
    """Section 4/5 "un endpoint de démarrage OAuth".

    MAIL-1.1C2-S fix: `state` previously embedded the raw user_id/mailbox_id directly, which
    made it a predictable, non-expiring, infinitely-reusable CLAIM rather than actual proof of
    who started the flow (this checkpoint's own explicit finding -- "le préfixe mail: ne doit
    servir qu'au routage, jamais constituer une preuve d'autorisation", which was equally true
    of the raw ids it wrapped). `state` now carries nothing but an unguessable, single-use,
    time-limited MailOAuthState token (uuid4, same precedent as
    project_management.models.sharepoint.OAuthState) -- the callback looks the real user/
    mailbox up FROM that row, never from anything the client could have supplied or replayed.

    Reuses settings.MS_REDIRECT_URI unchanged -- Azure's app registration has exactly one
    allowed redirect URI, already in use by the Calendar flow; this is why the callback is an
    extension of the EXISTING MicrosoftAuthViewSet.callback rather than a second route."""
    try:
        check_mailbox_address(mailbox.email_address)    # LINKS-RECETTE-03: no-op outside recette
    except RecetteMailBlocked as error:
        raise MailOAuthError(str(error)) from error
    state_row = MailOAuthState.objects.create(user=user, mailbox=mailbox)
    params = {
        "client_id": settings.MS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.MS_REDIRECT_URI,
        "response_mode": "query",
        "scope": MS_MAIL_SCOPES,
        "state": f"{STATE_PREFIX}:{state_row.token}",
    }
    return f"{MS_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def can_start_mail_oauth(user, mailbox):
    """Section 4 (individual: owner only) / section 5 (shared: an authorized company
    administrator, or the mailbox's own current delegate self-reconnecting after
    needs_reconsent/expired) -- never re-implemented at the callback, only re-verified there
    from the database, same belt-and-suspenders precedent as every other access check in this
    lot."""
    if mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
        return mailbox.owner_user_id == user.id
    return can_administer_mailbox(user, mailbox) or mailbox.connected_by_id == user.id


def get_mail_access_token(auth_code):
    """Mirrors userapis/microsoft_outlook.py::get_access_token's shape exactly, with
    MS_MAIL_SCOPES instead of MS_OUTLOOK_CALENDAR_SCOPES -- the token request's own scope
    should match what was actually requested at /authorize."""
    data = {
        "client_id": settings.MS_CLIENT_ID,
        "client_secret": settings.MS_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": settings.MS_REDIRECT_URI,
        "code": auth_code,
        "scope": MS_MAIL_SCOPES,
    }
    response = requests.post(MS_OAUTH_TOKEN_URL, data=data, timeout=10)
    return response.json()


def verify_shared_mailbox_access(access_token, mailbox_email):
    """MAIL-1.1C2-S section 4: this function proves exactly ONE thing and nothing more --
    that the delegate's token can READ this specific shared mailbox's inbox metadata. It is
    NOT, and must never be treated as, proof of Exchange Send As/Send on Behalf rights. Three
    distinct facts this flow ever establishes, never conflated:
      1. Authentification Microsoft réussie -- the OAuth code exchange succeeded (proves the
         delegate's own Microsoft identity, nothing about this mailbox at all).
      2. Accès en lecture à la boîte partagée -- THIS function, read-only, checked here.
      3. Autorisation Exchange d'envoyer depuis cette boîte -- NEVER established by this lot.
         No sending is built yet (out of scope), so there is nothing to gate with a model
         field right now; the deliberate choice (ticket's own "ou conserver la boîte dans un
         état non opérationnel pour l'envoi") is to NOT invent one prematurely. A future
         send-building lot MUST perform its own, independent send-capability check before
         ever allowing a real send -- MailboxConnection.status == active here must never be
         read by that future code as proof of (3), only of (1) and (2).

    Section 5's own explicit rule: OAuth success for the delegate is never, on its own,
    proof they hold real Exchange rights on THIS shared mailbox -- those are tenant-side
    configuration this code cannot grant or fully see. This is a deliberately minimal,
    read-only probe (a single GET on the mailbox's own inbox folder metadata -- never sends,
    moves, or deletes anything) used only to decide whether the connection may transition to
    "active" (meaning (1)+(2) above, explicitly never (3)). Any non-200 (403 ErrorAccessDenied
    being the expected case when Exchange-side access isn't actually configured, but also
    404/5xx) is treated identically: "not yet verified" -- never assumed to be true by
    default."""
    url = MS_GRAPH_USER_MAILFOLDER_URL.format(email=quote(mailbox_email, safe=""))
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10
        )
    except requests.RequestException:
        return False
    return response.status_code == 200


def store_mail_tokens(user, tokens):
    """Section 3/6's own stated requirement, stronger than SECURITY-MAIL-0's global,
    currently-disabled MS_TOKEN_ENCRYPTION_WRITE_ENABLED flag (never flipped on by this lot,
    never a persistent environment change): a NEW Mail token is ALWAYS explicitly encrypted
    here before being written, regardless of that flag's state. encrypt_token() itself raises
    TokenCryptoError when no valid key is configured -- that exception is never caught here,
    so a misconfigured environment can never result in a plaintext Mail token being written;
    the caller (the OAuth callback) turns it into a clear, non-500 error response instead.

    Idempotent against SECURITY-MAIL-0's own pre_save/post_save signals: when
    MS_TOKEN_ENCRYPTION_WRITE_ENABLED is False (today's default), pre_save does nothing and
    these already-"encv1:"-prefixed strings are written to the DB exactly as given -- still
    encrypted. If that flag is ever enabled later, encrypt_token()'s own idempotency guard
    (SECURITY-MAIL-0C) means pre_save re-encrypting an already-encrypted value is a safe no-op,
    never double-encryption.

    Deliberately never passes an already-encrypted value through
    UserOutlookCredentials.objects.create(): that constructs a NEW instance from the given
    kwargs, which fires SECURITY-MAIL-0C's post_init receiver immediately -- post_init always
    decrypts (it is never gated by MS_TOKEN_ENCRYPTION_WRITE_ENABLED), so a pre-encrypted value
    handed to .create() would be silently decrypted back to plaintext before the row is ever
    saved (found by this lot's own targeted test). get_or_create() with no token fields first
    guarantees a real, already-constructed instance to assign onto instead -- a plain
    attribute assignment after construction does not re-fire post_init.
    """
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expiry_time = timezone.now() + timedelta(seconds=int(tokens.get("expires_in") or 3600))

    # Encrypt FIRST, on the local strings, before touching the DB at all -- if encrypt_token()
    # raises (no valid key configured), nothing has been created or written yet: no empty
    # stub UserOutlookCredentials row, no partial state (found by this lot's own targeted
    # test).
    encrypted_access = encrypt_token(access_token)
    encrypted_refresh = encrypt_token(refresh_token) if refresh_token else refresh_token

    credentials, _ = UserOutlookCredentials.objects.get_or_create(user=user)
    credentials.outlook_access_token = encrypted_access
    credentials.outlook_refresh_token = encrypted_refresh
    credentials.outlook_token_expires = expiry_time
    credentials.save(
        update_fields=[
            "outlook_access_token", "outlook_refresh_token", "outlook_token_expires",
        ]
    )
    return credentials


def _consume_state(state_token):
    """MAIL-1.1C2-S: the single-use/expiry/anti-replay guard. Locks the row
    (select_for_update, inside an atomic block) so two concurrent requests racing on the SAME
    token can never both pass the consumed_at check -- the second one always loses the race
    and is rejected, exactly the "usage unique" + "protection contre le rejeu" the checkpoint
    asked for. consumed_at is set and saved BEFORE anything else in the caller happens (the
    token exchange, the mailbox update), so a token can never be reused even if a LATER step
    in this same request fails."""
    try:
        token_uuid = uuid.UUID(str(state_token))
    except (ValueError, TypeError, AttributeError):
        raise MailOAuthError("Invalid state parameter.")
    with transaction.atomic():
        try:
            state_row = MailOAuthState.objects.select_for_update().get(token=token_uuid)
        except (MailOAuthState.DoesNotExist, DjangoValidationError):
            raise MailOAuthError("Invalid or unknown state parameter.")
        if state_row.consumed_at is not None:
            raise MailOAuthError("This authorization link has already been used.")
        if state_row.is_expired:
            raise MailOAuthError("This authorization link has expired. Please try again.")
        state_row.consumed_at = timezone.now()
        state_row.save(update_fields=["consumed_at"])
    return state_row


def handle_mail_oauth_callback(state_token, code):
    """The one place a Mail OAuth callback is actually processed.

    MAIL-1.1C2-S fix: `user`/`mailbox` are no longer read from client-suppliable ids at all --
    they come exclusively from the MailOAuthState row the unguessable, single-use, expiring
    `state_token` resolves to (see _consume_state and build_mail_oauth_url's own docstring).
    The company/authorization re-checks below are kept as defense-in-depth on top of that (an
    admin's authority or the mailbox's own state could have changed in the window between
    starting the flow and completing it) -- point 4's "comment éviter toute élévation de
    privilèges par manipulation du callback" is now satisfied twice over, not merely once.

    Returns the updated MailboxConnection; raises MailOAuthError (message-only, never a token
    or secret) for every rejection path."""
    state_row = _consume_state(state_token)
    user = state_row.user
    mailbox = state_row.mailbox

    # Anti-privilege-escalation: the mailbox and the user must genuinely belong to the same
    # company, re-checked here independently of whatever was true when the flow started.
    if mailbox.company_profile_id != user.company_id:
        raise MailOAuthError("This mailbox does not belong to your company.")
    if not can_start_mail_oauth(user, mailbox):
        raise MailOAuthError("You are not authorized to connect this mailbox.")

    try:
        check_mailbox_address(mailbox.email_address)    # LINKS-RECETTE-03: no-op outside recette
    except RecetteMailBlocked as error:
        raise MailOAuthError(str(error)) from error

    tokens = get_mail_access_token(code)
    if "access_token" not in tokens:
        raise MailOAuthError("Failed to retrieve an access token from Microsoft.")
    try:
        confirm_tenant_with_microsoft(tokens["access_token"])    # nothing is stored for a foreign tenant
    except RecetteMailBlocked as error:
        raise MailOAuthError(str(error)) from error
    if recette_mode() and mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
        # LINKS-RECETTE-03: the address (and its domain) is only a claim; in recette the identity
        # behind the token is verified with Microsoft before anything is stored. Shared
        # mailboxes keep verify_shared_mailbox_access below.
        from userapis.mailbox_send import _verify_individual_sender_identity
        if not _verify_individual_sender_identity(tokens["access_token"], mailbox.email_address):
            raise MailOAuthError("The connected Microsoft account does not match this mailbox address.")

    try:
        store_mail_tokens(user, tokens)
    except TokenCryptoError:
        raise MailOAuthError(
            "Mail connection is temporarily unavailable: token encryption is not configured."
        )

    mailbox.connected_by = user
    if mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
        # Section 4: "une transition vers active uniquement après validation de
        # l'authentification" -- for an individual mailbox, a successful token exchange for
        # its own owner IS that validation (same trust level the existing Calendar flow
        # already uses for this same user/token pair).
        mailbox.status = MailboxConnection.Status.ACTIVE
    else:
        # Section 5: never automatic -- only a real, verified probe may set "active".
        if verify_shared_mailbox_access(tokens["access_token"], mailbox.email_address):
            mailbox.status = MailboxConnection.Status.ACTIVE
        else:
            mailbox.status = MailboxConnection.Status.NEEDS_RECONSENT
    mailbox.save(update_fields=["connected_by", "status", "updated_at"])
    return mailbox
