from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from ecpmpapp.constants import UserType

class Command(BaseCommand):
    help = 'Add "Project Manager" to team_members_roles in existing ECPMPConfig objects'

    def handle(self, *args, **kwargs):
        updated_count = 0
        # Get all ECPMPConfig objects
        configs = ECPMPConfig.objects.all()

        for config in configs:
            # Convert to lowercase for case-insensitive comparison
            existing_roles = [role.lower() for role in config.team_members_roles]
            if UserType.PROJECT_MANAGER.value not in existing_roles:
                # Append "Project Manager" to the team_members_roles list
                config.team_members_roles.append(UserType.PROJECT_MANAGER.value)
                config.save()  # Save the updated object
                updated_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Successfully added "Project Manager" role to {updated_count} ECPMPConfig objects'
        ))