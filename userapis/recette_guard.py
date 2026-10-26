"""LINKS-RECETTE-03: server-side safety guard for the shared "recette" environment.

Inert unless `settings.RECETTE_MODE` is true (only ecpmpapp.settings.recette sets it), so every
other environment behaves exactly as before. In recette it is FAIL-CLOSED: any missing setting,
unparsable address or unknown tenant means "refuse".

Microsoft's OAuth/Graph endpoints are fixed constants (ecpmpapp.constants) and cannot be pointed
at a sandbox, so the isolation is enforced here, at the few places that can reach a real mailbox:
  * correspondence_send.send_correspondence -- BEFORE the send slot/event is created;
  * mailbox_send.send_via_graph             -- second layer, BEFORE any token or Graph call;
  * mailbox_oauth / the mailbox views       -- only allow-listed mailboxes of the recette tenant;
  * the Calendar/Contacts OAuth callback and Outlook event creation (which invites attendees).
Recipients are matched as FULL addresses (To, Cc and Bcc), never by domain.

TENANT GUARANTEE (confirm_tenant_with_microsoft): the tenant is NOT read from the token. Microsoft
Graph access tokens are meant for Graph, not for third parties, so decoding one locally (or even
checking its signature) would prove nothing we can rely on. Instead the token is presented to
Microsoft Graph itself (GET /organization, delegated User.Read) and the tenant id Microsoft answers
with must equal RECETTE_M365_TENANT_ID. That gives: Microsoft accepted this token (authenticated
call over TLS to graph.microsoft.com) AND it belongs to that tenant. It does not give a local
cryptographic proof of the token, and it depends on the TLS channel (egress is restricted to the
Microsoft/Auth0 hosts by the recette egress proxy). Account identity (mail / userPrincipalName)
is confirmed separately by Graph /me for individual mailboxes.
"""

import re

import requests
from django.conf import settings

MS_GRAPH_ORGANIZATION_URL = "https://graph.microsoft.com/v1.0/organization?$select=id"

_ADDRESS_RE = re.compile(r"^[a-z0-9._%+\-']+@[a-z0-9\-]+(\.[a-z0-9\-]+)+$")


class RecetteMailBlocked(Exception):
    """A send or a mailbox connection refused by the recette guard. Message only: never an
    address list, token or secret."""


def recette_mode():
    return bool(getattr(settings, "RECETTE_MODE", False))


def normalize_address(value):
    """Lower-cased bare address, or None for anything else (display names, several addresses in
    one string, angle brackets, spaces...) -- those can never match the allow-list."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _ADDRESS_RE.match(value) else None


def _as_list(value):
    """A single value or a list/tuple/set as a list; anything odd stays an item so that it fails
    normalize_address() instead of raising."""
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]


def _allow_list(setting_name):
    return {
        address
        for address in (normalize_address(item) for item in (getattr(settings, setting_name, None) or ()))
        if address
    }


def check_send_allowed(*, sender_address, to, cc=(), bcc=(), provider):
    """Raise RecetteMailBlocked unless this exact send is allowed in recette. No-op elsewhere."""
    if not recette_mode():
        return
    if provider != "graph":
        raise RecetteMailBlocked(
            "SMTP sending is disabled in recette: send through a test Microsoft 365 mailbox."
        )
    if not getattr(settings, "RECETTE_MAIL_SEND_ENABLED", False):
        raise RecetteMailBlocked("Sending is disabled in recette (RECETTE_MAIL_SEND_ENABLED is off).")
    senders = _allow_list("RECETTE_ALLOWED_SENDERS")
    recipients = _allow_list("RECETTE_ALLOWED_RECIPIENTS")
    if not senders or not recipients:
        raise RecetteMailBlocked("Sending is disabled in recette: the sender/recipient allow-lists are empty.")
    sender = normalize_address(sender_address)
    if sender is None or sender not in senders:
        raise RecetteMailBlocked("This sender mailbox is not allowed in recette.")
    to_list = _as_list(to)
    if not to_list:
        raise RecetteMailBlocked("A recipient is required.")
    for raw in [*to_list, *_as_list(cc), *_as_list(bcc)]:
        address = normalize_address(raw)
        if address is None or address not in recipients:
            raise RecetteMailBlocked(f'Recipient "{raw}" is not on the recette allow-list.')


def check_mailbox_address(address):
    """Only allow-listed addresses may be registered, connected or used as a mailbox."""
    if not recette_mode():
        return
    normalized = normalize_address(address)
    if normalized is None or normalized not in _allow_list("RECETTE_ALLOWED_SENDERS"):
        raise RecetteMailBlocked("This mailbox address is not allowed in recette.")


def confirm_tenant_with_microsoft(access_token):
    """The Microsoft account behind `access_token` must belong to the recette tenant. The tenant
    id comes from Microsoft Graph's own answer (see the module docstring), never from decoding the
    token. No tenant configured, no token, any HTTP/JSON problem or any other tenant -> refused."""
    if not recette_mode():
        return
    expected = str(getattr(settings, "RECETTE_M365_TENANT_ID", "") or "").strip().lower()
    if not expected or not access_token:
        raise RecetteMailBlocked("This Microsoft account is not in the recette Microsoft 365 tenant.")
    tenant = ""
    try:
        response = requests.get(
            MS_GRAPH_ORGANIZATION_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
        if response.status_code == 200:
            organizations = response.json().get("value") or []
            if len(organizations) == 1:
                tenant = str(organizations[0].get("id") or "").strip().lower()
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        tenant = ""
    if tenant != expected:
        raise RecetteMailBlocked("This Microsoft account is not in the recette Microsoft 365 tenant.")


def block_in_recette(what):
    """Disable an outbound feature outright in recette (no-op elsewhere)."""
    if recette_mode():
        raise RecetteMailBlocked(f"{what} is disabled in recette.")
