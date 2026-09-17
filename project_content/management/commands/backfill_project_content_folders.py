from django.core.management.base import BaseCommand

from project_management.models import Project

from project_content.folders import ensure_project_content_folders, normalized_locale


class Command(BaseCommand):
    help = "Create the protected Proposal and Requirements Specification folders."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--project-id", type=int)
        parser.add_argument("--locale", default="en")

    def handle(self, *args, **options):
        locale = normalized_locale(options["locale"])
        projects = Project.objects.order_by("id")
        if options.get("project_id"):
            projects = projects.filter(pk=options["project_id"])
        dry_run = not options["apply"]
        created = 0
        for project in projects.iterator():
            rows = ensure_project_content_folders(project, locale, dry_run=dry_run)
            for row in rows:
                if row["created"]:
                    created += 1
                    prefix = "Would create" if dry_run else "Created"
                    self.stdout.write(
                        f"{prefix} {row['name']} for project {project.id}"
                    )
        mode = "dry-run" if dry_run else "apply"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {created} folders"))
