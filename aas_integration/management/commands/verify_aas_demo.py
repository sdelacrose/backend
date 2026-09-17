import json

from django.core.management.base import BaseCommand, CommandError

from project_management.models import BillOfMaterialPart, Project

from aas_integration.domain import ProjectBOMAASMapper
from aas_integration.adapters import create_aasx, read_aasx
from aas_integration.services import get_or_create_project_identity


class Command(BaseCommand):
    help = "Verify the deterministic AAS demo mapper against the ORM BOM."

    def handle(self, *args, **options):
        del args, options
        project = Project.objects.filter(project_name="AAS Demo Electric Motor Assembly").first()
        if not project:
            raise CommandError("Run seed_aas_demo first.")
        identity = get_or_create_project_identity(project)
        document = ProjectBOMAASMapper().map(project, identity.logical_aas_id)
        orm_rows = list(BillOfMaterialPart.objects.filter(project=project).order_by("level", "sort_order", "id"))
        entry_node = document.submodels[0]["submodelElements"][0]
        aas_rows = list(self._entities(entry_node.get("statements", [])))
        if len(orm_rows) != len(aas_rows):
            raise CommandError(f"ORM/AAS row mismatch: {len(orm_rows)} != {len(aas_rows)}")
        names = [next(item["value"] for item in row["statements"] if item["idShort"] == "PartName") for row in aas_rows]
        expected = [row.name for row in orm_rows]
        if names != expected:
            raise CommandError("ORM/AAS names or ordering differ.")
        round_trip = read_aasx(create_aasx(document.as_environment()))
        if len(round_trip) != 3:
            raise CommandError("The AASX round trip did not preserve the shell, BOM and project metadata submodels.")
        self.stdout.write(self.style.SUCCESS(json.dumps({"project_id": project.id, "rows": len(orm_rows), "source_hash": document.hash})))

    def _entities(self, statements):
        for statement in statements:
            if statement.get("modelType") != "Entity":
                continue
            yield statement
            yield from self._entities(statement.get("statements", []))
