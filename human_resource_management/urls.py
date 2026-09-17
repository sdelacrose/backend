from rest_framework.routers import DefaultRouter
from human_resource_management.views import LeaveRequestViewSet, StaffViewSet, StaffObservationViewSet, StaffConfigurationViewSet
from django.urls import path, include

router = DefaultRouter()

router.register('leave-requests', LeaveRequestViewSet, basename='leave-request')
router.register('staff', StaffViewSet, basename='staff')
router.register('staff-observation', StaffObservationViewSet, basename='staff_observation')
router.register('staff-configuration', StaffConfigurationViewSet, basename='staff_configuration')
urlpatterns = [
    path('', include(router.urls)),
]
