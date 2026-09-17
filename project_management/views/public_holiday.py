from django.contrib.auth.models import AnonymousUser
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from project_management.models import CompanyPublicHoliday
from project_management.permissions import CompanyPublicHolidayPermission
from project_management.serializers import (
    CompanyPublicHolidaySerializer,
    CompanyPublicHolidayImportRequestSerializer,
    CompanyPublicHolidayImportResultSerializer,
)
from project_management.services.public_holiday import import_company_public_holidays


class CompanyPublicHolidayViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyPublicHolidaySerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["date", "source"]
    ordering_fields = ["date", "created_at", "updated_at"]
    ordering = ["date", "id"]

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [CompanyPublicHolidayPermission(action="view", scope="company")]
        if self.action in ["create", "update", "partial_update", "destroy", "import_holidays"]:
            return [CompanyPublicHolidayPermission(action="update", scope="company")]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return CompanyPublicHoliday.objects.none()

        company = getattr(user, "company", None)
        if not company:
            return CompanyPublicHoliday.objects.none()

        queryset = CompanyPublicHoliday.objects.filter(company_profile=company)

        requested_company_id = self.request.query_params.get("company_profile")
        if requested_company_id and str(company.id) != str(requested_company_id):
            return CompanyPublicHoliday.objects.none()

        return queryset.order_by(*self.ordering)

    def get_object(self):
        obj = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        self.check_object_permissions(self.request, obj)
        return obj

    def perform_create(self, serializer):
        serializer.save(
            company_profile=self.request.user.company,
            created_by=self.request.user,
        )

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        instance.delete()

    @action(detail=False, methods=["post"], url_path="import")
    def import_holidays(self, request, *args, **kwargs):
        serializer = CompanyPublicHolidayImportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = import_company_public_holidays(
            company_profile=request.user.company,
            created_by=request.user,
            country_code=serializer.validated_data["country_code"],
            year=serializer.validated_data["year"],
        )

        response_serializer = CompanyPublicHolidayImportResultSerializer(result)
        return Response(response_serializer.data, status=status.HTTP_200_OK)
