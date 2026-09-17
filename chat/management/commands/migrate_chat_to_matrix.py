from django.core.management.base import BaseCommand

from chat.matrix_service import matrix_service
from chat.models import ChatRoom, Message


class Command(BaseCommand):
    help = "Backfill chat rooms and messages into Matrix mappings"

    def add_arguments(self, parser):
        parser.add_argument("--room-id", type=int, default=None)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--all-rooms", action="store_true")

    def handle(self, *args, **options):
        if not matrix_service.enabled:
            self.stdout.write(self.style.WARNING("MATRIX_ENABLED is false. Nothing to migrate."))
            return

        room_id = options.get("room_id")
        limit = options.get("limit")
        dry_run = options.get("dry_run")
        all_rooms = options.get("all_rooms")

        room_queryset = ChatRoom.objects.all().order_by("id")
        if room_id:
            room_queryset = room_queryset.filter(id=room_id)
        elif not all_rooms:
            room_queryset = room_queryset.filter(messages__isnull=False).distinct()
        if limit:
            room_queryset = room_queryset[:limit]

        self.stdout.write(
            f"Migrating {'all rooms' if all_rooms else 'rooms with messages'} | dry_run={dry_run}"
        )

        for room in room_queryset:
            self.stdout.write(f"Processing room {room.id} - {room.name}")
            if not dry_run:
                matrix_service.ensure_room(room)

            messages = Message.objects.filter(room=room).order_by("created_at", "id")
            for message in messages:
                if dry_run:
                    continue
                matrix_service.sync_message(message)

        self.stdout.write(self.style.SUCCESS("Matrix migration command completed."))
