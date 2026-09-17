from django.core.management.base import BaseCommand
from project_management.models import Project, ProjectTeamMembers

class Command(BaseCommand):
    help = "Add the project owner as a team member with 'Project Manager' role and 'RW' access if not already present."

    def handle(self, *args, **kwargs):
        projects_updated = 0

        # Iterate through all projects
        for project in Project.objects.all():
            project_owner = project.user  # Assuming 'user' is the project owner field
            
            manager_tm, _ = ProjectTeamMembers.objects.get_or_create(
                project=project,
                team_member=project_owner,
                defaults={'role': 'project manager', 'project_access': "RW"}
            )
            projects_updated += 1
            self.stdout.write(self.style.SUCCESS(
                f"Added project owner {project_owner.full_name} as team member to project {project.project_name} with 'Project Manager' role and 'RW' access."
            ))

        # Output results
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully updated {projects_updated} projects by adding project owner as team member."
            )
        )