from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from models.models import Staff, User
from human_resource_management.models.staff_documents import StaffDocument, StaffDocumentCategory, StaffDocumentFile, StaffDocumentFileStatusEvent, StaffDocumentGrant
from human_resource_management.models.sst import SSTCase
from human_resource_management.permissions import (
    can_manage_sst_case, can_transition_staff_document_file_status, can_update_staff_document,
    can_upload_staff_document_file, can_view_staff_document, has_sst_manage_permission,
    has_sst_payroll_attestation_permission, has_sst_payroll_view_permission, has_sst_view_permission,
    has_staff_document_update_permission, has_staff_document_view_permission,
)
from human_resource_management.serializers.staff_documents import (
    serialize_category, serialize_document, serialize_file, serialize_grant, validate_upload,
)
from human_resource_management.services.hr_reminders import notify_document_requested
from human_resource_management.services.staff_documents import (
    StaffDocumentError, create_staff_document, create_staff_document_file, grant_staff_document_access,
    rename_staff_document, revoke_staff_document_access, transition_staff_document_file_status,
)


class StaffDocumentCategoryViewSet(viewsets.GenericViewSet):
    """L03: company-scoped catalog, same convention as CompanyCostItemType
    (B3.2) -- never accepts a company id in the request (isolation by
    construction), empty at first."""

    def _company(self, request):
        company = getattr(request.user, "company", None)
        if company is None:
            raise DRFPermissionDenied("No company associated with this account.")
        return company

    def list(self, request, *args, **kwargs):
        company = self._company(request)
        has_general = (
            has_staff_document_view_permission(request.user)
            or getattr(company, "admin_user_id", None) == request.user.id
        )
        # LOT L04 (SST): an SST-only holder (dossier or payroll) must still
        # reach this list -- the general staff_management.view_documents
        # permission is a completely separate right they may never hold.
        # Without this, the SST module's own "pick a category" screens
        # (declare/add-document/deposit attestation) would 403 outright for
        # exactly the viewers who are SUPPOSED to use them -- a real gap
        # found during browser verification, not a hypothetical.
        has_sst = (
            has_sst_manage_permission(request.user)
            or has_sst_view_permission(request.user)
            or has_sst_payroll_view_permission(request.user)
            or has_sst_payroll_attestation_permission(request.user)
        )
        if not has_general and not has_sst:
            raise DRFPermissionDenied("You do not have permission to view this company's document categories.")
        categories = StaffDocumentCategory.objects.filter(company_profile=company).order_by("name")
        # Never reveal that an SST catalog exists to a viewer who isn't
        # SST-habilitated in ANY way -- not just the documents, the CATEGORY
        # itself stays invisible through the general document catalog too.
        if not has_sst:
            categories = categories.exclude(is_sst=True)
        # Symmetrically: an SST-only holder (no general right) never sees
        # the non-SST catalog either -- two separate universes, neither
        # implies the other.
        if not has_general:
            categories = categories.filter(is_sst=True)
        return Response([serialize_category(c) for c in categories])

    def create(self, request, *args, **kwargs):
        company = self._company(request)
        is_sst = bool(request.data.get("is_sst", False))
        if is_sst:
            # LOT L04 (SST): a completely separate gate -- never has_staff_
            # document_update_permission, never the Company Admin fallback.
            if not has_sst_manage_permission(request.user):
                raise DRFPermissionDenied("You do not have permission to manage SST document categories.")
        elif not has_staff_document_update_permission(request.user) and getattr(company, "admin_user_id", None) != request.user.id:
            raise DRFPermissionDenied("You do not have permission to manage this company's document categories.")
        name = request.data.get("name")
        if not name:
            raise ValidationError({"name": "This field is required."})
        category = StaffDocumentCategory.objects.create(
            company_profile=company, name=name,
            # An SST category is always PRIVATE to one collaborator -- never
            # SHARED, regardless of what the request supplies.
            scope=StaffDocumentCategory.SCOPE_PRIVATE if is_sst else request.data.get("scope", StaffDocumentCategory.SCOPE_PRIVATE),
            requires_validity_date=bool(request.data.get("requires_validity_date", False)),
            is_sst=is_sst,
            created_by=request.user,
        )
        return Response(serialize_category(category), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None, *args, **kwargs):
        company = self._company(request)
        if not has_staff_document_update_permission(request.user) and getattr(company, "admin_user_id", None) != request.user.id:
            raise DRFPermissionDenied("You do not have permission to manage this company's document categories.")
        category = get_object_or_404(StaffDocumentCategory, pk=pk, company_profile=company)
        if "is_active" in request.data:
            category.is_active = bool(request.data["is_active"])
            category.save(update_fields=["is_active"])
        return Response(serialize_category(category))


class StaffDocumentViewSet(viewsets.GenericViewSet):
    """L03: list (?staff=<id> for private docs, or ?shared=true for the
    company's shared docs) + create only -- files/grants/status are managed
    through the nested viewsets below, never here."""

    def _target_staff(self, request, staff_id):
        staff = get_object_or_404(Staff.objects.select_related("user__company"), pk=staff_id)
        if getattr(staff.user, "company_id", None) != getattr(request.user, "company_id", None):
            raise DRFPermissionDenied("You do not have permission to access this staff profile's documents.")
        return staff

    def list(self, request, *args, **kwargs):
        if request.query_params.get("shared") == "true":
            company = getattr(request.user, "company", None)
            if company is None:
                return Response([])
            documents = StaffDocument.objects.filter(company_profile=company, staff__isnull=True)
            documents = [d for d in documents if can_view_staff_document(request.user, d)]
            return Response([serialize_document(d, request, include_grants=True) for d in documents])

        staff = self._target_staff(request, request.query_params.get("staff"))
        documents = StaffDocument.objects.filter(staff=staff).select_related("category")
        documents = [d for d in documents if can_view_staff_document(request.user, d)]
        return Response([serialize_document(d, request, include_grants=True) for d in documents])

    def create(self, request, *args, **kwargs):
        category = get_object_or_404(StaffDocumentCategory, pk=request.data.get("category"))
        title = request.data.get("title")
        if not title:
            raise ValidationError({"title": "This field is required."})

        if category.scope == StaffDocumentCategory.SCOPE_SHARED:
            company = getattr(request.user, "company", None)
            if company is None or category.company_profile_id != company.id:
                raise DRFPermissionDenied("This category does not belong to your company.")
            if not has_staff_document_update_permission(request.user) and getattr(company, "admin_user_id", None) != request.user.id:
                raise DRFPermissionDenied("You do not have permission to create a shared document.")
            document = create_staff_document(staff=None, company_profile=company, category=category, title=title, actor=request.user)
            return Response(serialize_document(document, request), status=status.HTTP_201_CREATED)

        staff = self._target_staff(request, request.data.get("staff"))
        if category.company_profile_id != staff.user.company_id:
            raise DRFPermissionDenied("This category does not belong to this staff member's company.")
        is_self = request.user.id == staff.user_id
        if category.is_sst:
            # LOT L04 (SST): a completely separate, narrower gate -- never
            # has_staff_document_update_permission, never the Company Admin
            # fallback. The SST module has its own dedicated endpoints for
            # this; this generic path only ever reaches here if someone
            # submits an is_sst category id directly to it, so the same
            # confidentiality rule must hold here too, not only there.
            if not (is_self or can_manage_sst_case(request.user, staff)):
                raise DRFPermissionDenied("You do not have permission to create this SST document.")
        else:
            is_hr_or_admin = (
                has_staff_document_update_permission(request.user)
                or getattr(staff.user.company, "admin_user_id", None) == request.user.id
            )
            if not (is_self or is_hr_or_admin):
                raise DRFPermissionDenied("You do not have permission to create this document.")
        sst_case = None
        sst_case_id = request.data.get("sst_case")
        if sst_case_id:
            if not category.is_sst:
                raise ValidationError({"sst_case": "Only an SST-category document can be linked to an SST case."})
            sst_case = get_object_or_404(SSTCase, pk=sst_case_id, staff=staff)
        document = create_staff_document(
            staff=staff, company_profile=staff.user.company, category=category, title=title,
            actor=request.user, sst_case=sst_case,
        )
        return Response(serialize_document(document, request), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None, *args, **kwargs):
        document = get_object_or_404(StaffDocument, pk=pk)
        if not can_update_staff_document(request.user, document):
            raise DRFPermissionDenied("You do not have permission to rename this document.")
        new_title = request.data.get("title")
        if not new_title:
            raise ValidationError({"title": "This field is required."})
        rename_staff_document(staff_document=document, new_title=new_title, actor=request.user)
        return Response(serialize_document(document, request))


class StaffDocumentFileViewSet(viewsets.GenericViewSet):
    """L03: nested under one StaffDocument -- list(implicit via parent)/
    create/download only, never update/delete (immutable versions)."""

    parser_classes = [MultiPartParser, FormParser]

    @staticmethod
    def _document(staff_document_id):
        document = StaffDocument.objects.filter(pk=staff_document_id).select_related("staff__user__company", "company_profile").first()
        if document is None:
            raise Http404
        return document

    @staticmethod
    def _file(document, file_id):
        file = StaffDocumentFile.objects.filter(staff_document=document, pk=file_id).first()
        if file is None:
            raise Http404
        return file

    def create(self, request, staff_document_id=None, *args, **kwargs):
        document = self._document(staff_document_id)
        if not can_upload_staff_document_file(request.user, document):
            raise DRFPermissionDenied("You do not have permission to upload a version of this document.")
        uploaded = request.FILES.get("file")
        if uploaded is None:
            raise ValidationError({"file": "A file is required."})
        errors = validate_upload(uploaded)
        if errors:
            raise ValidationError({"file": errors})
        try:
            doc_file = create_staff_document_file(
                staff_document=document, actor=request.user, uploaded_file=uploaded,
                valid_from=request.data.get("valid_from") or None, valid_until=request.data.get("valid_until") or None,
            )
        except StaffDocumentError as exc:
            raise ValidationError(str(exc))
        if document.staff_id and request.user.id != document.staff.user_id:
            from django.db import transaction
            transaction.on_commit(lambda: notify_document_requested(staff_document=document, actor=request.user))
        return Response(serialize_file(doc_file, request), status=status.HTTP_201_CREATED)

    def download(self, request, staff_document_id=None, file_id=None, *args, **kwargs):
        document = self._document(staff_document_id)
        if not can_view_staff_document(request.user, document):
            raise DRFPermissionDenied("You do not have permission to access this document.")
        file = self._file(document, file_id)
        if not file.file:
            raise Http404
        return FileResponse(file.file.open("rb"), as_attachment=True, filename=file.file_name)


class StaffDocumentFileStatusViewSet(viewsets.GenericViewSet):
    def create(self, request, staff_document_id=None, file_id=None, *args, **kwargs):
        document = StaffDocumentFileViewSet._document(staff_document_id)
        file = StaffDocumentFileViewSet._file(document, file_id)
        if not can_transition_staff_document_file_status(request.user, document):
            raise DRFPermissionDenied("You do not have permission to change this document's status.")
        new_status = request.data.get("status")
        if new_status not in dict(StaffDocumentFileStatusEvent.STATUS_CHOICES):
            raise ValidationError({"status": "Invalid status."})
        try:
            transition_staff_document_file_status(
                file=file, new_status=new_status, actor=request.user, note=request.data.get("note", ""),
            )
        except StaffDocumentError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_file(file, request))


class StaffDocumentGrantViewSet(viewsets.GenericViewSet):
    def create(self, request, staff_document_id=None, *args, **kwargs):
        document = StaffDocumentFileViewSet._document(staff_document_id)
        if document.category.is_sst:
            # LOT L04 (SST): confidentiality is closed -- no ad-hoc
            # exceptional access for SST pieces, per the ticket's explicit
            # instruction ("ni ... accord documentaire ordinaire"). The
            # fixed permission set (self/view_sst_dossier/manage_sst_cases)
            # is the only way in, never a per-document grant to anyone else.
            raise DRFPermissionDenied("SST documents do not support exceptional access grants.")
        if not can_update_staff_document(request.user, document):
            raise DRFPermissionDenied("You do not have permission to grant access to this document.")
        target_user = get_object_or_404(User, pk=request.data.get("user"))
        try:
            grant = grant_staff_document_access(staff_document=document, user=target_user, actor=request.user)
        except StaffDocumentError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_grant(grant), status=status.HTTP_201_CREATED)

    def destroy(self, request, staff_document_id=None, pk=None, *args, **kwargs):
        document = StaffDocumentFileViewSet._document(staff_document_id)
        if not can_update_staff_document(request.user, document):
            raise DRFPermissionDenied("You do not have permission to revoke access to this document.")
        grant = get_object_or_404(StaffDocumentGrant, pk=pk, staff_document=document)
        try:
            revoke_staff_document_access(grant=grant, actor=request.user)
        except StaffDocumentError as exc:
            raise ValidationError(str(exc))
        return Response(status=status.HTTP_204_NO_CONTENT)
