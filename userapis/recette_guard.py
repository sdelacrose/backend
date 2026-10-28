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

import base64
import json
import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

MS_GRAPH_ORGANIZATION_URL = "https://graph.microsoft.com/v1.0/organization?$select=id"

_TENANT_REFUSED = "This Microsoft account is not in the recette Microsoft 365 tenant."
_GUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
_GRAPH_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.]{1,64}$")

# MAIL-RECETTE-01N (temporary diagnostic): the public tenant Microsoft uses for personal accounts.
MS_CONSUMER_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"
_MAX_DIAGNOSTIC_TOKEN_CHARS = 16384

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
    if not expected:
        _refuse_tenant("tenant_not_configured")
    if not access_token:
        _refuse_tenant("no_token")
    try:
        response = requests.get(
            MS_GRAPH_ORGANIZATION_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
    except requests.Timeout:
        _refuse_tenant("graph_timeout")
    except requests.RequestException as error:
        _refuse_tenant("graph_network_error", error=type(error).__name__)
    status = getattr(response, "status_code", None)
    if status != 200:
        details = {"status": status, "graph_error": _graph_error_code(response)}
        if status == 400:
            details.update(_bad_request_diagnostic(access_token, expected))
        _refuse_tenant("graph_http_status", **details)
    try:
        payload = response.json()
    except ValueError:
        _refuse_tenant("graph_invalid_json")
    if not isinstance(payload, dict):
        _refuse_tenant("graph_unexpected_payload")
    organizations = payload.get("value") or []
    if not isinstance(organizations, list) or not organizations:
        _refuse_tenant("organization_absent")
    if len(organizations) > 1:
        _refuse_tenant("organization_multiple", count=len(organizations))
    first = organizations[0]
    tenant = str(first.get("id") or "").strip().lower() if isinstance(first, dict) else ""
    if not tenant:
        _refuse_tenant("organization_without_id")
    if tenant != expected:
        # Only comparisons are logged -- never either identifier. `same_prefix` tells a different
        # tenant (False) from a malformed or decorated setting (often True / expected not a GUID).
        _refuse_tenant(
            "tenant_mismatch",
            returned_is_guid=bool(_GUID_RE.match(tenant)),
            expected_is_guid=bool(_GUID_RE.match(expected)),
            same_prefix=tenant[:8] == expected[:8],
        )


def _refuse_tenant(reason, **details):
    """Log WHY the tenant check refused (technical, no personal or secret data) and refuse. The
    refusal is always the same message: the log is the only place the reason is visible. Only
    values built here are logged (reason codes, HTTP status, counts, booleans, a sanitized Graph
    error code, an exception class name) -- never a token, address or full identifier."""
    logger.warning(
        "recette tenant check refused: reason=%s%s",
        reason,
        "".join(f" {name}={value}" for name, value in details.items()),
    )
    raise RecetteMailBlocked(_TENANT_REFUSED) from None


def _unverified_jwt_claims(token):
    """The payload of a JWT, decoded WITHOUT checking its signature -- for the diagnostic below
    only. None for anything that is not a reasonably sized, well formed JWT (an opaque token, bad
    base64 or JSON, a payload that is not an object). Never raises."""
    if not isinstance(token, str) or not token or len(token) > _MAX_DIAGNOSTIC_TOKEN_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, TypeError, RecursionError):  # bad base64/JSON/UTF-8 are all ValueErrors
        return None
    return claims if isinstance(claims, dict) else None


def _bad_request_diagnostic(access_token, expected):
    """MAIL-RECETTE-01N, TEMPORARY: what kind of token Graph answered 400 to, as four short
    enumerations only (never a claim, identifier or address). Read from the UNVERIFIED payload, so
    it is a hint for whoever reads the log: nothing ever authorizes or refuses on it -- the caller
    refuses regardless. Never raises."""
    context = {"tenant_match": "unknown", "consumer_account": "unknown",
               "token_type": "unknown", "token_is_jwt": "false", "graph_error_category": "bad_request"}
    try:
        claims = _unverified_jwt_claims(access_token)
        if claims is None:
            return context
        context["token_is_jwt"] = "true"
        tid = claims.get("tid")
        tid = tid.strip().lower() if isinstance(tid, str) else ""
        if _GUID_RE.match(tid):
            context["tenant_match"] = "true" if tid == expected else "false"
            context["consumer_account"] = "true" if tid == MS_CONSUMER_TENANT_ID else "false"
        if claims.get("idtyp") == "app" or (claims.get("roles") and not claims.get("scp")):
            context["token_type"] = "application"
        elif isinstance(claims.get("scp"), str) and claims["scp"].strip():
            context["token_type"] = "delegated"
    except Exception:  # noqa: BLE001 -- a diagnostic must never change the (refusing) outcome
        pass
    return context


def _graph_error_code(response):
    """Graph's short error code (e.g. 'Authorization_RequestDenied') of a failed answer, or
    'none'. Only a short code-like string is ever returned."""
    try:
        code = response.json().get("error", {}).get("code")
    except (ValueError, AttributeError, TypeError):
        return "none"
    return code if isinstance(code, str) and _GRAPH_ERROR_CODE_RE.match(code) else "none"


def microsoft_app_configured():
    """False in recette while the Microsoft app registration (client id, secret, tenant) is
    incomplete; always True elsewhere. Callers must then make NO Microsoft/SharePoint call."""
    if not recette_mode():
        return True
    return all(str(getattr(settings, name, "") or "").strip()
               for name in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID",
                            "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"))


def require_microsoft_app():
    if not microsoft_app_configured():
        raise RecetteMailBlocked("The Microsoft app registration is not configured in recette.")


def block_in_recette(what):
    """Disable an outbound feature outright in recette (no-op elsewhere)."""
    if recette_mode():
        raise RecetteMailBlocked(f"{what} is disabled in recette.")
