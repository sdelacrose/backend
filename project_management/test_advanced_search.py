from contextlib import nullcontext
from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.postgres.search import SearchVector
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from models.models import (
    CompanyProfile,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from project_management.models import (
    ActionItem,
    ActionItemComment,
    Document,
    DocumentContentSegment,
    DocumentSearchIndex,
    Folder,
    Milestone,
    Project,
    ProjectTeamMembers,
    SearchFieldValue,
    SearchContentSegment,
    SearchRecord,
    SearchRelation,
)
from project_management.serializers.advanced_search import (
    AdvancedSearchQuerySerializer,
    AdvancedSearchScopeSerializer,
)
from project_management.services.advanced_search import (
    AdvancedSearchValidationError,
    _apply_global_scope_filter,
    _deduplicate_financial_documents,
    _validate_global_project_ids,
    apply_structured_filters,
    apply_text_search,
    parse_search_language,
)
from project_management.services.advanced_search_resilience import (
    AdvancedSearchBusyError,
    AdvancedSearchRateLimitError,
    advanced_search_capacity,
    is_statement_timeout,
)
from project_management.services.search_indexing import (
    IndexedPayload,
    MATRIX_COMMENT_ROUTES,
    SOURCE_ROUTES,
    index_document_content,
    index_object_assessment_content,
    search_record_route,
    source_route,
)


class AdvancedSearchContractTests(SimpleTestCase):
    def test_search_language_supports_filters_and_preserves_text(self):
        parsed = parse_search_language(
            'required-skill:drill manufacturer:FESTO "operating pressure"'
        )

        self.assertEqual(parsed.text, "operating pressure")
        self.assertEqual(parsed.filters["required_skills"], ["drill"])
        self.assertEqual(parsed.filters["manufacturer"], ["FESTO"])

    def test_search_language_rejects_unknown_fields(self):
        with self.assertRaises(AdvancedSearchValidationError) as context:
            parse_search_language("unknown-field:value")

        self.assertIn("Unknown search field", context.exception.detail)
        self.assertIn("required-skill", context.exception.suggestions)

    def test_query_contract_rejects_unknown_filters(self):
        serializer = AdvancedSearchQuerySerializer(
            data={
                "query": "sample",
                "filters": {"unsupported_filter": "value"},
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("unsupported_filter", serializer.errors["filters"])

    def test_query_contract_rejects_unknown_top_level_fields(self):
        serializer = AdvancedSearchQuerySerializer(
            data={"query": "sample", "unsupported_option": True}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("unsupported_option", serializer.errors)

    def test_query_contract_rejects_reversed_date_range(self):
        serializer = AdvancedSearchQuerySerializer(
            data={
                "query": "sample",
                "filters": {
                    "date_from": "2026-07-22",
                    "date_to": "2026-07-21",
                },
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("date_to", serializer.errors["filters"])

    def test_global_scope_contract_accepts_valid_payload(self):
        serializer = AdvancedSearchScopeSerializer(
            data={
                "type": "global",
                "include_company": True,
                "project_ids": [1, 2],
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data,
            {
                "type": "global",
                "include_company": True,
                "project_ids": [1, 2],
            },
        )

    def test_global_scope_contract_defaults_include_company_and_project_ids(self):
        serializer = AdvancedSearchScopeSerializer(data={"type": "global"})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["include_company"], True)
        self.assertEqual(serializer.validated_data["project_ids"], [])

    def test_global_scope_contract_rejects_project_id_field(self):
        serializer = AdvancedSearchScopeSerializer(
            data={"type": "global", "id": 42, "include_company": False}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("id", serializer.errors)

    def test_company_scope_strips_global_only_fields(self):
        serializer = AdvancedSearchScopeSerializer(
            data={
                "type": "company",
                "include_company": True,
                "project_ids": [1],
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {"type": "company"})

    def test_global_scope_query_contract(self):
        serializer = AdvancedSearchQuerySerializer(
            data={
                "query": "pump",
                "scope": {
                    "type": "global",
                    "include_company": False,
                    "project_ids": [10],
                },
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["scope"]["type"], "global")
        self.assertFalse(serializer.validated_data["scope"]["include_company"])
        self.assertEqual(serializer.validated_data["scope"]["project_ids"], [10])

    def test_source_route_uses_supported_detail_query_parameters(self):
        route = source_route(
            SearchRecord.SourceType.DOCUMENT,
            IndexedPayload(
                company_profile_id=1,
                project_id=2657,
                freelancer_id=None,
                title="Specification.pdf",
                metadata={"id": 42},
            ),
        )

        self.assertEqual(
            route,
            "/project-document-management?searchResultType=document&id=42&project_id=2657",
        )

    def test_source_route_uses_current_matrix_and_parts_routes(self):
        cases = (
            (
                SearchRecord.SourceType.SOLUTION_DEMAND,
                11,
                2657,
                "/project-system-requirements/customer-demands",
            ),
            (
                SearchRecord.SourceType.REQUIREMENT,
                12,
                2657,
                "/project-system-requirements/technical-requirements",
            ),
            (
                SearchRecord.SourceType.OBJECT_ASSESSMENT,
                20,
                2657,
                "/project-object-assessment",
            ),
            (
                SearchRecord.SourceType.BOM_PART,
                13,
                2657,
                "/project-product-services/bill-of-material",
            ),
            (
                SearchRecord.SourceType.AVAILABLE_PRODUCT_PART,
                14,
                None,
                "/company-product-services/available-products",
            ),
            (
                SearchRecord.SourceType.MACHINE,
                15,
                None,
                "/company-product-services/machines",
            ),
            (
                SearchRecord.SourceType.EQUIPMENT,
                16,
                None,
                "/company-product-services/equipment",
            ),
            (
                SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
                17,
                None,
                "/company-product-services/solution-space",
            ),
            (
                SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
                18,
                2657,
                "/project-product-services/solution-space",
            ),
            (
                SearchRecord.SourceType.INVOICE,
                19,
                None,
                "/billing-management",
            ),
            (
                SearchRecord.SourceType.PURCHASE_ORDER,
                21,
                None,
                "/purchase-orders",
            ),
        )

        for source_type, record_id, project_id, expected_route in cases:
            with self.subTest(source_type=source_type):
                route = source_route(
                    source_type,
                    IndexedPayload(
                        company_profile_id=1,
                        project_id=project_id,
                        freelancer_id=None,
                        title="Record",
                        metadata={"id": record_id},
                    ),
                )

                self.assertTrue(route.startswith(expected_route))
                self.assertIn(f"id={record_id}", route)

    def test_source_route_uses_action_item_comment_scope(self):
        for project_id, expected_route in (
            (None, "/company-action-items"),
            (2657, "/project-action-items"),
        ):
            with self.subTest(project_id=project_id):
                route = source_route(
                    SearchRecord.SourceType.ACTION_ITEM_COMMENT,
                    IndexedPayload(
                        company_profile_id=1,
                        project_id=project_id,
                        freelancer_id=None,
                        title="Comment",
                        metadata={"id": 20, "target_id": 42},
                    ),
                )

                self.assertTrue(route.startswith(expected_route))
                self.assertIn("id=42", route)

    def test_source_route_supports_every_matrix_comment_parent(self):
        cases = {
            "solution_demand": "/project-system-requirements/customer-demands",
            "requirement": "/project-system-requirements/technical-requirements",
            "transport": "/project-execution/transport",
            "available_product_part": "/company-product-services/available-products",
            "machine": "/company-product-services/machines",
            "equipment": "/company-product-services/equipment",
            "bom_part": "/project-product-services/bill-of-material",
            "company_solution_space": "/company-product-services/solution-space",
            "project_solution_space": "/project-product-services/solution-space",
        }
        self.assertEqual(set(MATRIX_COMMENT_ROUTES), set(cases))

        for parent_type, expected_route in cases.items():
            with self.subTest(parent_type=parent_type):
                company_parent_types = {
                    "available_product_part",
                    "machine",
                    "equipment",
                    "company_solution_space",
                }
                route = source_route(
                    SearchRecord.SourceType.MATRIX_COMMENT,
                    IndexedPayload(
                        company_profile_id=1,
                        project_id=None if parent_type in company_parent_types else 2657,
                        freelancer_id=None,
                        title="Comment",
                        metadata={
                            "id": 21,
                            "target_id": 43,
                            "parent_type": parent_type,
                        },
                    ),
                )

                self.assertTrue(route.startswith(expected_route))
                self.assertIn("id=43", route)

    def test_every_non_matrix_search_source_has_a_route(self):
        expected_source_types = set(SearchRecord.SourceType.values) - {
            SearchRecord.SourceType.MATRIX_COMMENT,
        }

        self.assertEqual(set(SOURCE_ROUTES), expected_source_types)

    def test_search_record_route_replaces_stale_indexed_url(self):
        record = SimpleNamespace(
            source_type=SearchRecord.SourceType.MACHINE,
            company_profile_id=1,
            project_id=None,
            freelancer_id=None,
            title="Machine",
            metadata={"id": 44, "url": "/company-machines?id=44"},
        )

        self.assertEqual(
            search_record_route(record),
            "/company-product-services/machines?searchResultType=machine&id=44",
        )

    def test_statement_timeout_detection_walks_wrapped_database_errors(self):
        database_error = OperationalError("query failed")
        database_error.__cause__ = RuntimeError(
            "canceling statement due to statement timeout"
        )

        self.assertTrue(is_statement_timeout(database_error))

    @override_settings(
        ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS=2,
        ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS_PER_USER=1,
        ADVANCED_SEARCH_CAPACITY_WAIT_SECONDS=0,
    )
    @patch("project_management.services.advanced_search_resilience._redis_client")
    def test_local_fallback_keeps_capacity_for_other_api_work(self, redis_client_mock):
        from redis.exceptions import RedisError

        redis_client_mock.return_value.eval.side_effect = RedisError("unavailable")

        with advanced_search_capacity(user_id=1):
            with self.assertRaises(AdvancedSearchBusyError):
                with advanced_search_capacity(user_id=1):
                    pass

            with advanced_search_capacity(user_id=2):
                with self.assertRaises(AdvancedSearchBusyError):
                    with advanced_search_capacity(user_id=3):
                        pass

    @override_settings(
        ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS=2,
        ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS_PER_USER=1,
        ADVANCED_SEARCH_CAPACITY_WAIT_SECONDS=3,
        ADVANCED_SEARCH_CAPACITY_POLL_INTERVAL_MS=100,
    )
    @patch("project_management.services.advanced_search_resilience.time.sleep")
    @patch("project_management.services.advanced_search_resilience._redis_client")
    def test_capacity_waits_for_an_in_flight_search(
        self,
        redis_client_mock,
        sleep_mock,
    ):
        client = redis_client_mock.return_value
        client.eval.side_effect = [1, -1, 1, 1]

        with advanced_search_capacity(user_id=1):
            pass

        sleep_mock.assert_called_once_with(0.1)
        self.assertEqual(client.eval.call_count, 4)


class AdvancedSearchResilienceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="advanced-search-resilience@example.com",
            password="password",
            first_name="Search",
            last_name="Resilience",
            user_type="freelancer",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch(
        "project_management.views.advanced_search.advanced_search_statement_timeout",
        return_value=nullcontext(),
    )
    @patch(
        "project_management.views.advanced_search.advanced_search_capacity",
        return_value=nullcontext(),
    )
    @patch("project_management.views.advanced_search.advanced_search")
    def test_success_response_contract_is_unchanged(
        self,
        search_mock,
        _capacity_mock,
        _timeout_mock,
    ):
        expected = {
            "count": 1,
            "page": 1,
            "page_size": 10,
            "results": [{"source_id": 42}],
        }
        search_mock.return_value = expected

        response = self.client.post(
            "/api/project_management/advanced-search/query/",
            {"query": "pump", "scope": {"type": "company"}},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, expected)

    @patch(
        "project_management.views.advanced_search.advanced_search_capacity",
        side_effect=AdvancedSearchBusyError(5),
    )
    def test_capacity_exhaustion_returns_retryable_response(self, _capacity_mock):
        response = self.client.post(
            "/api/project_management/advanced-search/query/",
            {"query": "pump", "scope": {"type": "company"}},
            format="json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["code"], "advanced_search_busy")
        self.assertEqual(response.data["retry_after"], 5)
        self.assertEqual(response["Retry-After"], "5")

    @patch(
        "project_management.views.advanced_search.advanced_search_capacity",
        side_effect=AdvancedSearchRateLimitError(17),
    )
    def test_rate_limit_returns_retry_after(self, _capacity_mock):
        response = self.client.post(
            "/api/project_management/advanced-search/query/",
            {"query": "pump", "scope": {"type": "company"}},
            format="json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["code"], "advanced_search_rate_limited")
        self.assertEqual(response["Retry-After"], "17")

    @patch(
        "project_management.views.advanced_search.advanced_search_statement_timeout",
        return_value=nullcontext(),
    )
    @patch(
        "project_management.views.advanced_search.advanced_search_capacity",
        return_value=nullcontext(),
    )
    @patch(
        "project_management.views.advanced_search.advanced_search",
        side_effect=OperationalError(
            "canceling statement due to statement timeout"
        ),
    )
    def test_database_timeout_does_not_escape_as_server_error(
        self,
        _search_mock,
        _capacity_mock,
        _timeout_mock,
    ):
        response = self.client.post(
            "/api/project_management/advanced-search/query/",
            {"query": "pump", "scope": {"type": "company"}},
            format="json",
        )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.data["code"], "advanced_search_timeout")

    @patch(
        "project_management.views.advanced_search.advanced_search_statement_timeout",
        return_value=nullcontext(),
    )
    @patch(
        "project_management.views.advanced_search.advanced_search_capacity",
        return_value=nullcontext(),
    )
    @patch(
        "project_management.views.advanced_search.advanced_search",
        side_effect=OperationalError("database connection failed"),
    )
    def test_non_timeout_database_errors_are_not_hidden(
        self,
        _search_mock,
        _capacity_mock,
        _timeout_mock,
    ):
        with self.assertRaises(OperationalError):
            self.client.post(
                "/api/project_management/advanced-search/query/",
                {"query": "pump", "scope": {"type": "company"}},
                format="json",
            )


@override_settings(ADVANCED_SEARCH_FUZZY_THRESHOLD=0.25)
class AdvancedSearchQueryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="advanced-search@example.com",
            password="password",
            first_name="Advanced",
            last_name="Search",
            user_type="freelancer",
        )

    def create_record(
        self,
        source_type,
        source_id,
        title,
        *,
        searchable_text="",
        metadata=None,
    ):
        record = SearchRecord.objects.create(
            source_type=source_type,
            source_id=source_id,
            freelancer=self.user,
            title=title,
            searchable_text=searchable_text,
            metadata=metadata or {},
        )
        SearchRecord.objects.filter(id=record.id).update(
            search_vector=(
                SearchVector("title", config="simple", weight="A")
                + SearchVector("searchable_text", config="simple", weight="C")
            )
        )
        return SearchRecord.objects.get(id=record.id)

    def add_field(self, record, key, value, *, semantic_id=""):
        field = SearchFieldValue.objects.create(
            record=record,
            field_ref=f"{record.source_type}:test:{key}",
            field_key=key,
            semantic_id=semantic_id,
            value_text=value,
            value_normalized=value.casefold(),
        )
        SearchFieldValue.objects.filter(id=field.id).update(
            search_vector=SearchVector("value_text", config="simple", weight="B")
        )
        return field

    def test_typo_search_matches_title_below_exact_result(self):
        exact = self.create_record("document", 1, "Sample Specification")
        unrelated = self.create_record("document", 2, "Purchase Order")

        matches = list(
            apply_text_search(SearchRecord.objects.all(), "sampel")
            .order_by("-search_score", "id")
            .values_list("id", flat=True)
        )

        self.assertIn(exact.id, matches)
        self.assertNotIn(unrelated.id, matches)

    def test_exact_search_does_not_join_segment_and_field_tables(self):
        matching = self.create_record("document", 1, "ai_chat.ful")
        self.create_record("action_item_comment", 2, "Unrelated comment")

        records = apply_text_search(SearchRecord.objects.all(), "ai_chat.ful")
        sql = str(records.query).upper()

        self.assertEqual(list(records.values_list("id", flat=True)), [matching.id])
        self.assertNotIn("LEFT OUTER JOIN", sql)
        self.assertIn("EXISTS", sql)

    def test_fuzzy_segment_search_does_not_join_related_tables(self):
        matching = self.create_record("document", 1, "Technical document")
        segment = SearchContentSegment.objects.create(
            record=matching,
            segment_type=SearchContentSegment.SegmentType.DOCUMENT,
            text="Centrifugal pump specification",
        )
        SearchContentSegment.objects.filter(id=segment.id).update(
            search_vector=SearchVector("text", config="simple")
        )

        records = apply_text_search(SearchRecord.objects.all(), "centrifugl")
        sql = str(records.query).upper()

        self.assertEqual(list(records.values_list("id", flat=True)), [matching.id])
        self.assertNotIn("LEFT OUTER JOIN", sql)
        self.assertIn("EXISTS", sql)

    def test_skill_and_semantic_filters_can_be_combined(self):
        matching = self.create_record("company_solution_space", 1, "FESTO Assembly")
        wrong_skill = self.create_record("company_solution_space", 2, "Other Assembly")
        self.add_field(matching, "required_skills", "drill transport")
        self.add_field(
            matching,
            "manufacturer",
            "FESTO",
            semantic_id="0173-1#02-AAO677#002",
        )
        self.add_field(wrong_skill, "required_skills", "solder")
        self.add_field(
            wrong_skill,
            "manufacturer",
            "FESTO",
            semantic_id="0173-1#02-AAO677#002",
        )

        records = apply_structured_filters(
            SearchRecord.objects.all(),
            {
                "required_skills": ["drill"],
                "semantic_fields": [
                    {
                        "semantic_id": "0173-1#02-AAO677#002",
                        "operator": "equals",
                        "value": "FESTO",
                    }
                ],
            },
            parse_search_language(""),
        )

        self.assertEqual(list(records.values_list("id", flat=True)), [matching.id])

    def test_linked_object_filter_requires_matching_relation(self):
        matching = self.create_record("document", 1, "Linked Specification")
        unrelated = self.create_record("document", 2, "Unlinked Specification")
        SearchRelation.objects.create(
            record=matching,
            relation_type="linked",
            related_type="bom_part",
            related_id=42,
            label="Part 2.3.1",
        )

        records = apply_structured_filters(
            SearchRecord.objects.all(),
            {"linked_object": {"type": "bom_part", "id": 42}},
            parse_search_language(""),
        )

        self.assertEqual(list(records.values_list("id", flat=True)), [matching.id])
        self.assertNotIn(unrelated.id, records.values_list("id", flat=True))

    def test_unknown_linked_object_type_is_rejected(self):
        with self.assertRaises(AdvancedSearchValidationError):
            apply_structured_filters(
                SearchRecord.objects.all(),
                {"linked_object": {"type": "private_chat"}},
                parse_search_language(""),
            )

    def test_financial_record_owns_matching_generated_document(self):
        purchase_order = self.create_record("purchase_order", 10, "PO-2026-001")
        document = self.create_record(
            "document",
            20,
            "PO-2026-001.pdf",
            metadata={"url": "/company-documents?id=20"},
        )
        SearchRelation.objects.create(
            record=purchase_order,
            relation_type="document",
            related_type="document",
            related_id=document.source_id,
            label=document.title,
        )

        records, nested = _deduplicate_financial_documents(
            SearchRecord.objects.filter(id__in=[purchase_order.id, document.id])
        )

        self.assertEqual(list(records.values_list("id", flat=True)), [purchase_order.id])
        self.assertEqual(nested[purchase_order.id][0]["source_id"], document.source_id)

    def test_object_assessment_notes_are_indexed_as_searchable_content(self):
        record = self.create_record(
            SearchRecord.SourceType.OBJECT_ASSESSMENT,
            42,
            "Commissioning note",
        )

        index_object_assessment_content(
            record,
            SimpleNamespace(
                notes_content_html=(
                    "<p>The pneumatic regulator needs an eight bar setting.</p>"
                ),
                description="Commissioning observation",
                file=None,
            ),
        )

        segments = list(
            SearchContentSegment.objects.filter(record=record)
            .order_by("sequence")
            .values_list("location_label", "text")
        )
        self.assertEqual(
            segments,
            [
                ("Notes", "The pneumatic regulator needs an eight bar setting."),
                ("Description", "Commissioning observation"),
            ],
        )

    def test_large_document_segment_is_split_before_vectorization(self):
        folder = Folder.objects.bulk_create(
            [Folder(freelancer=self.user, name="Large document")]
        )[0]
        document = Document.objects.bulk_create(
            [
                Document(
                    folder=folder,
                    author=self.user,
                    created_by=self.user,
                    file_name="solar-calculator.xlsx",
                )
            ]
        )[0]
        source_index = DocumentSearchIndex.objects.create(
            document=document,
            status=DocumentSearchIndex.Status.SEARCHABLE,
        )
        DocumentContentSegment.objects.create(
            document=document,
            index=source_index,
            segment_type="sheet",
            sequence=1,
            location_label="Calculations",
            text="solar production value " * 20_000,
        )
        record = self.create_record(
            SearchRecord.SourceType.DOCUMENT,
            document.id,
            document.file_name,
        )

        index_document_content(record, document)

        chunks = list(
            SearchContentSegment.objects.filter(record=record).order_by("id")
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(
            all(len(chunk.text.encode("utf-8")) <= 200_000 for chunk in chunks)
        )
        self.assertTrue(chunks[0].location_label.startswith("Calculations (1/"))


@override_settings(
    ADVANCED_SEARCH_ENABLED=True,
    ADVANCED_SEARCH_OBJECT_ASSESSMENTS=True,
    ADVANCED_SEARCH_BUSINESS_RECORDS=True,
    ADVANCED_SEARCH_COMMENTS=True,
)
class AdvancedSearchPermissionTests(TestCase):
    def setUp(self):
        self.notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        self.notification_patcher.start()
        self.user = User.objects.create_user(
            email="visible-search-user@example.com",
            password="password",
            first_name="Visible",
            last_name="User",
            user_type="freelancer",
        )
        self.other_user = User.objects.create_user(
            email="hidden-search-user@example.com",
            password="password",
            first_name="Hidden",
            last_name="User",
            user_type="freelancer",
        )
        self.visible_folder = Folder.objects.create(
            freelancer=self.user,
            name="Visible specifications",
        )
        self.hidden_folder = Folder.objects.create(
            freelancer=self.other_user,
            name="Hidden specifications",
        )
        self.visible_document = Document.objects.create(
            folder=self.visible_folder,
            author=self.user,
            created_by=self.user,
            file_name="visible-coupling-specification.txt",
            document_type="Specification",
            status="Draft",
        )
        self.hidden_document = Document.objects.create(
            folder=self.hidden_folder,
            author=self.other_user,
            created_by=self.other_user,
            file_name="hidden-coupling-specification.txt",
            document_type="Specification",
            status="Draft",
        )
        self.visible_record = self._create_search_record(
            self.visible_document,
            "Visible coupling specification",
        )
        self.hidden_record = self._create_search_record(
            self.hidden_document,
            "Hidden coupling specification",
        )
        SearchRelation.objects.create(
            record=self.visible_record,
            relation_type="document",
            related_type=SearchRecord.SourceType.DOCUMENT,
            related_id=self.hidden_document.id,
            label="Hidden coupling specification",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self.notification_patcher.stop()

    def _create_search_record(self, document, title):
        record = SearchRecord.objects.create(
            source_type=SearchRecord.SourceType.DOCUMENT,
            source_id=document.id,
            freelancer=document.folder.freelancer,
            title=title,
            searchable_text="Required coupling torque and pressure specification",
            metadata={
                "id": document.id,
                "status": "Draft",
                "author_id": document.author_id,
                "author": (
                    f"{document.author.first_name} {document.author.last_name}".strip()
                ),
                "url": f"/company-documents?id={document.id}",
            },
        )
        SearchRecord.objects.filter(id=record.id).update(
            search_vector=(
                SearchVector("title", config="simple", weight="A")
                + SearchVector("searchable_text", config="simple", weight="C")
            )
        )
        return SearchRecord.objects.get(id=record.id)

    def test_query_does_not_leak_inaccessible_records_or_relations(self):
        accessible_documents = Document.objects.filter(id=self.visible_document.id)
        with patch(
            "project_management.services.advanced_search.get_accessible_document_queryset",
            return_value=accessible_documents,
        ):
            response = self.client.post(
                "/api/project_management/advanced-search/query/",
                {
                    "query": "coupling specification",
                    "scope": {"type": "company"},
                    "result_types": ["document"],
                    "page": 1,
                    "page_size": 10,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["facets"],
            {"source_types": {"document": 1}},
        )
        self.assertEqual(
            [result["source_id"] for result in response.data["results"]],
            [self.visible_document.id],
        )
        self.assertEqual(response.data["results"][0]["linked_objects"], [])
        self.assertNotIn("Hidden coupling", str(response.data))


class AdvancedSearchProjectCommentPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="search-owner@example.com",
            password="password",
            first_name="Search",
            last_name="Owner",
            user_type="company_user",
        )
        self.member = User.objects.create_user(
            email="search-member@example.com",
            password="password",
            first_name="Search",
            last_name="Member",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.owner,
            company_name="Search Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="search@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(id__in=[self.owner.id, self.member.id]).update(
            company=self.company,
        )
        self.owner.refresh_from_db()
        self.member.refresh_from_db()
        self.project = Project.objects.bulk_create(
            [
                Project(
                    user=self.owner,
                    company_profile=self.company,
                    solution="Energy Consultant Solution",
                    project_name="Visible Project",
                    contract_value=1000,
                )
            ]
        )[0]
        self.project_role = Role.objects.create(
            name="Comment Search Viewer",
            company=self.company,
            type="solution",
            scope="project",
            created_by=self.owner,
        )
        view_permission = Permission.objects.create(
            key="action_item.view",
            name="View action items",
            module="action_item",
        )
        RolePermission.objects.create(
            role=self.project_role,
            permission=view_permission,
        )
        company_role = Role.objects.create(
            name="Assigned Project Viewer",
            company=self.company,
            type="solution",
            scope="company",
            created_by=self.owner,
        )
        project_list_permission = Permission.objects.create(
            key="projects.view_assigned_created",
            name="View assigned projects",
            module="projects",
        )
        RolePermission.objects.create(
            role=company_role,
            permission=project_list_permission,
        )
        UserRole.objects.create(
            user=self.member,
            role=company_role,
            company=self.company,
            assigned_by=self.owner,
        )
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    team_member=self.member,
                    role=self.project_role.name,
                    project_role=self.project_role,
                )
            ]
        )
        milestone = Milestone.objects.bulk_create(
            [
                Milestone(
                    project=self.project,
                    name="Visible Phase",
                    due_date="2026-09-30",
                    sort_order=1,
                )
            ]
        )[0]
        self.action_item = ActionItem.objects.bulk_create(
            [
                ActionItem(
                    milestone=milestone,
                    created_by=self.owner,
                    name="Visible Action Item",
                    target_date="2026-09-30",
                    status="open",
                )
            ]
        )[0]
        self.comment = ActionItemComment.objects.bulk_create(
            [
                ActionItemComment(
                    action_item=self.action_item,
                    sender=self.owner,
                    content="Piping issues require review",
                )
            ]
        )[0]
        record = SearchRecord.objects.create(
            source_type=SearchRecord.SourceType.ACTION_ITEM_COMMENT,
            source_id=self.comment.id,
            company_profile=self.company,
            project=self.project,
            title="Comment on Visible Action Item",
            subtitle="Search Owner",
            searchable_text=self.comment.content,
            metadata={
                "id": self.comment.id,
                "target_id": self.action_item.id,
                "parent_type": "action_item",
            },
        )
        SearchRecord.objects.filter(id=record.id).update(
            search_vector=(
                SearchVector("title", config="simple", weight="A")
                + SearchVector("searchable_text", config="simple", weight="C")
            )
        )
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def test_global_project_scope_returns_comments_visible_through_project_role(
        self,
    ):
        response = self._search_comments()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["source_id"],
            self.comment.id,
        )

    def test_global_project_scope_returns_assigned_action_item_comments(self):
        RolePermission.objects.filter(role=self.project_role).delete()
        assigned_permission = Permission.objects.create(
            key="action_item.view_assigned_created",
            name="View assigned action items",
            module="action_item",
        )
        RolePermission.objects.create(
            role=self.project_role,
            permission=assigned_permission,
        )
        self.action_item.assignees.add(self.member)

        response = self._search_comments()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["source_id"],
            self.comment.id,
        )

    def _search_comments(self):
        response = self.client.post(
            "/api/project_management/advanced-search/query/",
            {
                "query": "Piping issues",
                "scope": {
                    "type": "global",
                    "include_company": False,
                    "project_ids": [self.project.id],
                },
                "result_types": ["comment"],
                "page": 1,
                "page_size": 10,
            },
            format="json",
        )
        return response


class AdvancedSearchGlobalScopeFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="global-search-user@example.com",
            password="password",
            first_name="Global",
            last_name="Search",
            user_type="freelancer",
        )
        cls.project_one, cls.project_two = Project.objects.bulk_create(
            [
                Project(
                    user=cls.user,
                    project_name="Project One",
                    contract_value=1000,
                ),
                Project(
                    user=cls.user,
                    project_name="Project Two",
                    contract_value=2000,
                ),
            ]
        )
        cls.company_record = SearchRecord.objects.create(
            source_type=SearchRecord.SourceType.DOCUMENT,
            source_id=1,
            freelancer=cls.user,
            title="Company PO",
        )
        cls.project_one_record = SearchRecord.objects.create(
            source_type=SearchRecord.SourceType.DOCUMENT,
            source_id=2,
            freelancer=cls.user,
            project=cls.project_one,
            title="Project One PO",
        )
        cls.project_two_record = SearchRecord.objects.create(
            source_type=SearchRecord.SourceType.DOCUMENT,
            source_id=3,
            freelancer=cls.user,
            project=cls.project_two,
            title="Project Two PO",
        )

    def setUp(self):
        self.records = SearchRecord.objects.filter(
            id__in=[
                self.company_record.id,
                self.project_one_record.id,
                self.project_two_record.id,
            ]
        )

    def test_global_scope_filter_include_company_and_all_projects(self):
        scope = {"type": "global", "include_company": True, "project_ids": []}
        results = _apply_global_scope_filter(
            self.records,
            scope,
            {self.project_one.id, self.project_two.id},
        )

        self.assertEqual(
            set(results.values_list("id", flat=True)),
            {
                self.company_record.id,
                self.project_one_record.id,
                self.project_two_record.id,
            },
        )

    def test_global_scope_filter_include_company_and_selected_projects(self):
        scope = {
            "type": "global",
            "include_company": True,
            "project_ids": [self.project_one.id],
        }
        results = _apply_global_scope_filter(
            self.records,
            scope,
            {self.project_one.id, self.project_two.id},
        )

        self.assertEqual(
            set(results.values_list("id", flat=True)),
            {self.company_record.id, self.project_one_record.id},
        )

    def test_global_scope_filter_exclude_company_and_all_projects(self):
        scope = {"type": "global", "include_company": False, "project_ids": []}
        results = _apply_global_scope_filter(
            self.records,
            scope,
            {self.project_one.id, self.project_two.id},
        )

        self.assertEqual(
            set(results.values_list("id", flat=True)),
            {self.project_one_record.id, self.project_two_record.id},
        )

    def test_global_scope_filter_exclude_company_and_selected_projects(self):
        scope = {
            "type": "global",
            "include_company": False,
            "project_ids": [self.project_two.id],
        }
        results = _apply_global_scope_filter(
            self.records,
            scope,
            {self.project_one.id, self.project_two.id},
        )

        self.assertEqual(
            set(results.values_list("id", flat=True)),
            {self.project_two_record.id},
        )

    def test_validate_global_project_ids_rejects_inaccessible_projects(self):
        scope = {
            "type": "global",
            "include_company": False,
            "project_ids": [self.project_one.id, 9999],
        }

        with self.assertRaises(AdvancedSearchValidationError) as context:
            _validate_global_project_ids(scope, {self.project_one.id})

        self.assertIn("not accessible", context.exception.detail)
        self.assertIn("9999", context.exception.suggestions)


class AdvancedSearchGlobalScopeApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="global-search-api-user@example.com",
            password="password",
            first_name="Global",
            last_name="Api",
            user_type="freelancer",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch(
        "project_management.services.advanced_search._authorized_project_ids",
        return_value={101, 102},
    )
    @patch(
        "project_management.views.advanced_search.advanced_search_statement_timeout",
        return_value=nullcontext(),
    )
    @patch(
        "project_management.views.advanced_search.advanced_search_capacity",
        return_value=nullcontext(),
    )
    def test_global_scope_api_rejects_inaccessible_project_ids(
        self,
        _capacity_mock,
        _timeout_mock,
        _authorized_projects_mock,
    ):
        response = self.client.post(
            "/api/project_management/advanced-search/query/",
            {
                "query": "purchase",
                "scope": {
                    "type": "global",
                    "include_company": True,
                    "project_ids": [101, 999],
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not accessible", response.data["detail"])
