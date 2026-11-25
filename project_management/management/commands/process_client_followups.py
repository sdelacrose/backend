"""LOT 1: recovery of the automated client follow-ups (idempotent, never touches historical surveillances).

Replays, for every really successful OUT send that carries a revision born from a client return, the closure the
verified send should have produced (`process_send_event`), and (only for the return ids named with --returns) re-opens the
follow-up of a return whose automatic opening failed. Dry run by default: `--apply` writes. Surveillances without a follow-up are never touched.

    manage.py process_client_followups [--project ID] [--returns ID ...] [--apply]
"""
from django.core.management.base import BaseCommand

from project_management.models import ClientSurveillanceFollowUp, ClientSurveillanceReturn, TransmittalEvent
from project_management.services.client_surveillance_followup import (
    STATE_OF_CATEGORY, ensure_followup, plan_followups, process_send_event,
)


class Command(BaseCommand):
    help = "Replay the closure of client follow-ups from verified sends (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--project", type=int)
        parser.add_argument("--returns", type=int, nargs="*", help="Return ids whose follow-up must be (re)opened.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        apply = options["apply"]
        events = TransmittalEvent.objects.filter(
            event_type=TransmittalEvent.EventType.SEND_SUCCEEDED, transmittal__direction="OUT",
            transmittal__items__document_revision__client_followup__closed_at__isnull=True,
        ).select_related("transmittal").distinct().order_by("id")
        if options["project"]:
            events = events.filter(transmittal__project_id=options["project"])
        counts = {}
        for event in events:
            for row in plan_followups(event):
                counts[row["action"]] = counts.get(row["action"], 0) + 1
                self.stdout.write(f"follow-up {row['followup']} | SEND_SUCCEEDED #{event.pk} | {row['action']} | {row['reason'] or '-'}")
            if apply:
                process_send_event(event)
        # Historical returns (recorded before this lot) are never opened in bulk: only the returns named explicitly.
        for pk in options["returns"] or []:
            record = ClientSurveillanceReturn.objects.filter(pk=pk, followup__isnull=True, corrections__isnull=True).first()
            if record is None:
                self.stdout.write(f"return {pk} | skipped (unknown, replaced or already followed)")
                continue
            counts["OPEN_FOLLOWUP"] = counts.get("OPEN_FOLLOWUP", 0) + 1
            self.stdout.write(f"return {record.pk} | no follow-up | OPEN_FOLLOWUP")
            if apply:
                ensure_followup(record, None)
        self.stdout.write(("APPLIED. " if apply else "DRY RUN - nothing written. ") + str(counts or "Nothing to do."))
