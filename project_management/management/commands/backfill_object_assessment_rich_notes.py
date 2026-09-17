from django.core.management.base import BaseCommand
from project_management.models import ObjectAssessment
from project_management.object_assessment_notes import (
    plain_text_to_html,
    plain_text_to_tiptap_json,
)


class Command(BaseCommand):
    help = (
        "Backfill legacy ObjectAssessment Notes TXT files into structured rich "
        "notes fields."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Preview note conversions without saving any changes.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Iterator batch size for processing legacy notes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        queryset = ObjectAssessment.objects.filter(record_type="Notes")

        total_count = queryset.count()
        updated_count = 0
        skipped_count = 0
        error_count = 0

        if dry_run:
            self.stdout.write(self.style.WARNING("Running in dry-run mode"))

        self.stdout.write(
            self.style.SUCCESS(f"Found {total_count} note records to process")
        )

        for assessment in queryset.iterator(chunk_size=batch_size):
            try:
                if assessment.notes_content_json or (
                    isinstance(assessment.notes_content_html, str)
                    and assessment.notes_content_html.strip()
                ):
                    skipped_count += 1
                    continue

                if not assessment.file:
                    skipped_count += 1
                    continue

                with assessment.file.open("rb") as stored_file:
                    text = stored_file.read().decode("utf-8-sig")

                notes_content_json = plain_text_to_tiptap_json(text)
                notes_content_html = plain_text_to_html(text)

                if not dry_run:
                    assessment.notes_content_json = notes_content_json
                    assessment.notes_content_html = notes_content_html
                    assessment.save(
                        update_fields=[
                            "notes_content_json",
                            "notes_content_html",
                        ]
                    )

                updated_count += 1
                self.stdout.write(
                    f"Processed ObjectAssessment ID {assessment.id}"
                )
            except Exception as exc:
                error_count += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"Error processing ObjectAssessment ID {assessment.id}: {exc}"
                    )
                )

        self.stdout.write(self.style.SUCCESS("Backfill complete"))
        self.stdout.write(self.style.SUCCESS(f"Updated: {updated_count}"))
        self.stdout.write(self.style.SUCCESS(f"Skipped: {skipped_count}"))
        self.stdout.write(self.style.SUCCESS(f"Errors: {error_count}"))
