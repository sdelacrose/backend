from django.conf import settings
from rest_framework.permissions import BasePermission


class AIToolServicePermission(BasePermission):
    """
    AI tool endpoints are user-authorized by JWT and service-authenticated by a
    shared key. In local debug environments, an unset key is allowed so the
    developer can exercise the endpoints without provisioning secrets.
    """

    message = "AI tool service key is missing or invalid."

    def has_permission(self, request, view):
        expected_key = getattr(settings, "AI_TOOLS_SERVICE_API_KEY", "")
        header_name = getattr(settings, "AI_TOOLS_SERVICE_API_KEY_HEADER", "X-AI-Tools-Service-Key")
        provided_key = request.headers.get(header_name)

        if expected_key:
            return provided_key == expected_key

        return bool(getattr(settings, "DEBUG", False))

