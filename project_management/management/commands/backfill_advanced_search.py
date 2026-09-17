from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from project_management.services.search_indexing import (
    SOURCE_MODELS,
    backfill_search_records,
    dispatch_ready_search_jobs,
)


class Command(BaseCommand):
    help = "Queue generalized advanced-search indexing jobs for existing records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-type",
            action="append",
            dest="source_types",
            choices=sorted(SOURCE_MODELS),
        )
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--after-id", type=int, default=0)

    def handle(self, *args, **options):
        if not getattr(settings, "ADVANCED_SEARCH_ENABLED", True):
            raise CommandError("ADVANCED_SEARCH_ENABLED must be enabled.")

        limit = max(1, min(options["limit"], 500))
        after_id = max(0, options["after_id"])
        result = backfill_search_records(
            source_types=options["source_types"] or None,
            after_id=after_id,
            limit=limit,
        )
        dispatched = dispatch_ready_search_jobs(limit=limit)
        self.stdout.write(
            self.style.SUCCESS(
                f"Processed {result['processed']} record(s), queued {result['queued']}, "
                f"dispatched {dispatched}. Next cursor(s): "
                f"{result['next_after_by_source']}."
            )
        )
