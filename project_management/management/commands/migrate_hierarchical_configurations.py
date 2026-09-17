from django.core.management.base import BaseCommand, CommandError

from project_management.services.hierarchical_configuration_migration import (
    HierarchicalConfigurationMigrator,
)


class Command(BaseCommand):
    help = "Install hierarchical configurations and backfill document disciplines."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Inventory and report the migration without writing data.",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Apply configuration and document updates.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Number of documents processed per database batch.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size must be greater than zero.")

        migrator = HierarchicalConfigurationMigrator(
            apply=options["apply"],
            batch_size=batch_size,
        )
        inventory = migrator.inventory()
        mode = "APPLY" if options["apply"] else "DRY RUN"
        self.stdout.write(self.style.MIGRATE_HEADING(f"Mode: {mode}"))
        self.stdout.write(
            f"Configured discipline values: {len(inventory['configured_values'])}"
        )
        self.stdout.write(
            f"Document-used discipline values: {len(inventory['document_values'])}"
        )
        if inventory["document_values"]:
            self.stdout.write(
                "Document values: " + ", ".join(inventory["document_values"])
            )

        summary = migrator.run()
        for key, value in summary.items():
            self.stdout.write(f"{key.replace('_', ' ').title()}: {value}")

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run complete. Re-run with --apply after reviewing the output."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("Hierarchy migration complete."))
