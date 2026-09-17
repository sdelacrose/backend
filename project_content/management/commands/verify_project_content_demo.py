import hashlib
from io import BytesIO

from docx import Document as DocxDocument

from django.core.management.base import BaseCommand, CommandError

from companyprofileapis.models import Opportunity
from project_management.models import (
    Document,
    Project,
    Requirement,
)

from project_content.demo_sources import EDGE_SOURCE_FIXTURES
from project_content.folders import SYSTEM_FOLDER_NAMES
from project_content.models import (
    AcceptanceChecklist,
    AcceptanceCriterion,
    CompanyProjectDocumentTemplate,
    GeneratedProjectDocumentRevision,
)
from project_content.snapshots import build_project_content_snapshot, canonical_hash


class Command(BaseCommand):
    help = "Verify deterministic project-content demo data and generated artifacts."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", type=int, required=True)
        parser.add_argument("--minimum-requirements", type=int, default=24)
        parser.add_argument("--minimum-criteria", type=int, default=12)
        parser.add_argument("--require-edge-documents", action="store_true")

    def handle(self, *args, **options):
        project = Project.objects.select_related("company_profile").filter(
            pk=options["project_id"]
        ).first()
        if not project:
            raise CommandError("Project does not exist.")

        failures = []
        system_keys = set(
            project.folders.exclude(system_key=None).values_list("system_key", flat=True)
        )
        if not set(SYSTEM_FOLDER_NAMES).issubset(system_keys):
            failures.append("protected project-content folders are missing")

        source_documents = Document.objects.filter(
            folder__project=project,
            folder__name="Project Content AI Demo Sources",
        )
        if source_documents.count() != 4:
            failures.append("expected four deterministic source documents")
        if source_documents.exclude(search_index__status="searchable").exists():
            failures.append("all demo documents must be searchable")

        if options["require_edge_documents"]:
            self._verify_edge_documents(project, failures)

        requirement_count = Requirement.objects.filter(
            project=project,
            object_number__startswith="REQ-DEMO-",
        ).count()
        if requirement_count < options["minimum_requirements"]:
            failures.append(
                f"only {requirement_count} deterministic requirements are available"
            )
        if Opportunity.objects.filter(
            projects=project,
            title__startswith="Project Content Demo - ",
        ).count() != 2:
            failures.append("expected complete and incomplete commercial opportunities")

        for phase in AcceptanceChecklist.Phase.values:
            count = AcceptanceCriterion.objects.filter(
                checklist__project=project,
                checklist__phase=phase,
                stable_key__startswith="demo-criterion-",
            ).count()
            if count < options["minimum_criteria"]:
                failures.append(f"{phase} has only {count} demo criteria")

        first_pair = AcceptanceCriterion.objects.filter(
            checklist__project=project,
            stable_key="demo-criterion-0001",
        )
        if first_pair.count() != 2 or len(set(first_pair.values_list("pair_key", flat=True))) != 1:
            failures.append("the first FAT/CAT criterion is not traceably paired")
        if first_pair.exclude(manual_fields__contains=["acceptance_criteria"]).exists():
            failures.append("manual-edit preservation fixture is missing")

        if project.company_profile_id:
            active_templates = CompanyProjectDocumentTemplate.objects.filter(
                company=project.company_profile,
                is_active=True,
            ).count()
            if active_templates != 6:
                failures.append("expected six active kind/locale templates")

        first_snapshot, first_hash = build_project_content_snapshot(project)
        second_snapshot, second_hash = build_project_content_snapshot(project)
        if first_hash != second_hash or first_hash != canonical_hash(first_snapshot):
            failures.append("project source snapshot is not deterministic")
        if first_snapshot != second_snapshot:
            failures.append("repeated source snapshots differ")
        if len(first_snapshot.get("process_stations", [])) < 24:
            failures.append("process station context is missing from the snapshot")
        if len(first_snapshot.get("opportunities", [])) != 2:
            failures.append("linked opportunity context is missing from the snapshot")

        for revision in GeneratedProjectDocumentRevision.objects.filter(
            generated_document__project=project
        ).select_related("docx_document", "pdf_document"):
            for document, expected_hash in (
                (revision.docx_document, revision.docx_hash),
                (revision.pdf_document, revision.pdf_hash),
            ):
                document.file.open("rb")
                try:
                    actual_hash = hashlib.sha256(document.file.read()).hexdigest()
                finally:
                    document.file.close()
                if actual_hash != expected_hash:
                    failures.append(f"artifact checksum mismatch for revision {revision.id}")

        if failures:
            raise CommandError("; ".join(failures))
        self.stdout.write(
            self.style.SUCCESS(
                f"Project {project.id} demo verified: sources, requirements, FAT/CAT pairs, "
                "opportunities, templates, snapshots, and available revision artifacts are consistent."
            )
        )

    def _verify_edge_documents(self, project, failures):
        edge_documents = Document.objects.filter(
            folder__project=project,
            folder__name="Project Content AI Edge Sources",
        ).select_related("search_index")
        expected_names = {fixture.file_name for fixture in EDGE_SOURCE_FIXTURES}
        actual_names = set(edge_documents.values_list("file_name", flat=True))
        if actual_names != expected_names:
            failures.append("the mixed-quality edge document pack is incomplete")
            return
        if edge_documents.filter(file="").exists():
            failures.append("one or more edge documents have no stored DOCX file")
        if edge_documents.exclude(search_index__status="searchable").exists():
            failures.append("all edge documents must be searchable")

        documents_without_segments = edge_documents.filter(
            content_segments__isnull=True
        ).values_list("file_name", flat=True)
        if documents_without_segments:
            failures.append(
                "edge documents are missing indexed content: "
                + ", ".join(documents_without_segments)
            )

        for document in edge_documents:
            document.file.open("rb")
            try:
                DocxDocument(BytesIO(document.file.read()))
            except Exception:
                failures.append(f"invalid DOCX source fixture: {document.file_name}")
            finally:
                document.file.close()

        snapshot, _ = build_project_content_snapshot(
            project,
            document_ids=list(edge_documents.values_list("id", flat=True)),
        )
        source_text = "\n".join(row["text"] for row in snapshot["sources"])
        for expected_text in (
            "EDGE-HIER-005",
            "EUR 1",
            "0.5 seconds",
            "500 ms",
            "EDGE-BULK-20-10",
            "no product, process, commercial, or acceptance requirement",
        ):
            if expected_text not in source_text:
                failures.append(
                    f"edge snapshot is missing expected content: {expected_text}"
                )
