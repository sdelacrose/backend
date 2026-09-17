from django.urls import path, include
from health_check.views import EndpointTestingViewSet, APITestRunViewSet

from project_management.slash_router import OptionalSlashRouter

router = OptionalSlashRouter()

router.register(r'status', EndpointTestingViewSet, basename='status')
router.register(r'last-check', APITestRunViewSet, basename='last_check')

urlpatterns = [
    path('', include(router.urls)),
] 