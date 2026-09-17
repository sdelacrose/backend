from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from project_management.models import SearchIndexJob
from project_management.services.search_indexing import dispatch_ready_search_jobs


class Command(BaseCommand):
    help = "Recover interrupted or undispatched generalized search indexing jobs."

    def add_arguments(self, parser):
        parser.add_argument("--stale-minutes", type=int, default=20)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not getattr(settings, "ADVANCED_SEARCH_ENABLED", True):
            raise CommandError("ADVANCED_SEARCH_ENABLED must be enabled.")

        limit = max(1, min(options["limit"], 1000))
        stale_before = timezone.now() - timedelta(
            minutes=max(1, options["stale_minutes"])
        )
        stale_jobs = SearchIndexJob.objects.filter(
            status=SearchIndexJob.Status.RUNNING,
            started_at__lt=stale_before,
        ).order_by("started_at", "id")[:limit]
        recovered = 0
        for job in stale_jobs:
            job.status = (
                SearchIndexJob.Status.FAILED
                if job.attempt_count >= job.max_attempts
                else SearchIndexJob.Status.RETRYING
            )
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

        dispatched = dispatch_ready_search_jobs(limit=limit)
        self.stdout.write(
            self.style.SUCCESS(
                f"Recovered {recovered} stale job(s) and dispatched "
                f"{dispatched} ready job(s)."
            )
        )
