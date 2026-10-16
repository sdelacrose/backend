"""MAIL-1.3D-C2-B1: POST /api/user/mailbox-preview/ -- the preview of one selected Outlook message
(see userapis.mailbox_preview). Nothing is stored; every response, error included, is `no-store`.
"""

from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from userapis.mailbox_preview import preview_message
from userapis.mailbox_read import MailboxReadError
from userapis.mailbox_search import InvalidSearchReference
from userapis.views.mailbox_search import _error


class MailboxPreviewRequestSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=4096)
    project = serializers.IntegerField(min_value=1, required=False)


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


class MailboxPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MailboxPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            payload = preview_message(
                request.user, reference=data["reference"], project_id=data.get("project"),
            )
        except InvalidSearchReference:
            return _no_store(Response(
                {"detail": "This selection is no longer valid; search again.", "code": "invalid_reference"},
                status=400,
            ))
        except MailboxReadError as exc:
            if exc.code == "not_found":              # deleted, or no longer in the folder it was shown from
                return _no_store(Response(
                    {"detail": "This message no longer exists in that folder of the mailbox.", "code": "message_gone"},
                    status=404,
                ))
            return _no_store(_error(exc))
        return _no_store(Response(payload))
