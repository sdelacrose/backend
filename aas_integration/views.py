import hmac
import json
import re
import time
from types import SimpleNamespace

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied, ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    Document,
    Project,
    ProjectSolutionSpaceItem,
)
from project_management.permissions import (
    AvailableProductPartPermission,
    BillOfMaterialPermission,
    CompanySolutionSpacePermission,
    ProjectSolutionSpacePermission,
)
from project_management.services.document_access import get_accessible_document_queryset
from project_management.utils import get_product_name_from_request, has_project_permission

from .adapters import DjangoArtifactStore
from .aasx_preview import (
    get_cached_aasx_preview,
    parse_and_cache_document_aasx,
    set_aasx_preview_state,
)
from .domain import (
    ProductAASMapper,
    ProjectBOMAASMapper,
    SolutionSpaceAASMapper,
    validation_report,
)
from .models import (
    AASPublication,
    AASResourceIdentity,
    AASSubmodelTemplate,
    ExternalAASClient,
    ExternalAASGrant,
    FestoHandover,
    ProductAASProfile,
)
from .official_templates import (
    import_official_template,
    official_template_catalog,
)
from .profiles import product_aas_field_contract
from .permissions import has_aas_models_permission
from .serializers import (
    AASFieldProvenanceSerializer,
    AASPublicationSerializer,
    AASSubmodelTemplateSerializer,
    ExternalAASGrantSerializer,
    FestoHandoverSerializer,
    ProductAASProfileSerializer,
    ProductOriginClassificationSerializer,
)
from .services import (
    confirm_festo_handover,
    create_festo_preview,
    current_publication,
    enqueue_publication,
    get_or_create_company_assembly_identity,
    get_or_create_product_identity,
    get_or_create_project_identity,
    get_or_create_project_assembly_identity,
    infer_aas_origin_from_values,
    mark_festo_handover_launched,
    mark_product_out_of_date,
    publish_resource,
    resource_summary,
    synchronize_product_aas_profile,
    validate_product,
    validate_project,
    validate_company_assembly,
    validate_project_assembly,
)
from .tasks import parse_aasx_document_preview, process_aas_outbox_event
from .template_services import (
    activate_template,
    deprecate_template,
    import_submodel_templates,
    validate_template,
)


def _require_enabled(flag="AAS_ENABLED"):
    if not getattr(settings, flag, False):
        raise Http404


def _require_aas_access(request, *, write=False):
    if not has_aas_models_permission(request.user, write=write):
        raise PermissionDenied()


def _project(request, project_id: int, write=False) -> Project:
    _require_aas_access(request, write=write)
    project = get_object_or_404(Project, id=project_id)
    try:
        has_project_permission(
            "PUT" if write else "GET",
            request.user,
            project,
            get_product_name_from_request(request),
        )
    except DjangoPermissionDenied as exc:
        raise PermissionDenied(str(exc)) from exc
    module_permission = BillOfMaterialPermission(action="update" if write else "view")
    permission_request = SimpleNamespace(
        user=request.user,
        method="POST" if write else "GET",
        query_params=request.query_params,
        data={},
    )
    permission_view = SimpleNamespace(action="detail")
    if not module_permission.has_object_permission(
        permission_request,
        permission_view,
        SimpleNamespace(project=project),
    ):
        raise PermissionDenied()
    return project


def _product(request, product_id: int, write=False) -> AvailableProductPart:
    _require_aas_access(request, write=write)
    product = get_object_or_404(AvailableProductPart, id=product_id)
    permission = AvailableProductPartPermission(action="update" if write else "view", scope="company")
    permission_request = SimpleNamespace(user=request.user, method="POST" if write else "GET")
    if not permission.has_object_permission(permission_request, SimpleNamespace(action="detail"), product):
        raise PermissionDenied()
    return product


def _assembly(request, assembly_id: int, write=False) -> CompanySolutionSpaceItem:
    _require_aas_access(request, write=write)
    assembly = get_object_or_404(
        CompanySolutionSpaceItem.objects.select_related(
            "aas_product__aas_profile",
            "aas_product__aas_identity",
        ),
        id=assembly_id,
    )
    permission = CompanySolutionSpacePermission(
        action="update" if write else "view",
        scope="company",
    )
    permission_request = SimpleNamespace(
        user=request.user,
        method="POST" if write else "GET",
        query_params=request.query_params,
        data=request.data,
    )
    if not permission.has_object_permission(
        permission_request,
        SimpleNamespace(action="detail"),
        assembly,
    ):
        raise PermissionDenied()
    return assembly


def _project_assembly(
    request,
    assembly_id: int,
    write=False,
) -> ProjectSolutionSpaceItem:
    _require_aas_access(request, write=write)
    assembly = get_object_or_404(
        ProjectSolutionSpaceItem.objects.select_related(
            "project",
            "project__company_profile",
            "source_company_item__aas_product__aas_profile",
            "source_company_item__aas_product__aas_identity",
            "aas_identity",
        ),
        id=assembly_id,
    )
    permission = ProjectSolutionSpacePermission(
        action="update" if write else "view",
        scope="project",
    )
    permission_request = SimpleNamespace(
        user=request.user,
        method="POST" if write else "GET",
        query_params=request.query_params,
        data=request.data,
    )
    if not permission.has_object_permission(
        permission_request,
        SimpleNamespace(action="detail"),
        assembly,
    ):
        raise PermissionDenied()
    return assembly


def _can_open_advanced_editor(request) -> bool:
    return bool(
        settings.AAS_ADVANCED_EDITOR_ENABLED
        and request.user.is_authenticated
        and request.user.company_id
        and has_aas_models_permission(request.user)
    )


def _can_write_product_aas(request, product: AvailableProductPart) -> bool:
    if not has_aas_models_permission(request.user, write=True):
        return False
    permission = AvailableProductPartPermission(action="update", scope="company")
    return permission.has_object_permission(
        SimpleNamespace(user=request.user, method="POST"),
        SimpleNamespace(action="detail"),
        product,
    )


def _summary_payload(
    identity: AASResourceIdentity | None,
    *,
    can_write=False,
    advanced_editor=False,
    festo_handover=False,
):
    features = {
        "can_write": can_write,
        "festo_handover": (
            settings.FESTO_HANDOVER_ENABLED and can_write and festo_handover
        ),
        "advanced_editor": advanced_editor,
    }
    if not identity:
        return {
            "status": "unpublished",
            "out_of_date": True,
            "resource_id": None,
            "aas_id": None,
            "current_publication": None,
            "latest_publication": None,
            "grants": [],
            "provenance": [],
            "features": features,
        }
    summary = resource_summary(identity)
    active_publication_id = summary["current_publication"].id if summary["current_publication"] else None
    serializer_context = {"active_publication_id": active_publication_id}
    summary["current_publication"] = (
        AASPublicationSerializer(summary["current_publication"], context=serializer_context).data
        if summary["current_publication"]
        else None
    )
    summary["latest_publication"] = (
        AASPublicationSerializer(summary["latest_publication"], context=serializer_context).data
        if summary["latest_publication"]
        else None
    )
    summary["grants"] = ExternalAASGrantSerializer(identity.external_grants.select_related("client").order_by("-created_at"), many=True).data
    summary["provenance"] = AASFieldProvenanceSerializer(
        identity.field_provenance.order_by("field_path"),
        many=True,
    ).data
    summary["features"] = features
    return summary


def _translate_validation_error(exc: DjangoValidationError) -> ValidationError:
    if hasattr(exc, "message_dict"):
        return ValidationError(exc.message_dict)
    return ValidationError(exc.messages)


def _submodel_template(request, template_id, *, write=False):
    _require_enabled()
    _require_aas_access(request, write=write)
    company_id = getattr(request.user, "company_id", None)
    if not company_id:
        raise PermissionDenied()
    return get_object_or_404(
        AASSubmodelTemplate.objects.prefetch_related(
            "field_mappings",
            "supplementary_files",
        ),
        id=template_id,
        company_id=company_id,
    )


class AASSubmodelTemplateListView(APIView):
    def get(self, request):
        _require_enabled()
        _require_aas_access(request)
        company_id = getattr(request.user, "company_id", None)
        if not company_id:
            return Response({"count": 0, "results": []})
        templates = AASSubmodelTemplate.objects.filter(
            company_id=company_id,
        ).prefetch_related("field_mappings", "supplementary_files")
        requested_status = request.query_params.get("status")
        if requested_status:
            if requested_status not in AASSubmodelTemplate.Status.values:
                raise ValidationError({"status": "Choose a valid template status."})
            templates = templates.filter(status=requested_status)
        search = str(request.query_params.get("search") or "").strip()
        if search:
            templates = templates.filter(
                Q(name__icontains=search)
                | Q(id_short__icontains=search)
                | Q(template_identifier__icontains=search)
                | Q(semantic_id__icontains=search)
            )
        return Response(
            {
                "count": templates.count(),
                "features": {
                    "can_write": has_aas_models_permission(
                        request.user,
                        write=True,
                    )
                },
                "results": AASSubmodelTemplateSerializer(
                    templates,
                    many=True,
                ).data,
            }
        )


class AASSubmodelTemplateImportView(APIView):
    @transaction.atomic
    def post(self, request):
        _require_enabled()
        _require_aas_access(request, write=True)
        company_id = getattr(request.user, "company_id", None)
        if not company_id:
            raise PermissionDenied()
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Select an AAS JSON or AASX file."})
        max_bytes = settings.AASX_PREVIEW_MAX_COMPRESSED_BYTES
        if upload.size > max_bytes:
            raise ValidationError({"file": "The submodel template file is too large."})
        payload = upload.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValidationError({"file": "The submodel template file is too large."})
        raw_profile_keys = request.data.get("profile_keys")
        if raw_profile_keys:
            try:
                profile_keys = json.loads(raw_profile_keys)
            except (TypeError, ValueError):
                profile_keys = [
                    item.strip()
                    for item in str(raw_profile_keys).split(",")
                    if item.strip()
                ]
            if not isinstance(profile_keys, list) or not all(
                isinstance(item, str) and 0 < len(item.strip()) <= 100
                for item in profile_keys
            ):
                raise ValidationError(
                    {"profile_keys": "Use a list of product profile keys."}
                )
        else:
            profile_keys = ["idta-product"]
        templates = import_submodel_templates(
            company=request.user.company,
            user=request.user,
            file_name=upload.name,
            payload=payload,
            version=str(request.data.get("version") or "").strip() or None,
            profile_keys=profile_keys,
        )
        return Response(
            AASSubmodelTemplateSerializer(templates, many=True).data,
            status=status.HTTP_201_CREATED,
        )


class AASOfficialSubmodelCatalogView(APIView):
    def get(self, request):
        _require_enabled()
        _require_aas_access(request)
        company = getattr(request.user, "company", None)
        if company is None:
            raise PermissionDenied()
        return Response(
            {
                "features": {
                    "can_write": has_aas_models_permission(
                        request.user,
                        write=True,
                    )
                },
                "results": official_template_catalog(company),
            }
        )


class AASOfficialSubmodelImportView(APIView):
    def post(self, request, template_key):
        _require_enabled()
        _require_aas_access(request, write=True)
        company = getattr(request.user, "company", None)
        if company is None:
            raise PermissionDenied()
        template, created = import_official_template(
            company=company,
            user=request.user,
            key=template_key,
        )
        template = AASSubmodelTemplate.objects.prefetch_related(
            "field_mappings",
            "supplementary_files",
        ).get(id=template.id)
        return Response(
            {
                "created": created,
                "template": AASSubmodelTemplateSerializer(template).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class AASSubmodelTemplateView(APIView):
    def get(self, request, template_id):
        return Response(
            AASSubmodelTemplateSerializer(
                _submodel_template(request, template_id)
            ).data
        )

    @transaction.atomic
    def patch(self, request, template_id):
        template = _submodel_template(request, template_id, write=True)
        serializer = AASSubmodelTemplateSerializer(
            template,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        validate_template(template)
        template.refresh_from_db()
        return Response(AASSubmodelTemplateSerializer(template).data)

    def delete(self, request, template_id):
        template = _submodel_template(request, template_id, write=True)
        if template.status not in {
            AASSubmodelTemplate.Status.DRAFT,
            AASSubmodelTemplate.Status.INVALID,
        }:
            raise ValidationError(
                "Active and deprecated template versions are retained for audit history."
            )
        for item in template.supplementary_files.all():
            item.file.delete(save=False)
        template.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AASSubmodelTemplateValidateView(APIView):
    def post(self, request, template_id):
        template = _submodel_template(request, template_id, write=True)
        report = validate_template(template)
        return Response(report)


class AASSubmodelTemplateActivateView(APIView):
    def post(self, request, template_id):
        template = _submodel_template(request, template_id, write=True)
        result = activate_template(template, user=request.user)
        if not result["activated"]:
            return Response(
                {"validation_report": result["validation_report"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "template": AASSubmodelTemplateSerializer(result["template"]).data,
                "deprecated_versions": result["deprecated_versions"],
                "migrated_assignments": result["migrated_assignments"],
                "out_of_date_resources": result["out_of_date_resources"],
            }
        )


class AASSubmodelTemplateDeprecateView(APIView):
    def post(self, request, template_id):
        template = _submodel_template(request, template_id, write=True)
        out_of_date_resources = deprecate_template(template)
        template.refresh_from_db()
        return Response(
            {
                "template": AASSubmodelTemplateSerializer(template).data,
                "out_of_date_resources": out_of_date_resources,
            }
        )


def _authorize_resource(request, resource: AASResourceIdentity, *, write=False):
    if resource.project_id:
        return _project(request, resource.project_id, write=write)
    if resource.company_product_id:
        return _product(request, resource.company_product_id, write=write)
    if resource.company_assembly_id:
        return _assembly(request, resource.company_assembly_id, write=write)
    if resource.project_assembly_id:
        return _project_assembly(request, resource.project_assembly_id, write=write)
    if resource.festo_handover_id:
        return _project(request, resource.festo_handover.project_id, write=write)
    raise Http404


class AASXQueryRenderer(JSONRenderer):
    """Let DRF accept the documented ``?format=aasx`` download query."""

    format = "aasx"


class ProjectAASView(APIView):
    def get(self, request, project_id):
        _require_enabled()
        project = _project(request, project_id)
        try:
            _project(request, project_id, write=True)
            can_write = True
        except (PermissionDenied, Http404):
            can_write = False
        return Response(
            _summary_payload(
                getattr(project, "aas_identity", None),
                can_write=can_write,
                advanced_editor=_can_open_advanced_editor(request),
                festo_handover=True,
            )
        )


class ProjectAASModelView(APIView):
    def get(self, request, project_id):
        _require_enabled()
        project = _project(request, project_id)
        identity = getattr(project, "aas_identity", None)
        aas_id = identity.logical_aas_id if identity else "https://aas.sharemundolinks.com/ids/pending-project"
        document = ProjectBOMAASMapper().map(project, aas_id)
        focus = {
            "assembly_id": None,
            "bom_part_id": None,
            "id_shorts": [],
            "cleared": False,
        }

        def focused_descendants(rows, root_ids):
            children_by_parent = {}
            for row in rows:
                children_by_parent.setdefault(row.parent_id, []).append(row.id)
            focused_ids = set(root_ids)
            pending = list(focused_ids)
            while pending:
                for child_id in children_by_parent.get(pending.pop(), []):
                    if child_id not in focused_ids:
                        focused_ids.add(child_id)
                        pending.append(child_id)
            return focused_ids

        requested_bom_part_id = request.query_params.get("bom_part_id")
        requested_assembly_id = request.query_params.get("assembly_id")
        if requested_bom_part_id:
            try:
                bom_part_id = int(requested_bom_part_id)
            except (TypeError, ValueError):
                bom_part_id = None
            bom_part = (
                BillOfMaterialPart.objects.filter(id=bom_part_id, project=project)
                .only("id", "parent_id", "sort_order", "project_id")
                .first()
                if bom_part_id
                else None
            )
            if bom_part:
                rows = list(
                    BillOfMaterialPart.objects.filter(project=project)
                    .only("id", "parent_id", "sort_order", "project_id")
                    .order_by("level", "sort_order", "id")
                )
                focused_ids = focused_descendants(rows, {bom_part.id})
                focus = {
                    "assembly_id": None,
                    "bom_part_id": bom_part.id,
                    "id_shorts": [
                        f"Part_{row.get_sequence().replace('.', '_')}"
                        for row in rows
                        if row.id in focused_ids
                    ],
                    "cleared": False,
                }
            else:
                focus["cleared"] = True
        elif requested_assembly_id:
            try:
                assembly_id = int(requested_assembly_id)
            except (TypeError, ValueError):
                assembly_id = None
            assembly = (
                ProjectSolutionSpaceItem.objects.filter(
                    id=assembly_id,
                    project=project,
                )
                .select_related("source_company_item__aas_product")
                .first()
                if assembly_id
                else None
            )
            product_id = (
                assembly.source_company_item.aas_product_id
                if assembly and assembly.source_company_item_id
                else None
            )
            if product_id:
                rows = list(
                    BillOfMaterialPart.objects.filter(project=project)
                    .only("id", "parent_id", "sort_order", "project_id")
                    .order_by("level", "sort_order", "id")
                )
                focused_ids = focused_descendants(
                    rows,
                    BillOfMaterialPart.objects.filter(
                        project=project,
                        available_product_id=product_id,
                    ).values_list("id", flat=True),
                )
                focus = {
                    "assembly_id": assembly.id,
                    "bom_part_id": None,
                    "id_shorts": [
                        f"Part_{row.get_sequence().replace('.', '_')}"
                        for row in rows
                        if row.id in focused_ids
                    ],
                    "cleared": False,
                }
            else:
                # Invalid, inaccessible, or unlinked selections are deliberately
                # indistinguishable and never change the complete project model.
                focus["cleared"] = True
        return Response(
            {
                "environment": document.as_environment(),
                "validation": validation_report(document),
                "source_hash": document.hash,
                "focus": focus,
            }
        )


class ProjectAASValidateView(APIView):
    def post(self, request, project_id):
        _require_enabled()
        return Response(validate_project(_project(request, project_id)))


class ProjectAASPublishView(APIView):
    def post(self, request, project_id):
        _require_enabled()
        project = _project(request, project_id, write=True)
        identity = get_or_create_project_identity(project)
        event = enqueue_publication(identity, actor_id=request.user.id, immediate=True)
        if settings.AAS_PUBLICATION_RUN_SYNCHRONOUSLY:
            try:
                publication = publish_resource(identity.id, actor=request.user)
            except DjangoValidationError as exc:
                raise _translate_validation_error(exc) from exc
            return Response(AASPublicationSerializer(publication).data, status=status.HTTP_201_CREATED)
        process_aas_outbox_event.delay(str(event.id))
        return Response({"event_id": str(event.id), "status": "queued"}, status=status.HTTP_202_ACCEPTED)


class ProjectAASPublicationsView(APIView):
    def get(self, request, project_id):
        _require_enabled()
        project = _project(request, project_id)
        identity = getattr(project, "aas_identity", None)
        publications = (
            identity.publications.order_by(F("published_at").desc(nulls_last=True), "-created_at")
            if identity
            else AASPublication.objects.none()
        )
        active_publication = current_publication(identity) if identity else None
        return Response(
            AASPublicationSerializer(
                publications,
                many=True,
                context={"active_publication_id": active_publication.id if active_publication else None},
            ).data
        )


class ProductAASView(APIView):
    def get(self, request, product_id):
        _require_enabled()
        product = _product(request, product_id)
        can_write = _can_write_product_aas(request, product)
        payload = _summary_payload(
                getattr(product, "aas_identity", None),
                can_write=can_write,
                advanced_editor=_can_open_advanced_editor(request),
            )
        profile = synchronize_product_aas_profile(product)
        payload["profile"] = ProductAASProfileSerializer(
            profile,
            context={"request": request, "can_write": can_write},
        ).data
        return Response(payload)


class ProductAASProfileView(APIView):
    def get(self, request, product_id):
        _require_enabled()
        product = _product(request, product_id)
        profile = synchronize_product_aas_profile(product)
        return Response(
            ProductAASProfileSerializer(
                profile,
                context={"request": request, "can_write": _can_write_product_aas(request, product)},
            ).data
        )

    @transaction.atomic
    def patch(self, request, product_id):
        _require_enabled()
        product = _product(request, product_id, write=True)
        profile = synchronize_product_aas_profile(product)
        serializer = ProductAASProfileSerializer(
            profile,
            data=request.data,
            partial=True,
            context={"request": request, "can_write": True},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ProductOriginClassificationView(APIView):
    @transaction.atomic
    def post(self, request):
        _require_enabled()
        _require_aas_access(request, write=True)
        serializer = ProductOriginClassificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        products = list(
            AvailableProductPart.objects.filter(
                id__in=serializer.validated_data["product_ids"],
                company_profile_id=request.user.company_id,
            ).select_related("aas_profile")
        )
        if len(products) != len(set(serializer.validated_data["product_ids"])):
            raise PermissionDenied()
        for product in products:
            _product(request, product.id, write=True)
            profile = synchronize_product_aas_profile(product)
            profile.origin = serializer.validated_data["origin"]
            profile.save(update_fields=["origin", "updated_at"])
            mark_product_out_of_date(product, request.user.id)
        return Response(
            {
                "updated": len(products),
                "origin": serializer.validated_data["origin"],
            }
        )


class ProductAASListView(APIView):
    def get(self, request):
        _require_enabled()
        _require_aas_access(request)
        permission = AvailableProductPartPermission(action="view", scope="company")
        permission_request = SimpleNamespace(user=request.user, method="GET", query_params=request.query_params, data={})
        if not permission.has_permission(permission_request, SimpleNamespace(action="list")):
            raise PermissionDenied()
        company_id = getattr(request.user, "company_id", None)
        if not company_id:
            return Response({"count": 0, "results": []})
        try:
            limit = min(max(int(request.query_params.get("limit", 100)), 1), 500)
        except (TypeError, ValueError):
            raise ValidationError({"limit": "Use an integer from 1 to 500."})
        products = (
            AvailableProductPart.objects.filter(company_profile_id=company_id)
            .select_related("aas_identity", "aas_profile")
            .order_by("sort_order", "id")[:limit]
        )
        results = []
        advanced_editor = _can_open_advanced_editor(request)
        field_contract = product_aas_field_contract()
        for product in products:
            profile = synchronize_product_aas_profile(product)
            can_write = _can_write_product_aas(request, product)
            results.append(
                {
                    "id": product.id,
                    "name": product.name,
                    "part_number": product.part_number,
                    "supplier": profile.supplier_name or (product.custom_fields or {}).get("supplier", ""),
                    "origin": profile.origin,
                    "aas_eligible": profile.origin != ProductAASProfile.Origin.UNCLASSIFIED,
                    "editable_aas_fields": bool(
                        can_write and profile.origin == ProductAASProfile.Origin.MANUFACTURED
                    ),
                    "field_contract": field_contract,
                    "aas": _summary_payload(
                        getattr(product, "aas_identity", None),
                        can_write=can_write,
                        advanced_editor=advanced_editor,
                    ),
                }
            )
        return Response({"count": AvailableProductPart.objects.filter(company_profile_id=company_id).count(), "results": results})


def _assembly_aas_row(request, assembly: CompanySolutionSpaceItem) -> dict:
    product = assembly.aas_product
    profile = None
    if product:
        profile = synchronize_product_aas_profile(product)
    try:
        _assembly(request, assembly.id, write=True)
        can_write = True
    except (PermissionDenied, Http404):
        can_write = False
    fields = assembly.custom_fields or {}
    origin = (
        profile.origin
        if profile
        else infer_aas_origin_from_values(
            company_name=getattr(assembly.company_profile, "company_name", ""),
            manufacturer=fields.get("manufacturer"),
            supplier=fields.get("supplier"),
            supplier_aas_id=fields.get("supplier_aas_id") or fields.get("aas_id"),
        )
    )
    return {
        "selection_key": f"assembly:{assembly.id}",
        "assembly_id": assembly.id,
        "product_id": product.id if product else None,
        "name": assembly.assembly_name,
        "part_number": product.part_number or "" if product else "",
        "origin": origin,
        "aas_eligible": True,
        "editable_aas_fields": False,
        "aas": _summary_payload(
            getattr(assembly, "aas_identity", None),
            can_write=can_write,
            advanced_editor=_can_open_advanced_editor(request),
        ),
    }


class CompanyAssemblyAASListView(APIView):
    def get(self, request):
        _require_enabled()
        _require_aas_access(request)
        company_id = getattr(request.user, "company_id", None)
        if not company_id:
            return Response({"count": 0, "results": []})
        assemblies = list(
            CompanySolutionSpaceItem.objects.filter(company_profile_id=company_id)
            .select_related(
                "aas_product__aas_profile",
                "aas_product__aas_identity",
                "aas_identity",
            )
            .order_by("sort_order", "id")
        )
        rows = [_assembly_aas_row(request, assembly) for assembly in assemblies]
        return Response({"count": len(rows), "results": rows})


class CompanyAssemblyAASInitializeView(APIView):
    @transaction.atomic
    def post(self, request, assembly_id):
        _require_enabled()
        permitted = _assembly(request, assembly_id, write=True)
        assembly = CompanySolutionSpaceItem.objects.select_for_update().get(
            id=permitted.id
        )
        requested_origin = str(request.data.get("origin") or "").strip().lower()
        if requested_origin and requested_origin not in {
            ProductAASProfile.Origin.MANUFACTURED,
            ProductAASProfile.Origin.PURCHASED,
        }:
            raise ValidationError(
                {"origin": "Choose manufactured or purchased."}
            )
        if assembly.aas_product_id:
            product = assembly.aas_product
        else:
            fields = dict(assembly.custom_fields or {})
            product = AvailableProductPart.objects.create(
                company_profile=assembly.company_profile,
                name=assembly.assembly_name,
                part_number=str(
                    fields.get("article_number")
                    or fields.get("part_number")
                    or ""
                ).strip()
                or None,
                custom_fields=fields,
                sort_order=assembly.sort_order,
                level=max(1, assembly.level),
                created_by=request.user,
                modified_by=request.user,
            )
            assembly.aas_product = product
            assembly.save(update_fields=["aas_product", "updated_at"])
        profile = synchronize_product_aas_profile(product)
        if requested_origin:
            profile.origin = requested_origin
        if profile.origin == ProductAASProfile.Origin.UNCLASSIFIED:
            raise ValidationError(
                {
                    "origin": (
                        "Manufacturer or supplier evidence is missing. Choose "
                        "manufactured or purchased."
                    )
                }
            )
        fields = product.custom_fields or {}
        if profile.origin == ProductAASProfile.Origin.PURCHASED:
            profile.manufacturer_name = str(
                fields.get("manufacturer") or fields.get("supplier") or ""
            ).strip()
            profile.supplier_name = str(fields.get("supplier") or "").strip()
            profile.supplier_aas_id = str(
                fields.get("supplier_aas_id") or fields.get("aas_id") or ""
            ).strip()
            profile.global_asset_id = str(
                fields.get("global_asset_id") or fields.get("asset_id") or ""
            ).strip()
            profile.order_code = str(
                fields.get("order_code")
                or fields.get("article_number")
                or product.part_number
                or ""
            ).strip()
        profile.save(
            update_fields=[
                "origin",
                "manufacturer_name",
                "supplier_name",
                "supplier_aas_id",
                "global_asset_id",
                "order_code",
                "updated_at",
            ]
        )
        assembly.aas_product = product
        return Response(
            {
                **_assembly_aas_row(request, assembly),
                "profile": ProductAASProfileSerializer(
                    profile,
                    context={"request": request, "can_write": True},
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


def _assembly_model_response(assembly) -> Response:
    identity = getattr(assembly, "aas_identity", None)
    pending_suffix = (
        "pending-project-assembly"
        if isinstance(assembly, ProjectSolutionSpaceItem)
        else "pending-company-assembly"
    )
    aas_id = (
        identity.logical_aas_id
        if identity
        else f"https://aas.sharemundolinks.com/ids/{pending_suffix}"
    )
    document = SolutionSpaceAASMapper().map(assembly, aas_id)
    return Response(
        {
            "environment": document.as_environment(),
            "validation": validation_report(document),
            "source_hash": document.hash,
        }
    )


def _assembly_publications_response(assembly) -> Response:
    identity = getattr(assembly, "aas_identity", None)
    publications = (
        identity.publications.order_by(
            F("published_at").desc(nulls_last=True),
            "-created_at",
        )
        if identity
        else AASPublication.objects.none()
    )
    active_publication = current_publication(identity) if identity else None
    return Response(
        AASPublicationSerializer(
            publications,
            many=True,
            context={
                "active_publication_id": (
                    active_publication.id if active_publication else None
                )
            },
        ).data
    )


def _publish_assembly(request, identity) -> Response:
    event = enqueue_publication(
        identity,
        actor_id=request.user.id,
        immediate=True,
    )
    if settings.AAS_PUBLICATION_RUN_SYNCHRONOUSLY:
        try:
            publication = publish_resource(identity.id, actor=request.user)
        except DjangoValidationError as exc:
            raise _translate_validation_error(exc) from exc
        return Response(
            AASPublicationSerializer(publication).data,
            status=status.HTTP_201_CREATED,
        )
    process_aas_outbox_event.delay(str(event.id))
    return Response(
        {"event_id": str(event.id), "status": "queued"},
        status=status.HTTP_202_ACCEPTED,
    )


class CompanyAssemblyAASView(APIView):
    def get(self, request, assembly_id):
        _require_enabled()
        assembly = _assembly(request, assembly_id)
        try:
            _assembly(request, assembly_id, write=True)
            can_write = True
        except (PermissionDenied, Http404):
            can_write = False
        return Response(
            _summary_payload(
                getattr(assembly, "aas_identity", None),
                can_write=can_write,
                advanced_editor=_can_open_advanced_editor(request),
            )
        )


class CompanyAssemblyAASModelView(APIView):
    def get(self, request, assembly_id):
        _require_enabled()
        return _assembly_model_response(_assembly(request, assembly_id))


class CompanyAssemblyAASValidateView(APIView):
    def post(self, request, assembly_id):
        _require_enabled()
        return Response(validate_company_assembly(_assembly(request, assembly_id)))


class CompanyAssemblyAASPublishView(APIView):
    def post(self, request, assembly_id):
        _require_enabled()
        assembly = _assembly(request, assembly_id, write=True)
        return _publish_assembly(
            request,
            get_or_create_company_assembly_identity(assembly),
        )


class CompanyAssemblyAASPublicationsView(APIView):
    def get(self, request, assembly_id):
        _require_enabled()
        return _assembly_publications_response(_assembly(request, assembly_id))


class ProjectAssemblyAASView(APIView):
    def get(self, request, assembly_id):
        _require_enabled()
        assembly = _project_assembly(request, assembly_id)
        try:
            _project_assembly(request, assembly_id, write=True)
            can_write = True
        except (PermissionDenied, Http404):
            can_write = False
        return Response(
            _summary_payload(
                getattr(assembly, "aas_identity", None),
                can_write=can_write,
                advanced_editor=_can_open_advanced_editor(request),
            )
        )


class ProjectAssemblyAASModelView(APIView):
    def get(self, request, assembly_id):
        _require_enabled()
        return _assembly_model_response(_project_assembly(request, assembly_id))


class ProjectAssemblyAASValidateView(APIView):
    def post(self, request, assembly_id):
        _require_enabled()
        return Response(
            validate_project_assembly(_project_assembly(request, assembly_id))
        )


class ProjectAssemblyAASPublishView(APIView):
    def post(self, request, assembly_id):
        _require_enabled()
        assembly = _project_assembly(request, assembly_id, write=True)
        return _publish_assembly(
            request,
            get_or_create_project_assembly_identity(assembly),
        )


class ProjectAssemblyAASPublicationsView(APIView):
    def get(self, request, assembly_id):
        _require_enabled()
        return _assembly_publications_response(
            _project_assembly(request, assembly_id)
        )


class ProductAASModelView(APIView):
    def get(self, request, product_id):
        _require_enabled()
        product = _product(request, product_id)
        identity = getattr(product, "aas_identity", None)
        aas_id = identity.logical_aas_id if identity else "https://aas.sharemundolinks.com/ids/pending-product"
        document = ProductAASMapper().map(product, aas_id)
        return Response(
            {
                "environment": document.as_environment(),
                "validation": validation_report(document),
                "source_hash": document.hash,
            }
        )


class ProductAASPublicationsView(APIView):
    def get(self, request, product_id):
        _require_enabled()
        product = _product(request, product_id)
        identity = getattr(product, "aas_identity", None)
        publications = (
            identity.publications.order_by(F("published_at").desc(nulls_last=True), "-created_at")
            if identity
            else AASPublication.objects.none()
        )
        active_publication = current_publication(identity) if identity else None
        return Response(
            AASPublicationSerializer(
                publications,
                many=True,
                context={"active_publication_id": active_publication.id if active_publication else None},
            ).data
        )


class ProductAASValidateView(APIView):
    def post(self, request, product_id):
        _require_enabled()
        return Response(validate_product(_product(request, product_id)))


class ProductAASPublishView(APIView):
    def post(self, request, product_id):
        _require_enabled()
        product = _product(request, product_id, write=True)
        identity = get_or_create_product_identity(product)
        event = enqueue_publication(identity, actor_id=request.user.id, immediate=True)
        if settings.AAS_PUBLICATION_RUN_SYNCHRONOUSLY:
            try:
                publication = publish_resource(identity.id, actor=request.user)
            except DjangoValidationError as exc:
                raise _translate_validation_error(exc) from exc
            return Response(AASPublicationSerializer(publication).data, status=status.HTTP_201_CREATED)
        process_aas_outbox_event.delay(str(event.id))
        return Response({"event_id": str(event.id), "status": "queued"}, status=status.HTTP_202_ACCEPTED)


class PublicationView(APIView):
    def get(self, request, publication_id):
        _require_enabled()
        publication = get_object_or_404(
            AASPublication.objects.select_related(
                "resource__project",
                "resource__company_product",
                "resource__company_assembly",
                "resource__project_assembly__project",
                "resource__festo_handover__project",
            ),
            id=publication_id,
        )
        _authorize_resource(request, publication.resource)
        return Response(AASPublicationSerializer(publication).data)


class PublicationDownloadView(APIView):
    renderer_classes = [JSONRenderer, AASXQueryRenderer]

    def get(self, request, publication_id):
        _require_enabled()
        publication = get_object_or_404(
            AASPublication.objects.select_related(
                "resource__project",
                "resource__company_product",
                "resource__company_assembly",
                "resource__project_assembly__project",
                "resource__festo_handover__project",
            ),
            id=publication_id,
            status=AASPublication.Status.CURRENT,
        )
        _authorize_resource(request, publication.resource)
        output_format = request.query_params.get("format", "json").lower()
        if output_format not in {"json", "aasx"}:
            raise ValidationError({"format": "Use json or aasx."})
        key = publication.json_artifact_key if output_format == "json" else publication.aasx_artifact_key
        if not key:
            raise Http404
        return redirect(DjangoArtifactStore().download_url(key, expires_seconds=300))


class AASXDocumentPreviewView(APIView):
    def get(self, request, document_id):
        _require_enabled()
        document = get_object_or_404(
            get_accessible_document_queryset(request).select_related("folder"),
            id=document_id,
        )
        cached = get_cached_aasx_preview(document)
        if cached:
            response_status = (
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if cached.get("status") == "failed"
                else status.HTTP_200_OK
            )
            return Response(cached, status=response_status)
        if not document.file:
            return Response(
                {"status": "failed", "detail": "The document has no file attached."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        try:
            file_size = document.file.size
        except (OSError, ValueError):
            return Response(
                {"status": "failed", "detail": "The document file is unavailable."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if file_size > settings.AASX_PREVIEW_SYNC_THRESHOLD_BYTES:
            processing = {
                "status": "processing",
                "document_id": document.id,
                "file_name": (document.file_name or document.file.name).rsplit("/", 1)[-1],
            }
            set_aasx_preview_state(document, processing)
            parse_aasx_document_preview.delay(document.id)
            return Response(processing, status=status.HTTP_202_ACCEPTED)
        result = parse_and_cache_document_aasx(document)
        response_status = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if result.get("status") == "failed"
            else status.HTTP_200_OK
        )
        return Response(result, status=response_status)


class FestoPreviewView(APIView):
    def post(self, request, project_id):
        _require_enabled("FESTO_HANDOVER_ENABLED")
        project = _project(request, project_id, write=True)
        try:
            raw_item_ids = request.data.get("item_ids")
            selected_ids = None
            if raw_item_ids is not None:
                if not isinstance(raw_item_ids, list) or not raw_item_ids:
                    raise ValidationError({"item_ids": "Provide a non-empty list of BOM item IDs."})
                try:
                    selected_ids = {int(item_id) for item_id in raw_item_ids}
                except (TypeError, ValueError) as exc:
                    raise ValidationError({"item_ids": "Every item ID must be an integer."}) from exc
            handover, token, excluded = create_festo_preview(
                project,
                request.user,
                selected_ids=selected_ids,
            )
        except DjangoValidationError as exc:
            raise _translate_validation_error(exc) from exc
        return Response({"handover": FestoHandoverSerializer(handover).data, "confirmation_token": token, "excluded_items": excluded})


class FestoConfirmView(APIView):
    def post(self, request, handover_id):
        _require_enabled("FESTO_HANDOVER_ENABLED")
        handover = get_object_or_404(FestoHandover.objects.select_related("project"), id=handover_id)
        _project(request, handover.project_id, write=True)
        token = request.data.get("confirmation_token")
        if not token:
            raise ValidationError({"confirmation_token": "This field is required."})
        try:
            confirmed = confirm_festo_handover(handover.id, token, request.user)
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except DjangoValidationError as exc:
            raise _translate_validation_error(exc) from exc
        return Response(FestoHandoverSerializer(confirmed).data)


class FestoHandoverView(APIView):
    def get(self, request, handover_id):
        _require_enabled("FESTO_HANDOVER_ENABLED")
        handover = get_object_or_404(FestoHandover.objects.select_related("project", "publication"), id=handover_id)
        _project(request, handover.project_id)
        return Response(FestoHandoverSerializer(handover).data)


class FestoLaunchedView(APIView):
    def post(self, request, handover_id):
        _require_enabled("FESTO_HANDOVER_ENABLED")
        handover = get_object_or_404(
            FestoHandover.objects.select_related("project", "publication"),
            id=handover_id,
        )
        _project(request, handover.project_id, write=True)
        try:
            launched = mark_festo_handover_launched(handover)
        except DjangoValidationError as exc:
            raise _translate_validation_error(exc) from exc
        return Response(FestoHandoverSerializer(launched).data)


class ExternalGrantRevokeView(APIView):
    def post(self, request, grant_id):
        _require_enabled()
        grant = get_object_or_404(
            ExternalAASGrant.objects.select_related(
                "resource__project",
                "resource__company_product",
                "resource__company_assembly",
                "resource__project_assembly__project",
                "resource__festo_handover__project",
            ),
            id=grant_id,
        )
        _authorize_resource(request, grant.resource, write=True)
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class InternalGrantListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        configured_key = settings.AAS_INTERNAL_SERVICE_KEY
        provided_key = request.headers.get("X-AAS-Service-Key", "")
        if not configured_key or not hmac.compare_digest(provided_key, configured_key):
            raise PermissionDenied()
        if not settings.AAS_ENABLED or not settings.AAS_EXTERNAL_API_ENABLED:
            return Response({"aas_ids": []})
        subject = request.headers.get("X-External-Subject", "")
        oauth_client_id = request.headers.get("X-External-Client-ID", "")
        fingerprint = re.sub(
            r"[^0-9a-f]",
            "",
            request.headers.get("X-External-Certificate-Fingerprint", "").lower(),
        )
        client = None
        if oauth_client_id:
            client = ExternalAASClient.objects.filter(
                oauth_client_id=oauth_client_id,
                active=True,
            ).first()
        elif len(fingerprint) == 64:
            client = ExternalAASClient.objects.filter(
                certificate_fingerprint=fingerprint,
                active=True,
            ).first()
        elif subject:
            client = ExternalAASClient.objects.filter(
                auth0_subject=subject,
                oauth_client_id__isnull=True,
                active=True,
            ).first()
        if not client or "read" not in client.allowed_operations:
            return Response({"aas_ids": []})
        now = timezone.now()
        grants = ExternalAASGrant.objects.filter(
            client=client,
            can_read=True,
            revoked_at__isnull=True,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        identifiers = []
        for grant in grants.select_related("resource", "publication").distinct():
            revision_id = _revision_aas_id(grant)
            identifiers.append(revision_id)
            identifiers.extend(
                identifier
                for identifier in _resource_publication_identifiers(
                    grant.resource,
                    grant.publication,
                )
                if identifier != revision_id
            )
        return Response({"aas_ids": sorted(set(identifiers))})


class EditorSessionView(APIView):
    def post(self, request):
        _require_enabled("AAS_ADVANCED_EDITOR_ENABLED")
        if not settings.AAS_EDITOR_SESSION_KEY or not settings.AAS_EDITOR_BASE_URL:
            return Response(
                {"detail": "The advanced AAS viewer is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        _require_aas_access(request)
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": str(request.user.id),
                "company_id": str(request.user.company_id),
                "purpose": "aas-editor-launch",
                "aud": "sharemundo-aas-editor",
                "iat": now,
                "exp": now + settings.AAS_EDITOR_LAUNCH_TTL_SECONDS,
            },
            settings.AAS_EDITOR_SESSION_KEY,
            algorithm="HS256",
        )
        return Response(
            {
                "editor_url": f"{settings.AAS_EDITOR_BASE_URL.rstrip('/')}/editor/session",
                "launch_token": token,
                "expires_in": settings.AAS_EDITOR_LAUNCH_TTL_SECONDS,
            }
        )


class InternalEditorGrantListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        configured_key = settings.AAS_INTERNAL_SERVICE_KEY
        provided_key = request.headers.get("X-AAS-Service-Key", "")
        if not configured_key or not hmac.compare_digest(provided_key, configured_key):
            raise PermissionDenied()
        if not settings.AAS_ENABLED or not settings.AAS_ADVANCED_EDITOR_ENABLED:
            return Response({"aas_ids": []})

        user_id = request.headers.get("X-AAS-Editor-User-ID", "")
        company_id = request.headers.get("X-AAS-Editor-Company-ID", "")
        User = get_user_model()
        user = User.objects.filter(id=user_id, company_id=company_id, is_active=True).first()
        if not user or not has_aas_models_permission(user):
            return Response({"aas_ids": []})

        current_publications = Prefetch(
            "publications",
            queryset=AASPublication.objects.filter(status=AASPublication.Status.CURRENT).order_by("-published_at"),
            to_attr="editor_current_publications",
        )
        resources = AASResourceIdentity.objects.filter(
            company_id=company_id,
            active=True,
        ).prefetch_related(current_publications)
        identifiers = []
        for resource in resources:
            if not resource.editor_current_publications:
                continue
            publication = resource.editor_current_publications[0]
            identifiers.extend(_resource_publication_identifiers(resource, publication))
        return Response({"aas_ids": sorted(set(identifiers))})


def _revision_aas_id(grant: ExternalAASGrant) -> str:
    return f"{grant.resource.logical_aas_id}/revisions/{grant.publication.revision}"


def _resource_publication_identifiers(resource: AASResourceIdentity, publication: AASPublication) -> list[str]:
    if publication.published_identifiers:
        return sorted(set(publication.published_identifiers))
    revision_id = f"{resource.logical_aas_id}/revisions/{publication.revision}"
    if resource.source_type == AASResourceIdentity.SourceType.PROJECT:
        suffixes = ["/submodels/hierarchical-bom", "/submodels/project-metadata"]
    elif resource.source_type == AASResourceIdentity.SourceType.FESTO_HANDOVER:
        suffixes = ["/submodels/hierarchical-bom", "/submodels/handover-metadata"]
    elif resource.source_type in {
        AASResourceIdentity.SourceType.COMPANY_ASSEMBLY,
        AASResourceIdentity.SourceType.PROJECT_ASSEMBLY,
    }:
        suffixes = [
            "/submodels/hierarchical-structures",
            "/submodels/assembly-metadata",
        ]
    else:
        suffixes = ["/submodels/nameplate", "/submodels/technical-data"]
    return [revision_id, *(f"{revision_id}{suffix}" for suffix in suffixes)]
