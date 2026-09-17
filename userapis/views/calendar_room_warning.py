from rest_framework import status
from rest_framework.response import Response


class CalendarEventRoomWarningMixin:
    def _room_warning_response(self, serializer):
        if not serializer.room_warning:
            return None

        return Response(
            {"room_warning": [serializer.room_warning]},
            status=status.HTTP_200_OK,
        )

    def _create_calendar_event_response(self, serializer):
        room_warning_response = self._room_warning_response(serializer)
        if room_warning_response is not None:
            return room_warning_response

        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def _update_calendar_event_response(self, serializer):
        room_warning_response = self._room_warning_response(serializer)
        if room_warning_response is not None:
            return room_warning_response

        event = serializer.save()
        return Response(self.get_serializer(event).data)
