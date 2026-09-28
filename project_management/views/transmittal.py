from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from rest_framework.response import Response

from project_management.filters import TransmittalFilter
from project_management.models import DocumentRevision, Transmittal, TransmittalEvent, TransmittalItem
from project_management.permissions import TransmittalPermission
from project_management.serializers.transmittal import TransmittalItemSerializer, TransmittalSerializer
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item, remove_transmittal_item

# Same mapping style as CorrespondenceViewSet.get_permissions: one TransmittalPermission action
# per DRF action, never a broader/narrower check than the request actually needs.
_ACTION_TO_PERMISSION = {
    "list": "view",
    "retrieve": "view",
    "create": "create",
    "update": "update",
    "partial_update": "update",
    "destroy": "delete",
}


def _to_drf_error(error):
    detail = error.message_dict if hasattr(error, "message_dict") else error.messages
    return DRFValidationError(detail)


class TransmittalViewSet(viewsets.ModelViewSet):
    serializer_class = TransmittalSerializer
    filterset_class = TransmittalFilter
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    search_fields = ["transmittal_number", "subject"]

    def get_permissions(self):
        return [TransmittalPermission(_ACTION_TO_PERMISSION.get(self.action, "view"))]

    def get_queryset(self):
        user = self.request.user
        queryset = Transmittal.objects.select_related(
            "project", "external_company", "created_by",
        ).prefetch_related("items", "external_contacts")
        if user.is_freelancer:
            return queryset.filter(project__user=user).distinct()
        return queryset.filter(project__company_profile=user.company).distinct()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except DjangoValidationError as error:
            raise _to_drf_error(error)


def _transmittal_for_action(request, transmittal_id, action):
    transmittal = Transmittal.objects.filter(pk=transmittal_id).first()
    if transmittal is None:
        raise Http404
    if not TransmittalPermission(action).has_object_permission(request, None, transmittal):
        raise PermissionDenied()
    return transmittal


class TransmittalItemViewSet(viewsets.GenericViewSet):
    """Add/remove items -- explicit per-request authorization (same convention as
    CorrespondenceAttachmentViewSet), since the parent Transmittal must be resolved and
    scope-checked before anything else."""

    serializer_class = TransmittalItemSerializer
    filter_backends = []

    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "view")
        items = transmittal.items.select_related("document_revision")
        return Response(TransmittalItemSerializer(items, many=True).data)

    def create(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        revision_id = request.data.get("document_revision")
        # Requiredness is direction-dependent (required for OUT, nullable/not-applicable for
        # IN) and enforced by services.transmittal_items; here only reject an outright invalid
        # non-empty choice.
        distribution_purpose = request.data.get("distribution_purpose") or None
        if not revision_id:
            return Response(
                {"document_revision": "This field is required."}, status=status.HTTP_400_BAD_REQUEST,
            )
        if distribution_purpose and distribution_purpose not in TransmittalItem.DistributionPurpose.values:
            return Response(
                {"distribution_purpose": "Invalid distribution purpose."}, status=status.HTTP_400_BAD_REQUEST,
            )
        revision = DocumentRevision.objects.filter(pk=revision_id).select_related("document").first()
        if revision is None:
            return Response({"document_revision": "Not found."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            item = add_transmittal_item(transmittal, revision, distribution_purpose, request.user)
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        return Response(TransmittalItemSerializer(item).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, transmittal_id=None, item_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        item = TransmittalItem.objects.filter(transmittal=transmittal, pk=item_id).first()
        if item is None:
            raise Http404
        try:
            remove_transmittal_item(item, request.user)
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TransmittalIssueView(viewsets.GenericViewSet):
    def create(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "issue")
        try:
            finalized = finalize_transmittal(transmittal, request.user)
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        return Response(TransmittalSerializer(finalized).data)


class TransmittalEventViewSet(viewsets.GenericViewSet):
    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "view")
        events = transmittal.events.select_related("actor")
        return Response([
            {
                "id": event.pk,
                "event_type": event.event_type,
                "actor": {"id": event.actor_id, "name": event.actor.full_name} if event.actor_id else None,
                "metadata": event.metadata,
                "created_at": event.created_at,
            }
            for event in events
        ])
