"""LOT 3D-2: GET /correspondances -- the unified, read-only Correspondances register.

A thin view: all query building lives in services.correspondances_register. Authorization is
evaluated independently per entity type (documents.correspondence_view /
documents.transmittal_view), exactly like the individual Correspondence/Transmittal APIs
already do -- this endpoint never broadens what either already allows.
"""

from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from project_management.models import Project
from project_management.pagination import CustomPageNumberPagination
from project_management.permissions import CorrespondencePermission, TransmittalPermission
from project_management.services.correspondances_register import (
    annotate_ready_to_send,
    build_register_queryset,
    serialize_row,
)

ENTITY_TYPES = ("CORRESPONDENCE", "TRANSMITTAL")


def _truthy(value):
    return str(value).lower() in ("1", "true", "yes")


class CorrespondancesRegisterView(GenericAPIView):
    pagination_class = CustomPageNumberPagination

    def get(self, request, *args, **kwargs):
        user = request.user
        params = request.query_params

        project = None
        project_id = params.get("project")
        if project_id:
            project = Project.objects.filter(pk=project_id).first()
            if project is None:
                return Response({"project": "Not found."}, status=400)

        company_scope = _truthy(params.get("company_scope", "")) and project is None
        entity_type = (params.get("entity_type") or "").upper() or None
        if entity_type and entity_type not in ENTITY_TYPES:
            return Response({"entity_type": f"Must be one of {ENTITY_TYPES}."}, status=400)
        requested = {entity_type} if entity_type else set(ENTITY_TYPES)

        # 1. What CAN structurally exist in this context (Transmittal never in company scope).
        structurally_available = requested - ({"TRANSMITTAL"} if company_scope else set())
        if not structurally_available:
            return self._empty_response(request)

        # 2. Of what's structurally possible, what is this user actually authorized to see.
        # LOT 3D-3: routed through each permission class's own _check_action_or_full_access,
        # so documents.full_access is honored here exactly like it now is on the individual
        # Correspondence/Transmittal APIs -- never a separate, looser check.
        scope, context = ("project", {"project": project}) if project else ("company", {})
        allowed = set()
        if "CORRESPONDENCE" in structurally_available and CorrespondencePermission()._check_action_or_full_access(
            user, "correspondence_view", scope, context
        ):
            allowed.add("CORRESPONDENCE")
        if "TRANSMITTAL" in structurally_available and TransmittalPermission()._check_action_or_full_access(
            user, "transmittal_view", scope, context
        ):
            allowed.add("TRANSMITTAL")

        if not allowed:
            raise PermissionDenied()

        direction = (params.get("direction") or "").upper() or None
        if direction and direction not in ("IN", "OUT"):
            return Response({"direction": "Must be IN or OUT."}, status=400)

        company = params.get("company")
        company = int(company) if company and company.isdigit() else None
        contact = params.get("contact")
        contact = int(contact) if contact and contact.isdigit() else None

        ordering = [o for o in (params.get("ordering") or "").split(",") if o]

        queryset = build_register_queryset(
            user,
            project=project,
            company_scope=company_scope,
            entity_types=tuple(allowed),
            direction=direction,
            company=company,
            contact=contact,
            correspondence_type=params.get("correspondence_type") or None,
            search=params.get("search") or None,
            status=params.get("status") or None,
            ordering=ordering,
        )

        page = self.paginate_queryset(queryset)
        rows = annotate_ready_to_send([serialize_row(row) for row in page])
        return self.get_paginated_response(rows)

    def _empty_response(self, request):
        paginator = self.pagination_class()
        paginator.paginate_queryset([], request)
        return paginator.get_paginated_response([])
