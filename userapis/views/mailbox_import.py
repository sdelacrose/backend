"""MAIL-1.3D-C2-A: POST /api/user/mailbox-import/ -- the user's explicit import of a message they
found with the mailbox search (see userapis.mailbox_import for every rule).

The body carries the signed reference, never a bare Graph id; the reference is verified and every
right is checked again server-side before anything is read or written.
"""

from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from userapis.mailbox_import import MODE_ATTACH, MODE_COMPLETE, MODE_CREATE, MailboxImportError, import_message
from userapis.mailbox_preview import MAX_FILES
from userapis.mailbox_read import MailboxReadError
from userapis.views.mailbox_search import _error as _read_error

CREATE_ONLY_FIELDS = (
    "external_reference", "correspondence_type", "sender_company", "sender_contact",
    "parent_correspondence", "response_required",
)


class MailboxImportRequestSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=4096)
    project = serializers.IntegerField(min_value=1)
    mode = serializers.ChoiceField(choices=(MODE_CREATE, MODE_ATTACH, MODE_COMPLETE))
    # The signed references of the attachments the user chose in the preview; nothing else is imported.
    attachments = serializers.ListField(
        child=serializers.CharField(max_length=4096), required=False, max_length=MAX_FILES,
    )
    target_correspondence = serializers.IntegerField(min_value=1, required=False)
    external_reference = serializers.CharField(max_length=255, required=False, allow_blank=True, trim_whitespace=True)
    correspondence_type = serializers.CharField(max_length=255, required=False, allow_blank=True)
    sender_company = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    sender_contact = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    parent_correspondence = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    response_required = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if attrs["mode"] == MODE_CREATE:
            # external_reference is optional: without it the IN receives a LINKS reference
            if attrs.get("target_correspondence"):
                raise serializers.ValidationError({"target_correspondence": "Only used when attaching."})
        elif attrs["mode"] == MODE_COMPLETE:
            # the correspondence is the one this message was imported into: never chosen by the client
            extra = [name for name in ("target_correspondence",) + CREATE_ONLY_FIELDS if attrs.get(name) not in (None, "")]
            if extra:
                raise serializers.ValidationError({name: "Not used when completing an import." for name in extra})
        else:
            if not attrs.get("target_correspondence"):
                raise serializers.ValidationError({"target_correspondence": "The correspondence to attach to is required."})
            extra = [name for name in CREATE_ONLY_FIELDS if attrs.get(name) not in (None, "")]
            if extra:
                raise serializers.ValidationError({name: "Only used when creating." for name in extra})
        return attrs


class MailboxImportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MailboxImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = import_message(
                request, reference=data["reference"], project_id=data["project"], mode=data["mode"],
                target_id=data.get("target_correspondence"),
                fields={name: data.get(name) for name in CREATE_ONLY_FIELDS},
                attachment_references=data.get("attachments") or [],
            )
        except MailboxImportError as exc:
            return Response({"detail": exc.detail, "code": exc.code, **exc.extra}, status=exc.status)
        except MailboxReadError as exc:
            return _read_error(exc)
        corr, message = result.correspondence, result.message
        return Response(
            {
                "result": result.outcome,
                "correspondence": {
                    "id": corr.pk, "direction": corr.direction, "reference": corr.display_reference,
                    "project": corr.project_id, "status": corr.status,
                },
                "message": {"id": message.pk, "state": message.state, "folder": message.folder},
                "documents": result.documents,
            },
            status=201 if result.outcome in ("created", "attached") else 200,
        )
