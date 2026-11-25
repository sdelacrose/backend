"""LOT RH 10: idempotent batch entry point for monthly leave accrual.

No celery-beat/periodic-task scheduler exists anywhere in this backend
(checked: Celery is configured for on-demand @shared_task jobs only, no
CELERY_BEAT_SCHEDULE, no django-celery-beat). This command is written to be
safely re-run by whatever external scheduler ops attaches (cron, Windows
Task Scheduler, a CI/infra cron job) -- running it automatically is NOT
wired up by this lot; it must still be branched in.
"""
from django.core.management.base import BaseCommand

from human_resource_management.services.leave_accrual import run_monthly_accrual


class Command(BaseCommand):
    help = (
        "Credits monthly leave accrual for every eligible (employee, leave type) pair, "
        "catching up any month from each leave type's accrual_effective_date through the "
        "last fully-completed local month. Safe to re-run: already-credited months are "
        "detected and skipped, never double-credited."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", type=int, default=None,
            help="Restrict to one company_profile id. Default: every company.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be processed without writing any credit.",
        )

    def handle(self, *args, **options):
        report = run_monthly_accrual(
            company_profile_id=options["company"], dry_run=options["dry_run"],
        )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"DRY RUN -- {report.processed} (employee, leave type, month) slots would be evaluated."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Processed {report.processed} slots: {report.credited} newly credited, "
                f"{report.already_done} already done (idempotent skip)."
            ))

        if report.skipped:
            self.stdout.write(self.style.WARNING(f"{len(report.skipped)} skipped (no data/no presence):"))
            for employee_id, leave_type_id, month, reason in report.skipped[:50]:
                self.stdout.write(f"  employee={employee_id} leave_type={leave_type_id} month={month}: {reason}")
            if len(report.skipped) > 50:
                self.stdout.write(f"  ... and {len(report.skipped) - 50} more")

        if report.errors:
            self.stdout.write(self.style.ERROR(f"{len(report.errors)} error(s):"))
            for employee_id, leave_type_id, month, error in report.errors[:50]:
                self.stdout.write(f"  employee={employee_id} leave_type={leave_type_id} month={month}: {error}")
            if len(report.errors) > 50:
                self.stdout.write(f"  ... and {len(report.errors) - 50} more")
