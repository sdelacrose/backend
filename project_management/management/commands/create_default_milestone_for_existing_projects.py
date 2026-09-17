from django.core.management.base import BaseCommand
from project_management.models import Milestone, Project

class Command(BaseCommand):
    help = "Creates a milestone for all existing projects if an exact one does not exist"

    def handle(self, *args, **kwargs):
        projects = Project.objects.all()
        count_created = 0

        for project in projects:
            # Check if a milestone with the same name, dates, and color already exists
            if not Milestone.objects.filter(
                project=project,
                name="Default Milestone",
                start_date=project.project_start_date,
                due_date=project.project_end_date,
                colour=project.project_colour
            ).exists():
                try:
                    Milestone.objects.create(
                        project=project,
                        name="Default Milestone",
                        start_date=project.project_start_date,
                        due_date=project.project_end_date,
                        reminder_setting="none",  # Set the reminder as specified
                        colour=project.project_colour,  # Set the milestone colour
                        status="open",
                    )
                    count_created += 1
                    self.stdout.write(self.style.SUCCESS(f"Created Milestone for Project: {project.project_name}"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Unable to create milestone for project {project.project_name}"))
        if count_created == 0:
            self.stdout.write(self.style.WARNING("No new milestones were created. All projects already have a matching milestone."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Successfully created {count_created} milestones."))
