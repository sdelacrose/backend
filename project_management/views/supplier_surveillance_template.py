from django.http import Http404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from models.models import CompanyProfile
from project_management.models import Project, SupplierSurveillanceTemplate
from project_management.permissions import ECPMPConfigPermission
from project_management.serializers.supplier_surveillance_template import (
    SupplierSurveillanceTemplateSerializer,
)
from project_management.services.supplier_surveillance_template import (
    resolve_company_for_project,
)

# L3C section 18: the narrowest existing permission for a comparable Document Control
# configuration action -- the same `ecpmp_configurations` permission the Project Configuration
# page itself already checks (ProjectConfig's own PERMISSION_MODULES.ECPMP_CONFIGURATIONS), not
# a new supplier_surveillance.* family (explicitly deferred).


def _resolve_project(request):
    project_id = request.query_params.get("project") or request.data.get("project")
    if not project_id:
        raise ValidationError({"project": "A project is required."})
    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        raise Http404
    return project


def _assert_permission(request, project, *, write):
    permission = ECPMPConfigPermission(
        action="update" if write else "view", scope="project"
    )
    context = {"project": project}
    allowed = (
        permission._check_update_permission(request.user, None, context)
        if write
        else permission._check_view_permission(request.user, None, context)
    )
    if not allowed:
        raise PermissionDenied(
            "You do not have permission to manage Supplier Surveillance Templates."
        )


class SupplierSurveillanceTemplateViewSet(viewsets.ModelViewSet):
    """L3C: Template -> Section -> Item editor, Company-scoped (L3A/L3C section 4).

    `project` is required on every request (query param for GET, body field for POST/PATCH) --
    used only to resolve the owning Company and to reuse the existing Document Control
    configuration permission (ecpmp_configurations), never stored on the Template itself.
    """

    serializer_class = SupplierSurveillanceTemplateSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def _company_for_request(self, *, write):
        project = _resolve_project(self.request)
        _assert_permission(self.request, project, write=write)
        return resolve_company_for_project(project)

    def get_queryset(self):
        company_id = self._company_for_request(write=self.request.method not in ("GET",))
        return SupplierSurveillanceTemplate.objects.filter(
            company_id=company_id
        ).prefetch_related("sections__items")

    def create(self, request, *args, **kwargs):
        company_id = self._company_for_request(write=True)
        serializer = self.get_serializer(
            data=request.data,
            context={
                **self.get_serializer_context(),
                "company": CompanyProfile.objects.get(pk=company_id),
                "user": request.user,
            },
        )
        serializer.is_valid(raise_exception=True)
        template = serializer.save()
        return Response(
            self.get_serializer(template).data, status=status.HTTP_201_CREATED
        )
