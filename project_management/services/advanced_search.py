import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib.postgres.search import (
    SearchQuery,
    SearchRank,
    TrigramSimilarity,
    TrigramWordSimilarity,
)
from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Greatest

from project_management.models import (
    ActionItem,
    ActionItemComment,
    MatrixComment,
    SearchContentSegment,
    SearchFieldValue,
    SearchIndexJob,
    SearchRecord,
    SearchRelation,
    SemanticPropertyDefinition,
)
from project_management.services.document_access import get_accessible_document_queryset
from project_management.services.search_indexing import search_record_route


RESULT_TYPE_SOURCES = {
    "document": {SearchRecord.SourceType.DOCUMENT},
    "object_assessment": {SearchRecord.SourceType.OBJECT_ASSESSMENT},
    "parts_and_assemblies": {
        SearchRecord.SourceType.AVAILABLE_PRODUCT_PART,
        SearchRecord.SourceType.MACHINE,
        SearchRecord.SourceType.EQUIPMENT,
        SearchRecord.SourceType.BOM_PART,
        SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
        SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
        SearchRecord.SourceType.SOLUTION_DEMAND,
        SearchRecord.SourceType.REQUIREMENT,
    },
    "financial": {
        SearchRecord.SourceType.PURCHASE_ORDER,
        SearchRecord.SourceType.INVOICE,
    },
    "comment": {
        SearchRecord.SourceType.ACTION_ITEM_COMMENT,
        SearchRecord.SourceType.MATRIX_COMMENT,
    },
}

SOURCE_GROUPS = {
    source_type: group
    for group, source_types in RESULT_TYPE_SOURCES.items()
    for source_type in source_types
}

BUSINESS_SOURCE_TYPES = (
    RESULT_TYPE_SOURCES["parts_and_assemblies"]
    | RESULT_TYPE_SOURCES["financial"]
)

SEARCH_KEY_ALIASES = {
    "required-skill": "required_skills",
    "required_skill": "required_skills",
    "erforderliche-fahigkeit": "required_skills",
    "competence-requise": "required_skills",
    "provided-skill": "provided_skills",
    "provided_skill": "provided_skills",
    "bereitgestellte-fahigkeit": "provided_skills",
    "competence-fournie": "provided_skills",
    "manufacturer": "manufacturer",
    "hersteller": "manufacturer",
    "fabricant": "manufacturer",
    "project": "project",
    "projekt": "project",
    "projet": "project",
    "semantic-id": "semantic_id",
    "semantic_id": "semantic_id",
    "type": "type",
    "typ": "type",
    "status": "status",
    "linked-to": "linked_to",
    "linked_to": "linked_to",
}

ALLOWED_RELATED_TYPES = {
    *SearchRecord.SourceType.values,
    "action_item",
    "contact_company",
    "folder",
    "project",
    "transport",
}


class AdvancedSearchValidationError(ValueError):
    def __init__(self, detail: str, *, suggestions: list[str] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.suggestions = suggestions or []


class SearchRequestProxy:
    def __init__(self, request, query_params: dict[str, Any]):
        self._request = request
        self.user = request.user
        self.query_params = query_params
        self.GET = query_params
        self.data = {}
        self.method = getattr(request, "method", "POST")

    def __getattr__(self, name):
        return getattr(self._request, name)


@dataclass(frozen=True)
class ParsedSearch:
    text: str
    filters: dict[str, list[str]]


def parse_search_language(raw_query: str) -> ParsedSearch:
    filters: dict[str, list[str]] = defaultdict(list)
    text_tokens = []
    try:
        tokens = shlex.split(raw_query or "")
    except ValueError as exc:
        raise AdvancedSearchValidationError("The search contains an unmatched quote.") from exc

    for token in tokens:
        if ":" not in token:
            text_tokens.append(token)
            continue
        key, value = token.split(":", 1)
        normalized_key = SEARCH_KEY_ALIASES.get(key.casefold())
        if not normalized_key:
            raise AdvancedSearchValidationError(
                f'Unknown search field "{key}".',
                suggestions=sorted({key for key in SEARCH_KEY_ALIASES if "_" not in key}),
            )
        if not value.strip():
            raise AdvancedSearchValidationError(f'A value is required for "{key}".')
        filters[normalized_key].append(value.strip())
    return ParsedSearch(text=" ".join(text_tokens).strip(), filters=dict(filters))


def _view_queryset(view_class, request):
    view = view_class()
    view.request = request
    view.action = "list"
    view.args = ()
    view.kwargs = {}
    view.format_kwarg = None
    return view.get_queryset()


def _proxy_request(request, scope: dict[str, Any]):
    query_params = {}
    original = getattr(request, "query_params", {})
    solution = original.get("solution")
    if solution:
        query_params["solution"] = solution
    scope_type = scope.get("type", "company")
    if scope_type == "project":
        project_id = scope.get("id")
        if not project_id:
            raise AdvancedSearchValidationError("A project ID is required for project search.")
        query_params.update(
            {
                "project": project_id,
                "project_id": project_id,
                "folder__project": project_id,
            }
        )
    elif scope_type == "global":
        if getattr(request.user, "company_id", None):
            query_params["company_profile"] = request.user.company_id
    elif scope_type != "company":
        raise AdvancedSearchValidationError(
            "Search scope must be company, project, or global."
        )
    elif getattr(request.user, "company_id", None):
        query_params["company_profile"] = request.user.company_id
    return SearchRequestProxy(request, query_params)


def _authorized_project_ids(request, scope: dict[str, Any]) -> set[int]:
    from project_management.views.project import ProjectViewSet

    proxy = _proxy_request(request, scope)
    project_ids = set(_view_queryset(ProjectViewSet, proxy).values_list("id", flat=True))
    return project_ids


def _validate_global_project_ids(scope: dict[str, Any], authorized_project_ids: set[int]):
    requested_ids = scope.get("project_ids") or []
    if not requested_ids:
        return
    invalid_ids = sorted(set(requested_ids) - authorized_project_ids)
    if invalid_ids:
        raise AdvancedSearchValidationError(
            "One or more project IDs are not accessible.",
            suggestions=[str(project_id) for project_id in invalid_ids],
        )


def _apply_global_scope_filter(
    records,
    scope: dict[str, Any],
    authorized_project_ids: set[int],
):
    include_company = bool(scope.get("include_company", True))
    requested_project_ids = scope.get("project_ids") or []

    if include_company and not requested_project_ids:
        return records

    scope_filter = Q(pk__in=[])
    if include_company:
        scope_filter |= Q(project__isnull=True)

    if requested_project_ids:
        scope_filter |= Q(project_id__in=requested_project_ids)
    elif not include_company and authorized_project_ids:
        scope_filter |= Q(project_id__in=authorized_project_ids)

    filtered = records.filter(scope_filter)
    return filtered


def enabled_source_types() -> set[str]:
    enabled = {SearchRecord.SourceType.DOCUMENT}
    if getattr(settings, "ADVANCED_SEARCH_OBJECT_ASSESSMENTS", True):
        enabled.add(SearchRecord.SourceType.OBJECT_ASSESSMENT)
    if getattr(settings, "ADVANCED_SEARCH_BUSINESS_RECORDS", True):
        enabled.update(BUSINESS_SOURCE_TYPES)
    if getattr(settings, "ADVANCED_SEARCH_COMMENTS", True):
        enabled.update(RESULT_TYPE_SOURCES["comment"])
    return enabled


def authorized_source_querysets(
    request,
    scope: dict[str, Any],
    source_types: set[str] | None = None,
    authorized_project_ids: set[int] | None = None,
):
    from project_management.models import Folder
    from project_management.views.action_item import ActionItemViewSet
    from project_management.views.billing import InvoiceViewSet
    from project_management.views.contact import ContactCompanyViewSet
    from project_management.views.matrix import (
        AvailableProductPartViewSet,
        BillOfMaterialPartViewSet,
        CompanySolutionSpaceItemViewSet,
        EquipmentViewSet,
        MachineViewSet,
        ProjectSolutionSpaceItemViewSet,
        RequirementViewSet,
        SolutionDemandViewSet,
        TransportViewSet,
    )
    from project_management.views.object_assessment import ObjectAssessmentViewSet
    from project_management.views.project import ProjectViewSet
    from project_management.views.purchase_order import PurchaseOrderViewSet

    requested = set(source_types or enabled_source_types()) & enabled_source_types()
    proxy = _proxy_request(request, scope)
    scope_type = scope.get("type", "company")
    project_id = scope.get("id") if scope_type == "project" else None
    source_querysets = {}
    accessible_documents = get_accessible_document_queryset(
        proxy,
        project_id=project_id,
    )
    if SearchRecord.SourceType.DOCUMENT in requested:
        source_querysets[SearchRecord.SourceType.DOCUMENT] = accessible_documents
    source_querysets["folder"] = Folder.objects.filter(
        id__in=accessible_documents.values("folder_id")
    )
    source_querysets["project"] = _view_queryset(ProjectViewSet, proxy)
    source_querysets["contact_company"] = _view_queryset(ContactCompanyViewSet, proxy)
    view_sources = {
        SearchRecord.SourceType.OBJECT_ASSESSMENT: ObjectAssessmentViewSet,
        SearchRecord.SourceType.SOLUTION_DEMAND: SolutionDemandViewSet,
        SearchRecord.SourceType.REQUIREMENT: RequirementViewSet,
        SearchRecord.SourceType.AVAILABLE_PRODUCT_PART: AvailableProductPartViewSet,
        SearchRecord.SourceType.MACHINE: MachineViewSet,
        SearchRecord.SourceType.EQUIPMENT: EquipmentViewSet,
        SearchRecord.SourceType.BOM_PART: BillOfMaterialPartViewSet,
        SearchRecord.SourceType.COMPANY_SOLUTION_SPACE: CompanySolutionSpaceItemViewSet,
        SearchRecord.SourceType.PROJECT_SOLUTION_SPACE: ProjectSolutionSpaceItemViewSet,
        SearchRecord.SourceType.PURCHASE_ORDER: PurchaseOrderViewSet,
        SearchRecord.SourceType.INVOICE: InvoiceViewSet,
    }
    for source_type, view_class in view_sources.items():
        source_querysets[source_type] = _view_queryset(view_class, proxy)

    action_items = _view_queryset(ActionItemViewSet, proxy)
    if (
        scope_type == "global"
        and SearchRecord.SourceType.ACTION_ITEM_COMMENT in requested
    ):
        # Project roles require a project-scoped request; the global proxy only
        # carries company context.
        if authorized_project_ids is None:
            authorized_project_ids = _authorized_project_ids(request, scope)
            _validate_global_project_ids(scope, authorized_project_ids)

        requested_project_ids = set(scope.get("project_ids") or [])
        project_ids = requested_project_ids or authorized_project_ids
        action_item_filter = Q(id__in=action_items.values("id"))
        for scoped_project_id in project_ids:
            project_proxy = _proxy_request(
                request,
                {"type": "project", "id": scoped_project_id},
            )
            project_action_items = _view_queryset(
                ActionItemViewSet,
                project_proxy,
            ).filter(
                Q(milestone__project_id=scoped_project_id)
                | Q(parent_action_item__milestone__project_id=scoped_project_id)
            )
            action_item_filter |= Q(id__in=project_action_items.values("id"))
        action_items = ActionItem.objects.filter(action_item_filter).distinct()

    source_querysets["action_item"] = action_items
    source_querysets["transport"] = _view_queryset(TransportViewSet, proxy)

    if SearchRecord.SourceType.ACTION_ITEM_COMMENT in requested:
        action_items = source_querysets["action_item"]
        source_querysets[SearchRecord.SourceType.ACTION_ITEM_COMMENT] = (
            ActionItemComment.objects.filter(
                action_item_id__in=action_items.values("id"),
                is_deleted=False,
            )
        )

    if SearchRecord.SourceType.MATRIX_COMMENT in requested:
        matrix_targets = {
            MatrixComment.TARGET_SOLUTION_DEMAND: (
                source_querysets[SearchRecord.SourceType.SOLUTION_DEMAND],
                "solution_demand_id",
            ),
            MatrixComment.TARGET_REQUIREMENT: (
                source_querysets[SearchRecord.SourceType.REQUIREMENT],
                "requirement_id",
            ),
            MatrixComment.TARGET_TRANSPORT: (
                source_querysets["transport"],
                "transport_id",
            ),
            MatrixComment.TARGET_AVAILABLE_PRODUCT_PART: (
                source_querysets[SearchRecord.SourceType.AVAILABLE_PRODUCT_PART],
                "available_product_part_id",
            ),
            MatrixComment.TARGET_MACHINE: (
                source_querysets[SearchRecord.SourceType.MACHINE],
                "machine_id",
            ),
            MatrixComment.TARGET_EQUIPMENT: (
                source_querysets[SearchRecord.SourceType.EQUIPMENT],
                "equipment_id",
            ),
            MatrixComment.TARGET_BOM_PART: (
                source_querysets[SearchRecord.SourceType.BOM_PART],
                "bom_part_id",
            ),
            MatrixComment.TARGET_COMPANY_SOLUTION_SPACE: (
                source_querysets[SearchRecord.SourceType.COMPANY_SOLUTION_SPACE],
                "company_solution_space_item_id",
            ),
            MatrixComment.TARGET_PROJECT_SOLUTION_SPACE: (
                source_querysets[SearchRecord.SourceType.PROJECT_SOLUTION_SPACE],
                "project_solution_space_item_id",
            ),
        }
        comment_filter = Q(pk__in=[])
        for target_type, (target_queryset, target_field) in matrix_targets.items():
            source_querysets.setdefault(target_type, target_queryset)
            comment_filter |= Q(
                target_type=target_type,
                **{f"{target_field}__in": target_queryset.values("id")},
            )
        source_querysets[SearchRecord.SourceType.MATRIX_COMMENT] = (
            MatrixComment.objects.filter(comment_filter, is_deleted=False)
        )
    return source_querysets


def _authorized_record_queryset(request, scope, source_types: set[str] | None = None):
    authorized_project_ids = None
    if scope.get("type") == "global":
        authorized_project_ids = _authorized_project_ids(request, scope)
        _validate_global_project_ids(scope, authorized_project_ids)

    source_querysets = authorized_source_querysets(
        request,
        scope,
        source_types,
        authorized_project_ids,
    )
    selected_source_types = set(source_types or enabled_source_types())
    permission_filter = Q(pk__in=[])
    for source_type, queryset in source_querysets.items():
        if (
            source_type not in SearchRecord.SourceType.values
            or source_type not in selected_source_types
        ):
            continue
        permission_filter |= Q(source_type=source_type, source_id__in=queryset.values("id"))
    records = SearchRecord.objects.filter(
        permission_filter,
        source_type__in=selected_source_types,
    )
    scope_type = scope.get("type", "company")
    if scope_type == "project":
        records = records.filter(project_id=scope.get("id"))
    elif scope_type == "global":
        records = _apply_global_scope_filter(
            records,
            scope,
            authorized_project_ids or set(),
        )
    return records.distinct(), source_querysets


def _expand_result_types(result_types: list[str] | None) -> set[str]:
    if not result_types:
        return enabled_source_types()
    expanded = set()
    for result_type in result_types:
        if result_type in RESULT_TYPE_SOURCES:
            expanded.update(RESULT_TYPE_SOURCES[result_type])
        elif result_type in SOURCE_GROUPS:
            expanded.add(result_type)
        else:
            raise AdvancedSearchValidationError(f'Unknown result type "{result_type}".')
    return expanded & enabled_source_types()


def _expand_type_syntax(type_values: list[str]) -> set[str]:
    expanded = set()
    for value in type_values:
        normalized = value.casefold().replace("-", "_")
        if normalized in RESULT_TYPE_SOURCES:
            expanded.update(RESULT_TYPE_SOURCES[normalized])
        elif normalized in SOURCE_GROUPS:
            expanded.add(normalized)
        else:
            raise AdvancedSearchValidationError(
                f'Unknown record type "{value}".',
                suggestions=sorted(RESULT_TYPE_SOURCES),
            )
    return expanded & enabled_source_types()


def _apply_value_filter(records, *, key=None, semantic_id=None, value="", operator="contains"):
    field_values = SearchFieldValue.objects.filter(record_id__in=records.values("id"))
    if key:
        field_values = field_values.filter(Q(field_key__iexact=key) | Q(field_ref__iexact=key))
    if semantic_id:
        field_values = field_values.filter(semantic_id__iexact=semantic_id)
    normalized_value = str(value or "").strip()
    if normalized_value:
        if operator == "equals":
            field_values = field_values.filter(value_normalized__iexact=normalized_value.casefold())
        elif operator == "starts_with":
            field_values = field_values.filter(value_normalized__istartswith=normalized_value.casefold())
        elif operator == "contains":
            field_values = field_values.filter(value_normalized__icontains=normalized_value.casefold())
        else:
            raise AdvancedSearchValidationError(f'Unsupported field operator "{operator}".')
    return records.filter(id__in=field_values.values("record_id"))


def apply_structured_filters(records, filters: dict[str, Any], parsed: ParsedSearch):
    required_skills = list(filters.get("required_skills") or []) + parsed.filters.get("required_skills", [])
    provided_skills = list(filters.get("provided_skills") or []) + parsed.filters.get("provided_skills", [])
    for skill in required_skills:
        records = _apply_value_filter(records, key="required_skills", value=skill)
    for skill in provided_skills:
        records = _apply_value_filter(records, key="provided_skills", value=skill)

    field_filters = list(filters.get("fields") or [])
    field_filters.extend(
        {"field_ref": "manufacturer", "operator": "equals", "value": value}
        for value in parsed.filters.get("manufacturer", [])
    )
    for field_filter in field_filters:
        records = _apply_value_filter(
            records,
            key=field_filter.get("field_ref"),
            value=field_filter.get("value"),
            operator=field_filter.get("operator", "contains"),
        )
    semantic_filters = list(filters.get("semantic_fields") or [])
    semantic_filters.extend(
        {"semantic_id": value, "operator": "contains", "value": ""}
        for value in parsed.filters.get("semantic_id", [])
    )
    for field_filter in semantic_filters:
        records = _apply_value_filter(
            records,
            semantic_id=field_filter.get("semantic_id"),
            value=field_filter.get("value"),
            operator=field_filter.get("operator", "contains"),
        )

    statuses = list(filters.get("status") or []) + parsed.filters.get("status", [])
    if statuses:
        status_filter = Q(pk__in=[])
        for status in statuses:
            status_filter |= Q(metadata__status__iexact=str(status).strip())
        records = records.filter(status_filter)
    author_ids = filters.get("author_ids") or []
    if author_ids:
        records = records.filter(
            Q(metadata__author_id__in=author_ids) | Q(metadata__sender_id__in=author_ids)
        )
    if filters.get("date_from"):
        records = records.filter(source_updated_at__date__gte=filters["date_from"])
    if filters.get("date_to"):
        records = records.filter(source_updated_at__date__lte=filters["date_to"])

    linked_object = filters.get("linked_object") or {}
    parsed_linked = parsed.filters.get("linked_to", [])
    if parsed_linked and not linked_object:
        linked_object = {"type": parsed_linked[0]}
    if linked_object.get("type"):
        if linked_object["type"] not in ALLOWED_RELATED_TYPES:
            raise AdvancedSearchValidationError(
                f'Unknown linked object type "{linked_object["type"]}".',
                suggestions=sorted(ALLOWED_RELATED_TYPES),
            )
        relation_query = SearchRelation.objects.filter(
            record_id__in=records.values("id"),
            related_type=linked_object["type"],
        )
        if linked_object.get("id"):
            relation_query = relation_query.filter(related_id=linked_object["id"])
        records = records.filter(id__in=relation_query.values("record_id"))
    return records


def _exact_text_search(records, query_text: str, search_query):
    segment_matches = SearchContentSegment.objects.filter(
        record_id=OuterRef("pk"),
    ).filter(
        Q(search_vector=search_query) | Q(text__icontains=query_text)
    )
    field_matches = SearchFieldValue.objects.filter(
        record_id=OuterRef("pk"),
    ).filter(
        Q(search_vector=search_query) | Q(value_text__icontains=query_text)
    )
    return records.annotate(
        vector_rank=SearchRank(F("search_vector"), search_query),
        segment_match=Exists(segment_matches),
        field_match=Exists(field_matches),
        exact_score=Case(
            When(Q(title__iexact=query_text) | Q(subtitle__iexact=query_text), then=Value(1.0)),
            When(Q(title__istartswith=query_text) | Q(subtitle__istartswith=query_text), then=Value(0.92)),
            When(Q(title__icontains=query_text) | Q(subtitle__icontains=query_text), then=Value(0.86)),
            default=Value(0.0),
            output_field=FloatField(),
        ),
    ).filter(
        Q(search_vector=search_query)
        | Q(title__icontains=query_text)
        | Q(subtitle__icontains=query_text)
        | Q(searchable_text__icontains=query_text)
        | Q(segment_match=True)
        | Q(field_match=True)
    ).annotate(
        search_score=Greatest(
            F("exact_score"),
            F("vector_rank"),
            Case(
                When(segment_match=True, then=Value(0.7)),
                When(field_match=True, then=Value(0.65)),
                default=Value(0.0),
                output_field=FloatField(),
            ),
        )
    )


def _fuzzy_text_search(records, query_text: str, search_query):
    fuzzy_threshold = float(
        getattr(settings, "ADVANCED_SEARCH_FUZZY_THRESHOLD", 0.32)
    )
    segment_matches = (
        SearchContentSegment.objects.filter(record_id=OuterRef("pk"))
        .annotate(similarity=TrigramSimilarity("text", query_text))
        .filter(
            Q(search_vector=search_query)
            | Q(text__icontains=query_text)
            | Q(similarity__gte=fuzzy_threshold)
        )
    )
    field_matches = (
        SearchFieldValue.objects.filter(record_id=OuterRef("pk"))
        .annotate(
            similarity=TrigramSimilarity(
                "value_normalized",
                query_text.casefold(),
            )
        )
        .filter(
            Q(search_vector=search_query)
            | Q(value_text__icontains=query_text)
            | Q(similarity__gte=fuzzy_threshold)
        )
    )
    return records.annotate(
        vector_rank=SearchRank(F("search_vector"), search_query),
        title_similarity=TrigramSimilarity("title", query_text),
        title_word_similarity=TrigramWordSimilarity(query_text, "title"),
        text_similarity=TrigramSimilarity("searchable_text", query_text),
        text_word_similarity=TrigramWordSimilarity(query_text, "searchable_text"),
        segment_match=Exists(segment_matches),
        field_match=Exists(field_matches),
        exact_score=Case(
            When(Q(title__iexact=query_text) | Q(subtitle__iexact=query_text), then=Value(1.0)),
            When(Q(title__istartswith=query_text) | Q(subtitle__istartswith=query_text), then=Value(0.92)),
            When(Q(title__icontains=query_text) | Q(subtitle__icontains=query_text), then=Value(0.86)),
            default=Value(0.0),
            output_field=FloatField(),
        ),
    ).filter(
        Q(search_vector=search_query)
        | Q(title__icontains=query_text)
        | Q(subtitle__icontains=query_text)
        | Q(searchable_text__icontains=query_text)
        | Q(title_similarity__gte=fuzzy_threshold)
        | Q(title_word_similarity__gte=fuzzy_threshold)
        | Q(text_similarity__gte=fuzzy_threshold)
        | Q(text_word_similarity__gte=fuzzy_threshold)
        | Q(segment_match=True)
        | Q(field_match=True)
    ).annotate(
        search_score=Greatest(
            F("exact_score"),
            F("vector_rank"),
            F("title_similarity"),
            F("title_word_similarity"),
            F("text_similarity"),
            F("text_word_similarity"),
            Case(
                When(segment_match=True, then=Value(0.5)),
                When(field_match=True, then=Value(0.45)),
                default=Value(0.0),
                output_field=FloatField(),
            ),
        )
    )


def apply_text_search(records, query_text: str):
    if not query_text:
        return records.annotate(search_score=Value(0.0, output_field=FloatField()))

    search_query = SearchQuery(query_text, config="simple", search_type="websearch")
    exact_records = _exact_text_search(records, query_text, search_query)
    if exact_records.exists():
        return exact_records
    return _fuzzy_text_search(records, query_text, search_query)


def _snippet(text: str, query: str, *, limit: int = 260) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return ""
    tokens = [token.casefold() for token in re.findall(r"[\w-]+", query) if len(token) > 1]
    lower = cleaned.casefold()
    positions = [lower.find(token) for token in tokens if lower.find(token) >= 0]
    start = max(0, (min(positions) if positions else 0) - 80)
    end = min(len(cleaned), start + limit)
    result = cleaned[start:end]
    return f"{'...' if start else ''}{result}{'...' if end < len(cleaned) else ''}"


def _matched_details(records, query_text: str):
    record_ids = [record.id for record in records]
    segments_by_record = {}
    fields_by_record = {}
    if query_text and record_ids:
        query = SearchQuery(query_text, config="simple", search_type="websearch")
        segment_rows = SearchContentSegment.objects.filter(record_id__in=record_ids).annotate(
            match_rank=SearchRank(F("search_vector"), query),
            similarity=TrigramSimilarity("text", query_text),
        ).filter(
            Q(search_vector=query) | Q(text__icontains=query_text) | Q(similarity__gte=0.32)
        ).order_by("record_id", "-match_rank", "-similarity", "sequence")
        for segment in segment_rows:
            segments_by_record.setdefault(segment.record_id, segment)
        field_rows = SearchFieldValue.objects.filter(record_id__in=record_ids).annotate(
            match_rank=SearchRank(F("search_vector"), query),
            similarity=TrigramSimilarity("value_normalized", query_text.casefold()),
        ).filter(
            Q(search_vector=query) | Q(value_text__icontains=query_text) | Q(similarity__gte=0.32)
        ).order_by("record_id", "-match_rank", "-similarity", "id")
        for field in field_rows:
            fields_by_record.setdefault(field.record_id, field)
    return segments_by_record, fields_by_record


def _visible_relations(records, source_querysets):
    relations = SearchRelation.objects.filter(record_id__in=[record.id for record in records])
    allowed_by_type = {}
    for related_type in set(relations.values_list("related_type", flat=True)):
        queryset = source_querysets.get(related_type)
        if queryset is not None:
            relation_ids = relations.filter(related_type=related_type).values_list("related_id", flat=True)
            allowed_by_type[related_type] = set(
                queryset.filter(id__in=relation_ids).values_list("id", flat=True)
            )
    result = defaultdict(list)
    for relation in relations:
        if relation.related_id not in allowed_by_type.get(relation.related_type, set()):
            continue
        result[relation.record_id].append(
            {
                "relation_type": relation.relation_type,
                "source_type": relation.related_type,
                "source_id": relation.related_id,
                "label": relation.label,
            }
        )
    return result


def _deduplicate_financial_documents(records):
    financial_records = records.filter(
        source_type__in=RESULT_TYPE_SOURCES["financial"]
    )
    matched_documents = records.filter(
        source_type=SearchRecord.SourceType.DOCUMENT
    )
    matched_document_ids = set(matched_documents.values_list("source_id", flat=True))
    if not matched_document_ids or not financial_records.exists():
        return records, {}

    relations = SearchRelation.objects.filter(
        record_id__in=financial_records.values("id"),
        related_type=SearchRecord.SourceType.DOCUMENT,
        related_id__in=matched_document_ids,
    )
    duplicate_document_records = SearchRecord.objects.filter(
        source_type=SearchRecord.SourceType.DOCUMENT,
        source_id__in=relations.values("related_id"),
    )
    documents_by_source_id = {
        document.source_id: document
        for document in duplicate_document_records.only(
            "source_id",
            "title",
            "metadata",
        )
    }
    nested_matches = defaultdict(list)
    for relation in relations:
        document = documents_by_source_id.get(relation.related_id)
        if not document:
            continue
        nested_matches[relation.record_id].append(
            {
                "source_type": document.source_type,
                "source_id": document.source_id,
                "title": document.title,
                "url": (document.metadata or {}).get("url", ""),
            }
        )
    return records.exclude(id__in=duplicate_document_records.values("id")), nested_matches


def serialize_results(
    records,
    query_text: str,
    source_querysets,
    nested_matches=None,
):
    segments, fields = _matched_details(records, query_text)
    relations = _visible_relations(records, source_querysets)
    nested_matches = nested_matches or {}
    results = []
    for record in records:
        segment = segments.get(record.id)
        field = fields.get(record.id)
        approximate = bool(
            query_text
            and not record.title.casefold().startswith(query_text.casefold())
            and getattr(record, "exact_score", 0) == 0
            and float(getattr(record, "vector_rank", 0) or 0) == 0
        )
        exact_score = float(getattr(record, "exact_score", 0) or 0)
        if exact_score >= 0.86:
            snippet = _snippet(record.searchable_text, query_text)
            match_reason = "reference" if exact_score == 1 else "metadata"
            location = None
        elif segment:
            snippet = _snippet(segment.text, query_text)
            match_reason = "approximate_content" if approximate else "content"
            location = {
                "type": segment.segment_type,
                "number": segment.sequence,
                "label": segment.location_label,
            }
        elif field:
            snippet = _snippet(field.value_text, query_text)
            match_reason = "approximate_field" if approximate else "configured_field"
            location = None
        else:
            snippet = _snippet(record.searchable_text, query_text)
            match_reason = "approximate" if approximate else "metadata"
            location = None
        metadata = dict(record.metadata or {})
        result_url = search_record_route(record)
        metadata["url"] = result_url
        results.append(
            {
                "source_type": record.source_type,
                "source_group": SOURCE_GROUPS.get(record.source_type, "all"),
                "source_id": record.source_id,
                "title": record.title,
                "subtitle": record.subtitle,
                "snippet": snippet,
                "match_reason": match_reason,
                "approximate": approximate,
                "location": location,
                "score": round(float(getattr(record, "search_score", 0) or 0), 6),
                "index_status": record.index_status,
                "metadata": metadata,
                "url": result_url,
                "linked_objects": relations.get(record.id, []),
                "nested_matches": nested_matches.get(record.id, []),
            }
        )
    return results


def advanced_search(request, payload: dict[str, Any]):
    scope = payload.get("scope") or {"type": "company"}
    parsed = parse_search_language(payload.get("query", ""))
    if parsed.filters.get("project"):
        project_value = parsed.filters["project"][0]
        if not project_value.isdigit():
            raise AdvancedSearchValidationError("Project filters must use a numeric project ID.")
        scope = {"type": "project", "id": int(project_value)}

    selected_sources = _expand_result_types(payload.get("result_types"))
    if parsed.filters.get("type"):
        selected_sources &= _expand_type_syntax(parsed.filters["type"])
    records, source_querysets = _authorized_record_queryset(
        request,
        scope,
        selected_sources,
    )
    records = apply_structured_filters(records, payload.get("filters") or {}, parsed)
    records = apply_text_search(records, parsed.text)
    records, nested_matches = _deduplicate_financial_documents(records)

    ordering = payload.get("ordering", "relevance")
    if ordering == "newest":
        records = records.order_by(F("source_updated_at").desc(nulls_last=True), "-id")
    elif ordering == "oldest":
        records = records.order_by(F("source_updated_at").asc(nulls_last=True), "id")
    elif ordering == "title":
        records = records.order_by("title", "id")
    elif ordering == "relevance":
        records = records.order_by("-search_score", "title", "id")
    else:
        raise AdvancedSearchValidationError(f'Unknown ordering "{ordering}".')

    facets = {
        item["source_type"]: item["count"]
        for item in records.values("source_type").annotate(count=Count("id"))
    }
    total_count = records.count()
    page = max(1, int(payload.get("page") or 1))
    page_size = min(100, max(1, int(payload.get("page_size") or 10)))
    offset = (page - 1) * page_size
    page_records = list(records[offset : offset + page_size])
    serialized_results = serialize_results(
        page_records,
        parsed.text,
        source_querysets,
        nested_matches,
    )
    return {
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "next": page + 1 if offset + page_size < total_count else None,
        "previous": page - 1 if page > 1 else None,
        "facets": {"source_types": facets},
        "indexing": index_summary(
            request,
            scope,
            records=records,
            source_querysets=source_querysets,
        ),
        "applied": {
            "query": parsed.text,
            "syntax_filters": parsed.filters,
            "scope": scope,
        },
        "results": serialized_results,
    }


def index_summary(request, scope, *, records=None, source_querysets=None):
    if records is None:
        records, source_querysets = _authorized_record_queryset(request, scope)
    counts = {
        item["index_status"]: item["count"]
        for item in records.values("index_status").annotate(count=Count("id"))
    }
    source_filter = Q(pk__in=[])
    source_querysets = source_querysets or authorized_source_querysets(request, scope)
    for source_type, queryset in source_querysets.items():
        if source_type not in SearchRecord.SourceType.values:
            continue
        source_filter |= Q(source_type=source_type, source_id__in=queryset.values("id"))
    jobs = SearchIndexJob.objects.filter(source_filter)
    return {
        "records": sum(counts.values()),
        "statuses": counts,
        "queued": jobs.filter(status__in=["queued", "retrying"]).count(),
        "failed": jobs.filter(status="failed").count(),
    }


def search_config(request, scope):
    records, _ = _authorized_record_queryset(request, scope, enabled_source_types())
    fields = SearchFieldValue.objects.filter(record_id__in=records.values("id"))
    field_rows = fields.values(
        "field_ref",
        "field_key",
        "field_type",
        "semantic_id",
        "label_en",
        "label_de",
        "label_fr",
    ).distinct().order_by("field_key")
    semantic_ids = set(fields.exclude(semantic_id="").values_list("semantic_id", flat=True))
    definitions = {}
    if getattr(settings, "ADVANCED_SEARCH_SEMANTIC_CATALOG", True):
        definitions = {
            definition.semantic_id: definition.verification_status
            for definition in SemanticPropertyDefinition.objects.filter(
                semantic_id__in=semantic_ids
            )
        }
    authors = {}
    for id_key, name_key in (("author_id", "author"), ("sender_id", "sender")):
        author_rows = records.annotate(
            config_author_id=Cast(
                KeyTextTransform(id_key, "metadata"),
                IntegerField(),
            ),
            config_author_name=KeyTextTransform(name_key, "metadata"),
        ).filter(config_author_id__isnull=False).values(
            "config_author_id",
            "config_author_name",
        ).distinct()
        for row in author_rows:
            authors[row["config_author_id"]] = row["config_author_name"] or str(
                row["config_author_id"]
            )
    statuses = list(
        records.annotate(config_status=KeyTextTransform("status", "metadata"))
        .exclude(config_status__isnull=True)
        .exclude(config_status="")
        .values_list("config_status", flat=True)
        .distinct()
        .order_by("config_status")
    )
    return {
        "result_types": [
            result_type
            for result_type, source_types in RESULT_TYPE_SOURCES.items()
            if source_types & enabled_source_types()
        ],
        "source_types": sorted(enabled_source_types()),
        "orderings": ["relevance", "newest", "oldest", "title"],
        "authors": [
            {"id": author_id, "name": authors[author_id]}
            for author_id in sorted(authors, key=lambda item: authors[item].casefold())
        ],
        "statuses": statuses,
        "linked_object_types": sorted(ALLOWED_RELATED_TYPES),
        "fields": [
            {
                **row,
                "semantic_status": (
                    definitions.get(row["semantic_id"], "unknown")
                    if row["semantic_id"]
                    else "company_defined"
                ),
            }
            for row in field_rows
        ],
        "syntax": sorted({key for key in SEARCH_KEY_ALIASES if "_" not in key}),
    }
