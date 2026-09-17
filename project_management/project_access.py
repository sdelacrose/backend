from django.db.models import Q

from project_management.models import Project


def accessible_projects(user):
    """Return the shared tenant-safe project scope for project-owned features."""
    if not user or not user.is_authenticated:
        return Project.objects.none()
    query = Q(user=user) | Q(team_members__team_member=user)
    if not user.is_freelancer and user.company_id:
        query |= Q(company_profile_id=user.company_id)
    return Project.objects.filter(query).distinct()
