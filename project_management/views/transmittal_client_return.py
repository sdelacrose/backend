"""GED-WF.4.3.1: client returns received through an incoming Transmittal (lines, matching, files, qualification).

Rights (no new right): preparing/removing a line needs documents.transmittal_update in the project AND read access to
the document concerned; listing needs transmittal_view (lines of documents the user cannot read are not shown);
qualifying a received line needs documents.update (LOT 2) and access to the document; a kept file is
served only through this authenticated route (never a public link, never the storage path)."""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, Http404
from rest_framework import serializers, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from project_management.models import Transmittal, TransmittalClientReturnLine, TransmittalClientReturnLineFile
from project_management.services.client_surveillance_code import list_effective_codes
from project_management.services.client_surveillance_opening import can_process_client_return
from project_management.services.document_register import get_register_document_or_404, get_register_queryset
from project_management.services.client_surveillance_return import issue_requires_category
from project_management.services.transmittal_client_return import (
    add_client_return_line, find_client_return_matches, line_state, qualify_client_return_line, remove_client_return_line,
)
from project_management.views.transmittal import _to_drf_error, _transmittal_for_action


def _file_url(transmittal_id, line_id, file_id):
    return f"/api/project_management/transmittal/{transmittal_id}/client-return-lines/{line_id}/files/{file_id}"


def serialize_line(line, can_qualify):
    issue = line.client_issue
    revision = issue.document_revision
    document = revision.document
    record = line.client_return
    from project_management.services.client_distribution_history import _source_revision

    return {
        "id": line.pk, "state": line_state(line), "client_issue_id": issue.pk, "document_id": document.pk,
        "document_reference": document.full_document_number or document.identification_number or document.file_name,
        "document_file_name": document.file_name, "document_origin": document.document_origin,
        "source_revision": _source_revision(revision), "issue_client_revision": issue.client_revision_label,
        "category": line.category, "received_at": line.received_at, "source_channel": line.source_channel,
        "comment": line.comment, "client_reference": line.client_reference, "client_revision": line.client_revision,
        "send_event_id": line.send_event_id,
        "send_transmittal": ({"id": line.send_event.transmittal_id, "reference": line.send_event.transmittal.transmittal_number,
                              "sent_at": line.send_event.created_at} if line.send_event_id else None),
        "requires_category": issue_requires_category(issue), "no_send_proof": line.send_event_id is None, "no_send_justification": line.no_send_justification,
        "created_by": line.created_by.full_name if line.created_by_id else None, "created_at": line.created_at,
        "client_return": ({"id": record.pk, "category": record.category, "code": record.code_snapshot,
                           "label_en": record.label_en_snapshot, "label_fr": record.label_fr_snapshot,
                           "label_de": record.label_de_snapshot} if record else None),
        "can_qualify": bool(can_qualify and not record and line.transmittal.status == Transmittal.Status.ISSUED),
        "files": [{"id": f.pk, "file_name": f.file_name, "content_type": f.content_type, "file_size": f.file_size,
                   "kind": f.kind, "kept": f.blob_id is not None, "download_url": _file_url(line.transmittal_id, line.pk, f.pk)}
                  for f in line.files.all()],
    }


def _accessible(request, transmittal):
    return get_register_queryset(request, project_id=transmittal.project_id)


class TransmittalClientReturnLineViewSet(viewsets.GenericViewSet):
    filter_backends = []
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "view")
        lines = list(transmittal.client_return_lines.select_related(
            "client_issue__document_revision__document", "send_event__transmittal", "client_return", "created_by", "transmittal",
        ).prefetch_related("files"))
        visible_ids = set(_accessible(request, transmittal).filter(
            pk__in={l.client_issue.document_revision.document_id for l in lines}).values_list("pk", flat=True))
        shown = [l for l in lines if l.client_issue.document_revision.document_id in visible_ids]
        results = [serialize_line(l, can_process_client_return(request.user, l.client_issue.document_revision.document)) for l in shown]
        return Response({
            "results": results, "restricted": len(lines) - len(shown),
            "to_qualify": sum(1 for l in lines if line_state(l) == "TO_QUALIFY"),
            "codes": list_effective_codes(transmittal.project),
        })

    def create(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")

        class Input(serializers.Serializer):
            client_issue = serializers.IntegerField()
            category = serializers.ChoiceField(choices=TransmittalClientReturnLine.Category.choices, allow_null=True, allow_blank=True, required=False)
            received_at = serializers.DateTimeField()
            source_channel = serializers.ChoiceField(choices=TransmittalClientReturnLine.Channel.choices)
            comment = serializers.CharField(allow_blank=True, required=False, max_length=10000)
            client_reference = serializers.CharField(allow_blank=True, required=False, max_length=255)
            client_revision = serializers.CharField(allow_blank=True, required=False, max_length=100)
            send_event = serializers.IntegerField(required=False, allow_null=True)
            no_send_justification = serializers.CharField(allow_blank=True, required=False, max_length=2000)

        data = request.data
        payload = Input(data={k: (None if data.get(k) in ("", "null") and k == "send_event" else data.get(k))
                              for k in Input().fields if data.get(k) is not None})
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        accessible = _accessible(request, transmittal)
        try:
            line = add_client_return_line(
                transmittal, request.user, client_issue_id=values["client_issue"], category=values.get("category") or None,
                received_at=values["received_at"], source_channel=values["source_channel"], comment=values.get("comment", ""),
                client_reference=values.get("client_reference", ""), client_revision=values.get("client_revision", ""),
                send_event_id=values.get("send_event"), no_send_justification=values.get("no_send_justification", ""),
                uploads=request.FILES.getlist("files") if hasattr(request.FILES, "getlist") else [],
                kinds=data.getlist("file_kinds") if hasattr(data, "getlist") else [],
                can_view_document=lambda document: accessible.filter(pk=document.pk).exists(),
            )
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        except DjangoPermissionDenied as error:
            raise PermissionDenied(str(error))
        line = TransmittalClientReturnLine.objects.select_related(
            "client_issue__document_revision__document", "send_event__transmittal", "client_return", "created_by", "transmittal",
        ).prefetch_related("files").get(pk=line.pk)
        return Response(serialize_line(line, False), status=status.HTTP_201_CREATED)

    def destroy(self, request, transmittal_id=None, line_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        line = transmittal.client_return_lines.filter(pk=line_id).first()
        if line is None:
            raise Http404
        try:
            remove_client_return_line(line)
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TransmittalClientReturnMatchView(viewsets.GenericViewSet):
    """Candidates (never a choice) for a received return: ClientIssues of this project and client company."""

    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        if transmittal.direction != Transmittal.Direction.IN or not transmittal.external_company_id:
            raise ValidationError({"detail": "An incoming transmittal with its issuing company is required."})
        params = request.query_params
        results = find_client_return_matches(
            transmittal, _accessible(request, transmittal), params.get("reference", ""), params.get("revision", ""),
            params.get("out_reference", ""))
        return Response({"results": results, "codes": list_effective_codes(transmittal.project)})


class TransmittalClientReturnQualifyView(viewsets.GenericViewSet):
    """Later qualification of a received line: documents.update (LOT 2) + access to the document."""

    def create(self, request, transmittal_id=None, line_id=None, *args, **kwargs):
        line = TransmittalClientReturnLine.objects.select_related(
            "client_issue__document_revision__document", "transmittal").filter(pk=line_id, transmittal_id=transmittal_id).first()
        if line is None:
            raise Http404
        document = get_register_document_or_404(request, document_id=line.client_issue.document_revision.document_id)
        if not can_process_client_return(request.user, document):
            raise PermissionDenied("You do not have permission to record a client return.")
        category = request.data.get("category") or None
        comment = request.data.get("comment")
        try:
            record = qualify_client_return_line(line.pk, request.user, category=category, comment=comment)
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        line = TransmittalClientReturnLine.objects.select_related(
            "client_issue__document_revision__document", "send_event__transmittal", "client_return", "created_by", "transmittal",
        ).prefetch_related("files").get(pk=line.pk)
        from project_management.views.client_surveillance import serialize_retention
        return Response({"line": serialize_line(line, True), "client_return_id": record.pk, "retention": serialize_retention(record)}, status=status.HTTP_201_CREATED)


class TransmittalClientReturnFileView(viewsets.GenericViewSet):
    """Authenticated, forced download of a received file (pending or kept): transmittal_view + access to the document."""

    def retrieve(self, request, transmittal_id=None, line_id=None, file_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "view")
        row = TransmittalClientReturnLineFile.objects.select_related("blob", "line__client_issue__document_revision").filter(
            pk=file_id, line_id=line_id, line__transmittal=transmittal).first()
        if row is None:
            raise Http404
        document_id = row.line.client_issue.document_revision.document_id
        if not _accessible(request, transmittal).filter(pk=document_id).exists():
            raise Http404
        stored = row.blob.file if row.blob_id else row.file
        if not stored or not stored.storage.exists(stored.name):
            raise Http404
        response = FileResponse(stored.open("rb"), as_attachment=True, filename=row.file_name,
                                content_type=row.content_type or "application/octet-stream")
        response["X-Content-Type-Options"] = "nosniff"
        return response
