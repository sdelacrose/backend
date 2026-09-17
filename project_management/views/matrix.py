import os
from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.aggregates import StringAgg
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.http import Http404
from django.db import transaction
from django.db.models import (
    Case,
    Count,
    DateField,
    DecimalField,
    F,
    Max,
    OuterRef,
    Q,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Concat, Lower, NullIf, Trim
from django.shortcuts import get_object_or_404
from rest_framework import filters, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from django.utils import timezone

from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.ordering import TableOrderingFilter
from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    CompanyAssemblyProductionLink,
    ContactCompany,
    Equipment,
    EquipmentConfig,
    Machine,
    MachineConfig,
    MatrixComment,
    MatrixCommentReaction,
    PartsListConfig,
    Project,
    ProjectMatrixConfig,
    ProjectSolutionSpaceItem,
    Requirement,
    SolutionDemand,
    SolutionSpaceConfig,
    Transport,
)
from project_management.matrix_comment_mentions import get_matrix_mention_candidates
from project_management.models.constants import COLLABORATIVE_SOLUTION
from project_management.permissions import (
    AvailableProductPartPermission,
    BillOfMaterialPermission,
    CompanySolutionSpacePermission,
    EquipmentPermission,
    MachinePermission,
    RequirementPermission,
    ProjectSolutionSpacePermission,
    SolutionDemandPermission,
    TransportPermission,
)
from project_management.serializers.matrix import (
    CompanySolutionSpaceItemSerializer,
    MatrixCommentSerializer,
    MatrixCommentThreadSerializer,
    EquipmentConfigSerializer,
    EquipmentSerializer,
    MachineConfigSerializer,
    MachineSerializer,
    PartsListConfigSerializer,
    ProjectMatrixConfigSerializer,
    ProjectSolutionSpaceItemSerializer,
    AvailableProductPartSerializer,
    BillOfMaterialPartSerializer,
    RequirementSerializer,
    SolutionDemandSerializer,
    SolutionSpaceConfigSerializer,
    TransportSerializer,
    update_descendant_levels,
)
from project_management.serializers.supplier_product_import import (
    SupplierProductImportSerializer,
)
from project_management.services.supplier_product_import import (
    import_supplier_product_to_company_catalog,
)
from project_management.utils import get_product_name_from_request


def is_truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class ConfiguredJSONOrderingFilter(TableOrderingFilter):
    alias_prefix = "configured"

    @staticmethod
    def _configured_columns(view):
        cached = getattr(view, "_ordering_configured_column_map", None)
        if cached is not None:
            return cached
        getter = getattr(view, "get_ordering_configured_columns", None)
        if not getter:
            return {}
        columns = {
            str(column.get("key") or "").strip(): column
            for column in getter()
            if isinstance(column, dict) and str(column.get("key") or "").strip()
        }
        view._ordering_configured_column_map = columns
        return columns

    def get_valid_fields(self, queryset, view, context=None):
        fields = list(super().get_valid_fields(queryset, view, context))
        known = {field for field, _label in fields}
        fields.extend(
            (field, field)
            for field in sorted(set(self._configured_columns(view)) - known)
        )
        return fields

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset

        configured_columns = self._configured_columns(view)
        aliases = dict(getattr(view, "ordering_field_aliases", {}))
        text_fields = set(getattr(view, "ordering_text_fields", ()))
        natural_fields = set(getattr(view, "ordering_natural_fields", ()))

        for index, requested_field in enumerate(ordering):
            name = requested_field.lstrip("-")
            column = configured_columns.get(name)
            if not column:
                continue

            raw_alias = f"_{self.alias_prefix}_raw_order_{index}"
            value_alias = f"_{self.alias_prefix}_value_order_{index}"
            queryset = queryset.alias(
                **{raw_alias: KeyTextTransform(name, "custom_fields")}
            )
            column_type = str(column.get("type") or "text").lower()

            if column_type == "number":
                queryset = queryset.alias(
                    **{
                        value_alias: Case(
                            When(
                                **{
                                    f"{raw_alias}__regex": (
                                        r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*$"
                                    )
                                },
                                then=Cast(
                                    Trim(F(raw_alias)),
                                    output_field=DecimalField(
                                        max_digits=38,
                                        decimal_places=10,
                                    ),
                                ),
                            ),
                            default=Value(None),
                            output_field=DecimalField(
                                max_digits=38,
                                decimal_places=10,
                            ),
                        )
                    }
                )
            elif column_type == "date":
                queryset = queryset.alias(
                    **{
                        value_alias: Case(
                            When(
                                **{f"{raw_alias}__regex": r"^\d{4}-\d{2}-\d{2}$"},
                                then=Cast(F(raw_alias), output_field=DateField()),
                            ),
                            default=Value(None),
                            output_field=DateField(),
                        )
                    }
                )
            else:
                value_alias = raw_alias
                text_fields.add(name)
                natural_fields.add(name)

            aliases[name] = value_alias

        view.ordering_field_aliases = aliases
        view.ordering_text_fields = text_fields
        view.ordering_natural_fields = natural_fields
        return super().filter_queryset(request, queryset, view)


class AvailableProductOrderingFilter(ConfiguredJSONOrderingFilter):
    alias_prefix = "available_product"

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if ordering and any(
            field.lstrip("-") == "linked_documents" for field in ordering
        ):
            queryset = queryset.alias(
                _available_product_document_count=Count("documents", distinct=True)
            )
            view.ordering_field_aliases = {
                **dict(getattr(view, "ordering_field_aliases", {})),
                "linked_documents": "_available_product_document_count",
            }
        return super().filter_queryset(request, queryset, view)


class SolutionSpaceOrderingFilter(ConfiguredJSONOrderingFilter):
    alias_prefix = "solution_space"

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset

        aliases = dict(getattr(view, "ordering_field_aliases", {}))
        text_fields = set(getattr(view, "ordering_text_fields", ()))
        natural_fields = set(getattr(view, "ordering_natural_fields", ()))

        for index, requested_field in enumerate(ordering):
            name = requested_field.lstrip("-")
            alias = f"_solution_space_system_order_{index}"

            if name in {"required_skills", "provided_skills"}:
                queryset = queryset.alias(
                    **{alias: KeyTextTransform("0", name)}
                )
            elif name == "responsible_engineer":
                queryset = queryset.alias(
                    **{
                        alias: Coalesce(
                            NullIf(
                                Trim(
                                    Concat(
                                        F("responsible_engineer__first_name"),
                                        Value(" "),
                                        F("responsible_engineer__last_name"),
                                    )
                                ),
                                Value(""),
                                output_field=TextField(),
                            ),
                            F("responsible_engineer__email"),
                            output_field=TextField(),
                        )
                    }
                )
            elif name == "projects" and queryset.model is CompanySolutionSpaceItem:
                first_project_name = (
                    Project.objects.filter(company_solution_space_items=OuterRef("pk"))
                    .exclude(project_name__in=["", "-"])
                    .order_by(Lower("project_name"), "id")
                    .values("project_name")[:1]
                )
                queryset = queryset.alias(
                    **{alias: Subquery(first_project_name, output_field=TextField())}
                )
            elif name == "customers":
                relation = (
                    "company_solution_space_items"
                    if queryset.model is CompanySolutionSpaceItem
                    else "project_solution_space_items"
                )
                first_customer_name = (
                    ContactCompany.objects.filter(**{relation: OuterRef("pk")})
                    .exclude(company_name__in=["", "-"])
                    .order_by(Lower("company_name"), "id")
                    .values("company_name")[:1]
                )
                queryset = queryset.alias(
                    **{alias: Subquery(first_customer_name, output_field=TextField())}
                )
            elif name == "linked_documents":
                queryset = queryset.alias(
                    **{alias: Count("documents", distinct=True)}
                )
            elif name == "related_assemblies":
                outer_item_field = (
                    "pk"
                    if queryset.model is CompanySolutionSpaceItem
                    else "source_company_item_id"
                )
                assembly_type_field = (
                    "assembly_type"
                    if queryset.model is CompanySolutionSpaceItem
                    else "source_company_item__assembly_type"
                )
                process_names = (
                    CompanyAssemblyProductionLink.objects.filter(
                        product_assembly_id=OuterRef(outer_item_field)
                    )
                    .order_by()
                    .values("product_assembly_id")
                    .annotate(
                        names=StringAgg(
                            "process_assembly__assembly_name",
                            delimiter=", ",
                            ordering=(
                                Lower("process_assembly__assembly_name"),
                                "process_assembly_id",
                            ),
                        )
                    )
                    .values("names")[:1]
                )
                product_names = (
                    CompanyAssemblyProductionLink.objects.filter(
                        process_assembly_id=OuterRef(outer_item_field)
                    )
                    .order_by()
                    .values("process_assembly_id")
                    .annotate(
                        names=StringAgg(
                            "product_assembly__assembly_name",
                            delimiter=", ",
                            ordering=(
                                Lower("product_assembly__assembly_name"),
                                "product_assembly_id",
                            ),
                        )
                    )
                    .values("names")[:1]
                )
                queryset = queryset.alias(
                    **{
                        alias: Case(
                            When(
                                **{
                                    assembly_type_field: CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
                                },
                                then=Subquery(
                                    process_names,
                                    output_field=TextField(),
                                ),
                            ),
                            When(
                                **{
                                    assembly_type_field: CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
                                },
                                then=Subquery(
                                    product_names,
                                    output_field=TextField(),
                                ),
                            ),
                            default=Value(None),
                            output_field=TextField(),
                        )
                    }
                )
            else:
                continue

            aliases[name] = alias
            if name != "linked_documents":
                text_fields.add(name)
                natural_fields.add(name)

        view.ordering_field_aliases = aliases
        view.ordering_text_fields = text_fields
        view.ordering_natural_fields = natural_fields
        return super().filter_queryset(request, queryset, view)


class TransportOrderingFilter(ConfiguredJSONOrderingFilter):
    alias_prefix = "transport"


class BillOfMaterialOrderingFilter(ConfiguredJSONOrderingFilter):
    alias_prefix = "bom"

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if ordering:
            requested_fields = {field.lstrip("-") for field in ordering}
            count_fields = {
                "linked_documents": ("documents", "_bom_document_count"),
                "linked_observations": (
                    "object_assessments",
                    "_bom_observation_count",
                ),
                "linked_requirements": ("requirements", "_bom_requirement_count"),
            }
            aliases = dict(getattr(view, "ordering_field_aliases", {}))
            for field, (relation, alias) in count_fields.items():
                if field not in requested_fields:
                    continue
                queryset = queryset.alias(**{alias: Count(relation, distinct=True)})
                aliases[field] = alias
            view.ordering_field_aliases = aliases
        return super().filter_queryset(request, queryset, view)


class ProjectMatrixConfigViewSet(viewsets.ModelViewSet):
    queryset = ProjectMatrixConfig.objects.all()
    serializer_class = ProjectMatrixConfigSerializer
    filter_backends = [filters.OrderingFilter]
    ordering = ["-id"]

    def _get_matrix_permission_class(self):
        matrix_type = (
            self.request.query_params.get("type")
            or self.request.data.get("type")
            or "demand"
        )
        matrix_type = str(matrix_type).strip().lower()
        return {
            "requirement": RequirementPermission,
            "transport": TransportPermission,
            "demand": SolutionDemandPermission,
        }.get(matrix_type, SolutionDemandPermission)

    def get_permissions(self):
        permission_class = self._get_matrix_permission_class()
        if self.action == "create":
            return [permission_class(action="create", scope="project")]
        if self.action in ["update", "partial_update"]:
            return [permission_class(action="update", scope="project")]
        if self.action == "destroy":
            return [permission_class(action="delete", scope="project")]
        return [permission_class(action="view", scope="project")]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ProjectMatrixConfig.objects.none()

        product_name = get_product_name_from_request(self.request)
        products = COLLABORATIVE_SOLUTION if product_name in COLLABORATIVE_SOLUTION else [product_name]
        filters_q = Q(project__solution__in=products)

        project_id = self.request.query_params.get("project")
        if project_id:
            filters_q &= Q(project_id=project_id)

        if user.is_freelancer:
            filters_q &= Q(project__team_members__team_member=user)
        else:
            filters_q &= (
                Q(project__company_profile=user.company) |
                Q(project__user__company=user.company) |
                Q(project__team_members__team_member=user)
            )

        return ProjectMatrixConfig.objects.filter(filters_q).distinct().order_by("-id")

    def list(self, request, *args, **kwargs):
        project_id = request.query_params.get("project")
        if project_id:
            project = get_object_or_404(Project, id=project_id)
            config, _ = ProjectMatrixConfig.objects.get_or_create(
                project=project,
                defaults={
                    "created_by": request.user if request.user.is_authenticated else None,
                    "modified_by": request.user if request.user.is_authenticated else None,
                },
            )
            self.check_object_permissions(request, config)
            return Response(self.get_serializer(config).data)
        return super().list(request, *args, **kwargs)


class PartsListConfigViewSet(viewsets.ModelViewSet):
    queryset = PartsListConfig.objects.all()
    serializer_class = PartsListConfigSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering = ["-id"]

    def _is_project_config_request(self):
        if self.request.query_params.get("project") or self.request.data.get("project"):
            return True
        config_id = self.kwargs.get("pk")
        if not config_id:
            return False
        return PartsListConfig.objects.filter(id=config_id, project__isnull=False).exists()

    def get_permissions(self):
        if self._is_project_config_request():
            if self.action == "create":
                return [BillOfMaterialPermission(action="create", scope="project")]
            if self.action in ["update", "partial_update"]:
                return [BillOfMaterialPermission(action="update", scope="project")]
            if self.action == "destroy":
                return [BillOfMaterialPermission(action="delete", scope="project")]
            return [BillOfMaterialPermission(action="view", scope="project")]

        if self.action == "create":
            return [AvailableProductPartPermission(action="create", scope="company")]
        if self.action in ["update", "partial_update"]:
            return [AvailableProductPartPermission(action="update", scope="company")]
        if self.action == "destroy":
            return [AvailableProductPartPermission(action="delete", scope="company")]
        return [AvailableProductPartPermission(action="view", scope="company")]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return PartsListConfig.objects.none()

        config_id = self.kwargs.get("pk")
        if (
            config_id
            and PartsListConfig.objects.filter(
                id=config_id,
                project__isnull=False,
            ).exists()
        ):
            return (
                PartsListConfig.objects.filter(id=config_id)
                .filter(self._project_access_filter(user))
                .distinct()
            )

        project_id = self.request.query_params.get("project")
        company_id = self.request.query_params.get("company_profile")
        query_filters = Q()
        if project_id:
            query_filters &= Q(project_id=project_id)
        elif company_id:
            if user.is_freelancer or str(company_id) != str(user.company_id):
                return PartsListConfig.objects.none()
            query_filters &= Q(company_profile_id=user.company_id, project__isnull=True)
        elif not user.is_freelancer and user.company_id:
            query_filters &= Q(company_profile_id=user.company_id, project__isnull=True)
        else:
            return PartsListConfig.objects.none()

        if project_id:
            query_filters &= self._project_access_filter(user)
        elif not user.is_freelancer and user.company_id:
            query_filters &= Q(company_profile_id=user.company_id)

        return PartsListConfig.objects.filter(query_filters).distinct().order_by("-id")

    def _project_access_filter(self, user):
        if user.is_freelancer:
            return Q(project__user=user) | Q(project__team_members__team_member=user)
        return (
            Q(project__company_profile=user.company) |
            Q(project__user__company=user.company) |
            Q(project__team_members__team_member=user)
        )

    def list(self, request, *args, **kwargs):
        project_id = request.query_params.get("project")
        if project_id:
            project = get_object_or_404(Project, id=project_id)
            config, _ = PartsListConfig.objects.get_or_create(
                project=project,
                defaults={
                    "company_profile": project.company_profile,
                    "created_by": request.user if request.user.is_authenticated else None,
                    "modified_by": request.user if request.user.is_authenticated else None,
                },
            )
            self.check_object_permissions(request, config)
            return Response(self.get_serializer(config).data)

        company_id = getattr(request.user, "company_id", None)
        if company_id and not request.user.is_freelancer:
            config, _ = PartsListConfig.objects.get_or_create(
                company_profile_id=company_id,
                project=None,
                defaults={
                    "created_by": request.user if request.user.is_authenticated else None,
                    "modified_by": request.user if request.user.is_authenticated else None,
                },
            )
            return Response(self.get_serializer(config).data)

        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        project = serializer.validated_data.get("project")
        company_profile = serializer.validated_data.get("company_profile")
        user = self.request.user
        if project:
            if not self._user_can_access_project(user, project):
                raise PermissionDenied()
            serializer.save(company_profile=project.company_profile, project=project)
            return
        if company_profile and (user.is_freelancer or company_profile.id != user.company_id):
            raise PermissionDenied()
        serializer.save(company_profile=company_profile, project=None)

    def perform_update(self, serializer):
        project = serializer.validated_data.get("project", serializer.instance.project)
        company_profile = serializer.validated_data.get("company_profile", serializer.instance.company_profile)
        user = self.request.user
        if project:
            if not self._user_can_access_project(user, project):
                raise PermissionDenied()
            serializer.save(company_profile=project.company_profile, project=project)
            return
        if company_profile and (user.is_freelancer or company_profile.id != user.company_id):
            raise PermissionDenied()
        serializer.save(company_profile=company_profile, project=None)

    def _user_can_access_project(self, user, project):
        if not user.is_authenticated:
            return False
        if project.user_id == user.id or project.team_members.filter(team_member=user).exists():
            return True
        return not user.is_freelancer and user.company_id and project.company_profile_id == user.company_id


class SolutionSpaceConfigViewSet(viewsets.ModelViewSet):
    queryset = SolutionSpaceConfig.objects.all()
    serializer_class = SolutionSpaceConfigSerializer
    filter_backends = [filters.OrderingFilter]
    ordering = ["-id"]

    def _is_project_request(self):
        if self.request.query_params.get("project") or self.request.data.get("project"):
            return True
        config_id = self.kwargs.get("pk")
        return bool(
            config_id
            and SolutionSpaceConfig.objects.filter(
                id=config_id,
                project__isnull=False,
            ).exists()
        )

    def get_permissions(self):
        permission_class = (
            ProjectSolutionSpacePermission
            if self._is_project_request()
            else CompanySolutionSpacePermission
        )
        scope = "project" if self._is_project_request() else "company"
        if self.action == "create":
            return [permission_class(action="create", scope=scope)]
        if self.action in ["update", "partial_update"]:
            return [permission_class(action="update", scope=scope)]
        if self.action == "destroy":
            return [permission_class(action="delete", scope=scope)]
        return [permission_class(action="view", scope=scope)]

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return SolutionSpaceConfig.objects.none()

        config_id = self.kwargs.get("pk")
        if (
            config_id
            and SolutionSpaceConfig.objects.filter(
                id=config_id,
                project__isnull=False,
            ).exists()
        ):
            return (
                SolutionSpaceConfig.objects.filter(id=config_id)
                .filter(self._project_access_filter(user))
                .distinct()
            )

        project_id = self.request.query_params.get("project")
        if project_id:
            return SolutionSpaceConfig.objects.filter(
                project_id=project_id,
            ).filter(self._project_access_filter(user)).distinct()
        if user.is_freelancer or not user.company_id:
            return SolutionSpaceConfig.objects.none()
        return SolutionSpaceConfig.objects.filter(
            company_profile_id=user.company_id,
            project__isnull=True,
        )

    def list(self, request, *args, **kwargs):
        project_id = request.query_params.get("project")
        if project_id:
            project = get_object_or_404(Project, id=project_id)
            if not self._user_can_access_project(request.user, project):
                raise PermissionDenied()
            config, _ = SolutionSpaceConfig.objects.get_or_create(
                project=project,
                defaults={
                    "company_profile": project.company_profile,
                    "created_by": request.user,
                    "modified_by": request.user,
                },
            )
            self.check_object_permissions(request, config)
            return Response(self.get_serializer(config).data)

        if request.user.is_freelancer or not request.user.company_id:
            return Response([], status=status.HTTP_200_OK)
        config, _ = SolutionSpaceConfig.objects.get_or_create(
            company_profile=request.user.company,
            project=None,
            defaults={
                "created_by": request.user,
                "modified_by": request.user,
            },
        )
        self.check_object_permissions(request, config)
        return Response(self.get_serializer(config).data)

    def perform_update(self, serializer):
        instance = serializer.instance
        if instance.project_id:
            if not self._user_can_access_project(self.request.user, instance.project):
                raise PermissionDenied()
            serializer.save(
                company_profile=instance.project.company_profile,
                project=instance.project,
            )
            return
        if self.request.user.is_freelancer or instance.company_profile_id != self.request.user.company_id:
            raise PermissionDenied()
        serializer.save(company_profile=instance.company_profile, project=None)

    def _project_access_filter(self, user):
        if user.is_freelancer:
            return Q(project__user=user) | Q(project__team_members__team_member=user)
        return (
            Q(project__company_profile=user.company)
            | Q(project__user__company=user.company)
            | Q(project__team_members__team_member=user)
        )

    @staticmethod
    def _user_can_access_project(user, project):
        if not user.is_authenticated:
            return False
        if project.user_id == user.id or project.team_members.filter(team_member=user).exists():
            return True
        return bool(
            not user.is_freelancer
            and user.company_id
            and project.company_profile_id == user.company_id
        )


class MachineConfigViewSet(viewsets.ModelViewSet):
    queryset = MachineConfig.objects.all()
    serializer_class = MachineConfigSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering = ["-id"]

    def get_permissions(self):
        if self.action == "create":
            return [MachinePermission(action="create", scope="company")]
        if self.action in ["update", "partial_update"]:
            return [MachinePermission(action="update", scope="company")]
        if self.action == "destroy":
            return [MachinePermission(action="delete", scope="company")]
        return [MachinePermission(action="view", scope="company")]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser) or user.is_freelancer or not user.company_id:
            return MachineConfig.objects.none()

        company_id = self.request.query_params.get("company_profile")
        if company_id and str(company_id) != str(user.company_id):
            return MachineConfig.objects.none()
        return MachineConfig.objects.filter(company_profile_id=user.company_id).order_by("-id")

    def list(self, request, *args, **kwargs):
        company_id = getattr(request.user, "company_id", None)
        if company_id and not request.user.is_freelancer:
            config, _ = MachineConfig.objects.get_or_create(
                company_profile_id=company_id,
                defaults={
                    "created_by": request.user if request.user.is_authenticated else None,
                    "modified_by": request.user if request.user.is_authenticated else None,
                },
            )
            return Response(self.get_serializer(config).data)
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        company_profile = serializer.validated_data.get("company_profile")
        user = self.request.user
        if not company_profile or user.is_freelancer or company_profile.id != user.company_id:
            raise PermissionDenied()
        serializer.save()

    def perform_update(self, serializer):
        company_profile = serializer.validated_data.get(
            "company_profile",
            serializer.instance.company_profile,
        )
        user = self.request.user
        if not company_profile or user.is_freelancer or company_profile.id != user.company_id:
            raise PermissionDenied()
        serializer.save()


class EquipmentConfigViewSet(viewsets.ModelViewSet):
    queryset = EquipmentConfig.objects.all()
    serializer_class = EquipmentConfigSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering = ["-id"]

    def get_permissions(self):
        if self.action == "create":
            return [EquipmentPermission(action="create", scope="company")]
        if self.action in ["update", "partial_update"]:
            return [EquipmentPermission(action="update", scope="company")]
        if self.action == "destroy":
            return [EquipmentPermission(action="delete", scope="company")]
        return [EquipmentPermission(action="view", scope="company")]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser) or user.is_freelancer or not user.company_id:
            return EquipmentConfig.objects.none()

        company_id = self.request.query_params.get("company_profile")
        if company_id and str(company_id) != str(user.company_id):
            return EquipmentConfig.objects.none()
        return EquipmentConfig.objects.filter(company_profile_id=user.company_id).order_by("-id")

    def list(self, request, *args, **kwargs):
        company_id = getattr(request.user, "company_id", None)
        if company_id and not request.user.is_freelancer:
            config, _ = EquipmentConfig.objects.get_or_create(
                company_profile_id=company_id,
                defaults={
                    "created_by": request.user if request.user.is_authenticated else None,
                    "modified_by": request.user if request.user.is_authenticated else None,
                },
            )
            return Response(self.get_serializer(config).data)
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        company_profile = serializer.validated_data.get("company_profile")
        user = self.request.user
        if not company_profile or user.is_freelancer or company_profile.id != user.company_id:
            raise PermissionDenied()
        serializer.save()

    def perform_update(self, serializer):
        company_profile = serializer.validated_data.get(
            "company_profile",
            serializer.instance.company_profile,
        )
        user = self.request.user
        if not company_profile or user.is_freelancer or company_profile.id != user.company_id:
            raise PermissionDenied()
        serializer.save()


class BaseMatrixViewSet(viewsets.ModelViewSet):
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    ordering_fields = ["id", "sort_order", "level", "created_at", "updated_at"]
    ordering = ["sort_order", "id"]
    search_fields = []
    module_name = ""

    def get_permission_class(self):
        return {
            "solution_demands": SolutionDemandPermission,
            "requirements": RequirementPermission,
            "transports": TransportPermission,
        }[self.module_name]

    def get_permissions(self):
        permission_class = self.get_permission_class()
        if self.action == "create":
            return [permission_class(action="create", scope="project")]
        if self.action in ["update", "partial_update", "move_up", "move_down", "indent", "outdent"]:
            return [permission_class(action="update", scope="project")]
        if self.action == "destroy":
            return [permission_class(action="delete", scope="project")]
        return [permission_class(action="view", scope="project")]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return self.queryset.none()

        product_name = get_product_name_from_request(self.request)
        products = COLLABORATIVE_SOLUTION if product_name in COLLABORATIVE_SOLUTION else [product_name]
        query_filters = Q(project__solution__in=products)

        project_id = self.request.query_params.get("project")
        if project_id:
            query_filters &= Q(project_id=project_id)
        parent = self.request.query_params.get("parent")
        if parent == "root":
            query_filters &= Q(parent__isnull=True)
        elif parent:
            query_filters &= Q(parent_id=parent)

        # For detail/update/delete routes, let object-level permissions decide the
        # module access result. The queryset only needs to prove project membership
        # so valid IDs do not get turned into 404s before permission checks run.
        if self.kwargs.get("pk") and not project_id:
            if user.is_freelancer:
                query_filters &= (
                    Q(project__user=user) |
                    Q(project__team_members__team_member=user)
                )
            else:
                query_filters &= (
                    Q(project__company_profile=user.company) |
                    Q(project__user__company=user.company) |
                    Q(project__user=user) |
                    Q(project__team_members__team_member=user)
                )

            return self.queryset.filter(query_filters).distinct().prefetch_related(
                "object_assessments",
                "documents",
            ).order_by("sort_order", "id")

        has_projects_company_permissions = require_any_permission_multiple_modules(
            user,
            {'projects': ['full_access', 'view_all_access']},
            'company',
            self.request.query_params,
        )
        has_matrix_permissions = require_any_permission_multiple_modules(
            user,
            {self.module_name: ['view', 'full_access']},
            'project',
            self.request.query_params,
        )

        if not user.is_freelancer and has_projects_company_permissions:
            query_filters &= (
                Q(project__company_profile=user.company) |
                Q(project__user__company=user.company) |
                Q(project__team_members__team_member=user)
            )
        elif has_matrix_permissions:
            if user.is_freelancer:
                query_filters &= (
                    Q(project__user=user) |
                    Q(project__team_members__team_member=user)
                )
            else:
                query_filters &= (
                    Q(project__user=user) |
                    Q(project__team_members__team_member=user)
                )
        else:
            return self.queryset.none()

        return self.queryset.filter(query_filters).distinct().prefetch_related(
            "object_assessments",
            "documents",
        ).order_by("sort_order", "id")

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        has_children = self.queryset.filter(parent=instance).exists()
        if has_children and not is_truthy(request.query_params.get("confirm")):
            return Response(
                {"detail": "This row has child rows. Pass confirm=true to delete the full subtree."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="move-up")
    def move_up(self, request, pk=None):
        return self._move(direction=-1)

    @action(detail=True, methods=["post"], url_path="move-down")
    def move_down(self, request, pk=None):
        return self._move(direction=1)

    @action(detail=True, methods=["post"])
    def indent(self, request, pk=None):
        instance = self.get_object()
        previous_sibling = self.queryset.filter(
            project=instance.project,
            parent=instance.parent,
            sort_order__lt=instance.sort_order,
        ).order_by("-sort_order", "-id").first()
        if not previous_sibling:
            return Response({"detail": "No previous sibling is available as parent."}, status=status.HTTP_400_BAD_REQUEST)
        if previous_sibling.level >= 10:
            return Response({"detail": "Cannot create hierarchy deeper than 10 levels."}, status=status.HTTP_400_BAD_REQUEST)
        subtree_depth = self._subtree_depth(instance)
        if previous_sibling.level + subtree_depth > 10:
            return Response({"detail": "Cannot create hierarchy deeper than 10 levels."}, status=status.HTTP_400_BAD_REQUEST)
        instance.parent = previous_sibling
        instance.level = previous_sibling.level + 1
        instance.sort_order = self._next_sort_order(instance.project, previous_sibling)
        instance.save(update_fields=["parent", "level", "sort_order", "updated_at"])
        update_descendant_levels(instance)
        return Response(self.get_serializer(instance).data)

    def _subtree_depth(self, instance):
        max_depth = 1
        pending = [(instance, 1)]
        while pending:
            node, depth = pending.pop()
            max_depth = max(max_depth, depth)
            children = self.queryset.filter(parent=node).only("id")
            pending.extend((child, depth + 1) for child in children)
        return max_depth

    @action(detail=True, methods=["post"])
    def outdent(self, request, pk=None):
        instance = self.get_object()
        if not instance.parent:
            return Response({"detail": "Root rows cannot be outdented."}, status=status.HTTP_400_BAD_REQUEST)
        old_parent = instance.parent
        instance.parent = old_parent.parent
        instance.level = old_parent.level
        instance.sort_order = old_parent.sort_order + 1
        instance.save(update_fields=["parent", "level", "sort_order", "updated_at"])
        self._normalize_sibling_order(instance.project, instance.parent)
        update_descendant_levels(instance)
        return Response(self.get_serializer(instance).data)

    def _move(self, direction):
        instance = self.get_object()
        siblings = list(self.queryset.filter(
            project=instance.project,
            parent=instance.parent,
        ).order_by("sort_order", "id"))
        current_index = next((index for index, item in enumerate(siblings) if item.id == instance.id), None)
        target_index = current_index + direction if current_index is not None else None
        if target_index is None or target_index < 0 or target_index >= len(siblings):
            return Response(self.get_serializer(instance).data)
        sibling = siblings[target_index]
        instance.sort_order, sibling.sort_order = sibling.sort_order, instance.sort_order
        instance.save(update_fields=["sort_order", "updated_at"])
        sibling.save(update_fields=["sort_order", "updated_at"])
        return Response(self.get_serializer(instance).data)

    def _next_sort_order(self, project, parent):
        last = self.queryset.filter(project=project, parent=parent).order_by("-sort_order").first()
        return (last.sort_order if last else 0) + 1

    def _normalize_sibling_order(self, project, parent):
        siblings = self.queryset.filter(project=project, parent=parent).order_by("sort_order", "id")
        for index, sibling in enumerate(siblings, start=1):
            if sibling.sort_order != index:
                sibling.sort_order = index
                sibling.save(update_fields=["sort_order", "updated_at"])


class SolutionDemandViewSet(BaseMatrixViewSet):
    queryset = SolutionDemand.objects.all()
    serializer_class = SolutionDemandSerializer
    search_fields = ["id", "subject", "demand_number"]
    module_name = "solution_demands"


class RequirementViewSet(BaseMatrixViewSet):
    queryset = Requirement.objects.all()
    serializer_class = RequirementSerializer
    search_fields = ["id", "name", "object_number", "skill"]
    module_name = "requirements"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("solution_demands")


class TransportViewSet(BaseMatrixViewSet):
    queryset = Transport.objects.all()
    serializer_class = TransportSerializer
    search_fields = ["id", "name", "transportation_lanes", "location", "unlocode"]
    module_name = "transports"
    filter_backends = [filters.SearchFilter, TransportOrderingFilter]
    ordering_fields = [
        "id",
        "sort_order",
        "level",
        "created_at",
        "updated_at",
        "name",
        "transport_start_date",
        "transport_end_date",
        "linked_documents",
        "linked_observations",
    ]
    ordering = ["sort_order", "id"]
    ordering_text_fields = {"name"}
    ordering_natural_fields = {"name"}

    def get_ordering_configured_columns(self):
        project_id = self.request.query_params.get("project")
        if not project_id:
            return []
        return (
            ProjectMatrixConfig.objects.filter(project_id=project_id)
            .values_list("transport_columns", flat=True)
            .first()
            or []
        )

    def get_queryset(self):
        return super().get_queryset().annotate(
            linked_documents=Count("documents", distinct=True),
            linked_observations=Count("object_assessments", distinct=True),
        )

    @action(detail=True, methods=["post"], url_path="move-up")
    def move_up(self, request, pk=None):
        return Response(
            {"detail": "Transport rows do not support reordering."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=["post"], url_path="move-down")
    def move_down(self, request, pk=None):
        return Response(
            {"detail": "Transport rows do not support reordering."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=["post"])
    def indent(self, request, pk=None):
        return Response(
            {"detail": "Transport rows do not support hierarchy."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=["post"])
    def outdent(self, request, pk=None):
        return Response(
            {"detail": "Transport rows do not support hierarchy."},
            status=status.HTTP_400_BAD_REQUEST,
        )


class HierarchyPaginatedListMixin:
    """Page matched tree rows while returning their ancestor context together."""

    hierarchy_base_queryset = None

    def optimize_hierarchy_queryset(self, queryset):
        return queryset

    @staticmethod
    def _tree_rows(queryset):
        return list(queryset.values_list("id", "parent_id", "sort_order"))

    @staticmethod
    def _tree_order(rows):
        children = {}
        row_ids = {row_id for row_id, _parent_id, _sort_order in rows}
        for row_id, parent_id, sort_order in rows:
            parent_key = parent_id if parent_id in row_ids else None
            children.setdefault(parent_key, []).append(
                (sort_order or 0, row_id),
            )
        for siblings in children.values():
            siblings.sort(key=lambda item: (item[0], item[1]))

        ordered = []
        visited = set()

        def visit(parent_id):
            for _sort_order, row_id in children.get(parent_id, []):
                if row_id in visited:
                    continue
                visited.add(row_id)
                ordered.append(row_id)
                visit(row_id)

        visit(None)
        for row_id in sorted(row_ids - visited):
            ordered.append(row_id)
            visit(row_id)
        return ordered

    @staticmethod
    def _ancestor_chain(row_id, parents):
        chain = []
        visited = set()
        parent_id = parents.get(row_id)
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            chain.append(parent_id)
            parent_id = parents.get(parent_id)
        chain.reverse()
        return chain

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        base_queryset = (
            self.hierarchy_base_queryset
            if self.hierarchy_base_queryset is not None
            else queryset
        )
        tree_rows = self._tree_rows(base_queryset)
        tree_order = self._tree_order(tree_rows)
        matched_ids = list(queryset.values_list("id", flat=True))
        matched_set = set(matched_ids)

        if not request.query_params.get("ordering"):
            matched_ids = [row_id for row_id in tree_order if row_id in matched_set]

        page_ids = self.paginate_queryset(matched_ids)
        if page_ids is None:
            page_ids = matched_ids

        parents = {row_id: parent_id for row_id, parent_id, _sort_order in tree_rows}
        include_context = str(
            request.query_params.get("include_hierarchy_context", "true")
        ).lower() not in {"0", "false", "no"}
        context_ids = set()
        if include_context:
            for row_id in page_ids:
                context_ids.update(self._ancestor_chain(row_id, parents))
        context_ids.difference_update(page_ids)

        selected_ids = set(page_ids) | context_ids
        objects = self.optimize_hierarchy_queryset(
            base_queryset.filter(id__in=selected_ids),
        )
        by_id = {item.id: item for item in objects}

        if request.query_params.get("ordering"):
            response_ids = []
            seen = set()
            for row_id in page_ids:
                for context_id in self._ancestor_chain(row_id, parents):
                    if context_id in selected_ids and context_id not in seen:
                        response_ids.append(context_id)
                        seen.add(context_id)
                if row_id not in seen:
                    response_ids.append(row_id)
                    seen.add(row_id)
        else:
            response_ids = [row_id for row_id in tree_order if row_id in selected_ids]

        serializer = self.get_serializer(
            [by_id[row_id] for row_id in response_ids if row_id in by_id],
            many=True,
        )
        if self.paginator is None:
            return Response(serializer.data)
        response = self.get_paginated_response(serializer.data)
        response.data["match_count"] = response.data["count"]
        response.data["context_count"] = len(context_ids)
        response.data["matched_ids"] = page_ids
        return response


class BasePartsViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    ordering_fields = ["id", "sort_order", "level", "created_at", "updated_at"]
    ordering = ["sort_order", "id"]
    search_fields = ["id", "name", "part_number"]

    def _mark_aas_out_of_date(self, instance):
        from aas_integration.services import (
            mark_company_assembly_out_of_date,
            mark_product_out_of_date,
            mark_project_assembly_out_of_date,
            mark_project_out_of_date,
        )

        if isinstance(instance, BillOfMaterialPart):
            mark_project_out_of_date(instance.project, getattr(self.request.user, "id", None))
        elif isinstance(instance, AvailableProductPart):
            mark_product_out_of_date(instance, getattr(self.request.user, "id", None))
        elif isinstance(instance, CompanySolutionSpaceItem):
            mark_company_assembly_out_of_date(
                instance,
                getattr(self.request.user, "id", None),
            )
        elif isinstance(instance, ProjectSolutionSpaceItem):
            mark_project_assembly_out_of_date(
                instance,
                getattr(self.request.user, "id", None),
            )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        has_children = self.queryset.filter(parent=instance).exists()
        if has_children and not is_truthy(request.query_params.get("confirm")):
            return Response(
                {"detail": "This row has child rows. Pass confirm=true to delete the full subtree."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        project = instance.project if isinstance(instance, BillOfMaterialPart) else None
        assembly_parent = (
            instance.parent
            if isinstance(instance, (CompanySolutionSpaceItem, ProjectSolutionSpaceItem))
            else None
        )
        super().perform_destroy(instance)
        if project:
            from aas_integration.services import mark_project_out_of_date

            mark_project_out_of_date(project, getattr(self.request.user, "id", None))
        elif assembly_parent:
            self._mark_aas_out_of_date(assembly_parent)

    @action(detail=True, methods=["post"], url_path="move-up")
    def move_up(self, request, pk=None):
        return self._move(direction=-1)

    @action(detail=True, methods=["post"], url_path="move-down")
    def move_down(self, request, pk=None):
        return self._move(direction=1)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def indent(self, request, pk=None):
        instance = self.get_object()
        previous_sibling = self.queryset.filter(
            **self._scope_filter_kwargs(instance),
            parent=instance.parent,
            sort_order__lt=instance.sort_order,
        ).order_by("-sort_order", "-id").first()
        if not previous_sibling:
            return Response({"detail": "No previous sibling is available as parent."}, status=status.HTTP_400_BAD_REQUEST)
        if previous_sibling.level >= 10:
            return Response({"detail": "Cannot create hierarchy deeper than 10 levels."}, status=status.HTTP_400_BAD_REQUEST)
        subtree_depth = self._subtree_depth(instance)
        if previous_sibling.level + subtree_depth > 10:
            return Response({"detail": "Cannot create hierarchy deeper than 10 levels."}, status=status.HTTP_400_BAD_REQUEST)
        instance.parent = previous_sibling
        instance.level = previous_sibling.level + 1
        instance.sort_order = self._next_sort_order(instance, previous_sibling)
        instance.save(update_fields=["parent", "level", "sort_order", "updated_at"])
        update_descendant_levels(instance)
        self._mark_aas_out_of_date(instance)
        return Response(self.get_serializer(instance).data)

    def _subtree_depth(self, instance):
        max_depth = 1
        pending = [(instance, 1)]
        while pending:
            node, depth = pending.pop()
            max_depth = max(max_depth, depth)
            children = self.queryset.filter(parent=node).only("id")
            pending.extend((child, depth + 1) for child in children)
        return max_depth

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def outdent(self, request, pk=None):
        instance = self.get_object()
        if not instance.parent:
            return Response({"detail": "Root rows cannot be outdented."}, status=status.HTTP_400_BAD_REQUEST)
        old_parent = instance.parent
        instance.parent = old_parent.parent
        instance.level = old_parent.level
        instance.sort_order = old_parent.sort_order + 1
        instance.save(update_fields=["parent", "level", "sort_order", "updated_at"])
        self._normalize_sibling_order(instance, instance.parent)
        update_descendant_levels(instance)
        self._mark_aas_out_of_date(instance)
        return Response(self.get_serializer(instance).data)

    @transaction.atomic
    def _move(self, direction):
        instance = self.get_object()
        siblings = list(self.queryset.filter(
            **self._scope_filter_kwargs(instance),
            parent=instance.parent,
        ).order_by("sort_order", "id"))
        current_index = next((index for index, item in enumerate(siblings) if item.id == instance.id), None)
        target_index = current_index + direction if current_index is not None else None
        if target_index is None or target_index < 0 or target_index >= len(siblings):
            return Response(self.get_serializer(instance).data)
        sibling = siblings[target_index]
        instance.sort_order, sibling.sort_order = sibling.sort_order, instance.sort_order
        instance.save(update_fields=["sort_order", "updated_at"])
        sibling.save(update_fields=["sort_order", "updated_at"])
        self._mark_aas_out_of_date(instance)
        return Response(self.get_serializer(instance).data)

    def _scope_filter_kwargs(self, instance):
        if hasattr(instance, "project_id"):
            return {"project": instance.project}
        return {"company_profile": instance.company_profile}

    def _next_sort_order(self, instance, parent):
        last = self.queryset.filter(**self._scope_filter_kwargs(instance), parent=parent).order_by("-sort_order").first()
        return (last.sort_order if last else 0) + 1

    def _normalize_sibling_order(self, instance, parent):
        siblings = self.queryset.filter(**self._scope_filter_kwargs(instance), parent=parent).order_by("sort_order", "id")
        for index, sibling in enumerate(siblings, start=1):
            if sibling.sort_order != index:
                sibling.sort_order = index
                sibling.save(update_fields=["sort_order", "updated_at"])


class AvailableProductPartViewSet(HierarchyPaginatedListMixin, BasePartsViewSet):
    queryset = AvailableProductPart.objects.all()
    serializer_class = AvailableProductPartSerializer
    filter_backends = [filters.SearchFilter, AvailableProductOrderingFilter]
    ordering_fields = [
        "id",
        "sort_order",
        "level",
        "name",
        "part_number",
        "linked_documents",
        "created_at",
        "updated_at",
    ]
    ordering_text_fields = {"name", "part_number"}
    ordering_natural_fields = {"name", "part_number"}
    search_fields = [
        "id",
        "name",
        "part_number",
        "aas_profile__manufacturer_name",
        "aas_profile__supplier_name",
        "custom_fields__manufacturer",
        "custom_fields__supplier",
    ]

    def get_ordering_configured_columns(self):
        columns = (
            PartsListConfig.objects.filter(
                company_profile_id=getattr(self.request.user, "company_id", None),
                project__isnull=True,
            )
            .values_list("available_product_columns", flat=True)
            .first()
            or []
        )
        return columns

    def optimize_hierarchy_queryset(self, queryset):
        return queryset.select_related("aas_profile", "aas_identity").prefetch_related(
            "object_assessments",
            "documents",
        )

    def get_permissions(self):
        if self.action == "create":
            return [AvailableProductPartPermission(action="create", scope="company")]
        if self.action in ["update", "partial_update", "move_up", "move_down", "indent", "outdent"]:
            return [AvailableProductPartPermission(action="update", scope="company")]
        if self.action == "destroy":
            return [AvailableProductPartPermission(action="delete", scope="company")]
        return [AvailableProductPartPermission(action="view", scope="company")]

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser) or user.is_freelancer or not user.company_id:
            return AvailableProductPart.objects.none()

        has_module_permissions = require_any_permission_multiple_modules(
            user,
            {'available_product_parts': ['view', 'full_access']},
            'company',
            self.request.query_params,
        )
        if not has_module_permissions:
            return AvailableProductPart.objects.none()

        company_id = self.request.query_params.get("company_profile")
        if company_id and str(company_id) != str(user.company_id):
            return AvailableProductPart.objects.none()
        query_filters = Q(company_profile_id=user.company_id)
        parent = self.request.query_params.get("parent")
        if parent == "root":
            query_filters &= Q(parent__isnull=True)
        elif parent:
            query_filters &= Q(parent_id=parent)

        base_queryset = AvailableProductPart.objects.filter(query_filters)
        self.hierarchy_base_queryset = base_queryset
        params = self.request.query_params
        filtered_queryset = base_queryset

        origin = params.get("origin")
        if origin == "unclassified":
            filtered_queryset = filtered_queryset.filter(
                Q(aas_profile__isnull=True) | Q(aas_profile__origin=origin)
            )
        elif origin in {"manufactured", "purchased"}:
            filtered_queryset = filtered_queryset.filter(aas_profile__origin=origin)

        aas_status = params.get("aas_status")
        if aas_status == "unpublished":
            filtered_queryset = filtered_queryset.filter(aas_identity__isnull=True)
        elif aas_status == "current":
            filtered_queryset = filtered_queryset.filter(
                aas_identity__isnull=False,
                aas_identity__out_of_date=False,
            )
        elif aas_status == "out_of_date":
            filtered_queryset = filtered_queryset.filter(
                aas_identity__out_of_date=True,
            )

        supplier = str(params.get("supplier") or "").strip()
        if supplier:
            filtered_queryset = filtered_queryset.filter(
                Q(aas_profile__supplier_name__icontains=supplier)
                | Q(custom_fields__supplier__icontains=supplier)
            )
        manufacturer = str(params.get("manufacturer") or "").strip()
        if manufacturer:
            filtered_queryset = filtered_queryset.filter(
                Q(aas_profile__manufacturer_name__icontains=manufacturer)
                | Q(custom_fields__manufacturer__icontains=manufacturer)
            )

        return self.optimize_hierarchy_queryset(
            filtered_queryset.distinct().order_by("sort_order", "id")
        )

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        if user.is_freelancer or not user.company_id:
            raise PermissionDenied()
        instance = serializer.save(company_profile=user.company)
        self._mark_aas_out_of_date(instance)

    @transaction.atomic
    def perform_update(self, serializer):
        if serializer.instance.company_profile_id != self.request.user.company_id:
            raise PermissionDenied()
        instance = serializer.save(company_profile=serializer.instance.company_profile)
        self._mark_aas_out_of_date(instance)


class MachineViewSet(BasePartsViewSet):
    queryset = Machine.objects.all()
    serializer_class = MachineSerializer
    search_fields = ["id", "name"]

    def get_permissions(self):
        if self.action == "create":
            return [MachinePermission(action="create", scope="company")]
        if self.action in ["update", "partial_update", "move_up", "move_down", "indent", "outdent"]:
            return [MachinePermission(action="update", scope="company")]
        if self.action == "destroy":
            return [MachinePermission(action="delete", scope="company")]
        return [MachinePermission(action="view", scope="company")]

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser) or user.is_freelancer or not user.company_id:
            return Machine.objects.none()

        has_module_permissions = require_any_permission_multiple_modules(
            user,
            {'machines': ['view', 'full_access']},
            'company',
            self.request.query_params,
        )
        if not has_module_permissions:
            return Machine.objects.none()

        company_id = self.request.query_params.get("company_profile")
        if company_id and str(company_id) != str(user.company_id):
            return Machine.objects.none()
        query_filters = Q(company_profile_id=user.company_id)
        parent = self.request.query_params.get("parent")
        if parent == "root":
            query_filters &= Q(parent__isnull=True)
        elif parent:
            query_filters &= Q(parent_id=parent)

        return Machine.objects.filter(query_filters).distinct().prefetch_related(
            "documents",
            "projects",
        ).order_by("sort_order", "id")

    def perform_create(self, serializer):
        user = self.request.user
        if user.is_freelancer or not user.company_id:
            raise PermissionDenied()
        serializer.save(company_profile=user.company)

    def perform_update(self, serializer):
        if serializer.instance.company_profile_id != self.request.user.company_id:
            raise PermissionDenied()
        serializer.save(company_profile=serializer.instance.company_profile)


class EquipmentViewSet(BasePartsViewSet):
    queryset = Equipment.objects.all()
    serializer_class = EquipmentSerializer
    search_fields = ["id", "name"]

    def get_permissions(self):
        if self.action == "create":
            return [EquipmentPermission(action="create", scope="company")]
        if self.action in ["update", "partial_update", "move_up", "move_down", "indent", "outdent"]:
            return [EquipmentPermission(action="update", scope="company")]
        if self.action == "destroy":
            return [EquipmentPermission(action="delete", scope="company")]
        return [EquipmentPermission(action="view", scope="company")]

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser) or user.is_freelancer or not user.company_id:
            return Equipment.objects.none()

        has_module_permissions = require_any_permission_multiple_modules(
            user,
            {'equipments': ['view', 'full_access']},
            'company',
            self.request.query_params,
        )
        if not has_module_permissions:
            return Equipment.objects.none()

        company_id = self.request.query_params.get("company_profile")
        if company_id and str(company_id) != str(user.company_id):
            return Equipment.objects.none()
        query_filters = Q(company_profile_id=user.company_id)
        parent = self.request.query_params.get("parent")
        if parent == "root":
            query_filters &= Q(parent__isnull=True)
        elif parent:
            query_filters &= Q(parent_id=parent)

        return Equipment.objects.filter(query_filters).distinct().prefetch_related(
            "documents",
        ).order_by("sort_order", "id")

    def perform_create(self, serializer):
        user = self.request.user
        if user.is_freelancer or not user.company_id:
            raise PermissionDenied()
        serializer.save(company_profile=user.company)

    def perform_update(self, serializer):
        if serializer.instance.company_profile_id != self.request.user.company_id:
            raise PermissionDenied()
        serializer.save(company_profile=serializer.instance.company_profile)


class BillOfMaterialPartViewSet(HierarchyPaginatedListMixin, BasePartsViewSet):
    queryset = BillOfMaterialPart.objects.all()
    serializer_class = BillOfMaterialPartSerializer

    @action(detail=False, methods=["get"], url_path="purchase-eligibility")
    def purchase_eligibility(self, request):
        from project_management.services.engineering_approval import purchase_block_reason
        try:
            ids = {int(value) for value in request.query_params.get("ids", "").split(",") if value}
        except ValueError:
            raise ValidationError({"ids": "Select valid BoM part IDs."})
        if not request.query_params.get("project") or len(ids) > 1000:
            raise ValidationError({"ids": "Select a project and up to 1000 cart parts."})
        parts = self.get_queryset().filter(id__in=ids).select_related("source_workstation")
        cache = {}
        return Response({"results": [
            {"id": part.id, "purchase_block_reason": purchase_block_reason(part, cache)}
            for part in parts
        ]})

    filter_backends = [filters.SearchFilter, BillOfMaterialOrderingFilter]
    ordering_fields = [
        "id",
        "sort_order",
        "level",
        "name",
        "part_number",
        "status",
        "available_product",
        "engineering_quantity",
        "engineering_unit",
        "linked_documents",
        "linked_observations",
        "linked_requirements",
        "created_at",
        "updated_at",
    ]
    ordering_text_fields = {"name", "part_number", "status", "engineering_unit"}
    ordering_natural_fields = {"name", "part_number", "engineering_unit"}
    search_fields = [
        "id",
        "name",
        "part_number",
        "status",
        "custom_fields__supplier",
        "custom_fields__manufacturer",
        "available_product__name",
        "available_product__part_number",
        "available_product__aas_profile__supplier_name",
        "available_product__aas_profile__manufacturer_name",
    ]

    def get_ordering_configured_columns(self):
        project_id = self.request.query_params.get("project")
        if not project_id:
            return []
        return (
            PartsListConfig.objects.filter(project_id=project_id)
            .values_list("bom_columns", flat=True)
            .first()
            or []
        )

    def optimize_hierarchy_queryset(self, queryset):
        return queryset.prefetch_related(
            "object_assessments",
            "documents",
            "requirements",
        ).select_related(
            "available_product",
            "available_product__aas_profile",
        )

    def get_permissions(self):
        if self.action == "create":
            return [BillOfMaterialPermission(action="create", scope="project")]
        if self.action in ["update", "partial_update", "move_up", "move_down", "indent", "outdent"]:
            return [BillOfMaterialPermission(action="update", scope="project")]
        if self.action == "destroy":
            return [BillOfMaterialPermission(action="delete", scope="project")]
        return [BillOfMaterialPermission(action="view", scope="project")]

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return BillOfMaterialPart.objects.none()

        product_name = get_product_name_from_request(self.request)
        products = COLLABORATIVE_SOLUTION if product_name in COLLABORATIVE_SOLUTION else [product_name]
        query_filters = Q(project__solution__in=products)

        project_id = self.request.query_params.get("project")
        if project_id:
            query_filters &= Q(project_id=project_id)
        parent = self.request.query_params.get("parent")
        if parent == "root":
            query_filters &= Q(parent__isnull=True)
        elif parent:
            query_filters &= Q(parent_id=parent)

        # For detail/update/delete routes, return rows scoped by project
        # membership and leave module permission enforcement to object-level checks.
        if self.kwargs.get("pk") and not project_id:
            if user.is_freelancer:
                query_filters &= (
                    Q(project__user=user) |
                    Q(project__team_members__team_member=user)
                )
            else:
                query_filters &= (
                    Q(project__company_profile=user.company) |
                    Q(project__user__company=user.company) |
                    Q(project__user=user) |
                    Q(project__team_members__team_member=user)
                )

            return BillOfMaterialPart.objects.filter(query_filters).distinct().prefetch_related(
                "object_assessments",
                "documents",
                "requirements",
            ).select_related("available_product").order_by("sort_order", "id")

        has_projects_company_permissions = require_any_permission_multiple_modules(
            user,
            {'projects': ['full_access', 'view_all_access']},
            'company',
            self.request.query_params,
        )
        has_module_permissions = require_any_permission_multiple_modules(
            user,
            {'bill_of_material': ['view', 'full_access']},
            'project',
            self.request.query_params,
        )

        if not user.is_freelancer and has_projects_company_permissions:
            query_filters &= (
                Q(project__company_profile=user.company) |
                Q(project__user__company=user.company) |
                Q(project__team_members__team_member=user)
            )
        elif user.is_freelancer and has_module_permissions:
            query_filters &= (
                Q(project__user=user) |
                Q(project__team_members__team_member=user)
            )
        elif has_module_permissions:
            query_filters &= (
                Q(project__user=user) |
                Q(project__team_members__team_member=user)
            )
        else:
            return BillOfMaterialPart.objects.none()

        base_queryset = BillOfMaterialPart.objects.filter(query_filters)
        self.hierarchy_base_queryset = base_queryset
        params = self.request.query_params
        filtered_queryset = base_queryset

        part_status = str(params.get("status") or "").strip()
        if part_status:
            filtered_queryset = filtered_queryset.filter(status=part_status)
        supplier = str(params.get("supplier") or "").strip()
        if supplier:
            filtered_queryset = filtered_queryset.filter(
                Q(custom_fields__supplier__icontains=supplier)
                | Q(available_product__aas_profile__supplier_name__icontains=supplier)
                | Q(available_product__custom_fields__supplier__icontains=supplier)
            )
        manufacturer = str(params.get("manufacturer") or "").strip()
        if manufacturer:
            filtered_queryset = filtered_queryset.filter(
                Q(custom_fields__manufacturer__icontains=manufacturer)
                | Q(
                    available_product__aas_profile__manufacturer_name__icontains=manufacturer
                )
                | Q(available_product__custom_fields__manufacturer__icontains=manufacturer)
            )
        available_product = params.get("available_product")
        if available_product and str(available_product).isdigit():
            filtered_queryset = filtered_queryset.filter(
                available_product_id=int(available_product)
            )

        return self.optimize_hierarchy_queryset(
            filtered_queryset.distinct().order_by("sort_order", "id")
        )

    @transaction.atomic
    def perform_create(self, serializer):
        project = serializer.validated_data.get("project")
        if not project or not self._user_can_access_project(self.request.user, project):
            raise PermissionDenied()
        instance = serializer.save()
        self._mark_aas_out_of_date(instance)

    @transaction.atomic
    def perform_update(self, serializer):
        project = serializer.validated_data.get("project", serializer.instance.project)
        if not self._user_can_access_project(self.request.user, project):
            raise PermissionDenied()
        instance = serializer.save()
        self._mark_aas_out_of_date(instance)

    def _user_can_access_project(self, user, project):
        if not user.is_authenticated:
            return False
        if project.user_id == user.id or project.team_members.filter(team_member=user).exists():
            return True
        return not user.is_freelancer and user.company_id and project.company_profile_id == user.company_id


class SolutionSpaceFilterMixin:
    filter_backends = [SolutionSpaceOrderingFilter]
    ordering_text_fields = {"assembly_name", "assembly_type"}
    ordering_natural_fields = {"assembly_name", "assembly_type"}

    def get_ordering_configured_columns(self):
        project_id = self.request.query_params.get("project")
        if project_id:
            columns = (
                SolutionSpaceConfig.objects.filter(project_id=project_id)
                .values_list("columns", flat=True)
                .first()
                or []
            )
        else:
            columns = (
                SolutionSpaceConfig.objects.filter(
                    company_profile_id=getattr(self.request.user, "company_id", None),
                    project__isnull=True,
                )
                .values_list("columns", flat=True)
                .first()
                or []
            )
        system_fields = {
            "assembly_name",
            "assembly_type",
            "required_skills",
            "provided_skills",
            "responsible_engineer",
            "projects",
            "customers",
            "year_of_construction",
        }
        return [
            column
            for column in columns
            if isinstance(column, dict)
            and str(column.get("key") or "").strip() not in system_fields
        ]

    @staticmethod
    def _csv_values(value):
        if not value:
            return []
        return [item.strip() for item in str(value).split(",") if item.strip()]

    @staticmethod
    def _common_search_filter(search):
        search_filter = (
            Q(assembly_name__icontains=search)
            | Q(responsible_engineer__first_name__icontains=search)
            | Q(responsible_engineer__last_name__icontains=search)
            | Q(responsible_engineer__email__icontains=search)
            | Q(customers__company_name__icontains=search)
        )
        if search.isdigit():
            search_filter |= Q(year_of_construction=int(search))
        return search_filter

    def _apply_common_filters(self, queryset, include_search=True):
        params = self.request.query_params
        assembly_name = params.get("assembly_name")
        if assembly_name:
            queryset = queryset.filter(assembly_name__icontains=assembly_name)

        assembly_type = params.get("assembly_type")
        valid_assembly_types = {
            choice[0] for choice in CompanySolutionSpaceItem.ASSEMBLY_TYPE_CHOICES
        }
        if assembly_type == CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS:
            queryset = queryset.filter(
                Q(assembly_type=assembly_type)
                | Q(assembly_type__isnull=True)
                | Q(assembly_type="")
            )
        elif assembly_type in valid_assembly_types:
            queryset = queryset.filter(assembly_type=assembly_type)

        for parameter, field_name in (
            ("required_skills", "required_skills"),
            ("provided_skills", "provided_skills"),
        ):
            skills = self._csv_values(params.get(parameter))
            if skills:
                skill_filter = Q()
                for skill in skills:
                    skill_filter |= Q(**{f"{field_name}__contains": [skill]})
                queryset = queryset.filter(skill_filter)

        engineer_ids = self._csv_values(params.get("responsible_engineers"))
        if engineer_ids:
            queryset = queryset.filter(responsible_engineer_id__in=engineer_ids)

        customer_ids = self._csv_values(params.get("customer_ids"))
        if customer_ids:
            queryset = queryset.filter(customers__id__in=customer_ids)

        customer_name = params.get("customer_name")
        if customer_name:
            queryset = queryset.filter(customers__company_name__icontains=customer_name)

        year = params.get("year_of_construction")
        if year:
            queryset = queryset.filter(year_of_construction=year)

        search = str(params.get("search") or "").strip()
        if search and include_search:
            queryset = queryset.filter(self._common_search_filter(search))
        return queryset.distinct()

class CompanySolutionSpaceItemViewSet(
    HierarchyPaginatedListMixin,
    SolutionSpaceFilterMixin,
    BasePartsViewSet,
):
    queryset = CompanySolutionSpaceItem.objects.all()
    serializer_class = CompanySolutionSpaceItemSerializer
    ordering_fields = [
        "id",
        "sort_order",
        "level",
        "assembly_name",
        "assembly_type",
        "required_skills",
        "provided_skills",
        "responsible_engineer",
        "projects",
        "customers",
        "year_of_construction",
        "related_assemblies",
        "linked_documents",
        "created_at",
        "updated_at",
    ]

    def optimize_hierarchy_queryset(self, queryset):
        return queryset.select_related(
            "responsible_engineer",
            "aas_product__aas_profile",
            "aas_product__aas_identity",
        ).prefetch_related(
            "projects",
            "customers",
            "documents",
            "linked_folders",
            "process_assembly_links__process_assembly",
            "product_assembly_links__product_assembly",
        )

    def get_permissions(self):
        if self.action in {"create", "copy_supplier_product"}:
            return [CompanySolutionSpacePermission(action="create", scope="company")]
        if self.action in [
            "update",
            "partial_update",
            "move_up",
            "move_down",
            "indent",
            "outdent",
            "copy_to_project",
            "select_for_project",
        ]:
            return [CompanySolutionSpacePermission(action="update", scope="company")]
        if self.action == "destroy":
            return [CompanySolutionSpacePermission(action="delete", scope="company")]
        return [CompanySolutionSpacePermission(action="view", scope="company")]

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser) or user.is_freelancer or not user.company_id:
            return CompanySolutionSpaceItem.objects.none()
        if not require_any_permission_multiple_modules(
            user,
            {"company_assembly_catalog": ["view", "full_access"]},
            "company",
            self.request.query_params,
        ):
            return CompanySolutionSpaceItem.objects.none()

        base_queryset = CompanySolutionSpaceItem.objects.filter(
            company_profile_id=user.company_id,
        )
        parent = self.request.query_params.get("parent")
        if parent == "root":
            base_queryset = base_queryset.filter(parent__isnull=True)
        elif parent:
            base_queryset = base_queryset.filter(parent_id=parent)
        self.hierarchy_base_queryset = base_queryset

        filtered_queryset = self._apply_common_filters(
            base_queryset,
            include_search=False,
        )
        project_ids = self._csv_values(self.request.query_params.get("project_ids"))
        if project_ids:
            filtered_queryset = filtered_queryset.filter(projects__id__in=project_ids)
        project_name = self.request.query_params.get("project_name")
        if project_name:
            filtered_queryset = filtered_queryset.filter(
                projects__project_name__icontains=project_name,
            )
        project_reference = self.request.query_params.get("project_reference")
        if project_reference:
            if str(project_reference).isdigit():
                filtered_queryset = filtered_queryset.filter(
                    projects__id=int(project_reference),
                )
            else:
                filtered_queryset = filtered_queryset.none()

        linked_ids = self._csv_values(
            self.request.query_params.get("linked_assembly_ids")
        )
        if linked_ids:
            filtered_queryset = filtered_queryset.filter(
                Q(process_assembly_links__process_assembly_id__in=linked_ids)
                | Q(product_assembly_links__product_assembly_id__in=linked_ids)
            )

        search = str(self.request.query_params.get("search") or "").strip()
        if search:
            search_filter = self._common_search_filter(search)
            search_filter |= Q(projects__project_name__icontains=search)
            if search.isdigit():
                search_filter |= Q(projects__id=int(search))
            filtered_queryset = filtered_queryset.filter(search_filter).distinct()

        return self.optimize_hierarchy_queryset(
            filtered_queryset.distinct().order_by("sort_order", "id")
        )

    def perform_create(self, serializer):
        user = self.request.user
        if user.is_freelancer or not user.company_id:
            raise PermissionDenied()
        instance = serializer.save(company_profile=user.company)
        self._mark_changed_company_assemblies_out_of_date(instance)

    def perform_update(self, serializer):
        if serializer.instance.company_profile_id != self.request.user.company_id:
            raise PermissionDenied()
        instance = serializer.save(company_profile=serializer.instance.company_profile)
        self._mark_changed_company_assemblies_out_of_date(instance)

    def _mark_changed_company_assemblies_out_of_date(self, instance):
        self._mark_aas_out_of_date(instance)
        changed_ids = getattr(instance, "_changed_related_assembly_ids", set())
        for related in CompanySolutionSpaceItem.objects.filter(id__in=changed_ids):
            self._mark_aas_out_of_date(related)

    def perform_destroy(self, instance):
        related_ids = set(
            instance.process_assembly_links.values_list(
                "process_assembly_id",
                flat=True,
            )
        ) | set(
            instance.product_assembly_links.values_list(
                "product_assembly_id",
                flat=True,
            )
        )
        super().perform_destroy(instance)
        for related in CompanySolutionSpaceItem.objects.filter(id__in=related_ids):
            self._mark_aas_out_of_date(related)

    @action(detail=False, methods=["post"], url_path="copy-supplier-product")
    def copy_supplier_product(self, request):
        from aas_integration.permissions import has_aas_models_permission

        if (
            request.user.is_freelancer
            or not request.user.company_id
            or not has_aas_models_permission(request.user, write=True)
        ):
            raise PermissionDenied()
        serializer = SupplierProductImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = import_supplier_product_to_company_catalog(
                company_profile=request.user.company,
                actor=request.user,
                payload=serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages}) from exc

        product = result.product
        profile = product.aas_profile
        identity = getattr(product, "aas_identity", None)
        created = result.created_product or result.created_assembly
        return Response(
            {
                "created": created,
                "created_product": result.created_product,
                "created_assembly": result.created_assembly,
                "assembly": self.get_serializer(result.assembly).data,
                "aas_model": {
                    "product_id": product.id,
                    "origin": profile.origin,
                    "status": "out_of_date" if identity else "unpublished",
                    "supplier": profile.supplier_name,
                    "supplier_aas_id": profile.supplier_aas_id,
                    "global_asset_id": profile.global_asset_id,
                    "source_payload_hash": profile.source_payload_hash,
                },
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="copy-to-project")
    @transaction.atomic
    def copy_to_project(self, request, pk=None):
        source = self.get_object()
        if source.assembly_type not in {
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        }:
            raise ValidationError(
                {
                    "assembly_type": (
                        "Classify the source in the Product or Process Assembly "
                        "Catalog before selecting it for a project."
                    )
                }
            )
        project = get_object_or_404(Project, id=request.data.get("project"))
        if project.company_profile_id != source.company_profile_id:
            raise PermissionDenied("The target project must belong to the same company.")
        if not self._user_can_access_project(request.user, project):
            raise PermissionDenied()
        if not require_any_permission_multiple_modules(
            request.user,
            {"project_assembly_catalog": ["create", "full_access"]},
            "project",
            {"project": project.id},
        ):
            raise PermissionDenied()

        from project_management.services.catalog_selection import select_company_assembly
        copied, root_created = select_company_assembly(source, project, request.user)
        self._mark_aas_out_of_date(copied)
        return Response(
            ProjectSolutionSpaceItemSerializer(
                copied,
                context={"request": request},
            ).data,
            status=status.HTTP_201_CREATED if root_created else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="select-for-project")
    @transaction.atomic
    def select_for_project(self, request):
        class SelectionSerializer(serializers.Serializer):
            project = serializers.IntegerField(min_value=1)
            selected_ids = serializers.ListField(
                child=serializers.IntegerField(min_value=1), min_length=1, max_length=100
            )

        data = SelectionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        ids = data.validated_data["selected_ids"]
        if len(ids) != len(set(ids)):
            raise ValidationError("Select each assembly only once.")
        project = get_object_or_404(Project.objects.select_for_update(), pk=data.validated_data["project"])
        if project.company_profile_id != request.user.company_id or not self._user_can_access_project(request.user, project):
            raise PermissionDenied()
        if not ProjectSolutionSpacePermission()._check_create_permission(
            request.user, {"project": project}
        ):
            raise PermissionDenied()
        visible_ids = self.get_queryset().filter(pk__in=ids).values("pk")
        sources = list(CompanySolutionSpaceItem.objects.select_for_update().filter(pk__in=visible_ids).order_by("level", "sort_order", "id"))
        if len(sources) != len(ids):
            raise ValidationError("A selected assembly is no longer available.")
        for source in sources:
            self.check_object_permissions(request, source)
        if any(row.assembly_type not in {"components", "production_lines"} for row in sources):
            raise ValidationError("Classify all selected assemblies before adding them to a project.")
        from project_management.services.catalog_selection import select_company_assembly

        copied_ids = []
        for source in sources:
            copied, _ = select_company_assembly(source, project, request.user, refresh_existing=False)
            self._mark_aas_out_of_date(copied)
            copied_ids.append(copied.pk)
        return Response({"selected_ids": ids, "project_item_ids": copied_ids})

    @staticmethod
    def _user_can_access_project(user, project):
        if project.user_id == user.id or project.team_members.filter(team_member=user).exists():
            return True
        return bool(
            not user.is_freelancer
            and user.company_id
            and project.company_profile_id == user.company_id
        )


class ProjectSolutionSpaceItemViewSet(
    HierarchyPaginatedListMixin,
    SolutionSpaceFilterMixin,
    BasePartsViewSet,
):
    queryset = ProjectSolutionSpaceItem.objects.all()
    serializer_class = ProjectSolutionSpaceItemSerializer
    ordering_fields = [
        "id",
        "sort_order",
        "level",
        "assembly_name",
        "assembly_type",
        "required_skills",
        "provided_skills",
        "responsible_engineer",
        "customers",
        "year_of_construction",
        "related_assemblies",
        "linked_documents",
        "created_at",
        "updated_at",
    ]

    def optimize_hierarchy_queryset(self, queryset):
        return queryset.select_related(
            "responsible_engineer",
            "source_company_item__responsible_engineer",
            "source_company_item__aas_product__aas_profile",
            "source_company_item__aas_product__aas_identity",
        ).prefetch_related(
            "customers",
            "object_assessments",
            "documents",
            "requirements",
            "linked_folders",
            "source_company_item__process_assembly_links__process_assembly",
            "source_company_item__product_assembly_links__product_assembly",
        )

    def get_permissions(self):
        if self.action == "copy_to_bom":
            return [BillOfMaterialPermission(action="create", scope="project")]
        if self.action == "create":
            return [ProjectSolutionSpacePermission(action="create", scope="project")]
        if self.action in [
            "update",
            "partial_update",
            "move_up",
            "move_down",
            "indent",
            "outdent",
        ]:
            return [ProjectSolutionSpacePermission(action="update", scope="project")]
        if self.action == "destroy":
            return [ProjectSolutionSpacePermission(action="delete", scope="project")]
        return [ProjectSolutionSpacePermission(action="view", scope="project")]

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ProjectSolutionSpaceItem.objects.none()

        project_id = self.request.query_params.get("project")
        query_filters = Q()
        if project_id:
            query_filters &= Q(project_id=project_id)
        if self.kwargs.get("pk") and not project_id:
            query_filters &= self._project_membership_filter(user)
        else:
            has_company_project_access = (
                not user.is_freelancer
                and require_any_permission_multiple_modules(
                    user,
                    {"projects": ["full_access", "view_all_access"]},
                    "company",
                    self.request.query_params,
                )
            )
            has_module_access = require_any_permission_multiple_modules(
                user,
                {"project_assembly_catalog": ["view", "full_access"]},
                "project",
                self.request.query_params,
            )
            if not has_company_project_access and not has_module_access:
                return ProjectSolutionSpaceItem.objects.none()
            query_filters &= self._project_membership_filter(
                user,
                include_company=has_company_project_access,
            )

        base_queryset = ProjectSolutionSpaceItem.objects.filter(
            query_filters,
            source_company_item__isnull=False,
        )
        parent = self.request.query_params.get("parent")
        if parent == "root":
            base_queryset = base_queryset.filter(parent__isnull=True)
        elif parent:
            base_queryset = base_queryset.filter(parent_id=parent)
        self.hierarchy_base_queryset = base_queryset
        filtered_queryset = self._apply_common_filters(base_queryset)
        source_ids = self._csv_values(
            self.request.query_params.get("source_company_item_ids")
        )
        if source_ids:
            filtered_queryset = filtered_queryset.filter(
                source_company_item_id__in=source_ids
            )
        return self.optimize_hierarchy_queryset(
            filtered_queryset.distinct().order_by("sort_order", "id")
        )

    def perform_create(self, serializer):
        project = serializer.validated_data.get("project")
        if not project or not self._user_can_access_project(self.request.user, project):
            raise PermissionDenied()
        instance = serializer.save()
        self._mark_aas_out_of_date(instance)

    def perform_update(self, serializer):
        project = serializer.validated_data.get("project", serializer.instance.project)
        if not self._user_can_access_project(self.request.user, project):
            raise PermissionDenied()
        instance = serializer.save()
        self._mark_aas_out_of_date(instance)

    @action(detail=True, methods=["post"], url_path="copy-to-bom")
    @transaction.atomic
    def copy_to_bom(self, request, pk=None):
        if settings.PROJECT_WORKSTATION_COMPOSITION_ENABLED:
            self.get_object()
            raise ValidationError({
                "detail": "Add this part in Engineering Desk, mark its workstation hierarchy Agreed, then use Copy to BoM."
            })
        selected = self.get_object()
        selected = ProjectSolutionSpaceItem.objects.select_for_update().get(
            pk=selected.pk,
        )
        source = selected.source_company_item

        if (
            selected.assembly_type
            != CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS
            or not source
            or source.assembly_type
            != CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS
        ):
            raise ValidationError(
                {
                    "detail": (
                        "Only approved Product Assembly Catalog entries can be "
                        "copied into the project BoM."
                    )
                }
            )

        existing = BillOfMaterialPart.objects.filter(
            project=selected.project,
            source_project_assembly=selected,
        ).order_by("id").first()
        if existing:
            return Response(
                {
                    "created": False,
                    "bom_part": BillOfMaterialPartSerializer(
                        existing,
                        context=self.get_serializer_context(),
                    ).data,
                },
                status=status.HTTP_200_OK,
            )

        product = source.aas_product
        profile = getattr(product, "aas_profile", None) if product else None
        custom_fields = dict((product.custom_fields if product else None) or {})
        custom_fields.update(dict(source.custom_fields or {}))
        custom_fields.update(dict(selected.custom_fields or {}))

        profile_fields = {
            "manufacturer": getattr(profile, "manufacturer_name", ""),
            "supplier": getattr(profile, "supplier_name", ""),
            "supplier_aas_id": getattr(profile, "supplier_aas_id", ""),
            "global_asset_id": getattr(profile, "global_asset_id", ""),
            "order_code": getattr(profile, "order_code", ""),
        }
        for field, value in profile_fields.items():
            if value:
                custom_fields[field] = value
        if custom_fields.get("global_asset_id"):
            custom_fields.setdefault("asset_id", custom_fields["global_asset_id"])
        custom_fields["quantity"] = 1

        next_sort_order = (
            BillOfMaterialPart.objects.filter(
                project=selected.project,
                parent__isnull=True,
            ).aggregate(max_order=Max("sort_order"))["max_order"]
            or 0
        ) + 1
        bom_part = BillOfMaterialPart.objects.create(
            project=selected.project,
            parent=None,
            level=1,
            sort_order=next_sort_order,
            name=source.assembly_name,
            part_number=(
                product.part_number
                if product and product.part_number
                else getattr(profile, "order_code", None)
            ),
            available_product=product,
            source_project_assembly=selected,
            custom_fields=custom_fields,
            created_by=request.user,
            modified_by=request.user,
        )
        bom_part.object_assessments.set(selected.object_assessments.all())
        bom_part.documents.set(selected.documents.all())
        bom_part.linked_folders.set(selected.linked_folders.all())
        bom_part.requirements.set(selected.requirements.all())
        self._mark_aas_out_of_date(bom_part)

        return Response(
            {
                "created": True,
                "bom_part": BillOfMaterialPartSerializer(
                    bom_part,
                    context=self.get_serializer_context(),
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="move-up")
    def move_up(self, request, pk=None):
        return self._reject_company_controlled_hierarchy_change()

    @action(detail=True, methods=["post"], url_path="move-down")
    def move_down(self, request, pk=None):
        return self._reject_company_controlled_hierarchy_change()

    @action(detail=True, methods=["post"])
    def indent(self, request, pk=None):
        return self._reject_company_controlled_hierarchy_change()

    @action(detail=True, methods=["post"])
    def outdent(self, request, pk=None):
        return self._reject_company_controlled_hierarchy_change()

    @staticmethod
    def _reject_company_controlled_hierarchy_change():
        return Response(
            {
                "detail": (
                    "Project assembly hierarchy is controlled by the approved "
                    "company catalog."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    @staticmethod
    def _project_membership_filter(user, include_company=False):
        membership = Q(project__user=user) | Q(project__team_members__team_member=user)
        if include_company and not user.is_freelancer:
            membership |= Q(project__company_profile=user.company) | Q(
                project__user__company=user.company,
            )
        return membership

    @staticmethod
    def _user_can_access_project(user, project):
        if project.user_id == user.id or project.team_members.filter(team_member=user).exists():
            return True
        return bool(
            not user.is_freelancer
            and user.company_id
            and project.company_profile_id == user.company_id
        )


class MatrixCommentViewSet(viewsets.ViewSet):
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def _get_target(self, target_type, item_id):
        if target_type == MatrixComment.TARGET_SOLUTION_DEMAND:
            return get_object_or_404(SolutionDemand, id=item_id)
        if target_type == MatrixComment.TARGET_REQUIREMENT:
            return get_object_or_404(Requirement, id=item_id)
        if target_type == MatrixComment.TARGET_TRANSPORT:
            return get_object_or_404(Transport, id=item_id)
        if target_type == MatrixComment.TARGET_AVAILABLE_PRODUCT_PART:
            return get_object_or_404(AvailableProductPart, id=item_id)
        if target_type == MatrixComment.TARGET_MACHINE:
            return get_object_or_404(Machine, id=item_id)
        if target_type == MatrixComment.TARGET_EQUIPMENT:
            return get_object_or_404(Equipment, id=item_id)
        if target_type == MatrixComment.TARGET_BOM_PART:
            return get_object_or_404(BillOfMaterialPart, id=item_id)
        if target_type == MatrixComment.TARGET_COMPANY_SOLUTION_SPACE:
            return get_object_or_404(CompanySolutionSpaceItem, id=item_id)
        if target_type == MatrixComment.TARGET_PROJECT_SOLUTION_SPACE:
            return get_object_or_404(ProjectSolutionSpaceItem, id=item_id)
        raise Http404

    def _check_target_permission(self, request, target_type, target, action):
        if target_type == MatrixComment.TARGET_AVAILABLE_PRODUCT_PART:
            permission = AvailableProductPartPermission(action=action, scope="company")
            check_method = (
                permission._check_view_permission if action == "view"
                else permission._check_delete_permission if action == "delete"
                else permission._check_update_permission
            )
            if not permission._belongs_to_company(request.user, target) or not check_method(request.user):
                raise PermissionDenied()
            return
        if target_type == MatrixComment.TARGET_MACHINE:
            permission = MachinePermission(action=action, scope="company")
            check_method = (
                permission._check_view_permission if action == "view"
                else permission._check_delete_permission if action == "delete"
                else permission._check_update_permission
            )
            if not permission._belongs_to_company(request.user, target) or not check_method(request.user):
                raise PermissionDenied()
            return
        if target_type == MatrixComment.TARGET_EQUIPMENT:
            permission = EquipmentPermission(action=action, scope="company")
            check_method = (
                permission._check_view_permission if action == "view"
                else permission._check_delete_permission if action == "delete"
                else permission._check_update_permission
            )
            if not permission._belongs_to_company(request.user, target) or not check_method(request.user):
                raise PermissionDenied()
            return
        if target_type == MatrixComment.TARGET_BOM_PART:
            permission = BillOfMaterialPermission(action=action, scope="project")
            context = {"project": target.project}
            if action == "view":
                allowed = permission._check_view_permission_project(request.user, target, context)
            elif action == "delete":
                allowed = permission._check_delete_permission_project(request.user, target, context)
            else:
                allowed = permission._check_update_permission_project(request.user, target, context)
            if not allowed:
                raise PermissionDenied()
            return
        if target_type == MatrixComment.TARGET_COMPANY_SOLUTION_SPACE:
            permission = CompanySolutionSpacePermission(action=action, scope="company")
            check_method = (
                permission._check_view_permission
                if action == "view"
                else permission._check_delete_permission
                if action == "delete"
                else permission._check_update_permission
            )
            if not permission._belongs_to_company(request.user, target) or not check_method(request.user):
                raise PermissionDenied()
            return
        if target_type == MatrixComment.TARGET_PROJECT_SOLUTION_SPACE:
            permission = ProjectSolutionSpacePermission(action=action, scope="project")
            context = {"project": target.project}
            if action == "view":
                allowed = permission._check_view_permission_project(request.user, target, context)
            elif action == "delete":
                allowed = permission._check_delete_permission_project(request.user, target, context)
            else:
                allowed = permission._check_update_permission_project(request.user, target, context)
            if not allowed:
                raise PermissionDenied()
            return
        permission = {
            MatrixComment.TARGET_SOLUTION_DEMAND: SolutionDemandPermission(
                action=action, scope="project"
            ),
            MatrixComment.TARGET_REQUIREMENT: RequirementPermission(
                action=action, scope="project"
            ),
            MatrixComment.TARGET_TRANSPORT: TransportPermission(
                action=action, scope="project"
            ),
        }[target_type]
        context = {"project": target.project}
        if action == "view":
            allowed = permission._check_view_permission_project(request.user, target, context)
        elif action == "delete":
            allowed = permission._check_delete_permission_project(request.user, target, context)
        else:
            allowed = permission._check_update_permission_project(request.user, target, context)
        if not allowed:
            raise PermissionDenied()

    def _assert_author(self, request, comment):
        if comment.sender_id != getattr(request.user, "id", None):
            raise PermissionDenied("Only the comment author can modify this comment.")

    def _get_queryset(self, target_type, item_id):
        if target_type == MatrixComment.TARGET_SOLUTION_DEMAND:
            return MatrixComment.objects.filter(target_type=target_type, solution_demand_id=item_id)
        if target_type == MatrixComment.TARGET_REQUIREMENT:
            return MatrixComment.objects.filter(target_type=target_type, requirement_id=item_id)
        if target_type == MatrixComment.TARGET_TRANSPORT:
            return MatrixComment.objects.filter(target_type=target_type, transport_id=item_id)
        if target_type == MatrixComment.TARGET_AVAILABLE_PRODUCT_PART:
            return MatrixComment.objects.filter(target_type=target_type, available_product_part_id=item_id)
        if target_type == MatrixComment.TARGET_MACHINE:
            return MatrixComment.objects.filter(target_type=target_type, machine_id=item_id)
        if target_type == MatrixComment.TARGET_EQUIPMENT:
            return MatrixComment.objects.filter(target_type=target_type, equipment_id=item_id)
        if target_type == MatrixComment.TARGET_BOM_PART:
            return MatrixComment.objects.filter(target_type=target_type, bom_part_id=item_id)
        if target_type == MatrixComment.TARGET_COMPANY_SOLUTION_SPACE:
            return MatrixComment.objects.filter(
                target_type=target_type,
                company_solution_space_item_id=item_id,
            )
        return MatrixComment.objects.filter(
            target_type=target_type,
            project_solution_space_item_id=item_id,
        )

    def list(self, request, target_type=None, item_id=None):
        target = self._get_target(target_type, item_id)
        self._check_target_permission(request, target_type, target, "view")
        queryset = self._get_queryset(target_type, item_id).filter(replied_to__isnull=True).order_by("-created_at")
        serializer = MatrixCommentThreadSerializer(queryset, many=True, context={"request": request})
        return Response({"results": serializer.data})

    def create(self, request, target_type=None, item_id=None):
        target = self._get_target(target_type, item_id)
        self._check_target_permission(request, target_type, target, "update")
        data = request.data.copy()
        data["target_type"] = target_type
        if target_type == MatrixComment.TARGET_SOLUTION_DEMAND:
            data["solution_demand"] = target.id
        elif target_type == MatrixComment.TARGET_REQUIREMENT:
            data["requirement"] = target.id
        elif target_type == MatrixComment.TARGET_TRANSPORT:
            data["transport"] = target.id
        elif target_type == MatrixComment.TARGET_AVAILABLE_PRODUCT_PART:
            data["available_product_part"] = target.id
        elif target_type == MatrixComment.TARGET_MACHINE:
            data["machine"] = target.id
        elif target_type == MatrixComment.TARGET_EQUIPMENT:
            data["equipment"] = target.id
        elif target_type == MatrixComment.TARGET_BOM_PART:
            data["bom_part"] = target.id
        elif target_type == MatrixComment.TARGET_COMPANY_SOLUTION_SPACE:
            data["company_solution_space_item"] = target.id
        else:
            data["project_solution_space_item"] = target.id
        serializer = MatrixCommentSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, target_type=None, item_id=None, comment_id=None):
        target = self._get_target(target_type, item_id)
        self._check_target_permission(request, target_type, target, "update")
        comment = get_object_or_404(self._get_queryset(target_type, item_id), id=comment_id)
        self._assert_author(request, comment)
        serializer = MatrixCommentSerializer(comment, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def destroy(self, request, target_type=None, item_id=None, comment_id=None):
        target = self._get_target(target_type, item_id)
        self._check_target_permission(request, target_type, target, "update")
        comment = get_object_or_404(self._get_queryset(target_type, item_id), id=comment_id)
        self._assert_author(request, comment)
        comment.is_deleted = True
        comment.deleted_by = request.user if request.user.is_authenticated else None
        comment.deleted_at = timezone.now()
        comment.save(update_fields=["is_deleted", "deleted_by", "deleted_at", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    def mention_candidates(self, request, target_type=None, item_id=None):
        target = self._get_target(target_type, item_id)
        self._check_target_permission(request, target_type, target, "view")
        search = request.query_params.get("search", "")
        raw_limit = request.query_params.get("limit", 20)

        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 20
        limit = min(max(limit, 1), 50)

        return Response(
            {
                "results": get_matrix_mention_candidates(
                    target=target,
                    target_type=target_type,
                    search=search,
                    limit=limit,
                )
            },
            status=status.HTTP_200_OK,
        )

    def upload_attachments(self, request, target_type=None, item_id=None):
        target = self._get_target(target_type, item_id)
        self._check_target_permission(request, target_type, target, "update")
        files = list(request.FILES.getlist("files") or [])

        if not files:
            single_file = request.FILES.get("file")
            if single_file:
                files = [single_file]

        if not files:
            return Response(
                {"error": "At least one file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_attachment_files = int(getattr(settings, "CHAT_ATTACHMENT_MAX_FILES", 5) or 5)
        if len(files) > max_attachment_files:
            return Response(
                {"error": f"You can upload at most {max_attachment_files} files per comment."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_total_size_mb = int(getattr(settings, "CHAT_ATTACHMENT_MAX_TOTAL_SIZE_MB", 25) or 25)
        max_total_size_bytes = max_total_size_mb * 1024 * 1024
        blocked_extensions = {
            str(ext).lower()
            for ext in getattr(
                settings,
                "CHAT_BLOCKED_ATTACHMENT_EXTENSIONS",
                [
                    ".exe",
                    ".bat",
                    ".cmd",
                    ".msi",
                    ".sql",
                    ".com",
                    ".scr",
                    ".pif",
                    ".vb",
                    ".vbs",
                    ".jar",
                ],
            )
        }

        validated_files = []
        total_size = 0

        for upload in files:
            original_name = os.path.basename(upload.name or "attachment")
            extension = os.path.splitext(original_name)[1].lower()

            if extension and extension in blocked_extensions:
                return Response(
                    {"error": f"File type '{extension}' is not allowed."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            file_size = int(upload.size or 0)
            total_size += file_size
            if total_size > max_total_size_bytes:
                return Response(
                    {"error": f"Total attachments size cannot exceed {max_total_size_mb}MB."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            validated_files.append(
                {
                    "upload": upload,
                    "original_name": original_name,
                    "extension": extension,
                    "size": file_size,
                    "mime_type": (upload.content_type or "").lower() or None,
                }
            )

        uploaded_attachments = []
        for file_item in validated_files:
            extension = file_item["extension"]
            storage_path = default_storage.save(
                f"matrix-comments/attachments/{target_type}/{item_id}/{uuid4().hex}{extension}",
                file_item["upload"],
            )
            attachment_url = default_storage.url(storage_path)
            if not str(attachment_url).startswith("http"):
                attachment_url = request.build_absolute_uri(attachment_url)

            uploaded_attachments.append(
                {
                    "url": attachment_url,
                    "file_name": file_item["original_name"],
                    "mime_type": file_item["mime_type"],
                    "size": file_item["size"],
                    "extension": extension or None,
                }
            )

        return Response(
            {
                "attachments": uploaded_attachments,
                "total_size": total_size,
            },
            status=status.HTTP_201_CREATED,
        )

    def toggle_reaction(self, request, target_type=None, item_id=None, comment_id=None):
        target = self._get_target(target_type, item_id)
        self._check_target_permission(request, target_type, target, "view")
        comment = get_object_or_404(self._get_queryset(target_type, item_id), id=comment_id)
        key = request.data.get("emoji") or request.data.get("key")
        if not key:
            return Response({"detail": "Reaction key is required."}, status=status.HTTP_400_BAD_REQUEST)
        key = str(key)
        if comment.is_deleted:
            return Response({"detail": "Cannot react to deleted comment."}, status=status.HTTP_400_BAD_REQUEST)
        if len(key) > 255:
            return Response({"detail": "Reaction key is too long."}, status=status.HTTP_400_BAD_REQUEST)
        existing = MatrixCommentReaction.objects.filter(comment=comment, user=request.user, key=key).first()
        if existing:
            existing.delete()
        else:
            MatrixCommentReaction.objects.create(comment=comment, user=request.user, key=key)
        refreshed_comment = self._get_queryset(target_type, item_id).get(id=comment.id)
        serializer = MatrixCommentSerializer(refreshed_comment, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
