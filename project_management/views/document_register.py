from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from project_management.models import DocumentControl
from project_management.permissions import DocumentPermission
from project_management.serializers.document_register import (
    DocumentControlWriteSerializer,
    DocumentRegisterRowSerializer,
    user_reference,
)
from project_management.services.document_register import (
    parse_int_or_none,
    apply_register_filters,
    apply_register_ordering,
    assert_can_update_document,
    get_allowed_owner_queryset,
    get_register_document_or_404,
    get_register_queryset,
    save_document_control,
)


class DocumentRegisterViewSet(viewsets.GenericViewSet):
    """Document Register: accessible Documents + optional Document Control data.

    Read scope is the Documents module scope. Write access requires the same
    permission as updating the underlying Document. Both are enforced here on
    the backend, starting from the accessible Documents queryset.
    """

    serializer_class = DocumentRegisterRowSerializer
    filter_backends = []

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.action == "list":
            permissions.append(DocumentPermission("view", "mixed"))
        return permissions

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def _lookup(self):
        return {
            "document_id": self.kwargs.get("document_id"),
            "control_id": self.kwargs.get("control_id"),
        }

    def list(self, request, *args, **kwargs):
        project_id = parse_int_or_none(request.query_params.get("project"))
        queryset = get_register_queryset(request, project_id=project_id)
        queryset = apply_register_filters(queryset, request.query_params)
        queryset = apply_register_ordering(
            queryset, request.query_params.get("ordering")
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        document = get_register_document_or_404(request, **self._lookup())
        return Response(self.get_serializer(document).data)

    def partial_update(self, request, *args, **kwargs):
        document = get_register_document_or_404(request, **self._lookup())
        assert_can_update_document(request.user, document)

        serializer = DocumentControlWriteSerializer(
            data=request.data,
            partial=True,
            context={"document": document, "user": request.user},
        )
        serializer.is_valid(raise_exception=True)
        save_document_control(document, serializer.validated_data)

        document = get_register_document_or_404(request, **self._lookup())
        return Response(self.get_serializer(document).data)

    def filter_options(self, request, *args, **kwargs):
        # Same gate as the list endpoint (mixed Documents view permission).
        if not DocumentPermission("view", "mixed")._check_mixed_list_view_permission(
            request.user
        ):
            raise PermissionDenied(
                "You do not have permission to view documents."
            )
        queryset = get_register_queryset(request)
        projects = (
            queryset.filter(folder__project__isnull=False)
            .order_by()
            .values("folder__project_id", "folder__project__project_name")
            .distinct()
        )
        owners = get_allowed_owner_queryset(request.user).order_by(
            "first_name", "last_name", "email"
        )
        return Response(
            {
                "projects": sorted(
                    (
                        {
                            "id": row["folder__project_id"],
                            "name": row["folder__project__project_name"],
                        }
                        for row in projects
                    ),
                    key=lambda project: (project["name"] or "").lower(),
                ),
                "owners": [user_reference(owner) for owner in owners],
                "document_types": [
                    value for value, _label in DocumentControl.DocumentType.choices
                ],
                "disciplines": [
                    value for value, _label in DocumentControl.Discipline.choices
                ],
                "statuses": [
                    value for value, _label in DocumentControl.Status.choices
                ],
            },
            status=status.HTTP_200_OK,
        )
