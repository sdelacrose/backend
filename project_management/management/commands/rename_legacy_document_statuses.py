from django.core.management.base import BaseCommand
from django.db import transaction

from project_management.models import Document, ObjectAssessment


STATUS_RENAMES = {
    "In review": "Under review",
    "Complete": "Approved",
}


class Command(BaseCommand):
    help = (
        "Rename legacy document and object-assessment statuses to the current "
        "document status values."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Preview updates without saving changes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("Running in dry-run mode"))

        summaries = [
            self._process_model(Document, "Document", dry_run),
            self._process_model(
                Document.history.model,
                "HistoricalDocument",
                dry_run,
            ),
            self._process_model(ObjectAssessment, "ObjectAssessment", dry_run),
            self._process_model(
                ObjectAssessment.history.model,
                "HistoricalObjectAssessment",
                dry_run,
            ),
        ]

        self.stdout.write(self.style.SUCCESS("Legacy document status rename complete"))
        total_rows = 0
        for summary in summaries:
            total_rows += summary["updated"]
            self.stdout.write(
                f"{summary['label']}: {summary['updated']} row(s) updated"
            )
            for old_value, new_value in STATUS_RENAMES.items():
                count = summary["counts"].get(old_value, 0)
                self.stdout.write(f"  - {old_value} -> {new_value}: {count}")

        self.stdout.write(self.style.SUCCESS(f"Total updated rows: {total_rows}"))

    def _process_model(self, model, label, dry_run):
        counts = {}
        updated = 0

        with transaction.atomic():
            for old_value, new_value in STATUS_RENAMES.items():
                queryset = model.objects.filter(status=old_value)
                count = queryset.count()
                counts[old_value] = count
                updated += count

                if not dry_run and count:
                    queryset.update(status=new_value)

            if dry_run:
                transaction.set_rollback(True)

        return {
            "label": label,
            "updated": updated,
            "counts": counts,
        }
