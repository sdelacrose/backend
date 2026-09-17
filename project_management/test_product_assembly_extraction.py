from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework.test import APIClient

from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import (
    CompanySolutionSpaceItem,
    Document,
    DocumentContentSegment,
    DocumentSearchIndex,
    Folder,
    ProductAssemblyAppliedRow,
    ProductAssemblyExtractionRun,
    ProductAssemblyExtractionSource,
    Project,
    ProjectTeamMembers,
    SolutionDemand,
)
from project_management.models.constants import CUSTOMER_DEMAND_DOCUMENT_TYPE
from project_management.services.product_assembly_extraction import (
    ProductAssemblyExtractionError,
    apply_extraction_run,
    create_extraction_run,
    process_extraction_run,
    retry_extraction_run,
    source_documents_changed,
)
from project_management.serializers.product_assembly_extraction import (
    ProductAssemblyExtractionRunSerializer,
)
from project_management.signals import create_default_folders
from project_management.views.product_assembly_extraction import (
    ProductAssemblyProjectManagerPermission,
    ProductAssemblyExtractionRunViewSet,
)


class ProductAssemblyExtractionTests(TestCase):
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
            email="assembly-owner@example.com",
            password="password",
            first_name="Assembly",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Assembly Company",
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
            project_name="DM-200",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.folder = Folder.objects.create(
            project=self.project,
            name="Customer Demands",
        )
        self.document = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name="DM-200 Customer Demand.docx",
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE,
            status="Draft",
        )
        self.index = DocumentSearchIndex.objects.create(
            document=self.document,
            status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash="hash-v1",
            parser_version="document-search-v1",
        )
        self.segment = DocumentContentSegment.objects.create(
            document=self.document,
            index=self.index,
            segment_type=DocumentContentSegment.SegmentType.PAGE,
            sequence=1,
            location_label="Page 1",
            text=(
                "CDE-001 requires a Bench Drilling Machine DM-200. "
                "CDE-002 defines its Support and Workholding Assembly. "
                "S01 Frame Preparation is a manufacturing station."
            ),
        )

    def tearDown(self):
        self.matrix_room_patcher.stop()
        self.notification_patcher.stop()
        post_save.connect(create_default_folders, sender=Project)

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_demand_generation_recovers_from_transient_timeout(self, post):
        import requests

        post.side_effect = [requests.ReadTimeout(), SimpleNamespace(
            status_code=200, json=lambda: self.extraction_result(),
        )]
        run = create_extraction_run(self.project, self.user)
        process_extraction_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAssemblyExtractionRun.Status.REVIEW_READY)

    def extraction_result(self):
        return {
            "schema_version": "1",
            "project_language": "en",
            "rows": [
                {
                    "proposal_key": "assembly_001",
                    "parent_key": None,
                    "name": "Bench Drilling Machine DM-200",
                    "demand_numbers": ["CDE-001"],
                    "confidence": 0.99,
                    "evidence": [
                        {
                            "document_id": self.document.id,
                            "segment_id": self.segment.id,
                            "location": "Page 1",
                            "quote": "CDE-001 requires a Bench Drilling Machine DM-200.",
                        }
                    ],
                },
                {
                    "proposal_key": "assembly_002",
                    "parent_key": "assembly_001",
                    "name": "Support and Workholding Assembly",
                    "demand_numbers": ["CDE-002"],
                    "confidence": 0.96,
                    "evidence": [
                        {
                            "document_id": self.document.id,
                            "segment_id": self.segment.id,
                            "location": "Page 1",
                            "quote": "CDE-002 defines its Support and Workholding Assembly.",
                        }
                    ],
                },
            ],
            "unresolved_items": [],
            "warnings": [],
        }

    def create_additional_source(self, name="Additional demand.pdf"):
        document = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name=name,
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE,
            status="Draft",
        )
        index = DocumentSearchIndex.objects.create(
            document=document,
            status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash=f"hash-{document.id}",
            parser_version="document-search-v2",
        )
        DocumentContentSegment.objects.create(
            document=document,
            index=index,
            segment_type=DocumentContentSegment.SegmentType.PAGE,
            sequence=1,
            location_label="Page 1",
            text="CDE-003 defines an additional Valve Assembly.",
        )
        return document

    def test_create_run_uses_only_searchable_customer_demand_documents(self):
        other_document = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name="Drawing.pdf",
            document_type="Technical Drawing:TDR",
            status="Draft",
        )
        DocumentSearchIndex.objects.create(
            document=other_document,
            status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash="other",
        )

        run = create_extraction_run(self.project, self.user)

        self.assertEqual(list(run.sources.values_list("document_id", flat=True)), [self.document.id])
        self.assertEqual(run.status, ProductAssemblyExtractionRun.Status.QUEUED)

    def test_create_run_accepts_customer_demand_display_label_from_upload(self):
        self.document.document_type = "Customer Demand"
        self.document.save(update_fields=["document_type", "updated_at"])

        run = create_extraction_run(self.project, self.user)

        self.assertEqual(
            list(run.sources.values_list("document_id", flat=True)),
            [self.document.id],
        )

    def test_all_current_review_detects_new_document_and_can_be_refreshed(self):
        run = create_extraction_run(self.project, self.user)
        run.status = ProductAssemblyExtractionRun.Status.REVIEW_READY
        run.result = self.extraction_result()
        run.save(update_fields=["status", "result", "updated_at"])
        additional_document = self.create_additional_source()

        self.assertTrue(source_documents_changed(run))

        replacement = retry_extraction_run(run, self.user)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAssemblyExtractionRun.Status.STALE)
        self.assertEqual(
            list(
                replacement.sources.values_list(
                    "document_id_snapshot",
                    flat=True,
                )
            ),
            [self.document.id, additional_document.id],
        )

    def test_review_detects_company_catalog_classification_changes(self):
        run = create_extraction_run(self.project, self.user)

        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Cross-Belt Sorter Assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            created_by=self.user,
            modified_by=self.user,
        )

        self.assertTrue(source_documents_changed(run))

    def test_explicit_source_review_ignores_unselected_document_additions(self):
        run = create_extraction_run(
            self.project,
            self.user,
            document_ids=[self.document.id],
        )
        self.create_additional_source()

        self.assertEqual(
            run.source_selection_mode,
            ProductAssemblyExtractionRun.SourceSelectionMode.EXPLICIT,
        )
        self.assertFalse(source_documents_changed(run))

    def test_source_document_can_be_deleted_without_losing_history(self):
        run = create_extraction_run(self.project, self.user)
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
            ProductAssemblyExtractionSource.objects.filter(run=run).exists()
        )
        serialized_source = ProductAssemblyExtractionRunSerializer(run).data[
            "sources"
        ][0]
        self.assertEqual(serialized_source["document_id"], document_id)
        self.assertEqual(serialized_source["file_name"], document_name)

    def test_create_run_rejects_project_without_customer_demand_documents(self):
        self.document.delete()

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            create_extraction_run(self.project, self.user)

        self.assertEqual(context.exception.code, "no_eligible_source_documents")

    def test_create_run_rejects_customer_demand_with_pending_search_index(self):
        self.index.status = DocumentSearchIndex.Status.PENDING
        self.index.save(update_fields=["status", "updated_at"])

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            create_extraction_run(self.project, self.user)

        self.assertEqual(context.exception.code, "source_documents_not_ready")

    def test_create_run_excludes_stored_only_customer_demand_archive(self):
        archive = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name="customer-demand.zip",
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE,
            status="Draft",
        )
        DocumentSearchIndex.objects.create(
            document=archive,
            status=DocumentSearchIndex.Status.UNSUPPORTED,
            mime_type="application/zip",
            extraction_method=DocumentSearchIndex.ExtractionMethod.NONE,
            content_hash="archive-hash",
        )

        run = create_extraction_run(self.project, self.user)

        self.assertEqual(
            list(run.sources.values_list("document_id", flat=True)),
            [self.document.id],
        )

    @patch(
        "project_management.services.product_assembly_extraction."
        "get_accessible_document_queryset"
    )
    def test_create_run_never_uses_documents_hidden_from_requester(
        self,
        accessible_documents,
    ):
        accessible_documents.return_value = Document.objects.none()

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            create_extraction_run(
                self.project,
                self.user,
                request=SimpleNamespace(user=self.user),
            )

        self.assertEqual(context.exception.code, "no_eligible_source_documents")

    def test_retry_replaces_historical_sources_with_current_documents(self):
        failed_run = create_extraction_run(self.project, self.user)
        failed_run.status = ProductAssemblyExtractionRun.Status.FAILED
        failed_run.save(update_fields=["status", "updated_at"])
        self.document.document_type = "Technical Drawing:TDR"
        self.document.save(update_fields=["document_type", "updated_at"])
        current_document = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name="DM-200 Revised Customer Demand.docx",
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE,
            status="Draft",
        )
        DocumentSearchIndex.objects.create(
            document=current_document,
            status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash="hash-current",
            parser_version="document-search-v1",
        )
        view = ProductAssemblyExtractionRunViewSet()
        view.get_object = lambda: failed_run
        view.get_serializer = lambda run: SimpleNamespace(data={"id": run.id})
        request = SimpleNamespace(user=self.user)

        with (
            patch(
                "project_management.services.product_assembly_extraction."
                "get_accessible_document_queryset",
                return_value=Document.objects.filter(folder__project=self.project),
            ),
            patch(
                "project_management.views.product_assembly_extraction."
                "dispatch_extraction_run"
            ),
        ):
            response = view.retry(request)

        self.assertEqual(response.status_code, 201, response.data)
        replacement = ProductAssemblyExtractionRun.objects.exclude(
            id=failed_run.id
        ).latest("id")
        self.assertEqual(
            list(replacement.sources.values_list("document_id", flat=True)),
            [current_document.id],
        )

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_process_and_apply_creates_evidence_linked_hierarchy_once(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        run = create_extraction_run(self.project, self.user)

        process_extraction_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAssemblyExtractionRun.Status.REVIEW_READY)
        self.assertNotIn("S01 Frame Preparation", [row["name"] for row in run.result["rows"]])

        apply_extraction_run(run, self.user)
        run.refresh_from_db()
        root = SolutionDemand.objects.get(
            subject="Bench Drilling Machine DM-200",
        )
        child = SolutionDemand.objects.get(
            subject="Support and Workholding Assembly",
        )
        self.assertEqual(child.parent, root)
        self.assertEqual(child.level, 2)
        self.assertEqual(root.demand_number, "CDE-001")
        self.assertEqual(
            root.custom_fields["product_assembly_extraction"]["demand_numbers"],
            ["CDE-001"],
        )
        self.assertEqual(list(root.documents.values_list("id", flat=True)), [self.document.id])
        self.assertEqual(ProductAssemblyAppliedRow.objects.filter(run=run).count(), 2)
        self.assertEqual(
            set(
                ProductAssemblyAppliedRow.objects.filter(run=run).values_list(
                    "solution_demand_id",
                    flat=True,
                )
            ),
            {root.id, child.id},
        )

        apply_extraction_run(run, self.user)
        self.assertEqual(
            SolutionDemand.objects.filter(project=self.project).count(),
            2,
        )

        request_payload = post.call_args.kwargs["json"]
        self.assertIn("company_assembly_catalog", request_payload)

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_process_sends_company_catalog_classification_context(self, post):
        process_assembly = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Cross-Belt Sorter Assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            created_by=self.user,
            modified_by=self.user,
        )
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        run = create_extraction_run(self.project, self.user)

        process_extraction_run(run.id)

        request_payload = post.call_args.kwargs["json"]
        self.assertIn(
            {
                "id": process_assembly.id,
                "parent_id": None,
                "name": process_assembly.assembly_name,
                "assembly_type": CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            },
            request_payload["company_assembly_catalog"],
        )

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_process_rejects_company_process_assembly_as_product(self, post):
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Bench-Drilling Machine DM 200",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            created_by=self.user,
            modified_by=self.user,
        )
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        run = create_extraction_run(self.project, self.user)

        process_extraction_run(run.id)
        run.refresh_from_db()

        self.assertEqual(run.status, ProductAssemblyExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "assembly_classification_conflict")
        self.assertEqual(
            SolutionDemand.objects.filter(project=self.project).count(),
            0,
        )

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_process_reports_when_documents_contain_no_product_assemblies(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "schema_version": "1",
                "project_language": "en",
                "rows": [],
                "unresolved_items": [],
                "warnings": [
                    "No physical product or assembly was identified in the source documents."
                ],
            },
        )
        run = create_extraction_run(self.project, self.user)

        process_extraction_run(run.id)
        run.refresh_from_db()

        self.assertEqual(
            run.status,
            ProductAssemblyExtractionRun.Status.REVIEW_READY,
        )
        self.assertEqual(run.result["rows"], [])
        self.assertEqual(
            SolutionDemand.objects.filter(project=self.project).count(),
            0,
        )

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            apply_extraction_run(run, self.user)
        self.assertEqual(context.exception.code, "no_product_assemblies_to_apply")

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_unexpected_worker_failure_is_retryable(self, post):
        post.side_effect = RuntimeError("unexpected worker failure")
        run = create_extraction_run(self.project, self.user)

        process_extraction_run(run.id)
        run.refresh_from_db()

        self.assertEqual(run.status, ProductAssemblyExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "unexpected_extraction_failure")
        self.assertEqual(
            SolutionDemand.objects.filter(project=self.project).count(),
            0,
        )

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_later_generation_is_hint_only_after_application(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        initial_run = create_extraction_run(self.project, self.user)
        process_extraction_run(initial_run.id)
        initial_run.refresh_from_db()
        apply_extraction_run(initial_run, self.user)

        hint_run = create_extraction_run(self.project, self.user)
        self.assertTrue(hint_run.hint_only)
        hint_run.result = self.extraction_result()
        hint_run.status = ProductAssemblyExtractionRun.Status.REVIEW_READY
        hint_run.save(update_fields=["result", "status", "updated_at"])
        with self.assertRaises(ProductAssemblyExtractionError) as context:
            apply_extraction_run(hint_run, self.user)

        self.assertEqual(context.exception.code, "hint_only_run")
        self.assertEqual(
            SolutionDemand.objects.filter(project=self.project).count(),
            2,
        )

    @patch("project_management.services.product_assembly_extraction.requests.post")
    def test_hint_matching_preserves_multilingual_existing_assemblies(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.extraction_result(),
        )
        initial_run = create_extraction_run(self.project, self.user)
        process_extraction_run(initial_run.id)
        initial_run.refresh_from_db()
        apply_extraction_run(initial_run, self.user)

        translated_result = self.extraction_result()
        translated_result["project_language"] = "de"
        translated_result["rows"][0]["name"] = "Tischbohrmaschine DM-200"
        translated_result["rows"][1]["name"] = "Aufnahmebaugruppe"
        translated_result["rows"].append(
            {
                "proposal_key": "assembly_003",
                "parent_key": "assembly_001",
                "name": "New Guard Assembly",
                "demand_numbers": ["CDE-003"],
                "confidence": 0.95,
                "evidence": [
                    {
                        "document_id": self.document.id,
                        "segment_id": self.segment.id,
                        "location": "Page 1",
                        "quote": "CDE-002 defines its Support and Workholding Assembly.",
                    }
                ],
            }
        )
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: translated_result,
        )

        hint_run = create_extraction_run(
            self.project,
            self.user,
            project_language="de",
        )
        process_extraction_run(hint_run.id)
        hint_run.refresh_from_db()

        self.assertTrue(hint_run.hint_only)
        self.assertEqual(
            [row["is_new_assembly"] for row in hint_run.result["rows"]],
            [False, False, True],
        )

    def test_generation_cannot_start_while_review_is_pending(self):
        create_extraction_run(self.project, self.user)

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            create_extraction_run(self.project, self.user)

        self.assertEqual(context.exception.code, "extraction_already_in_progress")

    def test_second_legacy_review_run_cannot_be_applied(self):
        first_run = create_extraction_run(self.project, self.user)
        first_run.result = self.extraction_result()
        first_run.status = ProductAssemblyExtractionRun.Status.REVIEW_READY
        first_run.save(update_fields=["result", "status", "updated_at"])
        second_run = ProductAssemblyExtractionRun.objects.create(
            project=self.project,
            status=ProductAssemblyExtractionRun.Status.REVIEW_READY,
            schema_version="1",
            project_language="en",
            source_snapshot_hash=first_run.source_snapshot_hash,
            result=self.extraction_result(),
            created_by=self.user,
            modified_by=self.user,
        )

        apply_extraction_run(first_run, self.user)

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            apply_extraction_run(second_run, self.user)

        self.assertEqual(context.exception.code, "extraction_already_applied")
        self.assertEqual(
            SolutionDemand.objects.filter(project=self.project).count(),
            2,
        )

    def test_apply_rejects_changed_source_snapshot(self):
        run = create_extraction_run(self.project, self.user)
        run.result = self.extraction_result()
        run.status = ProductAssemblyExtractionRun.Status.REVIEW_READY
        run.save(update_fields=["result", "status", "updated_at"])
        self.index.content_hash = "hash-v2"
        self.index.save(update_fields=["content_hash", "updated_at"])

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            apply_extraction_run(run, self.user)

        self.assertEqual(context.exception.code, "source_documents_changed")
        run.refresh_from_db()
        self.assertEqual(run.status, ProductAssemblyExtractionRun.Status.STALE)

    def test_invalid_evidence_is_rejected(self):
        run = create_extraction_run(self.project, self.user)
        result = self.extraction_result()
        result["rows"][0]["evidence"][0]["document_id"] = self.document.id + 999
        run.result = result
        run.status = ProductAssemblyExtractionRun.Status.REVIEW_READY
        run.save(update_fields=["result", "status", "updated_at"])

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            apply_extraction_run(run, self.user)

        self.assertEqual(context.exception.code, "invalid_evidence")

    def test_evidence_quote_must_exist_in_the_referenced_segment(self):
        run = create_extraction_run(self.project, self.user)
        result = self.extraction_result()
        result["rows"][0]["evidence"][0]["quote"] = (
            "This sentence was not present in the Customer Demand."
        )
        run.result = result
        run.status = ProductAssemblyExtractionRun.Status.REVIEW_READY
        run.save(update_fields=["result", "status", "updated_at"])

        with self.assertRaises(ProductAssemblyExtractionError) as context:
            apply_extraction_run(run, self.user)

        self.assertEqual(context.exception.code, "invalid_evidence_quote")

    def test_cross_company_user_cannot_access_extraction_runs(self):
        other_user = User.objects.create_user(
            email="other-company@example.com",
            password="password",
            first_name="Other",
            last_name="Company",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_user,
            company_name="Other Company",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="789",
            company_mob_no="012",
            company_web_address="https://other.example.com",
            company_email_address=other_user.email,
            company_tax_reference="other-tax",
            company_iban="other-iban",
        )
        other_user.company = other_company
        other_user.save(update_fields=["company"])

        client = APIClient()
        client.force_authenticate(user=other_user)
        response = client.get(
            "/api/project_management/product-assembly-extractions/",
            {"project": self.project.id},
        )

        self.assertEqual(response.status_code, 403)

    @patch(
        "project_management.views.product_assembly_extraction."
        "get_accessible_document_queryset"
    )
    def test_extraction_run_is_hidden_when_source_document_is_not_visible(
        self,
        accessible_documents,
    ):
        run = create_extraction_run(self.project, self.user)
        accessible_documents.return_value = Document.objects.none()
        client = APIClient()
        client.force_authenticate(user=self.user)

        with patch(
            "project_management.permissions.SolutionDemandPermission.has_permission",
            return_value=True,
        ):
            response = client.get(
                "/api/project_management/product-assembly-extractions/",
                {"project": self.project.id},
            )
        payload = response.json()
        results = payload if isinstance(payload, list) else payload["results"]

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(run.id, [item["id"] for item in results])

    def test_only_project_managers_can_mutate_extraction_runs(self):
        permission = ProductAssemblyProjectManagerPermission()
        self.assertTrue(permission._is_project_manager(self.user, self.project))

        team_member = User.objects.create_user(
            email="assembly-member@example.com",
            password="password",
            first_name="Assembly",
            last_name="Member",
            user_type="company_user",
            company=self.company,
        )
        membership = ProjectTeamMembers(
            project=self.project,
            team_member=team_member,
            role="architect",
            project_access="RW",
        )
        ProjectTeamMembers.objects.bulk_create([membership])
        membership.refresh_from_db()
        self.assertFalse(permission._is_project_manager(team_member, self.project))

        membership.role = "project manager"
        membership.save(update_fields=["role", "updated_at"])
        self.assertTrue(permission._is_project_manager(team_member, self.project))
