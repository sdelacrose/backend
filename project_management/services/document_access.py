from django.db.models import Q

from ecpmpapp.utils import require_any_permission_multiple_modules
from project_management.models import Document, Project
from project_management.models.constants import (
    COLLABORATIVE_SOLUTION,
    SPECIAL_SOLUTION,
)
from project_management.utils import get_product_name_from_request


def get_accessible_document_queryset(request, *, project_id=None):
    """Return only documents visible to the current user and product context."""
    user = request.user
    if user.is_anonymous:
        return Document.objects.none()

    if project_id is None:
        project_id = request.query_params.get("folder__project")

    product_name = get_product_name_from_request(request)
    if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
        products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
    else:
        products = [product_name]

    permission_context = {"project": project_id}
    has_company_documents = require_any_permission_multiple_modules(
        user,
        {"documents": ["view", "full_access"]},
        "company",
        permission_context,
    )
    has_company_projects = require_any_permission_multiple_modules(
        user,
        {"projects": ["full_access", "view_all_access"]},
        "company",
        permission_context,
    )
    has_project_documents = require_any_permission_multiple_modules(
        user,
        {
            "project": ["view", "full_access"],
            "documents": ["view", "full_access"],
        },
        "project",
        permission_context,
    )

    limited_permissions = [
        ("company", {"documents": ["view_created", "update_created", "delete_created"]}),
        ("project", {"documents": ["view_created", "update_created", "delete_created"]}),
        (
            "company",
            {
                "documents": [
                    "view_assigned_created",
                    "update_assigned_created",
                    "delete_assigned_created",
                ]
            },
        ),
        (
            "project",
            {
                "documents": [
                    "view_assigned_created",
                    "update_assigned_created",
                    "delete_assigned_created",
                ]
            },
        ),
    ]
    has_limited_documents = any(
        require_any_permission_multiple_modules(
            user,
            modules,
            scope,
            permission_context,
        )
        for scope, modules in limited_permissions
    )

    filters = Q()
    if not user.is_freelancer and has_company_documents:
        filters |= Q(folder__company_profile=user.company)

    member_projects = Project.objects.filter(
        team_members__team_member=user,
        solution__in=products,
    )
    if has_project_documents:
        filters |= Q(folder__project__in=member_projects)

    if not user.is_freelancer and has_company_projects:
        filters |= Q(
            folder__project__solution__in=products,
            folder__project__user__company=user.company,
        )

    if user.is_freelancer:
        filters |= Q(folder__freelancer=user)

    if has_limited_documents:
        filters |= Q(created_by=user) | Q(author=user) | Q(assignee=user)

    if not filters:
        return Document.objects.none()
    return (
        Document.objects.filter(filters)
        .select_related("search_index")
        .distinct()
        .order_by("-id")
    )
