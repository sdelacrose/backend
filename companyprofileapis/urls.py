from django.urls import path, include
from .views import (
    CompanyProfileViewSet, CompanySearchView, CompanyConfigViewSet, OpportunityViewSet,  NoticeBoardViewSet, RoleViewSet
)
from project_management.slash_router import OptionalSlashRouter


router = OptionalSlashRouter()
router.register('company-config', CompanyConfigViewSet)
router.register('profile', CompanyProfileViewSet, basename='company-profile')
router.register('opportunity', OpportunityViewSet, basename='opportunity')
router.register('notice-board', NoticeBoardViewSet, basename='notice-board')
router.register('role', RoleViewSet, basename='role')

urlpatterns = [
    path('', include(router.urls)),
    path("company-search", CompanySearchView.as_view(), name="company_search")
]
