from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ecpmpapp.permissions import LicensePermission
from models.display_feature_registry import (
    DISPLAY_FEATURES,
    DISPLAY_FEATURE_REGISTRY_VERSION,
    DISPLAY_FEATURE_REGISTRY_REVISION,
    DISPLAY_FEATURE_SCHEMA_MAJOR,
    normalize_solution_id,
)
from models.display_feature_resolver import (
    get_effective_visibility_map,
    sanitize_stored_display_overrides,
)
from models.models import SolutionDisplayConfiguration
from project_management.utils import get_product_name_from_request


class DisplayConfigurationView(APIView):
    """Return effective feature visibility for the authenticated user."""

    permission_classes = [IsAuthenticated, LicensePermission]

    @staticmethod
    def _permission_rules(solution):
        return {
            feature.key: [
                {
                    "scope": rule.scope.value,
                    "module": rule.module,
                    "actions": list(rule.actions),
                    "require_all": rule.require_all,
                }
                for rule in feature.permissions
            ]
            for feature in DISPLAY_FEATURES
            if solution in feature.available_in and feature.permissions
        }

    def get(self, request):
        try:
            solution = normalize_solution_id(
                get_product_name_from_request(request)
            )
        except ValueError as exc:
            raise ValidationError({"solution": str(exc)}) from exc

        configuration = None
        if request.user.company_id:
            configuration = SolutionDisplayConfiguration.objects.filter(
                company_id=request.user.company_id,
                solution=solution.value,
            ).first()

        overrides = sanitize_stored_display_overrides(
            solution,
            configuration.overrides if configuration else {},
        )
        return Response(
            {
                "solution": solution.value,
                "schema_major": DISPLAY_FEATURE_SCHEMA_MAJOR,
                "registry_revision": DISPLAY_FEATURE_REGISTRY_REVISION,
                "registry_version": DISPLAY_FEATURE_REGISTRY_VERSION,
                "permission_rules": self._permission_rules(solution),
                "effective_visibility": get_effective_visibility_map(
                    solution,
                    overrides,
                ),
            }
        )
