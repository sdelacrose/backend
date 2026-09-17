from django.core.management.base import BaseCommand

from project_management.models import Document
from project_management.services.document_indexing import enqueue_document_indexing, retry_due_jobs


class Command(BaseCommand):
    help = "Queue document indexing jobs for existing documents."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        limit = max(1, min(options["limit"], 1000))
        after_id = max(0, options["after_id"])
        force = options["force"]
        retried = retry_due_jobs(limit=limit)
        queued = 0
        documents = list(
            Document.objects.filter(file__isnull=False, id__gt=after_id)
            .order_by("id")[:limit]
        )
        for document in documents:
            if enqueue_document_indexing(document.id, force=force):
                queued += 1
        next_after_id = documents[-1].id if len(documents) == limit else None
        self.stdout.write(
            self.style.SUCCESS(
                f"Processed {len(documents)} document(s), queued {queued}, "
                f"redispatched {retried}. Next --after-id: {next_after_id or 'complete'}."
            )
        )
