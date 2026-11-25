from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from project_management.filters import TransmittalFilter
from project_management.models import DocumentRevision, Transmittal, TransmittalEvent, TransmittalItem
from project_management.permissions import TransmittalPermission
from project_management.serializers.transmittal import (
    TransmittalItemSerializer, TransmittalReceiptLineSerializer, TransmittalSerializer,
)
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_receipt import (
    ReceiptAnomalies, add_receipt_line, confirm_incoming_receipt, find_reception_matches, receipt_anomalies,
)
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


def transmittal_queryset_for_user(user):
    """The Transmittal rows `user` may see, by tenant scope (always via its project, LOT 3C
    amendment) -- shared with TransmittalViewSet.get_queryset() and the LOT 3D-2 unified
    Correspondances aggregation (services.correspondances_register)."""
    queryset = Transmittal.objects.select_related(
        "project", "external_company", "created_by",
    ).prefetch_related(
        # LOT 3E-1B: the nested `items` field's new document_revision_detail (serializers.
        # transmittal) needs the document too, not just the revision -- avoids an N+1 query per
        # item on GET /transmittal/<id>.
        "items__document_revision__document", "external_contacts",
    )
    if user.is_freelancer:
        return queryset.filter(project__user=user).distinct()
    return queryset.filter(project__company_profile=user.company).distinct()


class TransmittalViewSet(viewsets.ModelViewSet):
    serializer_class = TransmittalSerializer
    filterset_class = TransmittalFilter
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    search_fields = ["transmittal_number", "subject"]

    def get_permissions(self):
        return [TransmittalPermission(_ACTION_TO_PERMISSION.get(self.action, "view"))]

    def get_queryset(self):
        return transmittal_queryset_for_user(self.request.user)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def perform_destroy(self, instance):
        try:
            if instance.direction == Transmittal.Direction.IN and instance.status == Transmittal.Status.DRAFT:
                from project_management.services.transmittal_client_return import delete_draft_client_lines

                delete_draft_client_lines(instance)  # GED-WF.4.3.1: their relation is PROTECT, removed with the draft
            instance.delete()
        except DjangoValidationError as error:
            raise _to_drf_error(error)


def _project_is_visible_to(user, project):
    """GED-SEC.6: is the Transmittal's project part of the caller's own scope (same tenant -- same rule as
    transmittal_queryset_for_user -- AND the caller is its owner, a team member or the documentary owner)?
    Only used to choose between 404 (outside the scope: existence is not revealed) and 403 (inside the scope,
    permission missing). It never grants anything."""
    if user.is_freelancer:
        in_tenant = project.user_id == user.pk
    else:
        in_tenant = project.company_profile_id == user.company_id
    if not in_tenant:
        return False
    if project.user_id == user.pk or project.team_members.filter(team_member=user).exists():
        return True
    from ecpmpapp.utils import require_any_permission_multiple_modules
    from project_management.services.company_owner_document_access import documentary_owner_company_id

    if documentary_owner_company_id(user) == project.company_profile_id:
        return True
    # company-wide project access (same rule as get_accessible_document_queryset)
    return bool(require_any_permission_multiple_modules(
        user, {"projects": ["full_access", "view_all_access"]}, "company", {"project": project.pk}))


def _transmittal_for_action(request, transmittal_id, action):
    transmittal = Transmittal.objects.select_related("project").filter(pk=transmittal_id).first()
    if transmittal is None:
        raise Http404
    if not TransmittalPermission(action).has_object_permission(request, None, transmittal):
        if not _project_is_visible_to(request.user, transmittal.project):
            raise Http404
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
        # LOT 3E-1B: document_revision__document too, for the same reason as
        # transmittal_queryset_for_user's own prefetch above.
        items = transmittal.items.select_related("document_revision__document")
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
        if transmittal.direction == Transmittal.Direction.IN:
            # GED-WF.3: receiving is not issuing -- an incoming transmittal is confirmed through
            # its own action, which needs transmittal_update (transmittal_issue never confirms it).
            raise DRFValidationError(
                {"detail": "An incoming transmittal is confirmed with the receipt confirmation, not issued."}
            )
        try:
            finalized = finalize_transmittal(transmittal, request.user)
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        return Response(TransmittalSerializer(finalized).data)


class TransmittalReceiptCheckView(viewsets.GenericViewSet):
    """GED-WF.3: what would block the confirmation of an incoming draft (read-only)."""

    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        anomalies = receipt_anomalies(transmittal, request.user)
        return Response({"anomalies": anomalies, "can_confirm": not anomalies})


class TransmittalConfirmReceiptView(viewsets.GenericViewSet):
    """GED-WF.3: manual confirmation of an incoming transmittal -- documents.transmittal_update
    in the transmittal's own project scope (never transmittal_issue)."""

    def create(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        try:
            received = confirm_incoming_receipt(transmittal, request.user)
        except ReceiptAnomalies as error:
            return Response(
                {"detail": "The receipt cannot be confirmed.", "anomalies": error.anomalies},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        return Response(TransmittalSerializer(received).data)


class TransmittalReceiptLineViewSet(viewsets.GenericViewSet):
    """GED-WF.3.1: received documents PREPARED on an incoming draft (nothing canonical is created
    here). Listing needs transmittal_view, preparing/removing needs transmittal_update, both in the
    transmittal's own project -- a line id alone never gives access, it is always looked up through
    its transmittal."""

    filter_backends = []
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "view")
        lines = transmittal.receipt_lines.select_related("matched_document", "created_by")
        return Response(TransmittalReceiptLineSerializer(lines, many=True).data)

    def create(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        data = request.data
        try:
            line = add_receipt_line(
                transmittal, request.user,
                reference=data.get("reference"),
                uploaded_file=request.FILES.get("file"),
                external_revision=data.get("external_revision", ""),
                matched_document_id=data.get("matched_document") or None,
                allow_duplicate_reference=str(data.get("allow_duplicate_reference", "")).lower() in ("1", "true", "yes"),
            )
        except DjangoValidationError as error:
            if getattr(error, "code", None) == "duplicate_supplier_reference":
                raise DRFValidationError({"detail": error.messages[0], "code": "duplicate_supplier_reference"})
            raise _to_drf_error(error)
        return Response(TransmittalReceiptLineSerializer(line).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, transmittal_id=None, line_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        if transmittal.direction != Transmittal.Direction.IN or transmittal.status != Transmittal.Status.DRAFT:
            raise DRFValidationError({"detail": "Only a line of an incoming draft can be removed."})
        line = transmittal.receipt_lines.filter(pk=line_id).first()
        if line is None:
            raise Http404
        line.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TransmittalDocumentMatchView(viewsets.GenericViewSet):
    """GED-WF.3: canonical Supplier documents that may correspond to a received reference.
    Several results are returned as such -- the user selects explicitly, nothing is chosen here."""

    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "update")
        reference = request.query_params.get("reference", "")
        matches = find_reception_matches(
            transmittal, reference, request.query_params.get("external_revision"),
        )
        return Response({"matches": matches})


class TransmittalEventViewSet(viewsets.GenericViewSet):
    def list(self, request, transmittal_id=None, *args, **kwargs):
        transmittal = _transmittal_for_action(request, transmittal_id, "view")
        events = list(transmittal.events.select_related("actor"))
        visible_issue_ids = _visible_client_issue_ids(request, transmittal, events)
        return Response([
            {
                "id": event.pk,
                "event_type": event.event_type,
                "actor": {"id": event.actor_id, "name": event.actor.full_name} if event.actor_id else None,
                "metadata": _filter_client_return_lines(event.metadata, visible_issue_ids),
                "created_at": event.created_at,
            }
            for event in events
        ])


def _visible_client_issue_ids(request, transmittal, events):
    """GED-SEC.5: the client issues named by the events' client_return_lines whose document the caller can read --
    the same accessible-document filter as TransmittalClientReturnLineViewSet.list (get_register_queryset)."""
    from project_management.models import ClientIssue
    from project_management.services.document_register import get_register_queryset

    wanted = {
        row.get("client_issue")
        for event in events
        for row in ((event.metadata or {}).get("client_return_lines") or [])
        if isinstance(row, dict)
    }
    wanted.discard(None)
    if not wanted:
        return set()
    issue_documents = dict(ClientIssue.objects.filter(pk__in=wanted).values_list("pk", "document_revision__document_id"))
    visible_documents = set(
        get_register_queryset(request, project_id=transmittal.project_id)
        .filter(pk__in=set(issue_documents.values())).values_list("pk", flat=True)
    )
    return {issue for issue, document in issue_documents.items() if document in visible_documents}


def _filter_client_return_lines(metadata, visible_issue_ids):
    """Copy of the stored metadata (never written back) without the client_return_lines of an inaccessible document."""
    if not isinstance(metadata, dict) or not metadata.get("client_return_lines"):
        return metadata
    lines = metadata["client_return_lines"]
    kept = [row for row in lines if isinstance(row, dict) and row.get("client_issue") in visible_issue_ids]
    result = {**metadata, "client_return_lines": kept}
    if len(kept) != len(lines):
        result["client_return_lines_restricted"] = len(lines) - len(kept)
    return result


class TransmittalSendView(viewsets.GenericViewSet):
    def create(self, request, transmittal_id=None, *args, **kwargs):
        from rest_framework import serializers
        from project_management.services.transmittal_send import send_transmittal
        class SendInput(serializers.Serializer):
            recipient_contact = serializers.IntegerField(min_value=1, required=False)
            mailbox_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
            subject = serializers.CharField(max_length=500, required=False, allow_blank=False)
            body = serializers.CharField(max_length=20000, required=False, allow_blank=True, trim_whitespace=False)
        transmittal = _transmittal_for_action(request, transmittal_id, "issue")
        payload = SendInput(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            event = send_transmittal(transmittal, request.user,
                recipient_contact_id=data.get('recipient_contact'), mailbox_id=data.get('mailbox_id'),
                subject=data.get('subject'), body=data.get('body'))
        except DjangoValidationError as error:
            raise _to_drf_error(error)
        return Response({'id': event.pk, 'event_type': event.event_type, 'metadata': event.metadata},
                        status=200 if event.event_type == 'SEND_SUCCEEDED' else 502)
