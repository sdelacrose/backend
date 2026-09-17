from project_management.action_item_comment_mentions import (
    active_user_queryset,
    filter_users_by_full_name,
    is_active_user,
    serialize_mention_candidate,
    serialize_mention_user,
)
from project_management.models import MatrixComment
from project_management.permissions import (
    AvailableProductPartPermission,
    BillOfMaterialPermission,
    CompanySolutionSpacePermission,
    EquipmentPermission,
    MachinePermission,
    RequirementPermission,
    ProjectSolutionSpacePermission,
    SolutionDemandPermission,
    TransportPermission,
)
from models.models import User


def get_matrix_comment_target(comment_or_target, target_type=None):
    if target_type is None and isinstance(comment_or_target, MatrixComment):
        target_type = comment_or_target.target_type
        comment = comment_or_target
        if target_type == MatrixComment.TARGET_SOLUTION_DEMAND:
            return comment.solution_demand, target_type
        if target_type == MatrixComment.TARGET_REQUIREMENT:
            return comment.requirement, target_type
        if target_type == MatrixComment.TARGET_TRANSPORT:
            return comment.transport, target_type
        if target_type == MatrixComment.TARGET_AVAILABLE_PRODUCT_PART:
            return comment.available_product_part, target_type
        if target_type == MatrixComment.TARGET_MACHINE:
            return comment.machine, target_type
        if target_type == MatrixComment.TARGET_EQUIPMENT:
            return comment.equipment, target_type
        if target_type == MatrixComment.TARGET_BOM_PART:
            return comment.bom_part, target_type
        if target_type == MatrixComment.TARGET_COMPANY_SOLUTION_SPACE:
            return comment.company_solution_space_item, target_type
        if target_type == MatrixComment.TARGET_PROJECT_SOLUTION_SPACE:
            return comment.project_solution_space_item, target_type
        return None, target_type

    return comment_or_target, target_type


def get_project_for_matrix_target(target, target_type):
    if target_type in {
        MatrixComment.TARGET_SOLUTION_DEMAND,
        MatrixComment.TARGET_REQUIREMENT,
        MatrixComment.TARGET_TRANSPORT,
        MatrixComment.TARGET_BOM_PART,
        MatrixComment.TARGET_PROJECT_SOLUTION_SPACE,
    }:
        return getattr(target, "project", None)
    return None


def get_company_for_matrix_target(target, target_type):
    if target_type in {
        MatrixComment.TARGET_AVAILABLE_PRODUCT_PART,
        MatrixComment.TARGET_MACHINE,
        MatrixComment.TARGET_EQUIPMENT,
        MatrixComment.TARGET_COMPANY_SOLUTION_SPACE,
    }:
        return getattr(target, "company_profile", None)
    return None


def has_matrix_target_view_access(user, target, target_type):
    if not is_active_user(user) or target is None:
        return False

    if target_type == MatrixComment.TARGET_AVAILABLE_PRODUCT_PART:
        permission = AvailableProductPartPermission(action="view", scope="company")
        return permission._belongs_to_company(user, target) and permission._check_view_permission(
            user
        )

    if target_type == MatrixComment.TARGET_MACHINE:
        permission = MachinePermission(action="view", scope="company")
        return permission._belongs_to_company(user, target) and permission._check_view_permission(
            user
        )

    if target_type == MatrixComment.TARGET_EQUIPMENT:
        permission = EquipmentPermission(action="view", scope="company")
        return permission._belongs_to_company(user, target) and permission._check_view_permission(
            user
        )

    if target_type == MatrixComment.TARGET_BOM_PART:
        permission = BillOfMaterialPermission(action="view", scope="project")
        context = {"project": target.project}
        return permission._check_view_permission_project(user, target, context)

    if target_type == MatrixComment.TARGET_COMPANY_SOLUTION_SPACE:
        permission = CompanySolutionSpacePermission(action="view", scope="company")
        return permission._belongs_to_company(user, target) and permission._check_view_permission(
            user
        )

    if target_type == MatrixComment.TARGET_PROJECT_SOLUTION_SPACE:
        permission = ProjectSolutionSpacePermission(action="view", scope="project")
        context = {"project": target.project}
        return permission._check_view_permission_project(user, target, context)

    permission = {
        MatrixComment.TARGET_SOLUTION_DEMAND: SolutionDemandPermission(
            action="view", scope="project"
        ),
        MatrixComment.TARGET_REQUIREMENT: RequirementPermission(
            action="view", scope="project"
        ),
        MatrixComment.TARGET_TRANSPORT: TransportPermission(
            action="view", scope="project"
        ),
    }.get(target_type)
    if permission is None:
        return False

    context = {"project": target.project}
    return permission._check_view_permission_project(user, target, context)


def get_matrix_mention_candidate_queryset(target, target_type):
    project = get_project_for_matrix_target(target, target_type)
    if project:
        team_member_ids = project.team_members.values_list("team_member_id", flat=True)
        return active_user_queryset().filter(id__in=team_member_ids)

    company_profile = get_company_for_matrix_target(target, target_type)
    if company_profile:
        return active_user_queryset().filter(company=company_profile)

    return User.objects.none()


def get_matrix_mention_candidates(target, target_type, search="", limit=20):
    candidates = filter_users_by_full_name(
        get_matrix_mention_candidate_queryset(target, target_type),
        search,
    ).order_by("first_name", "last_name", "id")

    mention_candidates = []
    for user in candidates:
        if not has_matrix_target_view_access(user, target, target_type):
            continue

        mention_candidates.append(serialize_mention_candidate(user))
        if len(mention_candidates) >= limit:
            break

    return mention_candidates


def get_matrix_mention_user_snapshots(target, target_type, user_ids):
    users = get_matrix_mention_candidate_queryset(target, target_type).filter(
        id__in=user_ids
    )

    snapshots = {}
    for user in users:
        if has_matrix_target_view_access(user, target, target_type):
            snapshots[user.id] = serialize_mention_user(user)

    return snapshots
