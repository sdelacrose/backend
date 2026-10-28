"""Targeted, preview-first repair of recette document prerequisites (05AQ)."""
import hashlib
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from models.configuration_templates import get_company_configuration_defaults
from models.models import CompanyConfig, CompanyProfile
from project_management.models import Folder

FIELDS = (
    "company_roles", "document_types", "document_categories", "document_status",
    "document_internal_review_status", "document_external_review_status", "transmittal_category",
    "correspondence_types", "project_types", "contract_type", "priority_levels",
    "team_members_roles", "funding_strategy", "international_standard", "major_project_model",
    "commercial_model", "supporting_agreement", "status_options", "reminder_settings",
)


class Command(BaseCommand):
    help = "Preview a targeted recette repair; --apply requires the exact preview SHA256."

    def add_arguments(self, parser):
        parser.add_argument("--company-id", type=int, required=True)
        parser.add_argument("--company-name", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expected-plan-sha256")

    @transaction.atomic
    def handle(self, *args, **options):
        if getattr(settings, "RECETTE_MODE", False) is not True:
            raise CommandError("This repair only runs with RECETTE_MODE enabled.")
        companies = CompanyProfile.objects
        configs = CompanyConfig.objects
        if options["apply"]:
            companies = companies.select_for_update()
            configs = configs.select_for_update()
        try:
            company = companies.get(pk=options["company_id"], company_name=options["company_name"])
            config = configs.get(company_profile=company, solution="Energy Consultant Solution")
        except (CompanyProfile.DoesNotExist, CompanyConfig.DoesNotExist, CompanyConfig.MultipleObjectsReturned):
            raise CommandError("Expected one matching company and one Energy configuration; nothing changed.")
        roots = Folder.objects.filter(company_profile=company, name=f"{company.id}_company_root_folder")
        root_list = list(roots.values("id", "parent_id", "project_id", "freelancer_id"))
        if len(root_list) > 1 or any(row[key] is not None for row in root_list for key in ("parent_id", "project_id", "freelancer_id")):
            raise CommandError("Ambiguous document root; nothing changed.")
        defaults = get_company_configuration_defaults()
        changes = {
            field: {"before": getattr(config, field), "after": defaults[field]}
            for field in FIELDS if not getattr(config, field) and defaults.get(field)
        }
        plan = {"company_id": company.pk, "company_name": company.company_name,
                "config_id": config.pk, "create_root": not root_list,
                "existing_root_ids": [row["id"] for row in root_list],
                "configuration_changes": changes}
        encoded = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if options["apply"]:
            if options["expected_plan_sha256"] != digest:
                raise CommandError("Preview missing or changed: run again without --apply and review the new plan.")
            if plan["create_root"]:
                Folder.objects.create(company_profile=company, name=f"{company.id}_company_root_folder", parent=None)
            for field, change in changes.items():
                setattr(config, field, change["after"])
            if changes:
                config.save(update_fields=list(changes))
        self.stdout.write(encoded)
        self.stdout.write(f"plan_sha256={digest}")
        self.stdout.write("APPLIED" if options["apply"] else "PREVIEW ONLY — no data changed")
