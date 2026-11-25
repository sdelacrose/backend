import os
from pathlib import Path

from celery.schedules import crontab

from .common import *


DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

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

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

STATIC_URL = "/static/"
STATIC_ROOT = Path(BASE_DIR) / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(BASE_DIR) / "media"

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# LOT RH10F: LOCAL-ONLY Celery Beat schedule for the monthly leave accrual
# catch-up job (human_resource_management.tasks.run_monthly_leave_accrual_task).
# Celery's own built-in beat (no django-celery-beat needed for a fixed,
# static schedule like this one) -- reuses the exact same Celery app/broker/
# worker already running in this local docker-compose stack. The job is
# idempotent and self-catching-up (see run_monthly_accrual's docstring), so
# the exact frequency is not safety-critical, only latency-relevant --
# 02:00 daily is the recommended production cadence, applied here too so the
# local schedule is a faithful preview of what ops would configure. This
# setting exists ONLY in this local settings module -- never added to
# common.py/development.py/staging.py/production.py, so production's Celery
# Beat configuration (if any is ever added there) is completely untouched.
_LOCAL_CELERY_BEAT_SCHEDULE = {
    "run-monthly-leave-accrual-daily": {
        "task": "human_resource_management.run_monthly_leave_accrual",
        "schedule": crontab(hour=2, minute=0),
    },
    # LOT RH11: same reuse rationale as above -- runs AFTER the monthly
    # accrual job (02:30, not 02:00) so a cycle whose last month still
    # needed catching up has already been credited by the time closure
    # checks for it.
    "run-leave-cycle-closure-daily": {
        "task": "human_resource_management.run_leave_cycle_closure",
        "schedule": crontab(hour=2, minute=30),
    },
    # AI-1685: same local-only Celery Beat reuse rationale as the two
    # entries above -- matches the real production CronJob cadence
    # ("0 0 * * *", see manifests/ecpmp-api-deployment*.yaml) so this local
    # schedule previews exactly what Ops would configure. process_rule's
    # own anti-duplicate claim makes a missed/late/re-run tick a pure
    # no-op, same idempotence precedent as the two jobs above.
    "send-leave-balance-reminders-daily": {
        "task": "human_resource_management.send_leave_balance_reminders",
        "schedule": crontab(hour=0, minute=0),
    },
    # AI-2054: same local-only Celery Beat reuse rationale as the three
    # entries above. Daily is the catch-all for changes needing no PM write
    # to become effective (month-boundary rollover, a scheduled contractual
    # change reaching its effective_from) -- real-time re-evaluation on an
    # actual PM write is handled separately by signal receivers in
    # human_resource_management/signals.py (transaction.on_commit-dispatched),
    # never by this schedule alone.
    "evaluate-staff-workload-alerts-daily": {
        "task": "human_resource_management.evaluate_staff_workload_alerts",
        "schedule": crontab(hour=1, minute=0),
    },
    # MAIL-1.4-C: retries of the archiving of OUT send proofs (1 min -> 24 h schedule kept in the
    # proofs themselves). A no-op -- it reads nothing and calls nothing -- until
    # MAIL_SEND_PROOF_ARCHIVING_ENABLED and MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE are set below/in the
    # environment; local-only like the entries above.
    "sweep-send-proofs-every-minute": {
        "task": "project_management.sweep_send_proofs",
        "schedule": 60.0,
    },
    # L03/L05: same local-only Celery Beat reuse rationale as the entries
    # above, same AI-1685 dedup/retry shape reused by the underlying
    # service -- a missed/late/re-run tick is a pure no-op. Like
    # send-leave-balance-reminders-daily above, this is NOT wired into the
    # real production CronJob (manifests/ecpmp-api-deployment-prod.yaml) --
    # a deployment-manifest change is explicitly outside this lot's scope.
    "send-hr-reminders-daily": {
        "task": "human_resource_management.send_hr_reminders",
        "schedule": crontab(hour=0, minute=15),
    },
    # L05: same local-only Celery Beat reuse rationale as the entry above --
    # NOT wired into the real production CronJob (manifests/ecpmp-api-
    # deployment-prod.yaml). A missed/late/re-run tick is a pure no-op (the
    # job's own eligibility check -- OnboardingInstance.trigger_key -- is
    # idempotent regardless of how many times or how late it runs).
    "auto-trigger-offboarding-daily": {
        "task": "human_resource_management.auto_trigger_offboarding",
        "schedule": crontab(hour=0, minute=30),
    },
}

# Nothing is scheduled unless the environment explicitly asks for it: set
# LINKS_LOCAL_CELERY_BEAT_ENABLED=1 on the local celery-beat service for a controlled trial.
CELERY_BEAT_SCHEDULE = (
    _LOCAL_CELERY_BEAT_SCHEDULE if os.environ.get("LINKS_LOCAL_CELERY_BEAT_ENABLED") == "1" else {}
)

# MAIL-1.4-C: both settings make the worker archive the proof of every Microsoft 365 send made from
# that moment on (and only those): never the older history. OFF by default: switch on with
# MAIL_SEND_PROOF_ARCHIVING_ENABLED=1 and MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE=<ISO 8601 with a time
# zone, e.g. 2026-01-01T00:00:00+00:00>. A missing, malformed or zone-less date leaves the automatic
# archiving off.
from datetime import datetime

MAIL_SEND_PROOF_ARCHIVING_ENABLED = os.environ.get("MAIL_SEND_PROOF_ARCHIVING_ENABLED") == "1"
MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE = None
try:
    _archive_since = datetime.fromisoformat(
        os.environ.get("MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE", "").strip().replace("Z", "+00:00")
    )
except ValueError:
    _archive_since = None
if _archive_since is not None and _archive_since.tzinfo is not None:
    MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE = _archive_since
