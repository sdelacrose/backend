# project_management/management/commands/sync_document_config.py

from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig
from project_management.models.constants import (
    GC_DOCUMENT_TYPES,
    GC_DOCUMENT_CATEGORIES,
    GC_DOCUMENT_DISCIPLINES,
)

# Exclude Calendar and Revision only for all Configs
EXCLUDE_DOC_TYPES = {"Calendar:CAL", "Revision:REV"}

MODELS = [ECPMPConfig, GlobalConfig, CompanyConfig]
FIELD_CONSTANTS = {
    "document_categories": GC_DOCUMENT_CATEGORIES,
    "document_disciplines": GC_DOCUMENT_DISCIPLINES,
}

class Command(BaseCommand):
    help = "Sync document_* fields on all Configs against the GC_… constants, excluding Calendar and Revision for all Configs."

    def handle(self, *args, **kwargs):
        updated = 0

        for Model in MODELS:
            # apply exclusions on document_types for all Configs
            exclude_on_model = Model in (ECPMPConfig, CompanyConfig, GlobalConfig)

            for cfg in Model.objects.all():
                changed = False

                for field, master_list in FIELD_CONSTANTS.items():
                    master = set(master_list)

                    # strip excluded types on applicable models
                    if exclude_on_model and field in ["document_types", "document_categories", "document_disciplines"]:
                        master -= EXCLUDE_DOC_TYPES

                    items = getattr(cfg, field) or []

                    # remove excluded items from existing items if needed
                    if exclude_on_model and field in ["document_types", "document_categories", "document_disciplines"]:

                        new_items = [i for i in items if i not in EXCLUDE_DOC_TYPES]
                        if len(new_items) != len(items):
                            items = new_items
                            changed = True

                    for need in master:
                        if need not in items:
                            items.append(need)

                    # remove duplicates while preserving order
                    seen = set()
                    deduped_items = []
                    for item in items:
                        if item not in seen:
                            seen.add(item)
                            deduped_items.append(item)
                    items = deduped_items

                    # if changes detected, update the field on the model instance
                    if items != getattr(cfg, field):
                        setattr(cfg, field, items)
                        changed = True

                if changed:
                    cfg.save(update_fields=list(FIELD_CONSTANTS.keys()))
                    updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"✔ Synchronized {updated} config objects’ document fields."
            )
        )
