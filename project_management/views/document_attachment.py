import posixpath

from django.db import transaction
from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.reverse import reverse

from project_management.models import DocumentAttachment
from project_management.serializers.document_register import user_reference
from project_management.services.document_register import (
    assert_can_update_document,
    get_register_document_or_404,
)


def serialize_attachment(attachment, request, document):
    download_url = reverse(
        "document-attachment-download",
        kwargs={"document_id": document.id, "attachment_id": attachment.id},
        request=request,
    )
    return {
        "id": attachment.id,
        "file_name": attachment.file_name,
        "file_size": attachment.file_size,
        "uploaded_by": user_reference(attachment.uploaded_by),
        "created_at": attachment.created_at,
        "download_url": download_url,
        # Stored file (same kind of URL as a version's file_url): used for the preview.
        "file_url": request.build_absolute_uri(attachment.file.url),
    }


class DocumentAttachmentViewSet(viewsets.GenericViewSet):
    """Supporting files of one Document (annexes, not versions).

    Access is exactly the Documents access: the Document is resolved from the accessible
    queryset first (404 otherwise) and an attachment only through its Document. Upload and
    delete need the permission to update the document. An attachment never changes the
    controlled file, its versions, revision labels or workflow: an active workflow does not
    block them.
    """

    filter_backends = []
    parser_classes = [MultiPartParser, FormParser]

    def _attachment(self, document, attachment_id):
        attachment = DocumentAttachment.objects.filter(
            document=document, pk=attachment_id
        ).first()
        if attachment is None:
            raise Http404
        return attachment

    def list(self, request, document_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        attachments = DocumentAttachment.objects.filter(
            document=document
        ).select_related("uploaded_by")
        results = [serialize_attachment(a, request, document) for a in attachments]
        return Response({"count": len(results), "results": results})

    def create(self, request, document_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_can_update_document(request.user, document)
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"file": "A file is required."}, status=status.HTTP_400_BAD_REQUEST
            )
        attachment = DocumentAttachment(
            document=document,
            file_name=posixpath.basename(uploaded.name or "") or "attachment",
            file_size=uploaded.size,
            uploaded_by=request.user,
        )
        attachment.file = uploaded
        attachment.save()
        return Response(
            serialize_attachment(attachment, request, document),
            status=status.HTTP_201_CREATED,
        )

    def download(self, request, document_id=None, attachment_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        attachment = self._attachment(document, attachment_id)
        if not attachment.file:
            raise Http404
        return FileResponse(
            attachment.file.open("rb"),
            as_attachment=True,
            filename=attachment.file_name,
        )

    def destroy(self, request, document_id=None, attachment_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_can_update_document(request.user, document)
        attachment = self._attachment(document, attachment_id)
        stored = attachment.file
        with transaction.atomic():
            attachment.delete()
            if stored:
                transaction.on_commit(lambda: stored.storage.delete(stored.name))
        return Response(status=status.HTTP_204_NO_CONTENT)
