import posixpath

from django.db.models import Q
from django.db import transaction
from django.http import FileResponse, Http404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.reverse import reverse

from project_management.filters import CorrespondenceFilter
from project_management.models import Correspondence, CorrespondenceAttachment
from project_management.permissions import CorrespondencePermission
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.services.correspondence_response import (
    recompute_parent_response_status_from_responses,
)
from project_management.services.correspondence_workflow import is_content_locked

# Same mapping style as DocumentViewSet.get_permissions: one CorrespondencePermission action
# per DRF action, never a broader/narrower check than the request actually needs.
_ACTION_TO_PERMISSION = {
    "list": "view",
    "retrieve": "view",
    "create": "create",
    "update": "update",
    "partial_update": "update",
    "destroy": "delete",
}


def correspondence_queryset_for_user(user):
    """The Correspondence rows `user` may see, by tenant scope -- shared with
    CorrespondenceViewSet.get_queryset() and the LOT 3D-2 unified Correspondances aggregation
    (services.correspondances_register), so both apply the exact same tenant scope."""
    queryset = Correspondence.objects.select_related(
        "project", "company_profile", "freelancer",
        "sender_company", "sender_contact", "recipient_company", "recipient_contact",
        "parent_correspondence", "created_by",
    )
    if user.is_freelancer:
        return queryset.filter(
            Q(freelancer=user) | Q(project__user=user)
        ).distinct()
    return queryset.filter(
        Q(company_profile=user.company) | Q(project__company_profile=user.company)
    ).distinct()


class CorrespondenceViewSet(viewsets.ModelViewSet):
    serializer_class = CorrespondenceSerializer
    filterset_class = CorrespondenceFilter
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    search_fields = ["reference", "subject"]

    def get_permissions(self):
        return [CorrespondencePermission(_ACTION_TO_PERMISSION.get(self.action, "view"))]

    def get_queryset(self):
        return correspondence_queryset_for_user(self.request.user)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def perform_destroy(self, instance):
        # LOT 3D-3C0C: deleting a response (child) must not leave its surviving parent's
        # response_status stale -- recomputed from whichever responses remain right after the
        # delete, in the same request/transaction. Deleting a PARENT is unaffected: its own
        # children already survive via parent_correspondence's on_delete=SET_NULL (model field),
        # and there is no parent-of-a-parent to recompute here.
        parent = instance.parent_correspondence
        with transaction.atomic():
            instance.delete()
            if parent is not None:
                recompute_parent_response_status_from_responses(parent)


def _user_reference(user):
    if user is None:
        return None
    return {"id": user.id, "name": getattr(user, "full_name", str(user))}


def serialize_correspondence_attachment(attachment, request, correspondence):
    download_url = reverse(
        "correspondence-attachment-download",
        kwargs={"correspondence_id": correspondence.id, "attachment_id": attachment.id},
        request=request,
    )
    return {
        "id": attachment.id,
        "file_name": attachment.file_name,
        "file_size": attachment.file_size,
        "uploaded_by": _user_reference(attachment.uploaded_by),
        "created_at": attachment.created_at,
        "download_url": download_url,
        "file_url": request.build_absolute_uri(attachment.file.url),
    }


class CorrespondenceAttachmentViewSet(viewsets.GenericViewSet):
    """Multiple files attached to a Correspondence -- evidence of the communication only:
    never a controlled Document, DocumentRevision, or Document-level DocumentAttachment
    (LOT 3B section 9). Authorization is done explicitly per request (same convention as
    DocumentAttachmentViewSet) rather than through DRF's object-permission plumbing, since the
    parent Correspondence must be resolved and scope-checked before anything else.
    """

    filter_backends = []
    parser_classes = [MultiPartParser, FormParser]

    def _correspondence(self, request, correspondence_id, action):
        correspondence = Correspondence.objects.filter(pk=correspondence_id).first()
        if correspondence is None:
            raise Http404
        if not CorrespondencePermission(action).has_object_permission(request, self, correspondence):
            raise PermissionDenied()
        return correspondence

    def _attachment(self, correspondence, attachment_id):
        attachment = CorrespondenceAttachment.objects.filter(
            correspondence=correspondence, pk=attachment_id
        ).first()
        if attachment is None:
            raise Http404
        return attachment

    def list(self, request, correspondence_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "view")
        attachments = CorrespondenceAttachment.objects.filter(
            correspondence=correspondence
        ).select_related("uploaded_by")
        results = [
            serialize_correspondence_attachment(a, request, correspondence) for a in attachments
        ]
        return Response({"count": len(results), "results": results})

    def create(self, request, correspondence_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "update")
        # LOT 3D-3D1 section 8/14: attachments are part of "what is being approved" -- locked
        # the same way the Correspondence's own content fields are, server-side.
        if is_content_locked(correspondence):
            return Response(
                {"file": "Attachments cannot be changed while an internal validation workflow is active or approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"file": "A file is required."}, status=status.HTTP_400_BAD_REQUEST
            )
        attachment = CorrespondenceAttachment(
            correspondence=correspondence,
            file_name=posixpath.basename(uploaded.name or "") or "attachment",
            file_size=uploaded.size,
            uploaded_by=request.user,
        )
        attachment.file = uploaded
        attachment.save()
        return Response(
            serialize_correspondence_attachment(attachment, request, correspondence),
            status=status.HTTP_201_CREATED,
        )

    def download(self, request, correspondence_id=None, attachment_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "view")
        attachment = self._attachment(correspondence, attachment_id)
        if not attachment.file:
            raise Http404
        return FileResponse(
            attachment.file.open("rb"), as_attachment=True, filename=attachment.file_name,
        )

    def destroy(self, request, correspondence_id=None, attachment_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "update")
        if is_content_locked(correspondence):
            return Response(
                {"file": "Attachments cannot be changed while an internal validation workflow is active or approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        attachment = self._attachment(correspondence, attachment_id)
        stored = attachment.file
        with transaction.atomic():
            attachment.delete()
            if stored:
                transaction.on_commit(lambda: stored.storage.delete(stored.name))
        return Response(status=status.HTTP_204_NO_CONTENT)
