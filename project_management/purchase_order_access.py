from django.db.models import Q, QuerySet

from project_management.models import PurchaseOrder


PURCHASE_ORDER_PERMISSION_MODULE = "purchase_order"


def purchase_order_owner_scope_filter(user):
    """Return the PO owner scope available to the user's account."""
    if not user or not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])

    if getattr(user, "is_freelancer", False):
        return (
            Q(freelancer=user)
            | Q(project__user=user)
            | Q(project__team_members__team_member=user)
        )

    company_id = getattr(user, "company_id", None)
    if not company_id:
        return Q(pk__in=[])

    return (
        Q(project__isnull=True, company_profile_id=company_id)
        | Q(project__company_profile_id=company_id)
        | Q(
            project__company_profile__isnull=True,
            project__user__company_id=company_id,
        )
    )


def purchase_order_created_visibility_filter(user):
    """Include own rows and notices linked to a root PO created by the user."""
    if not user or not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])

    return Q(created_by=user) | Q(
        document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
        source_po__created_by=user,
    )


def purchase_order_project_visibility_filter(user):
    """Include project POs only when the user is an explicit team member."""
    if not user or not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])

    return Q(
        project__isnull=False,
        project__team_members__team_member=user,
    )


def filter_purchase_orders_to_owner_scope(queryset: QuerySet, user):
    return queryset.filter(purchase_order_owner_scope_filter(user)).distinct()


def filter_purchase_orders_to_created_visibility(queryset: QuerySet, user):
    owner_scoped_queryset = filter_purchase_orders_to_owner_scope(
        queryset,
        user,
    )
    return owner_scoped_queryset.filter(
        purchase_order_created_visibility_filter(user)
    ).distinct()


def filter_purchase_orders_to_project_visibility(queryset: QuerySet, user):
    owner_scoped_queryset = filter_purchase_orders_to_owner_scope(
        queryset,
        user,
    )
    return owner_scoped_queryset.filter(
        purchase_order_project_visibility_filter(user)
    ).distinct()


def filter_purchase_orders_to_created_or_project_visibility(
    queryset: QuerySet,
    user,
):
    owner_scoped_queryset = filter_purchase_orders_to_owner_scope(
        queryset,
        user,
    )
    return owner_scoped_queryset.filter(
        purchase_order_created_visibility_filter(user)
        | purchase_order_project_visibility_filter(user)
    ).distinct()


def is_purchase_order_in_owner_scope(purchase_order, user):
    if not purchase_order or not getattr(purchase_order, "pk", None):
        return False

    return filter_purchase_orders_to_owner_scope(
        PurchaseOrder.objects.filter(pk=purchase_order.pk),
        user,
    ).exists()


def is_purchase_order_created_visible(purchase_order, user):
    if not purchase_order or not getattr(purchase_order, "pk", None):
        return False

    return filter_purchase_orders_to_created_visibility(
        PurchaseOrder.objects.filter(pk=purchase_order.pk),
        user,
    ).exists()


def is_purchase_order_project_visible(purchase_order, user):
    if not purchase_order or not getattr(purchase_order, "pk", None):
        return False

    return filter_purchase_orders_to_project_visibility(
        PurchaseOrder.objects.filter(pk=purchase_order.pk),
        user,
    ).exists()
