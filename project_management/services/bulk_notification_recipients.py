from ecpmpapp.constants import UserType
from models.models import User
from project_management.models import ProjectTeamMembers


def get_project_bulk_notification_recipients(project):
    recipients = {project.user, project.created_by}
    if project.company_profile_id:
        recipients.update(
            User.objects.filter(
                company_id=project.company_profile_id,
                groups__name=UserType.ADMIN.value,
                is_active=True,
            ).distinct()
        )
    recipients.update(
        User.objects.filter(
            id__in=ProjectTeamMembers.objects.filter(
                project=project
            ).values_list("team_member_id", flat=True),
            is_active=True,
        )
    )
    return {recipient for recipient in recipients if recipient}
