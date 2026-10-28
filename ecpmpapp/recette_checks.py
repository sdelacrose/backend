"""LINKS-RECETTE-03: start-up safety check of the shared "recette" environment.

Pure function (no Django import) so it can be unit-tested; ecpmpapp.settings.recette calls it at
import time and refuses to start when it returns anything. Messages name VARIABLES only -- never
a value, so nothing secret can reach a log.
"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit

# Strings that identify the production/staging hosts and accounts known to this codebase.
# The production domain is sharemundolinks.com (staging/dev are sub-domains of it): the marker is the
# full domain, so the recette domain (e.g. recette.sharemundolinks.fr) is not caught by it.
DEFAULT_FORBIDDEN_MARKERS = ("sharemundolinks.com", "amplifyapp.com", "foliumaitech", "amazonaws.com",
                             "024848485297")

# Application URLs that must point at the recette host(s): checked on the SETTINGS (what the code
# really uses), not only on the environment, because common.py can inherit production values.
_HOST_URL_SETTINGS = ("BACKEND_URL", "FRONTEND_URL", "FRONTEND_URL_CPM", "FRONTEND_URL_CORE",
                      "FRONTEND_URL_ENTERPRISE", "FRONTEND_URL_BACK_OFFICE", "MS_REDIRECT_URI",
                      "AZURE_REDIRECT_URI")

# If any of these is set, the environment is wired to something recette must never reach.
FORBIDDEN_ENV = (
    "AWS_SQS_QUEUE_URL", "AWS_SQS_QUEUE_ARN", "AWS_SQS_ROLE_ARN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "FIREBASE_PROJECT_ID", "FIREBASE_CLIENT_EMAIL", "FIREBASE_PRIVATE_KEY",
    "FIREBASE_SERVICE_ACCOUNT_JSON", "FIREBASE_SERVICE_ACCOUNT_JSON_BASE64",
    "MATRIX_ACCESS_TOKEN", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD",
)

_INSECURE_DEFAULTS = {"HEALTH_CHECK_API_KEY": "health_check_secret_key_2024"}
_ADDRESS_RE = re.compile(r"^[a-z0-9._%+\-']+@[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
_CONSOLE_EMAIL = "django.core.mail.backends.console.EmailBackend"


def _https_urls(values):
    return bool(values) and all(str(v).startswith("https://") for v in values)


_PROXY_RE = re.compile(r"^http://[a-z0-9_-]+:\d+/?$")


def _host_of(url):
    """Lower-cased host of a URL (the scheme may be missing)."""
    return urlsplit(url if "://" in url else "//" + url).hostname or ""


def _addresses(host, resolve):
    """IP addresses `host` resolves to, or None when it does not resolve."""
    try:
        return {info[4][0].split("%")[0] for info in resolve(host, None)}
    except OSError:
        return None


def collect_problems(conf, environ, resolve=socket.getaddrinfo):
    """Return the list of problems (empty = safe to start). `conf` is the settings namespace,
    `environ` the process environment, `resolve` the DNS lookup (injectable for tests)."""
    problems = []

    def need(condition, message):
        if not condition:
            problems.append(message)

    need(conf.get("DEBUG") is False, "DEBUG must be False")
    hosts = list(conf.get("ALLOWED_HOSTS") or [])
    need(hosts and not any(h.startswith(("*", ".")) for h in hosts),
         "RECETTE_ALLOWED_HOSTS must list explicit hosts (no wildcard)")
    need(conf.get("CORS_ALLOW_ALL_ORIGINS") is False, "CORS_ALLOW_ALL_ORIGINS must be False")
    need(_https_urls(conf.get("CORS_ALLOWED_ORIGINS")), "RECETTE_ALLOWED_ORIGINS must list https origins")
    need(_https_urls(conf.get("CSRF_TRUSTED_ORIGINS")), "CSRF_TRUSTED_ORIGINS must list https origins")
    secret = str(conf.get("SECRET_KEY") or "")
    need(len(secret) >= 32 and not secret.startswith("django-insecure"),
         "RECETTE_SECRET_KEY must be a dedicated secret of at least 32 characters")
    need(conf.get("EMAIL_BACKEND") == _CONSOLE_EMAIL, "EMAIL_BACKEND must be the console backend")
    need(conf.get("MATRIX_ENABLED") is False, "MATRIX_ENABLED must be False")
    need(conf.get("CHAT_PUSH_NOTIFICATIONS_ENABLED") is False, "CHAT_PUSH_NOTIFICATIONS_ENABLED must be False")
    need(conf.get("CELERY_BEAT_SCHEDULE") == {}, "CELERY_BEAT_SCHEDULE must be empty")
    default_storage = (conf.get("STORAGES") or {}).get("default", {}).get("BACKEND", "")
    need(default_storage.endswith("FileSystemStorage"), "the default storage must be the dedicated file storage")

    need(environ.get("DJANGO_SETTINGS_MODULE") == "ecpmpapp.settings.recette",
         "DJANGO_SETTINGS_MODULE must be ecpmpapp.settings.recette")
    need(environ.get("CELERY_ENV") == "recette", "CELERY_ENV must be recette")

    need(str(environ.get("DB_NAME", "")).startswith("links_recette"), "DB_NAME must start with links_recette")
    for name in ("DB_HOST", "REDIS_HOST"):
        value = str(environ.get(name, ""))
        need(value and "." not in value, f"{name} must be the name of the recette container (no dotted host)")
        # A name proves nothing: it must also RESOLVE to a private (container-network) address.
        if value and "." not in value:
            found = _addresses(value, resolve)
            if found is None:
                problems.append(f"{name} cannot be resolved: start the recette containers first")
            else:
                need(found and all(ipaddress.ip_address(a).is_private for a in found),
                     f"{name} does not resolve to a private address")
    for name in ("HTTP_PROXY", "HTTPS_PROXY"):
        need(_PROXY_RE.match(str(environ.get(name, ""))),
             f"{name} must be the recette egress proxy (http://<container>:<port>)")
    for name in ("BACKEND_URL", "FRONTEND_URL"):
        need(_https_urls([environ.get(name, "")]), f"{name} must be set explicitly to the recette https URL")

    markers = tuple(DEFAULT_FORBIDDEN_MARKERS) + tuple(
        m.strip().lower() for m in str(environ.get("RECETTE_FORBIDDEN_MARKERS", "")).split(",") if m.strip()
    )
    for name in ("DB_HOST", "REDIS_HOST", "BACKEND_URL", "FRONTEND_URL", "FRONTEND_URL_CPM",
                 "FRONTEND_URL_CORE", "FRONTEND_URL_ENTERPRISE", "AUTH0_DOMAIN",
                 "AUTH0_ISSUER_BASE_URL", "MS_REDIRECT_URI"):
        value = str(environ.get(name, "")).lower()
        need(not any(marker in value for marker in markers), f"{name} points to a production/staging marker")
    allowed = {h.lower() for h in hosts}
    for name in _HOST_URL_SETTINGS:
        value = str(conf.get(name) or "").strip()
        if not value:
            problems.append(f"{name} must be set to a recette URL")
            continue
        need(_host_of(value) in allowed, f"{name} must point to one of RECETTE_ALLOWED_HOSTS")
        need(not any(marker in value.lower() for marker in markers),
             f"{name} setting points to a production/staging marker")
    for name in FORBIDDEN_ENV:
        need(not str(environ.get(name, "")).strip(), f"{name} must not be set in recette")
    for name, default in _INSECURE_DEFAULTS.items():
        need(environ.get(name) and environ.get(name) != default, f"{name} must be set to a dedicated value")

    keys = [k.strip() for k in str(conf.get("MS_TOKEN_ENCRYPTION_KEYS") or "").split(",") if k.strip()]
    need(keys, "MS_TOKEN_ENCRYPTION_KEYS must hold recette-only keys")
    if keys:
        try:
            from cryptography.fernet import Fernet
            for key in keys:
                Fernet(key.encode())
        except Exception:  # noqa: BLE001 -- name only, never the key
            problems.append("MS_TOKEN_ENCRYPTION_KEYS must be valid Fernet keys")
    need(conf.get("MS_TOKEN_ENCRYPTION_WRITE_ENABLED") is True, "MS_TOKEN_ENCRYPTION_WRITE_ENABLED must be True")

    if conf.get("MAIL_SEND_PROOF_ARCHIVING_ENABLED"):
        need(conf.get("MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE") is not None,
             "MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE is required when archiving is enabled")

    if conf.get("RECETTE_MAIL_SEND_ENABLED"):
        tenant = str(conf.get("RECETTE_M365_TENANT_ID") or "").strip()
        need(tenant, "RECETTE_M365_TENANT_ID is required when sending is enabled")
        allow_any_recipient = conf.get("RECETTE_MAIL_ALLOW_ANY_RECIPIENT", False)
        need(type(allow_any_recipient) is bool,
             "RECETTE_MAIL_ALLOW_ANY_RECIPIENT must be a boolean")
        names = ["RECETTE_ALLOWED_SENDERS"]
        if allow_any_recipient is not True or conf.get("RECETTE_ALLOWED_RECIPIENTS"):
            names.append("RECETTE_ALLOWED_RECIPIENTS")
        for name in names:
            addresses = list(conf.get(name) or [])
            need(addresses and all(_ADDRESS_RE.match(a) for a in addresses),
                 f"{name} must list full e-mail addresses (no domain or wildcard)")
    return problems
