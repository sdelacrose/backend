from django.core.management.base import BaseCommand
from django.db import transaction

from models import constants
from models.models import CompanyConfig, GlobalConfig, ProjectConfigTemplate
from project_management.models import ECPMPConfig


LEGACY_DOCUMENT_CATEGORY_ALIASES = {
    "General:GEN": "General:GAD",
    "Design:DES": "Design:DTS",
    "Technical:TEC": "Technical:TMP",
}


def normalized_document_categories(values):
    normalized = []
    seen = set()

    for default in constants.GC_DOCUMENT_CATEGORIES:
        normalized.append(default)
        seen.add(default)

    for raw_value in values or []:
        value = str(raw_value).strip()
        if not value:
            continue
        value = LEGACY_DOCUMENT_CATEGORY_ALIASES.get(value, value)
        if value not in seen:
            normalized.append(value)
            seen.add(value)

    return normalized


class Command(BaseCommand):
    help = (
        "Restore canonical document categories across global, company, project "
        "template, and project configurations while preserving custom values."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply repairs. Without this flag, only a dry-run is performed.",
        )

    def handle(self, *args, **options):
        apply_repairs = options["apply"]
        models = (GlobalConfig, CompanyConfig, ProjectConfigTemplate, ECPMPConfig)
        total = 0

        with transaction.atomic():
            for model in models:
                updated = []
                queryset = model.objects.select_for_update().only(
                    "id", "document_categories"
                )
                for configuration in queryset.iterator():
                    current = list(configuration.document_categories or [])
                    repaired = normalized_document_categories(current)
                    if repaired == current:
                        continue
                    configuration.document_categories = repaired
                    updated.append(configuration)

                if apply_repairs and updated:
                    model.objects.bulk_update(updated, ["document_categories"])

                total += len(updated)
                action = "Repaired" if apply_repairs else "Would repair"
                self.stdout.write(f"{model.__name__}: {action} {len(updated)} row(s)")

            if not apply_repairs:
                transaction.set_rollback(True)

        action = "Repaired" if apply_repairs else "Would repair"
        self.stdout.write(self.style.SUCCESS(f"{action} {total} total row(s)."))
