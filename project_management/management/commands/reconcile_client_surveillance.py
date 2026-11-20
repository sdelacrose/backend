"""GED-WF.4.2: SIMULATION of the historical catch-up of client surveillances.

Lists every really successful OUT send (SEND_SUCCEEDED) with its FOR_REVIEW items, and says what the
idempotent opening would do: open a surveillance, add a resend event, nothing (already done) or skip
(anomaly). It never writes anything: no surveillance, no event, no due date. There is deliberately no
apply mode in this lot -- `--apply` is refused. Issued-only and FOR_INFORMATION transmittals never appear.

    manage.py reconcile_client_surveillance [--project ID]
"""
from django.core.management.base import BaseCommand, CommandError

from project_management.models import TransmittalEvent
from project_management.services.client_surveillance_opening import plan_openings


class Command(BaseCommand):
    help = "Simulate (read-only) the opening of client surveillances and the closure of client follow-ups for past successful OUT sends."

    def add_arguments(self, parser):
        parser.add_argument("--project", type=int, help="Limit to one project id.")
        parser.add_argument("--apply", action="store_true", help="Refused in this lot.")

    def handle(self, *args, **options):
        if options["apply"]:
            raise CommandError("--apply is not available in this lot: the historical catch-up is simulation only.")
        events = TransmittalEvent.objects.filter(
            event_type=TransmittalEvent.EventType.SEND_SUCCEEDED, transmittal__direction="OUT",
        ).select_related("transmittal__project", "transmittal__external_company").order_by("id")
        if options["project"]:
            events = events.filter(transmittal__project_id=options["project"])
        counts = {}
        for event in events:
            transmittal = event.transmittal
            for row in plan_openings(event):
                counts[row["action"]] = counts.get(row["action"], 0) + 1
                self.stdout.write(
                    f"{transmittal.transmittal_number or transmittal.pk} | revision {row['document_revision']} | "
                    f"client {transmittal.external_company.company_name if transmittal.external_company else '-'} | FOR_REVIEW | "
                    f"SEND_SUCCEEDED #{event.pk} {event.created_at.isoformat()} | "
                    f"surveillance {row.get('existing_surveillance') or 'absent'} | "
                    f"{row['action']} | due_at {row.get('due_at') or '-'} | anomalies {','.join(row['anomalies']) or '-'}"
                )
        # LOT 1: the follow-ups (revisions born from a client return) these sends would close -- read-only too
        from project_management.services.client_surveillance_followup import plan_followups
        follow = {}
        for event in events:
            for row in plan_followups(event):
                follow[row["action"]] = follow.get(row["action"], 0) + 1
                self.stdout.write(f"follow-up {row['followup']} | revision {row['document_revision']} | SEND_SUCCEEDED #{event.pk} | {row['action']} | {row['reason'] or '-'}")
        self.stdout.write(f"SIMULATION ONLY - nothing written. {counts or 'No FOR_REVIEW send found.'}")
        if follow:
            self.stdout.write(f"FOLLOW-UPS (simulation): {follow}")
