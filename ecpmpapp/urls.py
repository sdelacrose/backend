from django.contrib import admin
from django.urls import path, include, re_path
from django.contrib import admin
from django.conf.urls.static import static
from django.http import HttpResponse
from django.conf import settings
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from project_management.views.recette_document_file import RecetteDocumentFileView

schema_view = get_schema_view(
    openapi.Info(
        title="ECPMP API",
        default_version='v1',
        description="API documentation"
    ),
    url=settings.BACKEND_URL,
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    path('media/documents/<path:relative_path>', RecetteDocumentFileView.as_view(), name='recette-document-file'),
    path('', lambda request: HttpResponse("Welcome to ECPMP!")),
    path('api/admin/', admin.site.urls),
    path("api/user/", include("userapis.urls")),
    path("api/company/", include("companyprofileapis.urls")),
    path("api/project_management/", include("project_management.urls")),
    path("api/ai-tools/", include("ai_tools.urls")),
    path("api/aas/", include("aas_integration.urls")),
    path("api/project-content/", include("project_content.urls")),
    re_path(r'^swagger(?P<format>\.json|\.yaml)$', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    path('api/swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path("api/chat/", include("chat.urls")),
    path("api/human_resource_management/", include("human_resource_management.urls")),
    path('api/notifications', include('notifications.urls')),
    path('api/health-check/', include('health_check.urls')),
] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
