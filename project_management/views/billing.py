from rest_framework import viewsets, filters
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.decorators import method_decorator
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
import logging
from django.db.models import Case, When, Value, CharField, F, FloatField, IntegerField, OuterRef, Subquery, Sum
from django.db.models.functions import Cast, Coalesce, Concat

from project_management.models import (
    Billing,
    Document,
    LineItem,
    Invoice,
    InvoiceLineItem,
    Project,
    MonthlyTimesheetSubmission,
    PurchaseOrder,
)
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from project_management.serializers import (
    BillingSerializer,
    LineItemSerializer,
    InvoiceSerializer,
)
from project_management.serializers.billing import (
    ApprovedMonthlyTimesheetInvoiceSummarySerializer,
    get_monthly_timesheet_invoiceable_documents,
)
from project_management.serializers.time_expense import _build_monthly_timesheet_preview
from project_management.utils import (
    create_pdf_invoice_by_bill,
    create_invoice_export_package,
    INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
    get_product_name_from_request,
    has_project_permission,
    get_currency_from_record,
    get_user_currency,
)
from project_management.filters import BillingInvoiceFilter
from ecpmpapp.constants import UserType
from project_management.currency_converter import ECBCurrencyConverter
from project_management.utils import symbol_to_currency_code
from project_management.permissions import InvoicePermission, PurchaseOrderPermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.ordering import TableOrderingFilter

logger = logging.getLogger(__name__)

class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend, TableOrderingFilter]
    filterset_class = BillingInvoiceFilter
    search_fields = ['reciepient_user__name', 'reciepient_user__surname', 'reciepient_company__company_name',
                     'status', 'invoice_date', 'due_date', 'grand_total', 'id', 'reference_number'
                     ]
    ordering_fields = [
        "id",
        "freelancer",
        "freelancer__email",
        "project",
        "project__project_name",
        "company_profile",
        "company_profile__company_name",
        "recipient",
        "invoice_date",
        "due_date",
        "company_currency_amount",
        "grand_total",
        "net_total_after_credit_notes",
        "invoice_against",
        "recipient_type",
        "status",
        "reference_number",
        "document_type",
        "purchase_order__reference",
        "linked_documents",
    ]
    ordering = ["-id"]
    ordering_text_fields = {
        "reference_number",
        "document_type",
        "recipient",
        "recipient_type",
        "project__project_name",
        "purchase_order__reference",
        "linked_documents",
        "status",
    }
    ordering_natural_fields = ordering_text_fields



    def get_permissions(self):
        if self.action == 'list':
            return [InvoicePermission('view', 'company')]
        elif self.action == 'retrieve':
            return [InvoicePermission('view', 'company')]
        elif self.action == 'create':
            return [InvoicePermission('create', 'company')]
        elif self.action in ['update', 'partial_update']:
            return [InvoicePermission('update', 'company')]
        elif self.action == 'destroy':
            return [InvoicePermission('delete', 'company')]
        elif self.action in ['approved_timesheet_summaries', 'export_package']:
            return [InvoicePermission('view', 'company')]
        return super().get_permissions()
    
    # def get_queryset(self):
    #     user = self.request.user
    #     if isinstance(user, AnonymousUser):
    #         return Invoice.objects.none()
    #     product_name = get_product_name_from_request(self.request)
    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]
    #     if not user.is_freelancer and user.groups.filter(name=UserType.ADMIN.value).exists():
    #         query_filters = (
    #             Q(project__isnull=False, project__solution__in=products) & 
    #             (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))
    #         )
    #     else:
    #         query_filters = Q(project__isnull=False, project__team_members__team_member=user, project__solution__in=products)

    #     if user.is_freelancer:
    #         query_filters |= Q(freelancer__isnull=False, freelancer=user)
    #     else:
    #         query_filters |= Q(company_profile__isnull=False, company_profile=user.company)

    #     return Invoice.objects.filter(query_filters).distinct()

    def get_queryset(self):
        """
        Filter queryset based on user permissions.
        This method should be called from the view's get_queryset method.
        """
        from django.db.models import Q
        from project_management.models import Invoice, Project
        from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
        from project_management.utils import get_product_name_from_request
        from ecpmpapp.utils import require_any_permission_multiple_modules
        
        user = self.request.user
        request = self.request
        
        if user.is_anonymous:
            return Invoice.objects.none()
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]
        
        filters = Q()
        
        # Check company-level permissions
        has_invoice_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'invoice': ['view', 'full_access']}, 
            'company', 
            request.query_params
        )
        
        has_invoice_view_created_permission = require_any_permission_multiple_modules(
            user, 
            {'invoice': ['view_created']}, 
            'company', 
            request.query_params
        )
        
        has_projects_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['full_access', 'view_all_access']}, 
            'company', 
            request.query_params
        )
        
        # Check project-level permissions
        has_project_permissions = require_any_permission_multiple_modules(
            user, 
            {'invoice': ['view', 'full_access']}, 
            'project', 
            request.query_params
        )
        
        # Company-level invoices (for users with invoice permissions at company level)
        if not user.is_freelancer and has_invoice_company_permissions:
            filters |= Q(company_profile=user.company)
        
        # Users with view_created permission can see invoices they created
        if has_invoice_view_created_permission:
            filters |= Q(created_by=user)
        
        # Project-level invoices (for users with project permissions)
        if has_project_permissions:
            # Get projects where user has project-level permissions
            user_projects = Project.objects.filter(
                team_members__team_member=user,
                solution__in=products
            )
            if user_projects.exists():
                filters |= Q(project__in=user_projects)
        
        # Company users with projects permissions (can see all company projects)
        if not user.is_freelancer and has_projects_company_permissions:
            company_project_filters = (
                Q(project__solution__in=products) & 
                Q(project__user__company=user.company)
            )
            filters |= company_project_filters
        
        # Freelancer logic
        if user.is_freelancer:
            # Freelancers get their own invoices
            freelancer_filters = Q(freelancer=user)
            filters |= freelancer_filters
            
            # Freelancers get project invoices where they have project-level permissions
            if has_project_permissions:
                freelancer_projects = Project.objects.filter(
                    team_members__team_member=user,
                    solution__in=products
                )
                if freelancer_projects.exists():
                    filters |= Q(project__in=freelancer_projects)
        
        # If no permissions found, return empty queryset
        if not filters:
            return Invoice.objects.none()
        
        queryset = Invoice.objects.filter(filters).select_related(
            'project',
            'company_profile',
            'freelancer',
            'reciepient_user',
            'reciepient_company',
            'source_invoice',
            'source_invoice__replacement_invoice',
            'replaced_invoice',
            'replaced_invoice__source_invoice',
            'replacement_invoice',
            'purchase_order',
        ).prefetch_related(
            'credit_notes',
            'replaced_invoice__credit_notes',
            'documents',
            'line_items',
        ).distinct().order_by('-id')

        credit_notes_total_subquery = (
            Invoice.objects.filter(source_invoice_id=OuterRef("pk"))
            .values("source_invoice_id")
            .annotate(total=Sum("grand_total"))
            .values("total")[:1]
        )

        queryset = queryset.annotate(
            recipient_type=Case(
                When(reciepient_user__isnull=False, then=Value('User')),
                When(reciepient_company__isnull=False, then=Value('Company')),
                default=Value('unknown'),
                output_field=CharField()
            ),
            recipient=Case(
                When(
                    reciepient_user__isnull=False,
                    then=Concat(F('reciepient_user__name'), Value(' '), F('reciepient_user__surname'))
                ),
                When(
                    reciepient_company__isnull=False,
                    then=F('reciepient_company__company_name')
                ),
                default=Value(''),
                output_field=CharField()
            ),
            credit_notes_total=Coalesce(
                Subquery(credit_notes_total_subquery, output_field=FloatField()),
                Value(0.0),
                output_field=FloatField(),
            ),
            net_total_after_credit_notes=Case(
                When(
                    document_type=Invoice.DocumentType.INVOICE,
                    then=Coalesce(F("grand_total"), Value(0.0), output_field=FloatField())
                    + F("credit_notes_total"),
                ),
                # Credit notes display "-" in this column, so their ordering
                # value must also be missing instead of their hidden amount.
                default=Value(None),
                output_field=FloatField(),
            ),
        ).annotate(
            # Matches UI Deduction Status: credit note → deducted,
            # invoice with credit notes → partially credited, else not credited.
            invoice_against=Case(
                When(
                    document_type=Invoice.DocumentType.CREDIT_NOTE,
                    then=Value(0),
                ),
                When(credit_notes_total__lt=0, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            ),
            # First visible linked invoice/credit-note reference for list ordering.
            linked_documents=Coalesce(
                Case(
                    When(
                        document_type=Invoice.DocumentType.CREDIT_NOTE,
                        then=Coalesce(
                            F("source_invoice__reference_number"),
                            Cast(F("source_invoice_id"), CharField()),
                        ),
                    ),
                    When(
                        replaced_invoice_id__isnull=False,
                        then=Coalesce(
                            F("replaced_invoice__reference_number"),
                            Cast(F("replaced_invoice_id"), CharField()),
                        ),
                    ),
                    default=Coalesce(
                        Subquery(
                            Invoice.objects.filter(source_invoice_id=OuterRef("pk"))
                            .order_by("invoice_date", "id")
                            .values("reference_number")[:1]
                        ),
                        F("replacement_invoice__reference_number"),
                        Cast(F("replacement_invoice__id"), CharField()),
                        Value(""),
                    ),
                    output_field=CharField(),
                ),
                Value(""),
                output_field=CharField(),
            ),
        )
        return queryset

    def perform_destroy(self, instance):
        if (
            instance.document_type == Invoice.DocumentType.INVOICE
            and instance.replaced_invoice_id
        ):
            raise ValidationError(
                "Replacement invoices that are part of a credit note adjustment chain cannot be deleted."
            )

        if (
            instance.document_type == Invoice.DocumentType.INVOICE
            and (
                instance.credit_notes.exists()
                or Invoice.objects.filter(replaced_invoice=instance).exists()
            )
        ):
            raise ValidationError(
                "This invoice cannot be deleted because it has linked credit notes or a replacement invoice."
            )

        if (
            instance.document_type == Invoice.DocumentType.CREDIT_NOTE
            and instance.source_invoice_id
            and Invoice.objects.filter(replaced_invoice=instance.source_invoice).exists()
        ):
            raise ValidationError(
                "Credit notes that already generated a replacement invoice cannot be deleted."
            )

        source_invoice = instance.source_invoice if instance.document_type == Invoice.DocumentType.CREDIT_NOTE else None
        purchase_order_id = instance.purchase_order_id
        super().perform_destroy(instance)
        if purchase_order_id:
            from project_management.purchase_order_invoice_status import (
                sync_purchase_order_status_from_linked_invoices,
            )

            sync_purchase_order_status_from_linked_invoices(purchase_order_id)
        if source_invoice:
            create_pdf_invoice_by_bill(source_invoice, self.request.user)
    

    def get_object(self):
        obj = get_object_or_404(Invoice, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        request_user = self.request.user

        if obj.company_profile:
            if not (obj.company_profile == request_user.company):
                raise PermissionDenied("You do not have permission to access this Invoice's company profile.")

        elif obj.freelancer:
            if not (obj.freelancer == request_user):
                raise PermissionDenied("You do not have permission to access this Invoice.")

        return obj

    @action(detail=False, methods=['get'], url_path='sum')
    @swagger_auto_schema(
        operation_description="Get the sum of all invoices converted to the company's currency",
        responses={
            200: openapi.Response(
                description="Invoice sum data",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'total_amount': openapi.Schema(type=openapi.TYPE_NUMBER, description='Total amount in company currency'),
                        'company_currency': openapi.Schema(type=openapi.TYPE_STRING, description='Company currency code'),
                        'company_currency_symbol': openapi.Schema(type=openapi.TYPE_STRING, description='Company currency symbol'),
                        'invoice_count': openapi.Schema(type=openapi.TYPE_INTEGER, description='Number of invoices'),
                        'currency_breakdown': openapi.Schema(
                            type=openapi.TYPE_OBJECT,
                            description='Breakdown by original currency'
                        ),
                        'conversion_rates': openapi.Schema(
                            type=openapi.TYPE_OBJECT,
                            description='Exchange rates used for conversion'
                        )
                    }
                )
            )
        }
    )
    def sum(self, request):
        """
        Calculate the sum of all invoices converted to the company's currency.
        Uses European Central Bank API for currency conversion rates.
        """
        try:
            user = request.user
            
            # Get the filtered queryset (same as get_queryset)
            queryset = self.filter_queryset(self.get_queryset())

            # Apply additional status filtering supporting:
            # - status=open
            # - status=open,in_progress
            # - status=-closed
            # - status=-closed,cancelled
            status_value = request.query_params.get('status')
            if status_value:
                is_negative = status_value.startswith('-')
                raw_value = status_value[1:] if is_negative else status_value
                criteria = [c.strip() for c in raw_value.split(',') if c.strip()]

                if criteria:
                    if is_negative:
                        queryset = queryset.exclude(status__in=criteria)
                    else:
                        queryset = queryset.filter(status__in=criteria)
            
            # Determine company currency symbol and convert to code
            company_currency_symbol = get_user_currency(user)
            
            # Convert symbol to currency code for ECB API
            company_currency_code = symbol_to_currency_code(company_currency_symbol)
            
            # Initialize currency converter
            converter = ECBCurrencyConverter()
            
            # Process invoices and group by currency
            currency_breakdown = {}
            conversion_rates = {}
            total_amount = 0.0
            
            for invoice in queryset:
                if invoice.company_currency_amount is not None:
                    total_amount += invoice.company_currency_amount
                    continue
                
                # Get invoice currency symbol
                invoice_currency_symbol = get_currency_from_record(invoice)
                
                # Convert symbol to currency code
                invoice_currency_code = symbol_to_currency_code(invoice_currency_symbol)
                
                # Get invoice amount
                amount = invoice.grand_total or 0.0
                if amount == 0:
                    continue
                
                # Initialize currency breakdown (use symbol for display)
                if invoice_currency_symbol not in currency_breakdown:
                    currency_breakdown[invoice_currency_symbol] = 0.0
                
                currency_breakdown[invoice_currency_symbol] += amount
                
                # Convert to company currency
                if invoice_currency_code.upper() == company_currency_code.upper():
                    converted_amount = amount
                else:
                    # Get conversion rate
                    rate = converter.get_conversion_rate(invoice_currency_code, company_currency_code)
                    if rate is None:
                        logger.warning(f"Could not get conversion rate for {invoice_currency_code} to {company_currency_code}")
                        # Skip this invoice if conversion fails
                        continue
                    
                    converted_amount = converter.convert_currency(amount, invoice_currency_code, company_currency_code)
                    if converted_amount is None:
                        logger.warning(f"Could not convert {amount} {invoice_currency_code} to {company_currency_code}")
                        continue
                    
                    # Store conversion rate for reference (use symbols for display)
                    conversion_rates[f"{invoice_currency_symbol} to {company_currency_symbol}"] = rate
                
                total_amount += converted_amount
            
            # Round total to 2 decimal places
            total_amount = round(total_amount, 2)

            return Response({
                'total_amount': total_amount,
                'company_currency': company_currency_code,
                'company_currency_symbol': company_currency_symbol,
                'invoice_count': queryset.count(),
                'currency_breakdown': currency_breakdown,
                'conversion_rates': conversion_rates,
            })
            
        except Exception as e:
            logger.error(f"Error calculating invoice sum: {str(e)}")
            return Response({
                'error': 'Failed to calculate invoice sum',
                'detail': str(e)
            }, status=500)

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                name="due_date_after",
                in_=openapi.IN_QUERY,
                description="Filter invoices with due date after this date (YYYY-MM-DD)",
                type=openapi.TYPE_STRING,
                format=openapi.FORMAT_DATE
            ),
            openapi.Parameter(
                name="due_date_before",
                in_=openapi.IN_QUERY,
                description="Filter invoices with due date before this date (YYYY-MM-DD)",
                type=openapi.TYPE_STRING,
                format=openapi.FORMAT_DATE
            ),
            openapi.Parameter(
                name="invoice_date_after",
                in_=openapi.IN_QUERY,
                description="Filter invoices with invoice date after this date (YYYY-MM-DD)",
                type=openapi.TYPE_STRING,
                format=openapi.FORMAT_DATE
            ),
            openapi.Parameter(
                name="invoice_date_before",
                in_=openapi.IN_QUERY,
                description="Filter invoices with invoice date before this date (YYYY-MM-DD)",
                type=openapi.TYPE_STRING,
                format=openapi.FORMAT_DATE
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @action(detail=False, methods=['get'], url_path='approved-timesheet-summaries')
    def approved_timesheet_summaries(self, request):
        user = request.user
        project_id = request.query_params.get("project")
        company_profile_id = request.query_params.get("company_profile")
        freelancer_id = request.query_params.get("freelancer")
        purchase_order_id = (
            request.query_params.get("purchase_order")
            or request.query_params.get("purchase_order_id")
            or request.query_params.get("po")
        )
        collaborator_values = (
            request.query_params.getlist("collaborators")
            or request.query_params.getlist("collaborator")
        )
        month = request.query_params.get("month")
        year = request.query_params.get("year")

        def parse_int_param(value, field_name):
            if value in (None, ""):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ValidationError({field_name: "Must be a valid integer."})

        def parse_int_list(values, field_name):
            parsed_values = []
            for raw_value in values:
                for value in str(raw_value).split(","):
                    value = value.strip()
                    if not value:
                        continue
                    parsed_values.append(parse_int_param(value, field_name))
            return parsed_values

        purchase_order_id = parse_int_param(purchase_order_id, "purchase_order")
        collaborator_ids = parse_int_list(collaborator_values, "collaborators")

        provided_scope_count = sum(
            1 for value in [project_id, company_profile_id, freelancer_id] if value
        )
        if provided_scope_count > 1:
            raise ValidationError(
                "Provide only one billing scope: project, company_profile, or freelancer."
            )

        scope_type = "monthly_timesheet"
        scope_id = None
        scope_name = ""
        project = None
        purchase_order = None
        target_company_id = None

        if project_id:
            project_id = int(project_id)
            project = get_object_or_404(Project, pk=project_id)
            if not has_project_permission(
                request.method,
                user,
                project,
                get_product_name_from_request(request),
            ):
                raise PermissionDenied(
                    "You do not have permission to access this project's approved monthly timesheets."
                )
            scope_id = project.id
            scope_name = project.project_name
            if project.user.is_freelancer:
                freelancer_id = project.user_id
            else:
                target_company_id = project.user.company_id
        elif freelancer_id:
            freelancer_id = int(freelancer_id)
            if freelancer_id != user.id:
                raise PermissionDenied(
                    "You do not have permission to access this freelancer's approved monthly timesheets."
                )
            scope_id = freelancer_id
            scope_name = user.full_name
        else:
            target_company_id = (
                int(company_profile_id) if company_profile_id else user.company_id
            )
            if not target_company_id or user.company_id != target_company_id:
                raise PermissionDenied(
                    "You do not have permission to access this company's approved monthly timesheets."
                )
            scope_id = target_company_id
            scope_name = user.company.company_name

        if purchase_order_id:
            visible_purchase_orders = PurchaseOrderPermission(
                action="view",
                scope="company",
            ).filter_queryset_for_view(
                PurchaseOrder.objects.select_related("project", "project__user"),
                user,
            )
            purchase_order = get_object_or_404(
                visible_purchase_orders,
                pk=purchase_order_id,
            )
            if not purchase_order.project_id:
                raise ValidationError(
                    {"purchase_order": "Purchase order is not linked to a project."}
            )
            if project_id and purchase_order.project_id != project_id:
                raise ValidationError(
                    {
                        "purchase_order": (
                            "Purchase order does not belong to the selected project."
                        )
                    }
                )
            if freelancer_id and purchase_order.project.user_id != freelancer_id:
                raise ValidationError(
                    {
                        "purchase_order": (
                            "Purchase order does not belong to this freelancer scope."
                        )
                    }
                )
            if (
                target_company_id
                and not purchase_order.project.user.is_freelancer
                and purchase_order.project.user.company_id != target_company_id
            ):
                raise ValidationError(
                    {
                        "purchase_order": (
                            "Purchase order does not belong to this company scope."
                        )
                    }
                )
            if not has_project_permission(
                request.method,
                user,
                purchase_order.project,
                get_product_name_from_request(request),
            ):
                raise PermissionDenied(
                    "You do not have permission to access this purchase order's approved monthly timesheets."
                )

        submissions = MonthlyTimesheetSubmission.objects.filter(
            status="approved",
        ).select_related("user", "user__staff", "user__company")

        if freelancer_id:
            submissions = submissions.filter(user_id=freelancer_id)
        else:
            submissions = submissions.filter(user__company_id=target_company_id)
        if collaborator_ids:
            submissions = submissions.filter(user_id__in=collaborator_ids)

        if month:
            submissions = submissions.filter(month=int(month))
        if year:
            submissions = submissions.filter(year=int(year))

        submissions = submissions.order_by(
            "-year",
            "-month",
            "user__first_name",
            "user__last_name",
        )

        submission_ids = list(submissions.values_list("id", flat=True))
        invoiced_submission_line_items = InvoiceLineItem.objects.filter(
            monthly_timesheet_submission_id__in=submission_ids,
        ).exclude(
            invoice__status="not paid"
        ).exclude(
            invoice__document_type=Invoice.DocumentType.CREDIT_NOTE
        )
        if project_id:
            invoiced_submission_line_items = invoiced_submission_line_items.filter(
                invoice__project_id=project_id,
            )
        elif freelancer_id:
            invoiced_submission_line_items = invoiced_submission_line_items.filter(
                invoice__freelancer_id=freelancer_id,
            )
        else:
            invoiced_submission_line_items = invoiced_submission_line_items.filter(
                invoice__company_profile_id=target_company_id,
            )
        if purchase_order_id:
            invoiced_submission_line_items = invoiced_submission_line_items.filter(
                invoice__purchase_order_id=purchase_order_id,
            )
        invoiced_submission_ids = set(
            invoiced_submission_line_items.values_list(
                "monthly_timesheet_submission_id",
                flat=True,
            )
        )

        payload = []
        for submission in submissions:
            if submission.id in invoiced_submission_ids:
                continue

            first_name = submission.user.first_name or ""
            last_name = submission.user.last_name or ""
            full_name = f"{first_name} {last_name}".strip() or submission.user.email
            preview = _build_monthly_timesheet_preview(
                submission.user,
                submission.month,
                submission.year,
            )

            if project_id or purchase_order_id:
                scoped_activities = []
                for week in preview.get("weeks", []):
                    for activity in week.get("activities", []):
                        if project_id:
                            activity_project = activity.get("project") or {}
                            if activity_project.get("id") != project_id:
                                continue
                        if purchase_order_id:
                            activity_purchase_order = (
                                activity.get("purchase_order") or {}
                            )
                            if activity_purchase_order.get("id") != purchase_order_id:
                                continue
                        if not activity.get("is_invoiceable", True):
                            continue
                        scoped_activities.append(activity)

                total_hours = round(
                    sum(
                        float(activity.get("row_total_hours") or 0)
                        for activity in scoped_activities
                    ),
                    2,
                )
                invoiceable_hours = round(
                    sum(
                        float(activity.get("row_total_hours") or 0)
                        for activity in scoped_activities
                    ),
                    2,
                )
                if invoiceable_hours <= 0:
                    continue
                linked_documents_map = {}
                for activity in scoped_activities:
                    for document in activity.get("linked_documents", []):
                        linked_documents_map[int(document["id"])] = document
                linked_document_ids = list(linked_documents_map.keys())
                linked_documents = list(
                    Document.objects.filter(id__in=linked_document_ids)
                )
            else:
                non_invoiceable_hours = 0.0
                for week in preview.get("weeks", []):
                    for activity in week.get("activities", []):
                        if activity.get("is_invoiceable", True):
                            continue
                        non_invoiceable_hours += float(
                            activity.get("row_total_hours") or 0
                        )

                total_hours = round(float(preview["month_total_hours"] or 0), 2)
                invoiceable_hours = round(
                    max(total_hours - non_invoiceable_hours, 0),
                    2,
                )
                linked_documents = get_monthly_timesheet_invoiceable_documents(
                    submission
                )
            description = f"Monthly timesheet {submission.month:02d}/{submission.year} - {full_name}"
            if purchase_order:
                description = f"{description} - {purchase_order.reference}"
            payload.append(
                {
                    "submission_id": submission.id,
                    "month": submission.month,
                    "year": submission.year,
                    "total_hours": total_hours,
                    "invoiceable_hours": invoiceable_hours,
                    "description": description,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "scope_name": preview.get("scope_summary") or scope_name,
                    "hourly_rate": (
                        submission.user.staff.hourly_rate
                        if hasattr(submission.user, "staff") and submission.user.staff
                        else None
                    ),
                    "user": {
                        "id": submission.user_id,
                        "name": full_name,
                        "email": submission.user.email,
                    },
                    "linked_documents": linked_documents,
                }
            )

        serializer = ApprovedMonthlyTimesheetInvoiceSummarySerializer(
            payload,
            many=True,
        )
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='export-package')
    def export_package(self, request, pk=None):
        invoice = self.get_object()
        include_mode = request.data.get(
            "include_mode",
            INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
        )

        try:
            package_document, _ = create_invoice_export_package(
                invoice,
                include_mode=include_mode,
                request_user=request.user,
            )
        except ValueError as exc:
            raise ValidationError(str(exc))

        return Response(
            {
                "document_id": package_document.id,
                "file_name": package_document.file_name
                or package_document.file.name.split("/")[-1],
                "file_url": package_document.file.url,
                "include_mode": include_mode,
            }
        )

class BillingViewSet(viewsets.ModelViewSet):
    queryset = Billing.objects.all().order_by('id')
    serializer_class = BillingSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    filterset_fields = ['project']
    search_fields = ['contact__name', 'contact__surname']

    def get_serializer(self, *args, **kwargs):
        serializer = super(BillingViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        return serializer

    def get_queryset(self):
        user = self.request.user
        if isinstance(self.request.user, AnonymousUser):
            return Billing.objects.none()
        return Billing.objects.filter(Q(project__user=user) | Q(project__team_members__team_member=user)).distinct().order_by('-id')

    def get_object(self):
        obj = get_object_or_404(Billing, pk=self.kwargs['pk'])
        if not has_project_permission(self.request.method, self.request.user, obj.project, get_product_name_from_request(self.request)):
            raise PermissionDenied("You do not have permission to access this billing record.")
        return obj


class LineItemViewSet(viewsets.ModelViewSet):
    queryset = LineItem.objects.all()
    serializer_class = LineItemSerializer

    def get_queryset(self):
        user = self.request.user
        if isinstance(self.request.user, AnonymousUser):
            return LineItem.objects.none()
        return LineItem.objects.filter(Q(bill__project__user=user) | Q(bill__project__team_members__team_member=user)).distinct().order_by('-id')

    def get_object(self):
        obj = get_object_or_404(LineItem, pk=self.kwargs['pk'])
        if not has_project_permission(self.request.method, self.request.user, obj.bill.project, get_product_name_from_request(self.request)):
            raise PermissionDenied("You do not have permission to access this line item.")
        return obj
