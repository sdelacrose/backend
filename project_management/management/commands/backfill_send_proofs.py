"""MAIL-1.4-C: prepare the (empty, PENDING) proofs of Microsoft 365 sends made before MAIL-1.4-A.

DRY RUN BY DEFAULT: without --apply nothing is written; the command only says what it WOULD create.
It reads LINKS rows only -- no Graph call, no download -- and never archives anything: a proof it
creates stays PENDING, and the automatic archiving ignores sends older than its activation date.
--limit is required. Sends whose Sent Items copy cannot be tied to them for certain are listed."""

from django.core.management.base import BaseCommand

from project_management.services.correspondence_send_proof import ensure_missing_send_proofs


class Command(BaseCommand):
    help = "Prepare the missing PENDING send proofs of older Microsoft 365 sends (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, required=True, help="Most sends to examine.")
        parser.add_argument("--mailbox", type=int, default=None, help="Only the sends of this mailbox id.")
        parser.add_argument("--apply", action="store_true", help="Really create the proofs (default: simulate).")

    def handle(self, *args, **options):
        if options["limit"] < 1:
            self.stderr.write("--limit must be at least 1.")
            return
        mailbox = None
        if options["mailbox"] is not None:
            from userapis.models import MailboxConnection

            mailbox = MailboxConnection.objects.filter(pk=options["mailbox"]).first()
            if mailbox is None:
                self.stderr.write("No such mailbox.")
                return
        report = ensure_missing_send_proofs(
            limit=options["limit"], mailbox=mailbox, dry_run=not options["apply"],
        )
        mode = "DRY RUN (nothing written)" if report.dry_run else "APPLIED"
        self.stdout.write(f"{mode}: examined {report.examined}, proofs {'to create' if report.dry_run else 'created'} {len(report.created)}")
        self.stdout.write(f"event ids: {report.created}")
        self.stdout.write(f"uncertain copy association (sent several times, fewer copies recorded): {report.uncertain_copy}")
        self.stdout.write(f"no Sent Items copy recorded: {report.no_recorded_copy}")
