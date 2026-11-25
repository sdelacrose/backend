from django.core.management.base import BaseCommand

from human_resource_management.models.reminder import LeaveBalanceReminderRule
from human_resource_management.services.leave_reminders import (
    LeaveReminderBlockedError,
    build_rule_preview,
    process_rule,
)


class Command(BaseCommand):
    help = (
        "AI-1685: processes every ACTIVE LeaveBalanceReminderRule, sending "
        "internal notifications/emails for every occurrence currently in "
        "its [deadline-anticipation, deadline] window with available "
        "balance, re-checking eligibility and the same anti-duplicate claim "
        "a manual trigger shares. Never acts on an inactive rule, never "
        "sends past a deadline. Use --dry-run to preview without sending."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Build the preview for every active rule without claiming/sending anything.",
        )
        parser.add_argument(
            "--rule", type=int, default=None,
            help="Process only this rule id (still requires it to be active, unless --dry-run).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        rule_id = options["rule"]
        rules = LeaveBalanceReminderRule.objects.all()
        if rule_id is not None:
            rules = rules.filter(pk=rule_id)
        elif not dry_run:
            rules = rules.filter(is_active=True)

        processed, blocked, skipped = 0, 0, 0
        for rule in rules:
            try:
                if dry_run:
                    entries = build_rule_preview(rule)
                    actionable = [e for e in entries if e["in_window"]]
                    self.stdout.write(
                        f"[DRY-RUN] Rule {rule.id} ({rule.name}): "
                        f"{len(actionable)}/{len(entries)} staff currently actionable."
                    )
                    processed += 1
                    continue
                entries = process_rule(rule, manual=False)
                sent = sum(1 for e in entries if e.get("deliveries"))
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Rule {rule.id} ({rule.name}): {sent}/{len(entries)} staff processed."
                    )
                )
                processed += 1
            except LeaveReminderBlockedError as exc:
                # AI-1685: blocked explicitly (e.g. no valid company
                # timezone) -- never a silent skip, never a fallback
                # timezone. Isolated per rule so one blocked/misconfigured
                # company never stops every other company's reminders.
                blocked += 1
                self.stderr.write(self.style.WARNING(f"Rule {rule.id} ({rule.name}) blocked: {exc}"))
            except Exception as exc:  # noqa: BLE001 - isolate one rule's failure from the rest
                skipped += 1
                self.stderr.write(self.style.ERROR(f"Rule {rule.id} ({rule.name}) failed: {exc}"))

        self.stdout.write(
            f"Done. processed={processed} blocked={blocked} failed={skipped} "
            f"(dry_run={dry_run})."
        )
