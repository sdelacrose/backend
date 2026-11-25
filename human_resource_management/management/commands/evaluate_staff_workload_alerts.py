from django.core.management.base import BaseCommand

from human_resource_management.models.workload import StaffWorkloadAlertConfig
from human_resource_management.services.workload import compute_staff_workload, current_month_bounds
from human_resource_management.services.workload_alerts import (
    WorkloadAlertBlockedError,
    evaluate_config,
    staff_queryset_for_config,
)


class Command(BaseCommand):
    help = (
        "AI-2054: evaluates every ACTIVE StaffWorkloadAlertConfig for the "
        "current calendar month (in each company's own configured IANA "
        "timezone), opening/persisting/closing overload episodes and "
        "delivering to every currently-authorized recipient -- the same "
        "evaluation and anti-duplicate claim the scheduled task and the "
        "manual recompute action share. Use --dry-run to compute rates "
        "without opening/closing any episode or sending anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Compute the current rate for every in-scope staff member without touching episodes/deliveries.",
        )
        parser.add_argument(
            "--company-profile", type=int, default=None,
            help="Restrict to configs for this company_profile id.",
        )
        parser.add_argument(
            "--solution", type=str, default=None,
            help="Restrict to configs for this solution.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        configs = StaffWorkloadAlertConfig.objects.filter(is_active=True).select_related("company_config")
        if options["company_profile"] is not None:
            configs = configs.filter(company_config__company_profile_id=options["company_profile"])
        if options["solution"]:
            configs = configs.filter(company_config__solution=options["solution"])

        processed, blocked, errors = 0, 0, 0
        for config in configs:
            if dry_run:
                period_start, period_end = current_month_bounds()
                overloaded_count, total = 0, 0
                for staff in staff_queryset_for_config(config).select_related("user"):
                    result = compute_staff_workload(
                        staff, period_start, period_end, solution=config.company_config.solution,
                    )
                    total += 1
                    if result.overloaded:
                        overloaded_count += 1
                self.stdout.write(
                    f"[DRY-RUN] Config {config.id} ({config.company_config_id}): "
                    f"{overloaded_count}/{total} staff currently above 100% for {period_start:%Y-%m}."
                )
                processed += 1
                continue
            try:
                entries = evaluate_config(config)
                opened = sum(1 for e in entries if e.get("outcome") == "episode_opened")
                persisted = sum(1 for e in entries if e.get("outcome") == "episode_persisted")
                closed = sum(1 for e in entries if e.get("outcome") == "episode_closed")
                self.stdout.write(self.style.SUCCESS(
                    f"Config {config.id}: {opened} opened, {persisted} persisted, {closed} closed "
                    f"({len(entries)} staff evaluated)."
                ))
                processed += 1
            except WorkloadAlertBlockedError as exc:
                blocked += 1
                self.stderr.write(self.style.WARNING(f"Config {config.id} blocked: {exc}"))
            except Exception as exc:  # noqa: BLE001 - isolate one config's failure from the rest
                errors += 1
                self.stderr.write(self.style.ERROR(f"Config {config.id} failed: {exc}"))

        self.stdout.write(
            f"Done. processed={processed} blocked={blocked} failed={errors} (dry_run={dry_run})."
        )
