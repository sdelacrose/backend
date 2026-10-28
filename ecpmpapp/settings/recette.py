"""LINKS-RECETTE-03: settings of the shared "recette" environment (one small VM, Docker Compose).

Built on `common` only -- it deliberately does NOT import `development`, `staging` or
`production` (their open hosts/CORS, SMTP and SQS defaults are exactly what recette must not
inherit). Everything dangerous is switched off here and checked again at start-up: the process
refuses to start (ImproperlyConfigured) if the configuration is unsafe. See
ecpmpapp.recette_checks and userapis.recette_guard.

Required variables (names only): RECETTE_SECRET_KEY, RECETTE_ALLOWED_HOSTS, RECETTE_ALLOWED_ORIGINS,
DB_NAME/USER/PASSWORD/HOST/PORT, REDIS_HOST, BACKEND_URL, FRONTEND_URL, HEALTH_CHECK_API_KEY,
MS_TOKEN_ENCRYPTION_KEYS, plus the Auth0/Azure ones common.py already requires.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# common.py reads these without a default although recette never uses AWS: neutral placeholders
# (the start-up check refuses real AWS credentials).
for _name, _value in (
    ("AWS_ACCESS_KEY_ID", ""),
    ("AWS_SECRET_ACCESS_KEY", ""),
    ("AWS_STORAGE_BUCKET_NAME", "links-recette-unused"),
    # LINKS-RECETTE-05H: no Microsoft app registration / Auth0 M2M client yet. Empty is NOT a
    # credential: userapis.recette_guard refuses every Microsoft/SharePoint flow while the app
    # registration is empty, and the health check never calls Auth0 without its own credentials.
    ("AUTH0_M2M_CLIENT_ID", ""),
    ("AUTH0_M2M_CLIENT_SECRET", ""),
    ("AZURE_CLIENT_ID", ""),
    ("AZURE_CLIENT_SECRET", ""),
    ("AZURE_TENANT_ID", ""),
    ("MS_REDIRECT_URI", "api/user/auth/outlook/callback"),
):
    os.environ.setdefault(_name, _value)

from .common import *  # noqa: E402,F401,F403
from ecpmpapp.recette_checks import collect_problems  # noqa: E402

RECETTE_MODE = True
ENV = "recette"

# No back-office in recette: it owns the licence tables everywhere else (see ecpmpapp/recette_apps.py).
INSTALLED_APPS = [*INSTALLED_APPS, "ecpmpapp.recette_apps.RecettePackageLicenseConfig"]

SECRET_KEY = env("RECETTE_SECRET_KEY", default="")
DEBUG = False
ALLOWED_HOSTS = env.list("RECETTE_ALLOWED_HOSTS", default=[])
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env.list("RECETTE_ALLOWED_ORIGINS", default=[])
CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
# common.py only rewrites these for local/dev/staging/prod and otherwise keeps the production
# hosts: recette always takes them from its own environment (checked against ALLOWED_HOSTS).
FRONTEND_URL_CPM = env("FRONTEND_URL_CPM", default="")
FRONTEND_URL_CORE = env("FRONTEND_URL_CORE", default="")
FRONTEND_URL_ENTERPRISE = env("FRONTEND_URL_ENTERPRISE", default="")
FRONTEND_URL_BACK_OFFICE = env("FRONTEND_URL_BACK_OFFICE", default=FRONTEND_URL)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # behind the HTTPS reverse proxy

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST"),
        "PORT": env("DB_PORT"),
    }
}

# Dedicated private storage on the VM volume -- never S3.
_DATA_DIR = Path(env("RECETTE_DATA_DIR", default="/srv/links-recette"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
STATIC_URL = "/static/"
STATIC_ROOT = _DATA_DIR / "static"
MEDIA_URL = "/media/"
MEDIA_ROOT = _DATA_DIR / "media"

# No real e-mail, no external notification, no scheduler.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
MATRIX_ENABLED = False
CHAT_PUSH_NOTIFICATIONS_ENABLED = False
CELERY_BEAT_SCHEDULE = {}

# MAIL-1.4 proof archiving: off until explicitly enabled (with its start date).
MAIL_SEND_PROOF_ARCHIVING_ENABLED = env.bool("MAIL_SEND_PROOF_ARCHIVING_ENABLED", default=False)
MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE = None
_since = env("MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE", default="").strip().replace("Z", "+00:00")
if _since:
    MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE = datetime.fromisoformat(_since)
    if MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE.tzinfo is None:
        MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE = MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE.replace(tzinfo=timezone.utc)

# Recette-only OAuth token keys; every token written is encrypted.
MS_TOKEN_ENCRYPTION_KEYS = env("MS_TOKEN_ENCRYPTION_KEYS", default="")
MS_TOKEN_ENCRYPTION_WRITE_ENABLED = env.bool("MS_TOKEN_ENCRYPTION_WRITE_ENABLED", default=True)

# Mail send guard (userapis.recette_guard): off until explicitly enabled, full addresses only.
RECETTE_MAIL_SEND_ENABLED = env.bool("RECETTE_MAIL_SEND_ENABLED", default=False)
# Explicit demo opt-in: sender and Microsoft tenant restrictions remain mandatory.
RECETTE_MAIL_ALLOW_ANY_RECIPIENT = env.bool("RECETTE_MAIL_ALLOW_ANY_RECIPIENT", default=False)
RECETTE_M365_TENANT_ID = env("RECETTE_M365_TENANT_ID", default="")
RECETTE_ALLOWED_SENDERS = [a.strip().lower() for a in env.list("RECETTE_ALLOWED_SENDERS", default=[])]
RECETTE_ALLOWED_RECIPIENTS = [a.strip().lower() for a in env.list("RECETTE_ALLOWED_RECIPIENTS", default=[])]

_problems = collect_problems(globals(), os.environ)
if _problems:
    raise ImproperlyConfigured(
        "Unsafe recette configuration, refusing to start:\n- " + "\n- ".join(_problems)
    )
