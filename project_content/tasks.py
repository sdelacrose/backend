from math import ceil

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .audit import record_project_content_event
from .models import GeneratedProjectDocument, ProjectDocumentGenerationRun
from .rendering import render_generation_run
from .services import (
    acceptance_generation_remaining_seconds,
    process_acceptance_generation_run,
    process_document_generation_run,
)


RETRYABLE_GENERATION_ERRORS = {
    "generation_service_rate_limited",
    "generation_service_unavailable",
}


def _mark_generation_retry_pending(run):
    run.status = run.Status.QUEUED
    run.completed_at = None
    run.error_code = ""
    run.error_message = ""
    run.save(
        update_fields=[
            "status",
            "completed_at",
            "error_code",
            "error_message",
            "updated_at",
        ]
    )
    if isinstance(run, ProjectDocumentGenerationRun):
        generated_document = run.generated_document
        generated_document.status = GeneratedProjectDocument.Status.GENERATING
        generated_document.save(update_fields=["status", "updated_at"])


@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def process_project_document_generation_run(self, run_id):
    try:
        run = process_document_generation_run(run_id)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(15 * (2 ** self.request.retries), 120))
    if (
        run.status == run.Status.FAILED
        and run.error_code in RETRYABLE_GENERATION_ERRORS
        and self.request.retries < self.max_retries
    ):
        error_code = run.error_code
        _mark_generation_retry_pending(run)
        raise self.retry(
            exc=RuntimeError(error_code),
            countdown=min(15 * (2 ** self.request.retries), 120),
        )


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=15,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_acceptance_checklist_generation_run(self, run_id):
    try:
        run = process_acceptance_generation_run(run_id)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(15 * (2 ** self.request.retries), 120))
    if run.status == run.Status.GENERATING:
        lease_seconds = getattr(
            settings,
            "PROJECT_CONTENT_GENERATION_STALE_SECONDS",
            300,
        )
        elapsed_seconds = (
            (timezone.now() - run.started_at).total_seconds()
            if run.started_at
            else lease_seconds
        )
        raise self.retry(
            exc=RuntimeError("acceptance_generation_lease_active"),
            countdown=max(1, ceil(min(lease_seconds - elapsed_seconds, acceptance_generation_remaining_seconds(run)))),
            max_retries=self.request.retries + 1,
        )
    if (
        run.status == run.Status.FAILED
        and run.error_code in RETRYABLE_GENERATION_ERRORS
        and self.request.retries < self.max_retries
    ):
        error_code = run.error_code
        remaining_seconds = acceptance_generation_remaining_seconds(run)
        if remaining_seconds <= 0:
            process_acceptance_generation_run(run.id)
            return
        _mark_generation_retry_pending(run)
        raise self.retry(
            exc=RuntimeError(error_code),
            countdown=max(1, min(15 * (2 ** self.request.retries), 120, ceil(remaining_seconds))),
        )


@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def render_project_document_generation_run(self, run_id, user_id):
    from models.models import User

    run = ProjectDocumentGenerationRun.objects.select_related(
        "generated_document"
    ).get(pk=run_id)
    if run.status == ProjectDocumentGenerationRun.Status.APPLIED:
        return
    try:
        render_generation_run(run, User.objects.get(pk=user_id))
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc,
                countdown=min(15 * (2 ** self.request.retries), 120),
            )
        run.status = ProjectDocumentGenerationRun.Status.FAILED
        run.error_code = "document_render_failed"
        run.error_message = "The reviewed document could not be rendered."
        run.save(
            update_fields=["status", "error_code", "error_message", "updated_at"]
        )
        generated_document = run.generated_document
        generated_document.status = (
            GeneratedProjectDocument.Status.OUT_OF_DATE
            if generated_document.current_revision_id
            else GeneratedProjectDocument.Status.FAILED
        )
        generated_document.save(update_fields=["status", "updated_at"])
        record_project_content_event(
            project=run.project,
            event_type="document_render_failed",
            instance=run,
            actor=run.modified_by,
            metadata={"error_code": run.error_code},
        )
        raise
