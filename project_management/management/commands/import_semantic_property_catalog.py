import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from project_management.models import SemanticPropertyDefinition


class Command(BaseCommand):
    help = "Import a versioned local IDTA/AAS semantic property catalogue from JSON."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--source-version", required=True)
        parser.add_argument("--replace", action="store_true")

    def handle(self, *args, **options):
        path = options["path"]
        if not path.is_file():
            raise CommandError(f"Catalogue file does not exist: {path}")
        raw_content = path.read_bytes()
        try:
            entries = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Catalogue is not valid JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise CommandError("Catalogue root must be a JSON list.")

        checksum = hashlib.sha256(raw_content).hexdigest()
        imported = 0
        invalid = 0
        semantic_ids = []
        for entry in entries:
            semantic_id = str(entry.get("semantic_id") or "").strip()
            if not semantic_id:
                invalid += 1
                continue
            semantic_ids.append(semantic_id)
            SemanticPropertyDefinition.objects.update_or_create(
                semantic_id=semantic_id,
                defaults={
                    "idta_template": str(entry.get("idta_template") or ""),
                    "template_version": str(entry.get("template_version") or ""),
                    "preferred_name_en": str(entry.get("preferred_name_en") or ""),
                    "preferred_name_de": str(entry.get("preferred_name_de") or ""),
                    "preferred_name_fr": str(entry.get("preferred_name_fr") or ""),
                    "data_type": str(entry.get("data_type") or ""),
                    "verification_status": SemanticPropertyDefinition.VerificationStatus.VERIFIED,
                    "source_version": options["source_version"],
                    "source_checksum": checksum,
                },
            )
            imported += 1

        deleted = 0
        if options["replace"]:
            deleted, _ = SemanticPropertyDefinition.objects.exclude(
                semantic_id__in=semantic_ids
            ).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {imported} semantic definition(s), skipped {invalid}, "
                f"removed {deleted}. Checksum: {checksum}."
            )
        )
