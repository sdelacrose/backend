from django.core.management.base import BaseCommand
from django.db.models import Count

from chat.models import ChatRoom, MatrixMessageMap, MatrixRoomMap, Message


class Command(BaseCommand):
    help = "Verify Matrix migration completeness (mappings coverage)"

    def add_arguments(self, parser):
        parser.add_argument("--room-id", type=int, default=None)
        parser.add_argument("--limit-rooms", type=int, default=20)
        parser.add_argument("--show-missing-message-ids", action="store_true")

    def handle(self, *args, **options):
        room_id = options.get("room_id")
        limit_rooms = int(options.get("limit_rooms") or 20)
        show_missing_message_ids = bool(options.get("show_missing_message_ids"))

        rooms_with_messages = ChatRoom.objects.annotate(msg_count=Count("messages")).filter(msg_count__gt=0)
        if room_id:
            rooms_with_messages = rooms_with_messages.filter(id=room_id)

        total_rooms_with_messages = rooms_with_messages.count()
        total_room_maps = MatrixRoomMap.objects.filter(chat_room__in=rooms_with_messages).count()
        rooms_missing_map = rooms_with_messages.filter(matrix_room_map__isnull=True)
        rooms_missing_map_count = rooms_missing_map.count()

        total_messages = Message.objects.filter(room__in=rooms_with_messages).count()
        total_message_maps = MatrixMessageMap.objects.filter(message__room__in=rooms_with_messages).count()
        missing_messages_qs = Message.objects.filter(room__in=rooms_with_messages, matrix_message_map__isnull=True)
        missing_messages_count = missing_messages_qs.count()

        self.stdout.write("Matrix migration verification")
        if room_id:
            self.stdout.write(f"- scope: room_id={room_id}")
        else:
            self.stdout.write("- scope: all rooms with messages")

        self.stdout.write(f"- rooms_with_messages: {total_rooms_with_messages}")
        self.stdout.write(f"- matrix_room_maps_for_scope: {total_room_maps}")
        self.stdout.write(f"- rooms_missing_matrix_room_map: {rooms_missing_map_count}")
        self.stdout.write(f"- messages_in_scope: {total_messages}")
        self.stdout.write(f"- matrix_message_maps_for_scope: {total_message_maps}")
        self.stdout.write(f"- messages_missing_matrix_message_map: {missing_messages_count}")

        if rooms_missing_map_count:
            self.stdout.write("")
            self.stdout.write("Rooms missing MatrixRoomMap (sample):")
            for room in rooms_missing_map.order_by("id")[:limit_rooms]:
                self.stdout.write(f"- room_id={room.id} name={room.name!r}")

        if missing_messages_count:
            self.stdout.write("")
            self.stdout.write("Rooms with missing MatrixMessageMap (top):")
            missing_by_room = (
                missing_messages_qs.values("room_id")
                .annotate(missing_count=Count("id"))
                .order_by("-missing_count")[:limit_rooms]
            )
            room_names = {
                room.id: room.name
                for room in ChatRoom.objects.filter(id__in=[row["room_id"] for row in missing_by_room])
            }
            for row in missing_by_room:
                rid = row["room_id"]
                self.stdout.write(
                    f"- room_id={rid} missing={row['missing_count']} name={room_names.get(rid, '')!r}"
                )

            if show_missing_message_ids:
                self.stdout.write("")
                self.stdout.write("Missing message ids (sample):")
                for mid in missing_messages_qs.values_list("id", flat=True).order_by("id")[:100]:
                    self.stdout.write(f"- message_id={mid}")
