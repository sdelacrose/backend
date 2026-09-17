from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase

from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import (
    Document,
    DocumentContentSegment,
    DocumentSearchIndex,
    Folder,
    ProductAssemblyExtractionSource,
    ProductAttributeAppliedColumn,
    ProductAttributeAppliedValue,
    ProductAttributeExtractionRun,
    ProductAttributeExtractionSource,
    Project,
    ProjectMatrixConfig,
    SolutionDemand,
)
from project_management.models.constants import CUSTOMER_DEMAND_DOCUMENT_TYPE
from project_management.services.product_attribute_extraction import (
    ProductAttributeExtractionError,
    apply_attribute_extraction_run,
    create_attribute_extraction_run,
    process_attribute_extraction_run,
    retry_attribute_extraction_run,
    source_documents_changed,
)
from project_management.services.product_assembly_extraction import (
    create_extraction_run as create_assembly_extraction_run,
)
from project_management.signals import create_default_folders
from project_management.views.product_attribute_extraction import (
    ProductAttributeExtractionRunViewSet,
)


class ProductAttributeExtractionTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        self.notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        self.notification_patcher.start()
        self.matrix_room_patcher = patch(
            "chat.signals.matrix_service.ensure_room",
            return_value=SimpleNamespace(room_type="project"),
        )
        self.matrix_room_patcher.start()
        self.user = User.objects.create_user(
            email="attribute-owner@example.com",
            password="password",
            first_name="Attribute",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Attribute Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.user.email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            document_types=[CUSTOMER_DEMAND_DOCUMENT_TYPE],
        )
        self.project = Project.objects.create(
            project_name="AML-500",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.folder = Folder.objects.create(project=self.project, name="Customer Demands")
        self.document = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name="AML-500 Customer Demand.docx",
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE,
            status="Draft",
        )
        self.index = DocumentSearchIndex.objects.create(
            document=self.document,
            status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash="attribute-hash-v1",
            parser_version="document-search-v1",
        )
        self.segment = DocumentContentSegment.objects.create(
            document=self.document,
            index=self.index,
            segment_type=DocumentContentSegment.SegmentType.PAGE,
            sequence=1,
            location_label="Page 2",
            text=(
                "CDE-101 requires the Motor Assembly to use a 400 V supply. "
                "The enclosure material shall be stainless steel."
            ),
        )
        self.demand = SolutionDemand.objects.create(
            project=self.project,
            subject="Motor Assembly",
            demand_number="CDE-101",
            level=1,
            sort_order=1,
            created_by=self.user,
            modified_by=self.user,
        )
        ProjectMatrixConfig.objects.create(
            project=self.project,
            demand_columns=[],
            created_by=self.user,
            modified_by=self.user,
        )

    def tearDown(self):
        self.matrix_room_patcher.stop()
        self.notification_patcher.stop()
        post_save.connect(create_default_folders, sender=Project)

    def extraction_result(self):
        evidence = [
            {
                "document_id": self.document.id,
                "segment_id": self.segment.id,
                "location": "Page 2",
                "quote": "CDE-101 requires the Motor Assembly to use a 400 V supply.",
            }
        ]
        return {
            "schema_version": "1",
            "project_language": "en",
            "attributes": [
                {
                    "proposal_key": "attribute_001",
                    "label": "Supply voltage",
                    "label_en": "Supply voltage",
                    "label_de": "Versorgungsspannung",
                    "label_fr": "Tension d'alimentation",
                    "unit": "V",
                    "description": "Required supply voltage.",
                    "existing_column_key": None,
                    "confidence": 0.98,
                    "evidence": evidence,
                    "values": [
                        {
                            "solution_demand_id": self.demand.id,
                            "value": "400 V",
                            "confidence": 0.98,
                            "evidence": evidence,
                        }
                    ],
                    "conflicts": [],
                }
            ],
            "unresolved_items": [],
            "warnings": [],
        }

    def create_additional_source(self):
        document = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name="Additional attributes.pdf",
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE,
            status="Draft",
        )
        index = DocumentSearchIndex.objects.create(
            document=document,
            status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash=f"attribute-hash-{document.id}",
            parser_version="document-search-v2",
        )
        DocumentContentSegment.objects.create(
            document=document,
            index=index,
            segment_type=DocumentContentSegment.SegmentType.PAGE,
            sequence=1,
            location_label="Page 3",
            text="The Motor Assembly manufacturer is Siemens.",
        )
        return document

    def test_review_ready_attribute_run_refreshes_with_new_documents(self):
        run = create_attribute_extraction_run(self.project, self.user)
        run.status = ProductAttributeExtractionRun.Status.REVIEW_READY
        run.result = self.extraction_result()
        run.save(update_fields=["status", "result", "updated_at"])
        additional_document = self.create_additional_source()

        self.assertTrue(source_documents_changed(run))

        replacement = retry_attribute_extraction_run(run, self.user)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.STALE)
        self.assertEqual(
            list(
                replacement.sources.values_list(
                    "document_id_snapshot",
                    flat=True,
                )
            ),
            [self.document.id, additional_document.id],
        )

    def test_attribute_source_document_can_be_deleted_without_losing_history(self):
        run = create_attribute_extraction_run(self.project, self.user)
        source = run.sources.get()
        document_id = self.document.id
        document_name = self.document.file_name

        self.document.delete()

        source.refresh_from_db()
        self.assertIsNone(source.document_id)
        self.assertIsNone(source.search_index_id)
        self.assertEqual(source.document_id_snapshot, document_id)
        self.assertEqual(source.document_name, document_name)
        self.assertTrue(
            ProductAttributeExtractionSource.objects.filter(run=run).exists()
        )

    def test_document_deletion_preserves_assembly_and_attribute_history(self):
        assembly_run = create_assembly_extraction_run(self.project, self.user)
        attribute_run = create_attribute_extraction_run(self.project, self.user)
        document_id = self.document.id

        self.document.delete()

        assembly_source = ProductAssemblyExtractionSource.objects.get(
            run=assembly_run,
        )
        attribute_source = ProductAttributeExtractionSource.objects.get(
            run=attribute_run,
        )
        self.assertIsNone(assembly_source.document_id)
        self.assertIsNone(attribute_source.document_id)
        self.assertEqual(assembly_source.document_id_snapshot, document_id)
        self.assertEqual(attribute_source.document_id_snapshot, document_id)

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_transient_timeout_recovers_without_creating_duplicate_values(self, post):
        import requests

        post.side_effect = [
            requests.ReadTimeout("slow upstream"),
            SimpleNamespace(status_code=200, json=lambda: self.extraction_result()),
        ]
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.REVIEW_READY)
        apply_attribute_extraction_run(run, self.user)
        apply_attribute_extraction_run(run, self.user)
        self.assertEqual(ProductAttributeAppliedValue.objects.filter(
            applied_column__run=run,
        ).count(), 1)

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_exhausted_timeouts_explain_failure_without_applying_values(self, post):
        import requests

        post.side_effect = requests.ReadTimeout("slow upstream")
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "extraction_service_timeout")
        self.assertIn("timed out", run.error_message)
        self.assertFalse(ProductAttributeAppliedValue.objects.exists())

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_ninety_second_upstream_response_can_complete(self, post):
        import requests

        def upstream(*args, **kwargs):
            timeout = kwargs["timeout"]
            read_timeout = timeout[1] if isinstance(timeout, tuple) else timeout
            if read_timeout < 90:
                raise requests.ReadTimeout("response requires 90 seconds")
            return SimpleNamespace(status_code=200, json=lambda: self.extraction_result())

        post.side_effect = upstream
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.REVIEW_READY)

    @patch("project_management.services.product_attribute_extraction.requests.post")
    @patch("project_management.services.ai_extraction_client.time.sleep")
    def test_transient_http_errors_recover_but_permission_errors_do_not_retry(self, sleep, post):
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                post.side_effect = [SimpleNamespace(status_code=status), SimpleNamespace(
                    status_code=200, json=lambda: self.extraction_result(),
                )]
                run = create_attribute_extraction_run(self.project, self.user)
                process_attribute_extraction_run(run.id)
                run.refresh_from_db()
                self.assertEqual(run.status, ProductAttributeExtractionRun.Status.REVIEW_READY)
                run.status = ProductAttributeExtractionRun.Status.CANCELLED
                run.save(update_fields=["status"])
        post.side_effect = [SimpleNamespace(status_code=403, json=lambda: {"detail": "Forbidden"})]
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.error_code, "extraction_service_failed")
        self.assertEqual(run.error_message, "Forbidden")

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_empty_extraction_is_reviewable_with_explanation_but_not_applicable(self, post):
        from project_management.serializers.product_attribute_extraction import (
            ProductAttributeExtractionRunSerializer,
        )

        result = {
            "schema_version": "1",
            "attributes": [],
            "warnings": ["Only shared production-line targets were found."],
            "unresolved_items": ["Line output cannot be assigned to each battery."],
        }
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()
        data = ProductAttributeExtractionRunSerializer(run).data
        self.assertEqual(data["status"], "review_ready")
        self.assertEqual(data["warnings"], result["warnings"])
        self.assertEqual(data["result"]["unresolved_items"], result["unresolved_items"])
        with self.assertRaises(ProductAttributeExtractionError) as error:
            apply_attribute_extraction_run(run, self.user)
        self.assertEqual(error.exception.code, "no_attributes_selected")

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_extract_and_apply_creates_configurable_column_and_value(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        run = create_attribute_extraction_run(self.project, self.user)

        process_attribute_extraction_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.REVIEW_READY)
        self.assertFalse(run.hint_only)

        apply_attribute_extraction_run(run, self.user)
        run.refresh_from_db()
        self.demand.refresh_from_db()
        config = ProjectMatrixConfig.objects.get(project=self.project)
        column = config.demand_columns[0]
        self.assertEqual(column["label_en"], "Supply voltage")
        self.assertEqual(column["type"], "text")
        self.assertEqual(self.demand.custom_fields[column["key"]], "400 V")
        self.assertEqual(ProductAttributeAppliedColumn.objects.filter(run=run).count(), 1)
        self.assertEqual(ProductAttributeAppliedValue.objects.filter(applied_column__run=run).count(), 1)

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_apply_preserves_existing_manual_value(self, post):
        config = ProjectMatrixConfig.objects.get(project=self.project)
        config.demand_columns = [
            {
                "key": "supply_voltage",
                "label": "Supply voltage",
                "label_en": "Supply voltage",
                "label_de": "Versorgungsspannung",
                "label_fr": "Tension d'alimentation",
                "type": "text",
                "order": 0,
            }
        ]
        config.save(update_fields=["demand_columns", "updated_at"])
        self.demand.custom_fields = {"supply_voltage": "Manual 230 V"}
        self.demand.save(update_fields=["custom_fields", "updated_at"])
        result = self.extraction_result()
        result["attributes"][0]["existing_column_key"] = "supply_voltage"
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: result,
        )
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()

        apply_attribute_extraction_run(run, self.user)
        self.demand.refresh_from_db()
        run.refresh_from_db()

        self.assertEqual(self.demand.custom_fields["supply_voltage"], "Manual 230 V")
        self.assertTrue(any("existing manual value" in warning for warning in run.warnings))

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_apply_rejects_matrix_changes_after_review(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()
        self.demand.subject = "Manually edited assembly"
        self.demand.save(update_fields=["subject", "updated_at"])

        with self.assertRaises(ProductAttributeExtractionError) as context:
            apply_attribute_extraction_run(run, self.user)

        self.assertEqual(context.exception.code, "extraction_inputs_changed")
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.STALE)

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_later_extraction_is_hint_only_and_cannot_write(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        first_run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(first_run.id)
        first_run.refresh_from_db()
        apply_attribute_extraction_run(first_run, self.user)

        hint_run = create_attribute_extraction_run(self.project, self.user)
        self.assertTrue(hint_run.hint_only)
        process_attribute_extraction_run(hint_run.id)
        hint_run.refresh_from_db()

        with self.assertRaises(ProductAttributeExtractionError) as context:
            apply_attribute_extraction_run(hint_run, self.user)
        self.assertEqual(context.exception.code, "hint_only_run")
        self.assertEqual(
            ProductAttributeAppliedColumn.objects.filter(run=hint_run).count(),
            0,
        )

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_rejects_value_for_customer_demand_in_another_project(self, post):
        result = self.extraction_result()
        result["attributes"][0]["values"][0]["solution_demand_id"] = 999999
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_attribute_extraction_run(self.project, self.user)

        process_attribute_extraction_run(run.id)
        run.refresh_from_db()

        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "invalid_solution_demand")

    def test_requires_existing_customer_demand_rows(self):
        SolutionDemand.objects.filter(project=self.project).delete()

        with self.assertRaises(ProductAttributeExtractionError) as context:
            create_attribute_extraction_run(self.project, self.user)

        self.assertEqual(context.exception.code, "no_customer_demands")

    def test_retry_reselects_current_eligible_documents(self):
        failed_run = SimpleNamespace(
            status=ProductAttributeExtractionRun.Status.FAILED,
            project=self.project,
            project_language="en",
        )
        replacement = SimpleNamespace(id=321, refresh_from_db=lambda: None)
        view = ProductAttributeExtractionRunViewSet()
        view.get_object = lambda: failed_run
        view.get_serializer = lambda run: SimpleNamespace(data={"id": run.id})
        request = SimpleNamespace(user=self.user)

        with (
            patch(
                "project_management.views.product_attribute_extraction.retry_attribute_extraction_run",
                return_value=replacement,
            ) as retry_run,
            patch(
                "project_management.views.product_attribute_extraction.dispatch_attribute_extraction_run"
            ) as dispatch_run,
        ):
            response = view.retry(request)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(retry_run.call_args.kwargs["run"], failed_run)
        self.assertEqual(retry_run.call_args.kwargs["user"], self.user)
        self.assertEqual(retry_run.call_args.kwargs["request"], request)
        dispatch_run.assert_called_once_with(replacement.id)

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_apply_multiple_attributes_respects_review_selections(self, post):
        result = self.extraction_result()
        result["attributes"][0]["selected"] = False
        material_evidence = [
            {
                "document_id": self.document.id,
                "segment_id": self.segment.id,
                "location": "Page 2",
                "quote": "The enclosure material shall be stainless steel.",
            }
        ]
        result["attributes"].append(
            {
                "proposal_key": "attribute_002",
                "label": "Enclosure material",
                "label_en": "Enclosure material",
                "label_de": "Gehäusematerial",
                "label_fr": "Matériau du boîtier",
                "unit": "",
                "description": "Required enclosure material.",
                "existing_column_key": None,
                "confidence": 0.97,
                "evidence": material_evidence,
                "values": [
                    {
                        "solution_demand_id": self.demand.id,
                        "value": "Stainless steel",
                        "confidence": 0.97,
                        "evidence": material_evidence,
                    }
                ],
                "conflicts": [],
            }
        )
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_attribute_extraction_run(self.project, self.user)
        process_attribute_extraction_run(run.id)
        run.refresh_from_db()

        apply_attribute_extraction_run(run, self.user)
        self.demand.refresh_from_db()
        config = ProjectMatrixConfig.objects.get(project=self.project)

        self.assertEqual(len(config.demand_columns), 1)
        self.assertEqual(config.demand_columns[0]["label"], "Enclosure material")
        self.assertEqual(
            self.demand.custom_fields[config.demand_columns[0]["key"]],
            "Stainless steel",
        )
        self.assertEqual(
            ProductAttributeAppliedColumn.objects.filter(run=run).count(),
            1,
        )

    @patch("project_management.services.product_attribute_extraction.requests.post")
    def test_rejects_fabricated_evidence_quote(self, post):
        result = self.extraction_result()
        result["attributes"][0]["evidence"][0]["quote"] = (
            "The product supports a fabricated voltage."
        )
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_attribute_extraction_run(self.project, self.user)

        process_attribute_extraction_run(run.id)
        run.refresh_from_db()

        self.assertEqual(run.status, ProductAttributeExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "invalid_evidence_quote")
