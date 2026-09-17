from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from project_management.models import DocumentIndexJob
from project_management.services.document_indexing import retry_due_jobs


class Command(BaseCommand):
    help = "Recover stale or undispatched document indexing jobs."

    def add_arguments(self, parser):
        parser.add_argument("--stale-minutes", type=int, default=20)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not getattr(settings, "DOCUMENT_INDEXING_ENABLED", False):
            raise CommandError("DOCUMENT_INDEXING_ENABLED must be enabled.")

        stale_minutes = max(1, options["stale_minutes"])
        limit = max(1, min(options["limit"], 1000))
        stale_before = timezone.now() - timedelta(minutes=stale_minutes)

        stale_jobs = DocumentIndexJob.objects.filter(
            status=DocumentIndexJob.Status.RUNNING,
            started_at__lt=stale_before,
        )[:limit]
        recovered = 0
        for job in stale_jobs:
            if job.attempt_count >= job.max_attempts:
                job.status = DocumentIndexJob.Status.FAILED
            else:
                job.status = DocumentIndexJob.Status.RETRYING
                job.available_at = timezone.now()
            job.error = "Worker stopped before the indexing attempt completed."
            job.failure_category = "worker_interrupted"
            job.save(
                update_fields=[
                    "status",
                    "available_at",
                    "error",
                    "failure_category",
                    "updated_at",
                ]
            )
            recovered += 1

        dispatched = retry_due_jobs(limit=limit)
        self.stdout.write(
            self.style.SUCCESS(
                f"Recovered {recovered} stale jobs and dispatched {dispatched} ready jobs."
            )
        )
