from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from rest_framework.exceptions import PermissionDenied

from ecpmpapp.utils import require_any_permission
from project_management.utils import get_product_name_from_request, has_project_permission


PROJECT_CONTENT_MODULE = "project_content"
ACCEPTANCE_TESTS_MODULE = "acceptance_tests"
ACTION_ITEM_MODULE = "action_item"


def require_project_content_access(
    request,
    project,
    *,
    module,
    write=False,
    approve=False,
    action=None,
):
    is_write = write or approve or action not in {None, "view"}
    method = "PUT" if is_write else "GET"
    try:
        has_project_permission(
            method,
            request.user,
            project,
            get_product_name_from_request(request),
        )
    except DjangoPermissionDenied as exc:
        raise PermissionDenied(str(exc)) from exc

    if action:
        actions = [action, "full_access"]
    elif not write:
        actions = ["view", "full_access"]
    elif approve:
        actions = ["approve", "full_access"]
    else:
        actions = ["update", "full_access"]
    if not require_any_permission(
        request.user,
        module,
        actions,
        "project",
        {"project": project},
    ):
        raise PermissionDenied()


def require_template_access(request, *, write=False):
    if not request.user.company_id:
        raise PermissionDenied()
    actions = ["full_access", "manage"] if write else ["view", "full_access", "manage"]
    if not require_any_permission(
        request.user,
        PROJECT_CONTENT_MODULE,
        actions,
        "company",
        {},
    ):
        raise PermissionDenied()


def require_acceptance_generation_access(request, project):
    """Authorize the replacement workflow under ordinary Action Items."""
    try:
        has_project_permission(
            "PUT",
            request.user,
            project,
            get_product_name_from_request(request),
        )
    except DjangoPermissionDenied as exc:
        raise PermissionDenied(str(exc)) from exc

    context = {"project": project}
    if not require_any_permission(
        request.user,
        ACTION_ITEM_MODULE,
        ["create", "full_access"],
        "project",
        context,
    ):
        raise PermissionDenied("Action Item creation permission is required.")
    if not require_any_permission(
        request.user,
        ACTION_ITEM_MODULE,
        ["generate_acceptance_tests", "full_access"],
        "project",
        context,
    ):
        raise PermissionDenied("Acceptance generation permission is required.")
    if not require_any_permission(
        request.user,
        "phases_management",
        ["view", "full_access"],
        "project",
        context,
    ):
        raise PermissionDenied("Phase visibility is required.")
