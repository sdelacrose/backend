import logging

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import (
    Case,
    CharField,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Coalesce, Concat
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from project_management.currency_converter import ECBCurrencyConverter
from ecpmpapp.ordering import TableOrderingFilter
from project_management.filters import PurchaseOrderFilter
from project_management.models import (
    Invoice,
    Project,
    ProjectPhase,
    PurchaseOrder,
    PurchaseOrderLineItem,
)
from project_management.permissions import PurchaseOrderPermission
from project_management.purchase_order_access import (
    filter_purchase_orders_to_owner_scope,
)
from project_management.serializers.purchase_order import (
    PurchaseOrderSerializer,
    get_purchase_order_display_total_incl_vat,
)
from project_management.utils import (
    get_currency_from_record,
    get_product_name_from_request,
    get_user_currency,
    has_project_permission,
    symbol_to_currency_code,
)

logger = logging.getLogger(__name__)


class PurchaseOrderSearchFilter(filters.SearchFilter):
    """
    Default SearchFilter over reference / status / notes / project name, plus primary-key
    match when the term is all digits (Purchase Orders UI search is labeled as id search).
    """

    def filter_queryset(self, request, queryset, view):
        raw = (request.query_params.get(self.search_param) or "").strip()
        text_qs = super().filter_queryset(request, queryset, view)
        if raw.isdigit():
            return (
                queryset.filter(pk=int(raw))
                | queryset.filter(customer_reference=raw)
                | text_qs
            ).distinct()
        return text_qs


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrder.objects.all().order_by("-id")
    serializer_class = PurchaseOrderSerializer
    filter_backends = [
        DjangoFilterBackend,
        PurchaseOrderSearchFilter,
        TableOrderingFilter,
    ]
    filterset_class = PurchaseOrderFilter
    search_fields = [
        "reference",
        "customer_reference",
        "status",
        "notes",
        "project__project_name",
    ]
    ordering_fields = [
        "id",
        "reference",
        "customer_reference",
        "po_date",
        "status",
        "created_at",
        "updated_at",
        "project",
        "project__project_name",
        "amount_incl_vat",
        "amount_excl_vat",
        "vat_amount",
        "display_amount_incl_vat",
        "display_amount_excl_vat",
        "display_vat_amount",
        "revised_amount_incl_vat",
        "document_type",
        "po_type",
        "po_recipient_type",
        "po_recipient_name",
        "po_emitting_type",
        "po_emitting_company_name",
        "linked_payment_milestone_count",
        "line_item_count",
        "sum_milestones_ht",
        "next_payment_milestone",
        "linked_po_documents",
    ]
    ordering = ["-id"]
    ordering_text_fields = {
        "reference",
        "customer_reference",
        "status",
        "project__project_name",
        "po_type",
        "po_recipient_type",
        "po_recipient_name",
        "po_emitting_type",
        "po_emitting_company_name",
        "linked_po_documents",
    }
    ordering_natural_fields = {
        "reference",
        "customer_reference",
        "project__project_name",
        "po_recipient_name",
        "po_emitting_company_name",
        "linked_po_documents",
    }

    def get_permissions(self):
        action_permissions = {
            "list": "view",
            "retrieve": "view",
            "sum": "view",
            "create": "create",
            "update": "update",
            "partial_update": "update",
            "destroy": "delete",
        }
        permission_action = action_permissions.get(self.action)
        if permission_action:
            return [PurchaseOrderPermission(permission_action, "company")]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return PurchaseOrder.objects.none()

        line_items_qs = PurchaseOrderLineItem.objects.select_related(
            "available_product"
        )
        cn_depth2 = PurchaseOrder.objects.order_by("po_date", "id").prefetch_related(
            Prefetch("line_items", queryset=line_items_qs),
            Prefetch(
                "change_notices",
                queryset=PurchaseOrder.objects.order_by("po_date", "id").prefetch_related(
                    Prefetch("line_items", queryset=line_items_qs),
                ),
            ),
        )
        cn_depth1 = PurchaseOrder.objects.order_by("po_date", "id").prefetch_related(
            Prefetch("line_items", queryset=line_items_qs),
            Prefetch("change_notices", queryset=cn_depth2),
        )
        base = PurchaseOrder.objects.select_related(
            "receiving_user",
            "receiving_company",
            "emitting_user",
            "emitting_company",
        ).prefetch_related(
            "documents",
            "documents__author",
            "documents__folder",
            Prefetch(
                "invoices",
                queryset=Invoice.objects.order_by("-invoice_date", "-id"),
            ),
            Prefetch("change_notices", queryset=cn_depth1),
            Prefetch("line_items", queryset=line_items_qs),
        )

        if getattr(self, "action", None) in {"list", "retrieve", "sum"}:
            qs = PurchaseOrderPermission(
                action="view",
                scope="company",
            ).filter_queryset_for_view(base, user)
            if getattr(self, "action", None) == "list":
                qs = self._annotate_list_sort_fields(qs)
            return qs

        return filter_purchase_orders_to_owner_scope(base, user)

    def _annotate_list_sort_fields(self, queryset):
        """Annotate fields used by list ordering for milestone + linked-doc columns."""
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        money_field = DecimalField(max_digits=12, decimal_places=2)
        queryset = queryset.annotate(
            po_root_id=Coalesce(
                F("source_po__source_po__source_po_id"),
                F("source_po__source_po_id"),
                F("source_po_id"),
                F("id"),
                output_field=IntegerField(),
            )
        )
        previous_change_notice = (
            PurchaseOrder.objects.filter(
                Q(source_po_id=OuterRef("po_root_id"))
                | Q(source_po__source_po_id=OuterRef("po_root_id"))
                | Q(source_po__source_po__source_po_id=OuterRef("po_root_id")),
                document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
            )
            .filter(
                Q(po_date__lt=OuterRef("po_date"))
                | Q(po_date=OuterRef("po_date"), id__lt=OuterRef("id"))
            )
            .order_by("-po_date", "-id")
        )
        root_purchase_order = PurchaseOrder.objects.filter(
            pk=OuterRef("po_root_id")
        )
        previous_vat_amount = Coalesce(
            Subquery(previous_change_notice.values("vat_amount")[:1]),
            Subquery(root_purchase_order.values("vat_amount")[:1]),
            Value(0),
            output_field=money_field,
        )
        previous_amount_incl_vat = Coalesce(
            Subquery(previous_change_notice.values("amount_incl_vat")[:1]),
            Subquery(root_purchase_order.values("amount_incl_vat")[:1]),
            Value(0),
            output_field=money_field,
        )
        first_change_notice_reference = (
            PurchaseOrder.objects.filter(source_po_id=OuterRef("pk"))
            .order_by("po_date", "id")
            .values("reference")[:1]
        )
        linked_po_documents_sort = Coalesce(
            F("source_po__source_po__source_po__reference"),
            F("source_po__source_po__reference"),
            F("source_po__reference"),
            Subquery(first_change_notice_reference, output_field=CharField()),
            Value(""),
            output_field=CharField(),
        )
        # Tip change-notice amount (depth-limited) for outgoing root PO revised TTC sort.
        latest_change_notice_amount = Subquery(
            PurchaseOrder.objects.filter(
                Q(source_po_id=OuterRef("pk"))
                | Q(source_po__source_po_id=OuterRef("pk"))
                | Q(source_po__source_po__source_po_id=OuterRef("pk")),
                document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
            )
            .order_by("-po_date", "-id")
            .values("amount_incl_vat")[:1],
            output_field=FloatField(),
        )
        receiving_contact_name = Concat(
            Coalesce(F("receiving_user__name"), Value("")),
            Value(" "),
            Coalesce(F("receiving_user__surname"), Value("")),
            output_field=CharField(),
        )
        emitting_contact_name = Concat(
            Coalesce(F("emitting_user__name"), Value("")),
            Value(" "),
            Coalesce(F("emitting_user__surname"), Value("")),
            output_field=CharField(),
        )
        return queryset.annotate(
            display_amount_excl_vat=Case(
                When(
                    document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
                    then=Coalesce(
                        F("change_amount_excl_vat"),
                        F("amount_excl_vat"),
                        output_field=money_field,
                    ),
                ),
                default=F("amount_excl_vat"),
                output_field=money_field,
            ),
            display_vat_amount=Case(
                When(
                    document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
                    then=ExpressionWrapper(
                        F("vat_amount") - previous_vat_amount,
                        output_field=money_field,
                    ),
                ),
                default=F("vat_amount"),
                output_field=money_field,
            ),
            display_amount_incl_vat=Case(
                When(
                    document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
                    then=ExpressionWrapper(
                        F("amount_incl_vat") - previous_amount_incl_vat,
                        output_field=money_field,
                    ),
                ),
                default=F("amount_incl_vat"),
                output_field=money_field,
            ),
            sum_milestones_ht=Coalesce(
                Sum("linked_payment_project_phases__amount"),
                Value(0.0),
                output_field=FloatField(),
            ),
            next_payment_milestone=Coalesce(
                Min(
                    "linked_payment_project_phases__date",
                    filter=Q(linked_payment_project_phases__date__gte=today),
                ),
                Min("linked_payment_project_phases__date"),
            ),
            linked_po_documents=Case(
                When(
                    po_type=PurchaseOrder.POType.OUTGOING,
                    then=linked_po_documents_sort,
                ),
                default=Value(""),
                output_field=CharField(),
            ),
            linked_payment_milestone_count=Coalesce(
                Subquery(
                    ProjectPhase.objects.filter(purchase_order_id=OuterRef("pk"))
                    .order_by()
                    .values("purchase_order_id")
                    .annotate(c=Count("id"))
                    .values("c")[:1],
                    output_field=IntegerField(),
                ),
                Value(0),
            ),
            line_item_count=Coalesce(
                Subquery(
                    PurchaseOrderLineItem.objects.filter(
                        purchase_order_id=OuterRef("pk")
                    )
                    .order_by()
                    .values("purchase_order_id")
                    .annotate(c=Count("id"))
                    .values("c")[:1],
                    output_field=IntegerField(),
                ),
                Value(0),
            ),
            po_recipient_type=Case(
                When(receiving_user_id__isnull=False, then=Value("Contact")),
                When(receiving_company_id__isnull=False, then=Value("Company")),
                default=Value(""),
                output_field=CharField(),
            ),
            po_recipient_name=Case(
                When(
                    receiving_user_id__isnull=False,
                    then=receiving_contact_name,
                ),
                When(
                    receiving_company_id__isnull=False,
                    then=F("receiving_company__company_name"),
                ),
                default=Value(""),
                output_field=CharField(),
            ),
            po_emitting_type=Case(
                When(emitting_user_id__isnull=False, then=Value("Contact")),
                When(emitting_company_id__isnull=False, then=Value("Company")),
                default=Value(""),
                output_field=CharField(),
            ),
            po_emitting_company_name=Case(
                When(
                    emitting_user_id__isnull=False,
                    then=emitting_contact_name,
                ),
                When(
                    emitting_company_id__isnull=False,
                    then=F("emitting_company__company_name"),
                ),
                default=Value(""),
                output_field=CharField(),
            ),
            revised_amount_incl_vat=Case(
                When(
                    po_type=PurchaseOrder.POType.OUTGOING,
                    document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
                    then=Coalesce(
                        latest_change_notice_amount,
                        F("amount_incl_vat"),
                        output_field=FloatField(),
                    ),
                ),
                default=Value(None),
                output_field=FloatField(),
            ),
        )

    def create(self, request, *args, **kwargs):
        raw_project = request.data.get("project")
        project_id = None if raw_project in (None, "", "null") else raw_project

        # Company-level purchase orders
        if project_id is None:
            if request.data.get("po_type") != PurchaseOrder.POType.OUTGOING:
                return Response(
                    {"project": "This field is required for incoming purchase orders."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if getattr(request.user, "is_freelancer", False):
                return Response(
                    {
                        "detail": "Company-level purchase orders are not available for freelancer accounts.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            company = getattr(request.user, "company", None)
            if not company:
                return Response(
                    {"detail": "Unable to resolve company for this user."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            serializer = self.get_serializer(
                data=request.data,
                context={
                    "project": None,
                    "company_profile": company,
                    "request": request,
                },
            )
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

        project = get_object_or_404(Project, pk=project_id)
        if not has_project_permission(
            request.method, request.user, project, get_product_name_from_request(request)
        ):
            raise PermissionDenied(
                "You do not have permission to create a purchase order for this project."
            )

        serializer = self.get_serializer(
            data=request.data,
            context={"project": project, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_destroy(self, instance):
        if instance.change_notices.exists():
            raise ValidationError(
                "This purchase order cannot be deleted because it has linked change notices."
            )

        try:
            with transaction.atomic():
                instance.invoices.update(purchase_order=None)
                super().perform_destroy(instance)
        except ProtectedError as exc:
            raise ValidationError(
                "This purchase order cannot be deleted because it is linked to other records."
            ) from exc

    @action(detail=False, methods=["get"], url_path="sum")
    @swagger_auto_schema(
        operation_description=(
            "Sum purchase order amounts (incl. VAT, matching list display) "
            "converted to the company currency."
        ),
        responses={
            200: openapi.Response(
                description="Purchase order sum data",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        "total_amount": openapi.Schema(
                            type=openapi.TYPE_NUMBER,
                            description="Total amount incl. VAT in company currency",
                        ),
                        "company_currency": openapi.Schema(
                            type=openapi.TYPE_STRING,
                            description="Company currency code",
                        ),
                        "company_currency_symbol": openapi.Schema(
                            type=openapi.TYPE_STRING,
                            description="Company currency symbol",
                        ),
                        "purchase_order_count": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Number of purchase orders in the sum",
                        ),
                    },
                ),
            )
        },
    )
    def sum(self, request):
        try:
            user = request.user
            filtered_ids = self.filter_queryset(self.get_queryset()).values_list(
                "pk", flat=True
            )
            queryset = PurchaseOrder.objects.filter(pk__in=filtered_ids).select_related(
                "project",
                "company_profile",
                "freelancer__freelancer_profile",
            )
            purchase_order_count = queryset.count()

            company_currency_symbol = get_user_currency(user)
            company_currency_code = symbol_to_currency_code(company_currency_symbol)
            converter = ECBCurrencyConverter()
            total_amount = 0.0

            for po in queryset:
                amount = float(get_purchase_order_display_total_incl_vat(po))
                if amount == 0:
                    continue

                po_currency_symbol = po.currency or get_currency_from_record(po)
                po_currency_code = symbol_to_currency_code(po_currency_symbol)

                if po_currency_code.upper() == company_currency_code.upper():
                    total_amount += amount
                    continue

                converted_amount = converter.convert_currency(
                    amount, po_currency_code, company_currency_code
                )
                if converted_amount is None:
                    logger.warning(
                        "Could not convert %s %s to %s for purchase order %s",
                        amount,
                        po_currency_code,
                        company_currency_code,
                        po.id,
                    )
                    continue
                total_amount += converted_amount

            return Response(
                {
                    "total_amount": round(total_amount, 2),
                    "company_currency": company_currency_code,
                    "company_currency_symbol": company_currency_symbol,
                    "purchase_order_count": purchase_order_count,
                }
            )
        except Exception as exc:
            logger.error("Error calculating purchase order sum: %s", exc)
            return Response(
                {
                    "error": "Failed to calculate purchase order sum",
                    "detail": str(exc),
                },
                status=500,
            )
