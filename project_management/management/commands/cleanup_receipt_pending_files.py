"""GED-WF.3.1: manual (never scheduled) cleanup of abandoned pending-receipt files.

A pending file normally disappears with its line (draft deleted, line removed, receipt confirmed). If that
deletion failed (it is logged), or a line was removed at database level, the file stays under the pending
prefix. This command lists the files under that prefix that no line references any more and, only with
--apply, deletes those older than --older-than-days (default 7). Files referenced by a line are never
touched, and files outside the prefix (controlled documents) are never listed.
"""

from datetime import timedelta

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.utils import timezone

from project_management.models import TransmittalClientReturnLineFile, TransmittalReceiptLine
from project_management.models.transmittal import PENDING_RECEIPT_PREFIX
from project_management.models.transmittal_client_return_line import PENDING_CLIENT_RETURN_PREFIX


def _walk(storage, directory):
    try:
        directories, files = storage.listdir(directory)
    except (FileNotFoundError, NotImplementedError):
        return
    for name in files:
        yield f"{directory}/{name}"
    for sub in directories:
        yield from _walk(storage, f"{directory}/{sub}")


class Command(BaseCommand):
    help = "List (default) or delete (--apply) unreferenced, old files under the pending-receipt prefix."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Really delete; without it, only list.")
        parser.add_argument("--older-than-days", type=int, default=7)

    def handle(self, *args, **options):
        # GED-WF.4.3.1: the supplier lines AND the client return files (dedicated prefix) still referenced by a draft
        # are never candidates, however old they are.
        referenced = set(TransmittalReceiptLine.objects.values_list("file", flat=True))
        referenced |= set(TransmittalClientReturnLineFile.objects.exclude(file__isnull=True).values_list("file", flat=True))
        limit = timezone.now() - timedelta(days=options["older_than_days"])
        candidates = deleted = 0
        names = [n for prefix in (PENDING_RECEIPT_PREFIX, PENDING_CLIENT_RETURN_PREFIX) for n in _walk(default_storage, prefix)]
        for name in names:
            if name in referenced:
                continue
            try:
                modified = default_storage.get_modified_time(name)
            except Exception:
                modified = None
            if modified is not None and modified > limit:
                continue
            candidates += 1
            self.stdout.write(("DELETE " if options["apply"] else "WOULD DELETE ") + name)
            if options["apply"]:
                try:
                    default_storage.delete(name)
                    deleted += 1
                except Exception as error:  # traced, the remaining files are still processed
                    self.stderr.write(f"FAILED {name}: {error}")
        self.stdout.write(f"{candidates} unreferenced file(s), {deleted} deleted.")
