from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase, override_settings

from models.models import CompanyProfile, User
from project_management.models import (
    CompanySolutionSpaceItem,
    ProcessAssemblyAppliedRow,
    ProcessAssemblyExtractionRun,
    ProcessAttributeAppliedColumn,
    ProcessAttributeExtractionRun,
    Project,
    ProjectMatrixConfig,
    ProjectSolutionSpaceItem,
    Requirement,
    SolutionDemand,
    SolutionSpaceConfig,
    ProjectWorkstation,
)
from project_management.services.process_requirements_extraction import (
    ProcessRequirementsExtractionError,
    apply_process_assembly_run,
    apply_process_attribute_run,
    create_process_assembly_run,
    create_process_attribute_run,
    process_process_assembly_run,
    process_process_attribute_run,
)
from project_management.signals import create_default_folders


class ProcessRequirementsExtractionTests(TestCase):
    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_partial_plan_cannot_apply_while_required_capabilities_are_missing(self, post):
        result = self.station_result()
        result["missing_capabilities"] = ["Torque verification"]
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        with self.assertRaises(ProcessRequirementsExtractionError) as caught:
            apply_process_assembly_run(run, self.user)
        self.assertEqual(caught.exception.code, "missing_process_capabilities")

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_empty_catalog_review_can_restart_without_applying_the_old_run(self, post):
        from project_management.services import process_requirements_extraction as service

        post.return_value = SimpleNamespace(status_code=200, json=lambda: {
            "schema_version": "1", "rows": [],
            "unresolved_items": ["Torque verification capability is missing."], "warnings": [],
        })
        first = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(first.id)
        replacement = service.retry_process_assembly_run(first, self.user)
        self.assertNotEqual(replacement.id, first.id)
        self.assertEqual(replacement.status, ProcessAssemblyExtractionRun.Status.QUEUED)
        self.assertFalse(replacement.hint_only)
        with self.assertRaises(ProcessRequirementsExtractionError) as caught:
            apply_process_assembly_run(first, self.user)
        self.assertEqual(caught.exception.code, "run_not_review_ready")

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
            email="process-owner@example.com",
            password="password",
            first_name="Process",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Process Company",
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
        self.project = Project.objects.create(
            project_name="Drill Machine Line",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.machine = SolutionDemand.objects.create(
            project=self.project,
            subject="Drill Machine",
            demand_number="CD-001",
            sort_order=1,
            level=1,
            custom_fields={"production_rate": "60 parts/hour"},
            created_by=self.user,
            modified_by=self.user,
        )
        self.drive = SolutionDemand.objects.create(
            project=self.project,
            parent=self.machine,
            subject="Drive Assembly",
            demand_number="CD-002",
            sort_order=1,
            level=2,
            custom_fields={"supply_voltage": "400 V"},
            created_by=self.user,
            modified_by=self.user,
        )
        company_process_line = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Machine Production Line",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            sort_order=1,
            level=1,
            created_by=self.user,
            modified_by=self.user,
        )
        company_drilling_station = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=company_process_line,
            assembly_name="Drilling Station",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            sort_order=1,
            level=2,
            custom_fields={"cycle_time": "60 seconds"},
            created_by=self.user,
            modified_by=self.user,
        )
        self.process_line = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=company_process_line,
            assembly_name=company_process_line.assembly_name,
            assembly_type=company_process_line.assembly_type,
            sort_order=1,
            level=1,
            created_by=self.user,
            modified_by=self.user,
        )
        self.drilling_station = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=company_drilling_station,
            parent=self.process_line,
            assembly_name=company_drilling_station.assembly_name,
            assembly_type=company_drilling_station.assembly_type,
            sort_order=1,
            level=2,
            custom_fields=dict(company_drilling_station.custom_fields),
            created_by=self.user,
            modified_by=self.user,
        )
        ProjectMatrixConfig.objects.create(
            project=self.project,
            demand_columns=[
                {
                    "key": "production_rate",
                    "label": "Production rate",
                    "label_en": "Production rate",
                    "label_de": "Produktionsrate",
                    "label_fr": "Cadence de production",
                    "type": "text",
                    "order": 0,
                }
            ],
            requirement_columns=[],
            created_by=self.user,
            modified_by=self.user,
        )
        SolutionSpaceConfig.objects.create(
            project=self.project,
            columns=[
                {
                    "key": "cycle_time",
                    "label": "Cycle time",
                    "label_en": "Cycle time",
                    "label_de": "Zykluszeit",
                    "label_fr": "Temps de cycle",
                    "type": "text",
                    "order": 0,
                }
            ],
            created_by=self.user,
            modified_by=self.user,
        )

    def tearDown(self):
        self.matrix_room_patcher.stop()
        self.notification_patcher.stop()
        post_save.connect(create_default_folders, sender=Project)

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_generation_receives_indexed_customer_document_evidence(self, post):
        from project_management.models import Document, DocumentSearchIndex, DocumentContentSegment, Folder
        from project_management.models.constants import CUSTOMER_DEMAND_DOCUMENT_TYPE

        document = Document.objects.create(
            folder=Folder.objects.create(project=self.project, name="Demand sources"),
            author=self.user, created_by=self.user, file_name="Line demand.docx",
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE, status="Draft",
        )
        index = DocumentSearchIndex.objects.create(
            document=document, status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash="station-source-v1", parser_version="v1",
        )
        DocumentContentSegment.objects.create(
            document=document, index=index, segment_type="page", sequence=1,
            location_label="Page 1",
            text="WS10 incoming storage and WS70 finished storage reuse one approved storage template.",
        )
        post.return_value = SimpleNamespace(status_code=200, json=lambda: self.station_result())
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        payload = post.call_args.kwargs["json"]
        evidence = payload["customer_demands"][0]["custom_fields"].get("source_document_evidence", {})
        self.assertIn("WS70 finished storage", str(evidence))
        self.assertIn("station-source-v1", str(evidence))
        index.content_hash = "station-source-v2"
        index.save(update_fields=["content_hash"])
        with self.assertRaises(ProcessRequirementsExtractionError) as caught:
            apply_process_assembly_run(run, self.user)
        self.assertEqual(caught.exception.code, "extraction_inputs_changed")

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_repeated_template_occurrences_are_applied_as_distinct_stations(self, post):
        from copy import deepcopy

        result = self.station_result()
        first = result["rows"][1]
        first.update(parent_key=None, name="Incoming drilling", station_number="IN")
        second = deepcopy(first)
        second.update(proposal_key="outgoing", name="Outgoing drilling", station_number="OUT")
        result["rows"] = [first, second]
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        applied = apply_process_assembly_run(run, self.user)
        self.assertEqual(applied.status, ProcessAssemblyExtractionRun.Status.APPLIED)
        self.assertEqual(len(applied.result["rows"]), 2)
        self.assertEqual({row["station_number"] for row in applied.result["rows"]}, {"IN", "OUT"})

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_generation_never_includes_another_projects_documents(self, post):
        from project_management.models import Document, DocumentSearchIndex, DocumentContentSegment, Folder
        from project_management.models.constants import CUSTOMER_DEMAND_DOCUMENT_TYPE

        other = Project.objects.create(project_name="Other project", contract_value=0,
                                       company_profile=self.company, user=self.user, created_by=self.user)
        document = Document.objects.create(
            folder=Folder.objects.create(project=other, name="Private sources"),
            author=self.user, created_by=self.user, file_name="Other demand.docx",
            document_type=CUSTOMER_DEMAND_DOCUMENT_TYPE, status="Draft",
        )
        index = DocumentSearchIndex.objects.create(
            document=document, status=DocumentSearchIndex.Status.SEARCHABLE,
            content_hash="other-project", parser_version="v1",
        )
        DocumentContentSegment.objects.create(document=document, index=index,
            segment_type="page", sequence=1, text="OTHER-PROJECT-PRIVATE-STATION")
        post.return_value = SimpleNamespace(status_code=200, json=lambda: self.station_result())
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        self.assertNotIn("OTHER-PROJECT-PRIVATE-STATION", str(post.call_args.kwargs["json"]))

    def station_result(self):
        demand_reference = {
            "source_type": "customer_demand",
            "source_id": self.machine.id,
            "field": "name",
        }
        solution_reference = {
            "source_type": "solution_space",
            "source_id": self.drilling_station.id,
            "field": "name",
        }
        process_line_reference = {
            "source_type": "solution_space",
            "source_id": self.process_line.id,
            "field": "name",
        }
        return {
            "schema_version": "1",
            "project_language": "en",
            "rows": [
                {
                    "proposal_key": "station_001",
                    "parent_key": None,
                    "name": "Machine Production Line",
                    "station_number": None,
                    "linked_demand_ids": [self.machine.id],
                    "linked_solution_space_ids": [self.process_line.id],
                    "source_kind": "matched",
                    "confidence": 0.9,
                    "rationale": "Coordinates production of the drill machine.",
                    "source_references": [demand_reference, process_line_reference],
                    "warnings": [],
                    "unresolved_questions": [],
                },
                {
                    "proposal_key": "station_002",
                    "parent_key": "station_001",
                    "name": "Drilling Station",
                    "station_number": None,
                    "linked_demand_ids": [self.machine.id, self.drive.id],
                    "linked_solution_space_ids": [self.drilling_station.id],
                    "source_kind": "matched",
                    "confidence": 0.98,
                    "rationale": "Matches the project station catalogue.",
                    "source_references": [demand_reference, solution_reference],
                    "warnings": [],
                    "unresolved_questions": [],
                },
            ],
            "unresolved_items": [],
            "warnings": [],
        }

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_apply_creates_only_the_reviewed_leaf_workstation_and_retries_safely(self, post):
        post.return_value = SimpleNamespace(status_code=200, json=lambda: self.station_result())
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        run.refresh_from_db()
        apply_process_assembly_run(run, self.user)
        stations = list(ProjectWorkstation.objects.filter(project=self.project))
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0].name, "Drilling Station")
        self.assertEqual(stations[0].source_requirement.name, "Drilling Station")
        apply_process_assembly_run(run, self.user)
        self.assertEqual(ProjectWorkstation.objects.filter(project=self.project).count(), 1)

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_generation_recovers_from_transient_timeout(self, post):
        import requests

        post.side_effect = [requests.ReadTimeout(), SimpleNamespace(
            status_code=200, json=lambda: self.station_result(),
        )]
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAssemblyExtractionRun.Status.REVIEW_READY)

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_apply_creates_hierarchy_numbers_and_traceability(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.station_result(),
        )
        run = create_process_assembly_run(self.project, self.user)

        process_process_assembly_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAssemblyExtractionRun.Status.REVIEW_READY)
        self.assertEqual(
            [row["station_number"] for row in run.result["rows"]],
            ["ST-001", "ST-002"],
        )

        apply_process_assembly_run(run, self.user)
        root = Requirement.objects.get(name="Machine Production Line")
        child = Requirement.objects.get(name="Drilling Station")
        self.assertEqual(child.parent, root)
        self.assertEqual(child.get_sequence(), "1.1")
        self.assertEqual(child.object_number, "ST-002")
        self.assertEqual(
            set(child.solution_demands.values_list("id", flat=True)),
            {self.machine.id, self.drive.id},
        )
        self.assertEqual(
            list(self.drilling_station.requirements.values_list("id", flat=True)),
            [child.id],
        )
        self.assertEqual(ProcessAssemblyAppliedRow.objects.filter(run=run).count(), 2)

        apply_process_assembly_run(run, self.user)
        self.assertEqual(Requirement.objects.filter(project=self.project).count(), 2)

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_apply_rejects_selected_child_without_parent(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.station_result(),
        )
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        run.refresh_from_db()
        rows = run.result["rows"]
        rows[0]["selected"] = False

        with self.assertRaises(ProcessRequirementsExtractionError) as context:
            apply_process_assembly_run(run, self.user, rows)

        self.assertEqual(context.exception.code, "unselected_parent")
        self.assertFalse(Requirement.objects.filter(project=self.project).exists())

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_apply_rejects_changed_inputs(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.station_result(),
        )
        run = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(run.id)
        run.refresh_from_db()
        self.machine.subject = "Manually revised drill machine"
        self.machine.save(update_fields=["subject", "updated_at"])

        with self.assertRaises(ProcessRequirementsExtractionError) as context:
            apply_process_assembly_run(run, self.user)

        self.assertEqual(context.exception.code, "extraction_inputs_changed")
        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAssemblyExtractionRun.Status.STALE)

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_extraction_rejects_foreign_source_reference(self, post):
        result = self.station_result()
        result["rows"][0]["source_references"][0]["source_id"] = 999999
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_assembly_run(self.project, self.user)

        process_process_assembly_run(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAssemblyExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "invalid_source_reference")

    def test_station_extraction_requires_company_approved_process_selection(self):
        ProjectSolutionSpaceItem.objects.filter(project=self.project).update(
            source_company_item=None,
        )

        with self.assertRaises(ProcessRequirementsExtractionError) as context:
            create_process_assembly_run(self.project, self.user)

        self.assertEqual(
            context.exception.code,
            "no_approved_process_assemblies",
        )

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_extraction_rejects_unmatched_process(self, post):
        result = self.station_result()
        result["rows"][0]["source_kind"] = "unmatched"
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_assembly_run(self.project, self.user)

        process_process_assembly_run(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAssemblyExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "unapproved_process_assembly")

    def _seed_requirement(self):
        requirement = Requirement.objects.create(
            project=self.project,
            name="Drilling Station",
            object_number="ST-001",
            sort_order=1,
            level=1,
            created_by=self.user,
            modified_by=self.user,
        )
        requirement.solution_demands.set([self.machine, self.drive])
        self.drilling_station.requirements.add(requirement)
        return requirement

    def attribute_result(self, requirement):
        source_reference = {
            "source_type": "solution_space",
            "source_id": self.drilling_station.id,
            "field": "custom_fields.cycle_time",
        }
        return {
            "schema_version": "1",
            "project_language": "en",
            "attributes": [
                {
                    "proposal_key": "process_attribute_001",
                    "label": "Cycle time",
                    "label_en": "Cycle time",
                    "label_de": "Zykluszeit",
                    "label_fr": "Temps de cycle",
                    "unit": "seconds",
                    "description": "Station cycle time.",
                    "existing_column_key": None,
                    "confidence": 0.99,
                    "source_references": [source_reference],
                    "values": [
                        {
                            "requirement_id": requirement.id,
                            "value": "60 seconds",
                            "confidence": 0.99,
                            "source_references": [source_reference],
                        }
                    ],
                    "conflicts": [],
                }
            ],
            "unresolved_items": [],
            "warnings": [],
        }

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_attributes_merge_disjoint_values_for_the_same_column(self, post):
        import copy

        first = self._seed_requirement()
        second = self._seed_requirement()
        result = self.attribute_result(first)
        duplicate = copy.deepcopy(result["attributes"][0])
        duplicate["proposal_key"] = "cycle_time_second_station"
        duplicate["values"][0]["requirement_id"] = second.id
        result["attributes"].append(duplicate)
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_attribute_run(self.project, self.user)

        process_process_attribute_run(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAttributeExtractionRun.Status.REVIEW_READY, run.error_message)
        self.assertEqual(len(run.result["attributes"]), 1)
        self.assertEqual(
            [value["requirement_id"] for value in run.result["attributes"][0]["values"]],
            [first.id, second.id],
        )

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_station_attributes_do_not_merge_conflicting_values_for_one_station(self, post):
        import copy

        requirement = self._seed_requirement()
        result = self.attribute_result(requirement)
        duplicate = copy.deepcopy(result["attributes"][0])
        duplicate["proposal_key"] = "conflicting_cycle_time"
        duplicate["values"][0]["value"] = "Cycle time is 60 seconds"
        result["attributes"].append(duplicate)
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_attribute_run(self.project, self.user)
        process_process_attribute_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAttributeExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "duplicate_target_column")

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_process_attributes_recover_from_transient_timeout(self, post):
        import requests

        requirement = self._seed_requirement()
        post.side_effect = [requests.ReadTimeout(), SimpleNamespace(
            status_code=200, json=lambda: self.attribute_result(requirement),
        )]
        run = create_process_attribute_run(self.project, self.user)
        process_process_attribute_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAttributeExtractionRun.Status.REVIEW_READY)

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_attribute_apply_creates_column_and_value(self, post):
        requirement = self._seed_requirement()
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.attribute_result(requirement),
        )
        run = create_process_attribute_run(self.project, self.user)
        process_process_attribute_run(run.id)
        run.refresh_from_db()
        apply_process_attribute_run(run, self.user)

        requirement.refresh_from_db()
        config = ProjectMatrixConfig.objects.get(project=self.project)
        column = config.requirement_columns[0]
        self.assertEqual(requirement.custom_fields[column["key"]], "60 seconds")
        self.assertEqual(ProcessAttributeAppliedColumn.objects.filter(run=run).count(), 1)

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_attribute_apply_uses_reviewed_label_for_run_language(self, post):
        requirement = self._seed_requirement()
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.attribute_result(requirement),
        )
        run = create_process_attribute_run(self.project, self.user)
        process_process_attribute_run(run.id)
        run.refresh_from_db()
        reviewed_attributes = run.result["attributes"]
        reviewed_attributes[0]["label"] = "Production cycle time"

        apply_process_attribute_run(run, self.user, reviewed_attributes)

        config = ProjectMatrixConfig.objects.get(project=self.project)
        column = config.requirement_columns[0]
        self.assertEqual(column["label"], "Production cycle time")
        self.assertEqual(column["label_en"], "Production cycle time")
        self.assertEqual(column["label_de"], "Zykluszeit")
        self.assertEqual(column["label_fr"], "Temps de cycle")

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_attribute_apply_preserves_existing_manual_value(self, post):
        requirement = self._seed_requirement()
        config = ProjectMatrixConfig.objects.get(project=self.project)
        config.requirement_columns = [
            {
                "key": "cycle_time",
                "label": "Cycle time",
                "label_en": "Cycle time",
                "label_de": "Zykluszeit",
                "label_fr": "Temps de cycle",
                "type": "text",
                "order": 0,
            }
        ]
        config.save(update_fields=["requirement_columns", "updated_at"])
        requirement.custom_fields = {"cycle_time": "Manual 45 seconds"}
        requirement.save(update_fields=["custom_fields", "updated_at"])
        result = self.attribute_result(requirement)
        result["attributes"][0]["existing_column_key"] = "cycle_time"
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_attribute_run(self.project, self.user)
        process_process_attribute_run(run.id)
        run.refresh_from_db()

        apply_process_attribute_run(run, self.user)

        requirement.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(requirement.custom_fields["cycle_time"], "Manual 45 seconds")
        self.assertTrue(
            any("existing manual value" in warning for warning in run.warnings)
        )

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_attribute_extraction_rejects_invented_numeric_value(self, post):
        requirement = self._seed_requirement()
        result = self.attribute_result(requirement)
        result["attributes"][0]["values"][0]["value"] = "42 seconds"
        post.return_value = SimpleNamespace(status_code=200, json=lambda: result)
        run = create_process_attribute_run(self.project, self.user)

        process_process_attribute_run(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, ProcessAttributeExtractionRun.Status.FAILED)
        self.assertEqual(run.error_code, "unsupported_quantitative_value")

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_later_station_run_is_hint_only(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.station_result(),
        )
        first = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(first.id)
        first.refresh_from_db()
        apply_process_assembly_run(first, self.user)

        hint = create_process_assembly_run(self.project, self.user)
        self.assertTrue(hint.hint_only)
        process_process_assembly_run(hint.id)
        hint.refresh_from_db()
        with self.assertRaises(ProcessRequirementsExtractionError) as context:
            apply_process_assembly_run(hint, self.user)
        self.assertEqual(context.exception.code, "hint_only_run")

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_empty_later_station_run_is_a_valid_hint_result(self, post):
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.station_result(),
        )
        first = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(first.id)
        first.refresh_from_db()
        apply_process_assembly_run(first, self.user)
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "schema_version": "1",
                "rows": [],
                "unresolved_items": [],
                "warnings": [],
            },
        )

        hint = create_process_assembly_run(self.project, self.user)
        process_process_assembly_run(hint.id)
        hint.refresh_from_db()

        self.assertTrue(hint.hint_only)
        self.assertEqual(hint.status, ProcessAssemblyExtractionRun.Status.REVIEW_READY)
        self.assertEqual(hint.result["rows"], [])

    @patch("project_management.services.process_requirements_extraction.requests.post")
    def test_empty_later_attribute_run_is_a_valid_hint_result(self, post):
        requirement = self._seed_requirement()
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: self.attribute_result(requirement),
        )
        first = create_process_attribute_run(self.project, self.user)
        process_process_attribute_run(first.id)
        first.refresh_from_db()
        apply_process_attribute_run(first, self.user)
        post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "schema_version": "1",
                "attributes": [],
                "unresolved_items": [],
                "warnings": [],
            },
        )

        hint = create_process_attribute_run(self.project, self.user)
        process_process_attribute_run(hint.id)
        hint.refresh_from_db()

        self.assertTrue(hint.hint_only)
        self.assertEqual(hint.status, ProcessAttributeExtractionRun.Status.REVIEW_READY)
        self.assertEqual(hint.result["attributes"], [])

    def test_attribute_run_requires_company_approved_process_assembly(self):
        self._seed_requirement()
        ProjectSolutionSpaceItem.objects.filter(project=self.project).delete()

        with self.assertRaises(ProcessRequirementsExtractionError) as context:
            create_process_attribute_run(self.project, self.user)

        self.assertEqual(
            context.exception.code,
            "no_approved_process_assemblies",
        )
