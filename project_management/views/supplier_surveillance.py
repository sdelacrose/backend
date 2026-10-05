from pathlib import Path

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework import status

from project_management.models import DocumentRevision, SupplierSurveillanceTemplate
from project_management.serializers.document_register import user_reference
from project_management.services.document_register import (
    assert_can_update_document,
    get_register_document_or_404,
)
from project_management.services.supplier_surveillance import (
    create_supplier_surveillance,
    get_supplier_surveillance_for_revision,
    resolve_project_for_surveillance,
)
from project_management.services.supplier_surveillance_template import (
    resolve_company_for_project,
)


def _absolute_url(request, url):
    """Same convention as views.document_revision._absolute_url."""
    return url if str(url).startswith("http") else request.build_absolute_uri(url)


def serialize_evidence_pdf(evidence, request=None):
    """L3G-2 section 24: the minimum L3G-3 needs -- url + generated_at (+ file_name). Never
    storage internals, server paths, the full evidence_metadata JSON, or the hash (section 24)."""
    if evidence is None:
        return None
    url = evidence.file.url
    return {
        "url": _absolute_url(request, url) if request is not None else url,
        "generated_at": evidence.generated_at,
        "file_name": Path(evidence.file.name).name,
    }


def serialize_supplier_surveillance(surveillance, request=None):
    document = surveillance.document_revision.document
    # L3C section 25/26: an L3B row may have template_snapshot = NULL -- tolerate it, never
    # retroactively invent one.
    snapshot = surveillance.template_snapshot or {}
    # L3G-1's OneToOne reverse accessor raises RelatedObjectDoesNotExist (an AttributeError
    # subclass) when no Evidence exists yet -- getattr(..., None) is the documented-safe way to
    # read it without an extra query or a try/except DoesNotExist import.
    evidence = getattr(surveillance, "evidence", None)
    return {
        "id": surveillance.id,
        "reference": surveillance.reference,
        "status": surveillance.status,
        "result": surveillance.result,
        "new_revision_required": surveillance.new_revision_required,
        "document_revision": surveillance.document_revision_id,
        "project": surveillance.project_id,
        "supplier": (
            {"id": document.external_issuer_company_id, "name": document.external_issuer_company.company_name}
            if document.external_issuer_company_id
            else None
        ),
        "supplier_reference": document.identification_number,
        "supplier_revision": surveillance.document_revision.external_revision,
        "supplier_transmittal_category": document.supplier_transmittal_category,
        "template_name": snapshot.get("template_name"),
        "template_version": snapshot.get("template_version"),
        "created_by": user_reference(surveillance.created_by),
        "created_at": surveillance.created_at,
        "closed_at": surveillance.closed_at,
        # L3E: server-derived only, never accepted from frontend input.
        "closed_by": user_reference(surveillance.closed_by),
        "closure_comment": surveillance.closure_comment,
        # L3G-2 section 24: null until generation succeeds (automatic post-closure attempt or
        # manual retry) -- never created/implied for a non-CLOSED Surveillance.
        "evidence_pdf": serialize_evidence_pdf(evidence, request=request),
    }


def serialize_available_template(template):
    return {
        "id": template.id,
        "name": template.name,
        "version": template.version,
        "sections_count": template.sections.count(),
        "items_count": sum(section.items.count() for section in template.sections.all()),
    }


class SupplierSurveillanceViewSet(viewsets.GenericViewSet):
    """L3B: foundation only -- create (on demand) and read the Supplier Surveillance of ONE
    exact DocumentRevision. No update, no delete, no close, no reviewers (later lots).

    Access starts from the accessible Documents queryset (404 otherwise), same as
    DocumentWorkflowViewSet; creating one needs the same document-update permission already
    used to start the internal workflow -- no dedicated supplier_surveillance permission exists
    yet (L3A/L3B section 13 deferred it).
    """

    filter_backends = []

    def _revision(self, request, document_id, revision_id):
        document = get_register_document_or_404(request, document_id=document_id)
        revision = DocumentRevision.objects.filter(
            document=document, pk=revision_id
        ).first()
        if revision is None:
            raise Http404
        return document, revision

    def retrieve(self, request, document_id=None, revision_id=None, *args, **kwargs):
        _document, revision = self._revision(request, document_id, revision_id)
        surveillance = get_supplier_surveillance_for_revision(revision)
        return Response(
            {
                "supplier_surveillance": (
                    serialize_supplier_surveillance(surveillance, request=request)
                    if surveillance
                    else None
                )
            }
        )

    def create(self, request, document_id=None, revision_id=None, *args, **kwargs):
        document, revision = self._revision(request, document_id, revision_id)
        assert_can_update_document(request.user, document)
        template_id = request.data.get("template") or request.data.get("template_id")
        template = SupplierSurveillanceTemplate.objects.filter(pk=template_id).first()
        if template is None:
            return Response(
                {"template": "A Surveillance Template is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            surveillance = create_supplier_surveillance(
                document_revision=revision, actor=request.user, template=template
            )
        except DjangoValidationError as error:
            detail = error.message_dict if hasattr(error, "message_dict") else {"detail": error.messages}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            serialize_supplier_surveillance(surveillance, request=request),
            status=status.HTTP_201_CREATED,
        )

    def templates(self, request, document_id=None, revision_id=None, *args, **kwargs):
        """L3C: active Surveillance Templates available for THIS exact revision's Project's
        Company -- the one the frontend's Create Surveillance Sheet selector offers."""
        document, revision = self._revision(request, document_id, revision_id)
        project = resolve_project_for_surveillance(document)
        company_id = resolve_company_for_project(project) if project else None
        queryset = (
            SupplierSurveillanceTemplate.objects.filter(
                company_id=company_id, is_active=True
            )
            if company_id
            else SupplierSurveillanceTemplate.objects.none()
        )
        return Response(
            {"templates": [serialize_available_template(t) for t in queryset]}
        )
