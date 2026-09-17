from django.core.management.base import BaseCommand
from django.db import transaction

from models.models import CompanyConfig

from companyprofileapis.services.configuration_visibility import (
    get_project_configuration_value_repairs,
    repair_project_configuration_values,
)


class Command(BaseCommand):
    help = (
        "Restore company configuration values referenced by projects and re-enable "
        "referenced values that were hidden. Known legacy project values are migrated "
        "to their supported replacements. Runs as a dry-run unless --apply is used."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the repairs. Without this flag, only a dry-run is performed.",
        )

    def handle(self, *args, **options):
        apply_repairs = options["apply"]
        configurations = CompanyConfig.objects.order_by("id")
        repaired_configurations = 0
        restored_values = 0
        reenabled_values = 0
        replaced_legacy_values = 0

        if apply_repairs:
            with transaction.atomic():
                (
                    repaired_configurations,
                    restored_values,
                    reenabled_values,
                    replaced_legacy_values,
                ) = self._process(
                    configurations.select_for_update(),
                    apply_repairs=True,
                )
        else:
            (
                repaired_configurations,
                restored_values,
                reenabled_values,
                replaced_legacy_values,
            ) = self._process(configurations, apply_repairs=False)

        mode = "Repaired" if apply_repairs else "Would repair"
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode} {repaired_configurations} configuration(s): "
                f"{restored_values} value(s) restored, "
                f"{reenabled_values} value(s) re-enabled, "
                f"{replaced_legacy_values} legacy value mapping(s)."
            )
        )

    def _process(self, configurations, *, apply_repairs):
        repaired_configurations = 0
        restored_values = 0
        reenabled_values = 0
        replaced_legacy_values = 0

        for configuration in configurations.iterator():
            missing, hidden, replacements = (
                get_project_configuration_value_repairs(configuration)
            )
            if not missing and not hidden and not replacements:
                continue

            repaired_configurations += 1
            restored_values += sum(len(values) for values in missing.values())
            reenabled_values += sum(len(values) for values in hidden.values())
            replaced_legacy_values += sum(
                len(values) for values in replacements.values()
            )
            self.stdout.write(
                self._format_repair(
                    configuration,
                    missing,
                    hidden,
                    replacements,
                    apply_repairs,
                )
            )
            if apply_repairs:
                repair_project_configuration_values(configuration)

        return (
            repaired_configurations,
            restored_values,
            reenabled_values,
            replaced_legacy_values,
        )

    def _format_repair(
        self,
        configuration,
        missing,
        hidden,
        replacements,
        apply_repairs,
    ):
        owner = (
            f"company={configuration.company_profile_id}"
            if configuration.company_profile_id
            else f"user={configuration.user_id}"
        )
        action = "Repairing" if apply_repairs else "Would repair"
        details = []
        if missing:
            details.append(f"restore={missing}")
        if hidden:
            details.append(f"re-enable={hidden}")
        if replacements:
            details.append(f"replace={replacements}")
        return (
            f"{action} config={configuration.id} ({owner}, "
            f"solution={configuration.solution}): {'; '.join(details)}"
        )
