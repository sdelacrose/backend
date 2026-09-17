from django.core.management.base import BaseCommand
from django.db import transaction

from project_management.models import Folder, Document, Project

class Command(BaseCommand):
    help = 'Create root and default subfolders for all existing projects'

    def handle(self, *args, **kwargs):
        default_folders = [
            "site_reports"
        ]

        projects = Project.objects.all()

        if not projects.exists():
            print(self.style.WARNING("No projects found."))
            return

        for project in projects:
            with transaction.atomic():
                root_folder, created = Folder.objects.get_or_create(
                    project=project,
                    name=f"{project.id}_root_folder",
                    parent=None,  # Root folder has no parent
                )

                # Create default subfolders
                for folder_name in default_folders:
                    Folder.objects.get_or_create(
                        project=project,
                        name=folder_name,
                        parent=root_folder,
                    )
                    print(self.style.SUCCESS(f"Created folder: {folder_name} under project: {project.id}"))

        print(self.style.SUCCESS("All folders created successfully."))
