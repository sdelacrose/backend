from django.contrib.auth.models import AnonymousUser
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from models.models import Staff

from human_resource_management.models.sst import SSTCase, SSTCasePeriod
from human_resource_management.models.staff_documents import StaffDocumentCategory
from human_resource_management.permissions import (
    can_manage_sst_case, can_view_staff_sst_dossier, has_sst_payroll_attestation_permission,
    has_sst_payroll_view_permission,
)
from human_resource_management.serializers.sst import (
    serialize_payroll_projection_row, serialize_sst_case,
)
from human_resource_management.services.sst import (
    SSTError, cancel_period, close_case, correct_period, declare_case, deposit_payroll_attestation,
    fetch_payroll_projection, log_document_added, record_decision, record_period,
)


def _target_staff(request, staff_id):
    staff = get_object_or_404(Staff.objects.select_related("user__company"), pk=staff_id)
    if getattr(staff.user, "company_id", None) != getattr(request.user, "company_id", None):
        raise DRFPermissionDenied("You do not have permission to access this staff profile's SST dossier.")
    return staff


class SSTCaseViewSet(viewsets.GenericViewSet):
    """LOT L04: list/retrieve/create a case, plus record-period/decision/
    close actions. Never reachable by a manager or technical/solution
    administrator -- see permissions.can_view_staff_sst_dossier/
    can_manage_sst_case, neither of which has a Company Admin fallback."""

    def list(self, request, *args, **kwargs):
        staff_id = request.query_params.get("staff")
        if not staff_id:
            raise ValidationError({"staff": "This field is required."})
        staff = _target_staff(request, staff_id)
        if not can_view_staff_sst_dossier(request.user, staff):
            raise DRFPermissionDenied("You do not have permission to view this staff member's SST dossier.")
        cases = SSTCase.objects.filter(staff=staff).select_related("staff__user", "declared_by").prefetch_related(
            "periods__leave_request",
        )
        return Response([serialize_sst_case(c) for c in cases])

    def retrieve(self, request, pk=None, *args, **kwargs):
        case = get_object_or_404(SSTCase.objects.select_related("staff__user", "declared_by"), pk=pk)
        if not can_view_staff_sst_dossier(request.user, case.staff):
            raise DRFPermissionDenied("You do not have permission to view this SST case.")
        return Response(serialize_sst_case(case, include_events=True))

    def create(self, request, *args, **kwargs):
        staff = _target_staff(request, request.data.get("staff"))
        category = request.data.get("category")
        if category not in dict(SSTCase.CATEGORY_CHOICES):
            raise ValidationError({"category": "Unknown SST category."})
        try:
            case = declare_case(
                staff=staff, category=category, actor=request.user,
                external_reference=request.data.get("external_reference", ""),
            )
        except SSTError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_sst_case(case), status=status.HTTP_201_CREATED)

    def _case_or_404(self, pk):
        return get_object_or_404(SSTCase.objects.select_related("staff__user"), pk=pk)

    @action(detail=True, methods=["post"], url_path="periods")
    def periods(self, request, pk=None):
        case = self._case_or_404(pk)
        start_date = request.data.get("start_date")
        end_date = request.data.get("end_date")
        if not start_date or not end_date:
            raise ValidationError({"start_date": "start_date and end_date are required."})
        try:
            record_period(case=case, start_date=start_date, end_date=end_date, actor=request.user)
        except SSTError as exc:
            raise ValidationError(str(exc))
        case.refresh_from_db()
        return Response(serialize_sst_case(case), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="decision")
    def decision(self, request, pk=None):
        case = self._case_or_404(pk)
        try:
            record_decision(case=case, note=request.data.get("note", ""), actor=request.user)
        except SSTError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_sst_case(case))

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None):
        case = self._case_or_404(pk)
        try:
            close_case(case=case, actor=request.user)
        except SSTError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_sst_case(case))

    @action(detail=True, methods=["post"], url_path="documents")
    def documents(self, request, pk=None):
        """Thin wrapper over the EXISTING StaffDocument creation path (see
        views.staff_documents -- it already validates the category is_sst
        and gates by can_manage_sst_case/self) -- this action only adds the
        SST-specific event/notification afterwards, never re-implements
        document storage."""
        from human_resource_management.services.staff_documents import create_staff_document

        case = self._case_or_404(pk)
        is_self = request.user.id == case.staff.user_id
        if not (is_self or can_manage_sst_case(request.user, case.staff)):
            raise DRFPermissionDenied("You do not have permission to add a document to this SST case.")
        category = get_object_or_404(StaffDocumentCategory, pk=request.data.get("category"), is_sst=True)
        if category.company_profile_id != case.company_profile_id:
            raise DRFPermissionDenied("This category does not belong to this case's company.")
        title = request.data.get("title")
        if not title:
            raise ValidationError({"title": "This field is required."})
        document = create_staff_document(
            staff=case.staff, company_profile=case.company_profile, category=category,
            title=title, actor=request.user, sst_case=case,
        )
        log_document_added(case=case, actor=request.user, title=title)
        from human_resource_management.serializers.staff_documents import serialize_document
        return Response(serialize_document(document, request), status=status.HTTP_201_CREATED)


class SSTCasePeriodViewSet(viewsets.GenericViewSet):
    """LOT L04: targeted correction/cancellation of one EXISTING period --
    never the classic LeaveRequest update endpoint (see services.sst's own
    docstring for why)."""

    def _period_or_404(self, pk):
        period = SSTCasePeriod.objects.select_related(
            "case__staff__user", "leave_request",
        ).filter(pk=pk).first()
        if period is None:
            raise Http404
        return period

    @action(detail=True, methods=["post"], url_path="correct")
    def correct(self, request, pk=None):
        period = self._period_or_404(pk)
        start_date = request.data.get("start_date")
        end_date = request.data.get("end_date")
        if not start_date or not end_date:
            raise ValidationError({"start_date": "start_date and end_date are required."})
        try:
            correct_period(
                period=period, start_date=start_date, end_date=end_date,
                actor=request.user, reason=request.data.get("reason", ""),
            )
        except SSTError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_sst_case(period.case, include_events=True))

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        period = self._period_or_404(pk)
        try:
            cancel_period(period=period, actor=request.user, reason=request.data.get("reason", ""))
        except SSTError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_sst_case(period.case, include_events=True))


class SSTPayrollViewSet(viewsets.GenericViewSet):
    """LOT L04: the paie surface -- a narrow, read-only projection and a
    deposit-only attestation action, gated by two SEPARATE permissions
    (never one implying the other, per the ticket's explicit instruction)."""

    parser_classes = [MultiPartParser, FormParser]

    def list(self, request, *args, **kwargs):
        if isinstance(request.user, AnonymousUser) or not has_sst_payroll_view_permission(request.user):
            raise DRFPermissionDenied("You do not have permission to view the SST payroll projection.")
        company = getattr(request.user, "company", None)
        if company is None:
            return Response([])
        rows = fetch_payroll_projection(company_profile=company, actor=request.user)
        return Response([serialize_payroll_projection_row(r) for r in rows])

    @action(detail=True, methods=["post"], url_path="payroll-attestation")
    def deposit_attestation(self, request, pk=None):
        if isinstance(request.user, AnonymousUser) or not has_sst_payroll_attestation_permission(request.user):
            raise DRFPermissionDenied("You do not have permission to deposit an SST payroll attestation.")
        case = get_object_or_404(SSTCase.objects.select_related("staff__user"), pk=pk)
        category = get_object_or_404(
            StaffDocumentCategory, pk=request.data.get("category"), is_sst=True, company_profile=case.company_profile,
        )
        uploaded = request.FILES.get("file")
        if not uploaded:
            raise ValidationError({"file": "This field is required."})
        try:
            document = deposit_payroll_attestation(
                case=case, actor=request.user, uploaded_file=uploaded, category=category,
            )
        except SSTError as exc:
            raise ValidationError(str(exc))
        from human_resource_management.serializers.staff_documents import serialize_document
        return Response(serialize_document(document, request), status=status.HTTP_201_CREATED)
