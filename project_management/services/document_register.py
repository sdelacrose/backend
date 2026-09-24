from django.db import transaction
from django.db.models import CharField, F, Func, Q, Value
from django.db.models.functions import Coalesce, Greatest, Lower, NullIf
from django.http import Http404
from rest_framework.exceptions import PermissionDenied

from models.models import User
from project_management.models import Document, DocumentControl
from project_management.permissions import DocumentPermission
from project_management.services.document_access import (
    get_accessible_document_queryset,
)

DEFAULT_REGISTER_ORDERING = "-updated"

REGISTER_ORDERING_EXPRESSIONS = {
    "reference": lambda: Lower("register_reference"),
    "title": lambda: Lower("register_title"),
    "project": lambda: Lower("register_project_name"),
    "revision": lambda: Lower("control__revision"),
    "status": lambda: F("control__status"),
    "updated": lambda: F("register_updated_at"),
}


def parse_int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_register_queryset(request, *, project_id=None):
    """Documents visible to the user in Documents, annotated for the register.

    The scope is exactly the one of the existing Documents module: the register
    never widens it.
    """
    queryset = get_accessible_document_queryset(request, project_id=project_id)
    file_basename = Func(
        F("file"),
        Value(".*/"),
        Value(""),
        function="REGEXP_REPLACE",
        output_field=CharField(),
    )
    queryset = queryset.select_related(
        "folder",
        "folder__project",
        "folder__company_profile",
        "control",
        "control__owner",
        "control__approver",
    ).annotate(
        register_title=Coalesce(
            NullIf(F("file_name"), Value("")),
            file_basename,
            output_field=CharField(),
        ),
        register_reference=Coalesce(
            NullIf(F("control__reference"), Value("")),
            NullIf(F("full_document_number"), Value("")),
            output_field=CharField(),
        ),
        register_updated_at=Greatest(
            F("updated_at"),
            Coalesce(F("control__updated_at"), F("updated_at")),
        ),
        register_project_name=F("folder__project__project_name"),
    )
    if project_id is not None:
        queryset = queryset.filter(folder__project_id=project_id)
    return queryset


def apply_register_filters(queryset, params):
    search = (params.get("q") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(register_reference__icontains=search)
            | Q(register_title__icontains=search)
        )

    for param, field in (
        ("document_type", "control__document_type"),
        ("discipline", "control__discipline"),
        ("status", "control__status"),
    ):
        value = (params.get(param) or "").strip()
        if value:
            queryset = queryset.filter(**{field: value})

    owner_id = parse_int_or_none(params.get("owner"))
    if owner_id is not None:
        queryset = queryset.filter(control__owner_id=owner_id)
    return queryset


def apply_register_ordering(queryset, ordering):
    ordering = (ordering or DEFAULT_REGISTER_ORDERING).strip()
    descending = ordering.startswith("-")
    key = ordering.lstrip("-")
    if key not in REGISTER_ORDERING_EXPRESSIONS:
        descending = DEFAULT_REGISTER_ORDERING.startswith("-")
        key = DEFAULT_REGISTER_ORDERING.lstrip("-")
    expression = REGISTER_ORDERING_EXPRESSIONS[key]()
    expression = (
        expression.desc(nulls_last=True)
        if descending
        else expression.asc(nulls_last=True)
    )
    return queryset.order_by(expression, "-id")


def get_register_document_or_404(request, *, document_id=None, control_id=None):
    """Resolve a register row starting from the accessible Documents queryset.

    A DocumentControl is never fetched on its own: the underlying Document must
    be accessible first, otherwise the result is a 404.
    """
    lookup = {"pk": document_id} if document_id is not None else {
        "control__pk": control_id
    }
    # Only used as permission context (project membership), never returned.
    project_id = (
        Document.objects.filter(**lookup)
        .values_list("folder__project_id", flat=True)
        .first()
    )
    document = get_register_queryset(request, project_id=project_id).filter(
        **lookup
    ).first()
    if document is None:
        raise Http404
    return document


def can_update_document(user, document):
    """Same write permission as updating the Document in Documents."""
    return DocumentPermission(action="update", scope="mixed").can_update_object(
        user, document
    )


def assert_can_update_document(user, document):
    if not can_update_document(user, document):
        raise PermissionDenied(
            "You do not have permission to update this Document."
        )


def get_allowed_owner_queryset(user):
    """Users that can be selected as Owner: only the current user's company."""
    if user.is_freelancer or not user.company_id:
        return User.objects.filter(pk=user.pk)
    return User.objects.filter(
        company_id=user.company_id,
        is_active=True,
        is_deleted=False,
        is_permanently_deleted=False,
    )


CONTROL_FIELDS = ("reference", "document_type", "discipline", "revision", "status")


@transaction.atomic
def save_document_control(document, validated_data):
    """Create the DocumentControl on first save, otherwise update it."""
    control, _created = DocumentControl.objects.get_or_create(document=document)
    for field in CONTROL_FIELDS:
        if field in validated_data:
            setattr(control, field, validated_data[field])
    if "owner" in validated_data:
        control.owner = validated_data["owner"]
    if "approver" in validated_data:
        control.approver = validated_data["approver"]
    control.save()
    return control
