from django.db.models import CharField, Value
from django.db.models.functions import Coalesce, Concat

from ecpmpapp.utils import require_any_permission_multiple_modules
from models.models import User


def get_root_action_item(action_item):
    current = action_item
    seen_ids = set()
    while current and current.parent_action_item_id and current.id not in seen_ids:
        seen_ids.add(current.id)
        current = current.parent_action_item
    return current or action_item


def get_project_for_action_item(action_item):
    root_action_item = get_root_action_item(action_item)
    milestone = getattr(root_action_item, "milestone", None)
    return getattr(milestone, "project", None)


def is_active_user(user):
    return (
        user
        and user.is_active
        and not user.is_deleted
        and not user.is_permanently_deleted
    )


def active_user_queryset():
    return User.objects.filter(
        is_active=True,
        is_deleted=False,
        is_permanently_deleted=False,
    )


def filter_users_by_full_name(queryset, search):
    search = str(search or "").strip()
    if not search:
        return queryset

    return queryset.annotate(
        full_name_search=Concat(
            Coalesce("first_name", Value("")),
            Value(" "),
            Coalesce("last_name", Value("")),
            output_field=CharField(),
        )
    ).filter(full_name_search__icontains=search)


def serialize_mention_user(user):
    return {
        "user_id": user.id,
        "name": (user.full_name or "").strip(),
        "user_image": user.user_image or "",
    }


def serialize_mention_candidate(user):
    return {
        "id": user.id,
        "name": (user.full_name or "").strip(),
        "user_image": user.user_image or "",
    }


def is_assigned_or_created(user, action_item):
    return (
        action_item.assignees.filter(id=user.id).exists()
        or getattr(action_item, "created_by_id", None) == user.id
    )


def has_action_item_view_access(user, action_item):
    if not is_active_user(user):
        return False

    root_action_item = get_root_action_item(action_item)
    project = get_project_for_action_item(action_item)

    if project:
        context = {"project": project}
        if (
            not getattr(user, "is_freelancer", False)
            and getattr(project, "company_profile_id", None)
            and getattr(user, "company_id", None) == project.company_profile_id
        ):
            if require_any_permission_multiple_modules(
                user,
                {"projects": ["full_access", "view_all_access"]},
                "company",
                context,
            ):
                return True
        if require_any_permission_multiple_modules(
            user,
            {"action_item": ["view", "full_access"]},
            "project",
            context,
        ):
            return True
        if is_assigned_or_created(user, action_item) and require_any_permission_multiple_modules(
            user,
            {"action_item": ["view_assigned_created"]},
            "project",
            context,
        ):
            return True
        return False

    company_profile = getattr(root_action_item, "company_profile", None)
    if company_profile:
        if getattr(user, "company_id", None) != company_profile.id:
            return False
        if require_any_permission_multiple_modules(
            user,
            {"action_item": ["view", "full_access"]},
            "company",
            {},
        ):
            return True
        if is_assigned_or_created(user, action_item) and require_any_permission_multiple_modules(
            user,
            {"action_item": ["view_assigned_created"]},
            "company",
            {},
        ):
            return True
        return False

    freelancer = getattr(root_action_item, "freelancer", None)
    return freelancer and freelancer.id == user.id


def get_action_item_mention_candidate_queryset(action_item):
    root_action_item = get_root_action_item(action_item)
    project = get_project_for_action_item(root_action_item)

    if project:
        team_member_ids = project.team_members.values_list("team_member_id", flat=True)
        return active_user_queryset().filter(id__in=team_member_ids)

    company_profile = getattr(root_action_item, "company_profile", None)
    if company_profile:
        return active_user_queryset().filter(company=company_profile)

    freelancer = getattr(root_action_item, "freelancer", None)
    if freelancer:
        return active_user_queryset().filter(id=freelancer.id)

    return User.objects.none()


def get_action_item_mention_candidates(action_item, search="", limit=20):
    candidates = filter_users_by_full_name(
        get_action_item_mention_candidate_queryset(action_item),
        search,
    ).order_by("first_name", "last_name", "id")

    mention_candidates = []
    for user in candidates:
        if not has_action_item_view_access(user, action_item):
            continue

        mention_candidates.append(serialize_mention_candidate(user))
        if len(mention_candidates) >= limit:
            break

    return mention_candidates


def get_action_item_mention_user_snapshots(action_item, user_ids):
    users = get_action_item_mention_candidate_queryset(action_item).filter(id__in=user_ids)

    snapshots = {}
    for user in users:
        if has_action_item_view_access(user, action_item):
            snapshots[user.id] = serialize_mention_user(user)

    return snapshots
