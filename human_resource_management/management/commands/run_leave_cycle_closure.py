"""LOT RH11: idempotent batch entry point for capped carry-over +
mandatory expiration cycle closure -- same reuse conventions as
`run_monthly_leave_accrual.py` (its own, separate Postgres advisory lock;
no local/production scheduler wiring beyond what that lot already
documented as deferred to ops).
"""
from django.core.management.base import BaseCommand

from human_resource_management.services.leave_carryover import run_cycle_closure


class Command(BaseCommand):
    help = (
        "Closes every eligible, not-yet-closed entitlement whose cycle has "
        "genuinely ended in its company's own timezone: finalizes due "
        "carry-overs, computes the capped carry-over (if any) into the "
        "following cycle, and records the closure. Safe to re-run: an "
        "already-closed entitlement is simply skipped, never reprocessed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", type=int, default=None,
            help="Restrict to one company_profile id. Default: every company.",
        )

    def handle(self, *args, **options):
        report = run_cycle_closure(company_profile_id=options["company"])
        self.stdout.write(self.style.SUCCESS(
            f"Closed {report.closed} entitlement(s)."
        ))
        if report.errors:
            self.stdout.write(self.style.ERROR(f"{len(report.errors)} error(s):"))
            for entitlement_id, message in report.errors[:50]:
                self.stdout.write(f"  entitlement={entitlement_id}: {message}")
