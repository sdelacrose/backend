"""SECURITY-MAIL-0C: Fernet-based encrypt/decrypt helpers for the Microsoft OAuth tokens stored
in UserOutlookCredentials (admin-server-models -- that model is never modified from here, see
userapis/signals.py for how decryption is attached to it without touching its class).

This module is READ-SIDE ONLY for now: userapis/signals.py wires decrypt_token() to a post_init
receiver; nothing in this lot calls encrypt_token() yet (write-side stays disabled until
SECURITY-MAIL-0D, per that ticket's own explicit instruction). encrypt_token() is provided
now so 0D only has to wire it into pre_save, never re-design the crypto/versioning scheme.

Versioned envelope (ENCRYPTION_PREFIX): lets a legacy plaintext token (no prefix -- returned
unchanged, never an error) and a Fernet-encrypted token (recognizable prefix) coexist in the
same column indefinitely, and leaves room for a future v2 envelope without ambiguity.

Never logs a token or key value anywhere in this module.
"""

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

logger = logging.getLogger(__name__)

# Versioned so a future encryption scheme change (e.g. a v2 envelope) can tell its own values
# apart from v1's without any ambiguity -- never bump this in place, add a new prefix instead.
ENCRYPTION_PREFIX = "encv1:"


class TokenCryptoError(Exception):
    """Raised for anything that prevents returning a definite plaintext/ciphertext value --
    never silently mapped to "treat it as plaintext" once a value already carries
    ENCRYPTION_PREFIX. That fallback is reserved exclusively for a value with NO prefix at
    all (a genuine historical plaintext token)."""


def _configured_fernet_keys():
    """MS_TOKEN_ENCRYPTION_KEYS: a comma-separated list of Fernet keys, newest/primary first
    (MultiFernet encrypts with the first, decrypts by trying each in order -- this is the
    rotation mechanism; see the SECURITY-MAIL-0A/0B reports for the rotation procedure).
    Deliberately never SECRET_KEY, never hardcoded, never generated here -- unset simply means
    "no encryption configured yet", which is a valid, supported state (legacy plaintext tokens
    keep working)."""
    raw = getattr(settings, "MS_TOKEN_ENCRYPTION_KEYS", "") or ""
    return [key.strip() for key in raw.split(",") if key.strip()]


def _fernet():
    """Returns a MultiFernet for the configured key(s), or None when none are configured
    (the "legacy-only, no encryption active" state). A configured-but-malformed key is a
    distinct, louder failure -- it must never be swallowed into "treat as unconfigured",
    since that could silently strand an already-encrypted token as undecryptable without any
    visible signal at startup."""
    keys = _configured_fernet_keys()
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(key.encode("ascii")) for key in keys])
    except (ValueError, TypeError) as error:
        logger.error(
            "MS_TOKEN_ENCRYPTION_KEYS is configured but at least one key is not a valid "
            "Fernet key (key material itself is never logged)."
        )
        raise TokenCryptoError(
            "MS_TOKEN_ENCRYPTION_KEYS is configured but malformed."
        ) from error


def encrypt_token(value):
    """Not wired into any write path in this lot (SECURITY-MAIL-0D's job). Idempotent: a
    value that already carries ENCRYPTION_PREFIX is returned unchanged rather than
    double-encrypted."""
    if value is None or value == "":
        return value
    if value.startswith(ENCRYPTION_PREFIX):
        return value
    fernet = _fernet()
    if fernet is None:
        raise TokenCryptoError(
            "Cannot encrypt: MS_TOKEN_ENCRYPTION_KEYS is not configured."
        )
    ciphertext = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    return ENCRYPTION_PREFIX + ciphertext


def decrypt_token(value):
    """Transparent, legacy-compatible decrypt for a value read from UserOutlookCredentials.

    - None / "" -> returned unchanged (explicit null/empty handling, never an error).
    - No ENCRYPTION_PREFIX -> returned UNCHANGED. This is the legacy-plaintext path: every
      token written before encryption is enabled, and every token for as long as
      MS_TOKEN_ENCRYPTION_KEYS stays unset, falls here. Never treated as an error.
    - Carries ENCRYPTION_PREFIX but no key configured, a wrong/rotated-out key, or corrupted
      ciphertext -> raises TokenCryptoError. Per the ticket's own impérative rule: a value
      that LOOKS encrypted must never be handed back as if it were a directly usable plaintext
      token (e.g. the literal ciphertext string sent to Microsoft Graph as a bearer token).
    """
    if value is None or value == "":
        return value
    if not value.startswith(ENCRYPTION_PREFIX):
        return value
    fernet = _fernet()
    if fernet is None:
        raise TokenCryptoError(
            "Token is encrypted but MS_TOKEN_ENCRYPTION_KEYS is not configured."
        )
    payload = value[len(ENCRYPTION_PREFIX):].encode("ascii")
    try:
        return fernet.decrypt(payload).decode("utf-8")
    except InvalidToken as error:
        raise TokenCryptoError(
            "Token carries the encryption prefix but could not be decrypted with any "
            "configured key (wrong/rotated-out key, or corrupted value)."
        ) from error
