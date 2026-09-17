from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from project_management.models.constants import COLLABORATIVE_SOLUTION
from rest_framework import serializers
from django.conf import settings
from ecpmpapp.constants import StatusType, UserType
from project_management.models import (
    Billing,
    LineItem,
    Folder,
    Document,
    Invoice,
    InvoiceLineItem,
    InvoiceReferenceCounter,
    TimeCard,
    MonthlyTimesheetSubmission,
    PurchaseOrder,
    TimesheetActivity,
)
from project_management.serializers import ContactSerialier, DocumentSerializer
from .time_expense import (
    _build_monthly_timesheet_preview,
    get_project_owner_valid_choices,
    get_valid_choices,
)
from project_management.serializers.purchase_order import get_po_revised_amount_incl_vat
from project_management.purchase_order_invoice_status import (
    sync_purchase_order_status_from_linked_invoices,
)
from project_management.invoice_status import get_automatic_invoice_status
from project_management.permissions import PurchaseOrderPermission
from project_management.utils import (
    create_pdf_invoice_by_bill,
    get_billing_document_label,
    get_invoice_reference_display,
    get_product_name_from_request,
    has_project_permission,
    get_root_folder_name_from_bill,
    get_user_currency,
)
from project_management.currency_converter import ECBCurrencyConverter
from project_management.utils import get_currency_from_record, symbol_to_currency_code
from models.models import Connection, ConnectionUser, User

INVOICE_PO_LINKED_TOTAL_EXCEEDS_PO_AMOUNT = (
    "The total amount of invoices linked to this PO exceeds the PO amount. "
    "Please adjust the invoice amount accordingly."
)

import logging

logger = logging.getLogger(__name__)


def get_monthly_timesheet_invoiceable_document_ids(submission):
    preview = _build_monthly_timesheet_preview(
        submission.user,
        submission.month,
        submission.year,
    )
    document_ids = []
    seen_document_ids = set()

    for week in preview.get("weeks", []):
        for activity in week.get("activities", []):
            if not activity.get("is_invoiceable", True):
                continue

            for document in activity.get("linked_documents", []):
                document_id = int(document["id"])
                if document_id in seen_document_ids:
                    continue
                seen_document_ids.add(document_id)
                document_ids.append(document_id)

    return document_ids


def get_monthly_timesheet_invoiceable_documents(submission):
    document_ids = get_monthly_timesheet_invoiceable_document_ids(submission)
    if not document_ids:
        return []

    documents_by_id = {
        document.id: document
        for document in Document.objects.filter(id__in=document_ids).select_related(
            "author",
            "folder",
            "folder__project",
            "folder__company_profile",
            "folder__freelancer",
        )
    }
    return [
        documents_by_id[document_id]
        for document_id in document_ids
        if document_id in documents_by_id
    ]


def get_invoice_auto_document_ids_from_line_items(line_items_data):
    auto_document_ids = []
    seen_document_ids = set()

    for item_data in line_items_data:
        monthly_timesheet_submission = item_data.get(
            "monthly_timesheet_submission"
        )
        if not monthly_timesheet_submission:
            continue

        for document_id in get_monthly_timesheet_invoiceable_document_ids(
            monthly_timesheet_submission
        ):
            if document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            auto_document_ids.append(document_id)

    return auto_document_ids


def get_effective_invoice_document_ids(invoice, *, line_items_data=None):
    auto_document_ids = (
        get_invoice_auto_document_ids_from_line_items(line_items_data)
        if line_items_data is not None
        else get_invoice_auto_document_ids_from_line_items(
            [
                {"monthly_timesheet_submission": item.monthly_timesheet_submission}
                for item in invoice.line_items.all()
                if item.monthly_timesheet_submission_id
            ]
        )
    )
    excluded_auto_document_ids = set(
        invoice.excluded_auto_documents.values_list("id", flat=True)
    )
    stored_document_ids = list(invoice.documents.values_list("id", flat=True))
    final_document_ids = list(stored_document_ids)
    seen_document_ids = set(final_document_ids)

    for document_id in auto_document_ids:
        if document_id in excluded_auto_document_ids or document_id in seen_document_ids:
            continue
        seen_document_ids.add(document_id)
        final_document_ids.append(document_id)

    return final_document_ids


def get_effective_invoice_documents(invoice, *, line_items_data=None):
    document_ids = get_effective_invoice_document_ids(
        invoice,
        line_items_data=line_items_data,
    )
    if not document_ids:
        return []

    documents_by_id = {
        document.id: document
        for document in Document.objects.filter(id__in=document_ids)
    }
    return [
        documents_by_id[document_id]
        for document_id in document_ids
        if document_id in documents_by_id
    ]


class LineItemSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    
    class Meta:
        model = LineItem
        fields = ['id', 'description', 'rate', 'quantity']

    def to_representation(self, instance):
        # Get the default representation
        representation = super().to_representation(instance)
        representation["total"] = instance.rate * instance.quantity
        return representation

class InvoiceLineItemSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    
    class Meta:
        model = InvoiceLineItem
        fields = [
            'id',
            'time_card',
            'expense_card',
            'monthly_timesheet_submission',
            'description',
            'vat_amount',
            'price',
            'quantity',
        ]

    def validate(self, data):
        time_card = data.get('time_card')
        expense_card = data.get('expense_card')
        monthly_timesheet_submission = data.get('monthly_timesheet_submission')
        description = data.get('description')

        linked_sources = [
            source for source in [time_card, expense_card, monthly_timesheet_submission] if source
        ]
        if len(linked_sources) > 1:
            raise serializers.ValidationError(
                "LineItem can only be associated with one source at a time."
            )
        if not linked_sources and not description:
            raise serializers.ValidationError(
                "LineItem must be associated with a TimeCard, ExpenseCard, Monthly Timesheet, or description."
            )

        return data


class ApprovedMonthlyTimesheetInvoiceSummarySerializer(serializers.Serializer):
    submission_id = serializers.IntegerField()
    month = serializers.IntegerField()
    year = serializers.IntegerField()
    total_hours = serializers.FloatField()
    invoiceable_hours = serializers.FloatField()
    description = serializers.CharField()
    scope_type = serializers.CharField()
    scope_id = serializers.IntegerField(allow_null=True)
    scope_name = serializers.CharField()
    hourly_rate = serializers.FloatField(allow_null=True)
    user = serializers.DictField()
    linked_documents = DocumentSerializer(many=True, read_only=True)


class InvoiceSerializer(serializers.ModelSerializer):
    invoice_date = serializers.DateField(required=False)
    due_date = serializers.DateField(required=False)
    status = serializers.CharField(required=False)
    line_items = InvoiceLineItemSerializer(many=True, required=False)
    source_invoice_id = serializers.PrimaryKeyRelatedField(
        source='source_invoice',
        queryset=Invoice.objects.all(),
        write_only=True,
        required=False,
        allow_null=True,
    )
    source_invoice = serializers.SerializerMethodField(read_only=True)
    replaced_invoice = serializers.SerializerMethodField(read_only=True)
    replacement_invoice = serializers.SerializerMethodField(read_only=True)
    linked_documents = serializers.SerializerMethodField(read_only=True)
    linked_credit_notes = serializers.SerializerMethodField(read_only=True)
    total_credit_notes_amount = serializers.SerializerMethodField(read_only=True)
    net_total_after_credit_notes = serializers.SerializerMethodField(read_only=True)
    purchase_order = serializers.PrimaryKeyRelatedField(
        queryset=PurchaseOrder.objects.all(),
        required=False,
        allow_null=True,
    )
    collaborators = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        many=True,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        permission = PurchaseOrderPermission(action="view", scope="company")
        self._purchase_order_view_scope = permission.get_view_scope(user)
        self.fields["purchase_order"].queryset = (
            permission.filter_queryset_for_view(
                PurchaseOrder.objects.all(),
                user,
                view_scope=self._purchase_order_view_scope,
            )
        )

    class Meta:
        model = Invoice
        fields = [
            'id', 'freelancer', 'project', 'company_profile', 'reciepient_user',
            'reciepient_company', 'invoice_date', 'due_date', 'status', 'documents',
            'grand_total', 'notes', 'line_items', 'created_at', 'updated_at',
            'vat_rate', 'company_currency_amount', 'conversion_rate', 'created_by',
            'document_type', 'reference_number', 'source_invoice', 'source_invoice_id',
            'replaced_invoice', 'replacement_invoice', 'linked_documents',
            'carried_forward_amount',
            'linked_credit_notes', 'total_credit_notes_amount', 'net_total_after_credit_notes',
            'purchase_order',
            'collaborators',
        ]
        read_only_fields = [
            'created_at',
            'updated_at',
            'created_by',
            'reference_number',
            'source_invoice',
            'replaced_invoice',
            'replacement_invoice',
            'linked_documents',
        ]

    def _serialize_invoice_reference(self, invoice, relation_type):
        if not invoice:
            return None

        return {
            "id": invoice.id,
            "reference_number": get_invoice_reference_display(invoice),
            "document_type": invoice.document_type,
            "status": invoice.status,
            "grand_total": invoice.grand_total,
            "invoice_date": invoice.invoice_date,
            "relation_type": relation_type,
        }

    def _get_replacement_invoice_instance(self, invoice):
        try:
            return invoice.replacement_invoice
        except Invoice.DoesNotExist:
            return None

    def _append_unique_linked_document(self, linked_documents, candidate, relation_type, seen_ids):
        if not candidate or candidate.id in seen_ids:
            return

        serialized = self._serialize_invoice_reference(candidate, relation_type)
        if not serialized:
            return

        linked_documents.append(serialized)
        seen_ids.add(candidate.id)

    def get_source_invoice(self, instance):
        if not instance.source_invoice_id:
            return None

        return {
            "id": instance.source_invoice.id,
            "reference_number": get_invoice_reference_display(instance.source_invoice),
            "status": instance.source_invoice.status,
            "grand_total": instance.source_invoice.grand_total,
            "total_credit_notes_amount": self._get_credit_notes_total(instance.source_invoice),
            "net_total_after_credit_notes": self._quantize_currency(
                (instance.source_invoice.grand_total or 0) + self._get_credit_notes_total(instance.source_invoice)
            ),
        }

    def get_replaced_invoice(self, instance):
        return self._serialize_invoice_reference(
            instance.replaced_invoice,
            "original_invoice",
        )

    def get_replacement_invoice(self, instance):
        replacement_invoice = self._get_replacement_invoice_instance(instance)
        return self._serialize_invoice_reference(
            replacement_invoice,
            "replacement_invoice",
        )

    def get_linked_credit_notes(self, instance):
        if instance.document_type != Invoice.DocumentType.INVOICE:
            return []

        return [
            {
                "id": credit_note.id,
                "reference_number": get_invoice_reference_display(credit_note),
                "status": credit_note.status,
                "grand_total": credit_note.grand_total,
                "invoice_date": credit_note.invoice_date,
            }
            for credit_note in instance.credit_notes.all().order_by('invoice_date', 'id')
        ]

    def get_total_credit_notes_amount(self, instance):
        if instance.document_type != Invoice.DocumentType.INVOICE:
            return 0
        return self._get_credit_notes_total(instance)

    def get_net_total_after_credit_notes(self, instance):
        if instance.document_type != Invoice.DocumentType.INVOICE:
            return instance.grand_total
        return round((instance.grand_total or 0) + self._get_credit_notes_total(instance), 2)

    def get_linked_documents(self, instance):
        linked_documents = []
        seen_ids = set()

        if instance.document_type == Invoice.DocumentType.CREDIT_NOTE:
            source_invoice = instance.source_invoice
            replacement_invoice = (
                self._get_replacement_invoice_instance(source_invoice)
                if source_invoice
                else None
            )
            if source_invoice:
                self._append_unique_linked_document(
                    linked_documents,
                    source_invoice,
                    "original_invoice",
                    seen_ids,
                )
            if replacement_invoice:
                self._append_unique_linked_document(
                    linked_documents,
                    replacement_invoice,
                    "replacement_invoice",
                    seen_ids,
                )
            return [item for item in linked_documents if item]

        if instance.replaced_invoice_id:
            self._append_unique_linked_document(
                linked_documents,
                instance.replaced_invoice,
                "original_invoice",
                seen_ids,
            )
            for credit_note in instance.replaced_invoice.credit_notes.all().order_by(
                "invoice_date",
                "id",
            ):
                self._append_unique_linked_document(
                    linked_documents,
                    credit_note,
                    "credit_note",
                    seen_ids,
                )
            for credit_note in instance.credit_notes.all().order_by("invoice_date", "id"):
                self._append_unique_linked_document(
                    linked_documents,
                    credit_note,
                    "credit_note",
                    seen_ids,
                )

            replacement_invoice = self._get_replacement_invoice_instance(instance)
            self._append_unique_linked_document(
                linked_documents,
                replacement_invoice,
                "replacement_invoice",
                seen_ids,
            )
            return [item for item in linked_documents if item]

        for credit_note in instance.credit_notes.all().order_by("invoice_date", "id"):
            self._append_unique_linked_document(
                linked_documents,
                credit_note,
                "credit_note",
                seen_ids,
            )

        replacement_invoice = self._get_replacement_invoice_instance(instance)
        self._append_unique_linked_document(
            linked_documents,
            replacement_invoice,
            "replacement_invoice",
            seen_ids,
        )

        return [item for item in linked_documents if item]

    def _inject_currency_conversion(self, validated_data, instance=None):
        try:
            amount = validated_data.get('grand_total')
            if amount is None and instance:
                amount = instance.grand_total
            amount = amount or 0
            sign = -1 if amount < 0 else 1
            absolute_amount = abs(amount)
            if absolute_amount == 0:
                validated_data['company_currency_amount'] = 0.0
                validated_data['conversion_rate'] = 1.0
                return validated_data

            # Get company currency
            request_user = self.context['request'].user
            company_currency_symbol = get_user_currency(request_user)
            company_currency_code = symbol_to_currency_code(company_currency_symbol)

            # Get invoice currency
            if instance:
                invoice_currency_symbol = get_currency_from_record(instance)
            else:
                invoice_currency_symbol = None
                if validated_data.get('project'):
                    invoice_currency_symbol = validated_data['project'].currency if hasattr(validated_data['project'], 'currency') else None
                elif validated_data.get('company_profile'):
                    invoice_currency_symbol = validated_data['company_profile'].currency if hasattr(validated_data['company_profile'], 'currency') else None
                elif validated_data.get('freelancer'):
                    invoice_currency_symbol = validated_data['freelancer'].currency if hasattr(validated_data['freelancer'], 'currency') else None
                    
            invoice_currency_code = symbol_to_currency_code(invoice_currency_symbol)

            if invoice_currency_code.upper() == company_currency_code.upper():
                validated_data['company_currency_amount'] = round(absolute_amount * sign, 2)
                validated_data['conversion_rate'] = 1.0
            else:
                converter = ECBCurrencyConverter()
                converted = converter.convert_currency(absolute_amount, invoice_currency_code, company_currency_code)
                rate = converter.get_conversion_rate(invoice_currency_code, company_currency_code)

                validated_data['company_currency_amount'] = round(converted * sign, 2) if converted is not None else None
                validated_data['conversion_rate'] = rate if rate else None
        except Exception as e:
            logger.warning(f"Currency conversion failed: {e}")
            validated_data['company_currency_amount'] = None
            validated_data['conversion_rate'] = None

        return validated_data

    def _get_owner_scope(self, *, project=None, company_profile=None, freelancer=None):
        if project:
            if project.user.is_freelancer:
                return {"company_profile": None, "freelancer": project.user}
            return {"company_profile": project.user.company, "freelancer": None}

        if company_profile:
            return {"company_profile": company_profile, "freelancer": None}

        if freelancer:
            return {"company_profile": None, "freelancer": freelancer}

        return {"company_profile": None, "freelancer": None}

    def _get_invoice_owner_scope(self, invoice):
        return self._get_owner_scope(
            project=invoice.project,
            company_profile=invoice.company_profile,
            freelancer=invoice.freelancer,
        )

    def _same_owner_scope(self, left, right):
        return (
            (left["company_profile"].id if left["company_profile"] else None)
            == (right["company_profile"].id if right["company_profile"] else None)
            and (left["freelancer"].id if left["freelancer"] else None)
            == (right["freelancer"].id if right["freelancer"] else None)
        )

    def _get_default_status(self, project=None, company_profile=None, freelancer=None):
        request = self.context['request']
        valid_choices = []

        if project:
            valid_choices = get_project_owner_valid_choices(
                "invoice_status",
                project,
                request,
            )
        elif company_profile:
            valid_choices = get_valid_choices("invoice_status", company_profile.id, request, is_company=True)
        elif freelancer:
            valid_choices = get_valid_choices("invoice_status", freelancer.id, request, is_freelancer=True)

        if "open" in valid_choices:
            return "open"
        if valid_choices:
            return valid_choices[0]
        return "open"

    def _get_accepted_connection_ids(self, links_id, target_type):
        accepted_connections = Connection.objects.filter(
            Q(
                Q(initiator_id=links_id, target_type=target_type)
                | Q(target_id=links_id, initiator_type=target_type)
            ),
            status=StatusType.ACCEPTED.value,
        ).values_list("initiator_id", "target_id")

        return {
            connection_id
            for initiator_id, target_id in accepted_connections
            for connection_id in [initiator_id, target_id]
            if connection_id != links_id
        }

    def _get_allowed_collaborators_queryset(self, request_user):
        if request_user.company:
            company_links_id = request_user.company.sharemundo_links_id
            connection_user_ids = ConnectionUser.objects.filter(
                Q(status=StatusType.ACCEPTED.value)
                & Q(
                    connection__initiator_type=UserType.COMPANY.value,
                    connection__target_type=UserType.COMPANY.value,
                )
                & (
                    Q(connection__initiator_id=company_links_id)
                    | Q(connection__target_id=company_links_id)
                )
                & ~Q(user__company=request_user.company)
            ).values_list("user__id", flat=True)
            connected_freelancer_ids = self._get_accepted_connection_ids(
                company_links_id,
                UserType.FREELANCER.value,
            )
            queryset = User.objects.filter(
                Q(id__in=connection_user_ids)
                | Q(sharemundo_links_id__in=connected_freelancer_ids)
                | Q(company=request_user.company)
            )
        elif request_user.is_freelancer:
            connected_freelancer_ids = self._get_accepted_connection_ids(
                request_user.sharemundo_links_id,
                UserType.FREELANCER.value,
            )
            queryset = User.objects.filter(
                Q(sharemundo_links_id__in=connected_freelancer_ids)
                | Q(id=request_user.id)
            )
        else:
            queryset = User.objects.none()

        return queryset.filter(
            is_active=True,
            is_deleted=False,
            is_permanently_deleted=False,
        ).distinct()

    def _serialize_collaborators(self, invoice):
        return [
            {
                "id": collaborator.id,
                "name": collaborator.full_name or collaborator.email,
                "email": collaborator.email,
            }
            for collaborator in invoice.collaborators.all().order_by(
                "first_name",
                "last_name",
                "email",
            )
        ]

    def _resolve_invoice_document_selection(
        self,
        *,
        line_items_data,
        selected_documents=None,
        instance=None,
    ):
        if line_items_data is None:
            line_items_data = (
                [
                    {
                        "monthly_timesheet_submission": (
                            item.monthly_timesheet_submission
                        )
                    }
                    for item in instance.line_items.all()
                    if item.monthly_timesheet_submission_id
                ]
                if instance
                else []
            )

        auto_document_ids = get_invoice_auto_document_ids_from_line_items(line_items_data)

        if selected_documents is None or (not instance and not selected_documents):
            if not instance:
                selected_document_ids = list(auto_document_ids)
                excluded_auto_document_ids = []
            else:
                selected_document_ids = get_effective_invoice_document_ids(
                    instance,
                    line_items_data=line_items_data,
                )
                excluded_auto_document_ids = list(
                    instance.excluded_auto_documents.values_list("id", flat=True)
                )
        else:
            selected_document_ids = [
                int(document.id if hasattr(document, "id") else document)
                for document in selected_documents
            ]
            excluded_auto_document_ids = [
                document_id
                for document_id in auto_document_ids
                if document_id not in selected_document_ids
            ]

        if not selected_document_ids and not excluded_auto_document_ids:
            return [], []

        documents_by_id = {
            document.id: document
            for document in Document.objects.filter(id__in=selected_document_ids)
        }
        excluded_documents_by_id = {
            document.id: document
            for document in Document.objects.filter(id__in=excluded_auto_document_ids)
        }
        return (
            [
                documents_by_id[document_id]
                for document_id in selected_document_ids
                if document_id in documents_by_id
            ],
            [
                excluded_documents_by_id[document_id]
                for document_id in excluded_auto_document_ids
                if document_id in excluded_documents_by_id
            ],
        )

    def _is_locked_credit_note_status(self, status):
        return str(status or "").strip().lower() in {"issued", "paid"}

    def _resolve_credit_note_status(self, status_name, *, project=None, company_profile=None, freelancer=None):
        """Credit notes only support open/issued; use config casing when available."""
        normalized = str(status_name or "").strip().lower()
        if normalized not in {"open", "issued"}:
            raise serializers.ValidationError(
                'Invalid credit note status. Allowed choices are: open, issued.'
            )
        try:
            return self._get_forced_status(
                normalized,
                project=project,
                company_profile=company_profile,
                freelancer=freelancer,
            )
        except serializers.ValidationError:
            return normalized

    def _get_forced_status(self, status_name, *, project=None, company_profile=None, freelancer=None):
        request = self.context["request"]
        valid_choices = []

        if project:
            valid_choices = get_project_owner_valid_choices(
                "invoice_status",
                project,
                request,
            )
        elif company_profile:
            valid_choices = get_valid_choices("invoice_status", company_profile.id, request, is_company=True)
        elif freelancer:
            valid_choices = get_valid_choices("invoice_status", freelancer.id, request, is_freelancer=True)

        normalized_lookup = {
            str(choice).strip().lower(): choice for choice in (valid_choices or [])
        }
        if status_name in normalized_lookup:
            return normalized_lookup[status_name]

        raise serializers.ValidationError(
            f'Invoice status "{status_name}" is not configured for this billing scope.'
        )

    def _get_reference_owner(self, invoice):
        owner_scope = self._get_invoice_owner_scope(invoice)
        return owner_scope["company_profile"], owner_scope["freelancer"]

    def _generate_reference_number(self, invoice):
        prefix = (
            InvoiceReferenceCounter.Prefix.CREDIT_NOTE
            if invoice.document_type == Invoice.DocumentType.CREDIT_NOTE
            else InvoiceReferenceCounter.Prefix.INVOICE
        )
        company_profile, freelancer = self._get_reference_owner(invoice)
        if not company_profile and not freelancer:
            raise serializers.ValidationError("Unable to generate billing reference without a billing owner.")

        # YYYYMM in the reference string only; sequence is continuous (one counter row per owner).
        period = invoice.invoice_date.strftime("%Y%m")
        counter_defaults = {
            "company_profile": company_profile,
            "freelancer": freelancer,
            "last_sequence": 0,
        }
        lookup = {
            "company_profile": company_profile,
            "freelancer": freelancer,
        }

        counter, _ = InvoiceReferenceCounter.objects.select_for_update().get_or_create(
            defaults=counter_defaults,
            **lookup,
        )

        existing_reference_max = self._get_existing_reference_max_sequence(
            invoice,
        )
        next_sequence = max(counter.last_sequence, existing_reference_max) + 1
        counter.last_sequence = next_sequence
        counter.save(update_fields=["last_sequence", "updated_at"])
        return f"{prefix}-{period}-{next_sequence:05d}"

    def _invoice_queryset_for_owner_scope(self, owner_scope):
        """Invoices that share the same billing reference counter as this owner scope."""
        company_profile = owner_scope["company_profile"]
        freelancer = owner_scope["freelancer"]
        if company_profile:
            # project.user.is_freelancer is a model property; use user_type for DB lookups.
            company_project_invoices = Q(
                project__isnull=False,
                project__user__company=company_profile,
            ) & (
                Q(project__user__user_type__isnull=True)
                | ~Q(project__user__user_type="freelancer")
            )
            return Invoice.objects.filter(
                Q(
                    project__isnull=True,
                    company_profile=company_profile,
                    freelancer__isnull=True,
                )
                | company_project_invoices
            )
        if freelancer:
            return Invoice.objects.filter(
                Q(
                    project__isnull=True,
                    company_profile__isnull=True,
                    freelancer=freelancer,
                )
                | Q(
                    project__isnull=False,
                    project__user__user_type="freelancer",
                    project__user=freelancer,
                )
            )
        return Invoice.objects.none()

    def _get_existing_reference_max_sequence(self, invoice):
        invoices = self._invoice_queryset_for_owner_scope(
            self._get_invoice_owner_scope(invoice),
        )
        if invoice.pk:
            invoices = invoices.exclude(pk=invoice.pk)
        invoices = invoices.only("reference_number")

        max_sequence = 0
        for existing_invoice in invoices:
            reference_number = str(existing_invoice.reference_number or "").strip()
            if not reference_number:
                continue

            try:
                sequence = int(reference_number.split("-")[-1])
            except (TypeError, ValueError):
                continue

            if sequence > max_sequence:
                max_sequence = sequence

        return max_sequence

    def _get_line_item_description(self, item_data):
        if item_data.get("description"):
            return item_data["description"]
        if item_data.get("time_card"):
            return item_data["time_card"].name
        if item_data.get("expense_card"):
            return item_data["expense_card"].name
        if item_data.get("monthly_timesheet_submission"):
            submission = item_data["monthly_timesheet_submission"]
            user_name = submission.user.full_name if submission.user_id else "User"
            return f"Monthly timesheet {submission.month:02d}/{submission.year} - {user_name}"
        return ""

    def _build_scope_filter_for_invoice(self, invoice):
        if invoice.project_id:
            return {"project_id": invoice.project_id}
        if invoice.company_profile_id:
            return {"company_profile_id": invoice.company_profile_id}
        if invoice.freelancer_id:
            return {"freelancer_id": invoice.freelancer_id}
        return {}

    def _get_line_item_unique_identifier(self, *, time_card=None, expense_card=None, monthly_timesheet_submission=None):
        if time_card:
            return f"time_card_{time_card.id}"
        if expense_card:
            return f"expense_card_{expense_card.id}"
        if monthly_timesheet_submission:
            return f"monthly_timesheet_submission_{monthly_timesheet_submission.id}"
        return None

    def _quantize_currency(self, value):
        return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _validate_invoice_against_po_limit(
        self,
        *,
        instance,
        purchase_order,
        grand_total,
    ):
        """Ensure linked invoices (incl. this one) do not exceed PO TTC."""
        if not purchase_order or grand_total is None:
            return

        from project_management.purchase_order_invoice_status import (
            LINKED_BILLING_DOCUMENT_TYPES,
        )

        po_max = float(get_po_revised_amount_incl_vat(purchase_order))
        other_documents = purchase_order.invoices.filter(
            document_type__in=LINKED_BILLING_DOCUMENT_TYPES,
        )
        if instance and instance.pk:
            other_documents = other_documents.exclude(pk=instance.pk)

        other_total = sum(
            self._quantize_currency(doc.grand_total or 0) for doc in other_documents
        )
        projected_total = self._quantize_currency(
            other_total + self._quantize_currency(grand_total)
        )

        if projected_total > po_max:
            raise serializers.ValidationError(
                {"grand_total": INVOICE_PO_LINKED_TOTAL_EXCEEDS_PO_AMOUNT}
            )

    def _enforce_invoice_po_limit_after_save(self, invoice):
        from project_management.purchase_order_invoice_status import (
            LINKED_BILLING_DOCUMENT_TYPES,
        )

        if (
            invoice.document_type not in LINKED_BILLING_DOCUMENT_TYPES
            or not invoice.purchase_order_id
        ):
            return
        purchase_order = PurchaseOrder.objects.get(pk=invoice.purchase_order_id)
        self._validate_invoice_against_po_limit(
            instance=invoice,
            purchase_order=purchase_order,
            grand_total=invoice.grand_total,
        )

    def _get_credit_notes_total(self, invoice, *, exclude_invoice_id=None):
        queryset = invoice.credit_notes.all()
        if exclude_invoice_id:
            queryset = queryset.exclude(id=exclude_invoice_id)
        total = queryset.aggregate(total=Sum('grand_total'))["total"] or 0
        return self._quantize_currency(total)

    def _build_credit_note_line_item(self, source_invoice, amount):
        vat_rate = Decimal(str(source_invoice.vat_rate or 0))
        gross_amount = Decimal(str(amount))
        if vat_rate > 0:
            divisor = Decimal("1") + (vat_rate / Decimal("100"))
            price = (gross_amount / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            price = gross_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        vat_amount = (gross_amount - price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return {
            "description": f"Credit note deduction for {get_invoice_reference_display(source_invoice)}",
            "vat_amount": float(vat_amount),
            "price": float(price),
            "quantity": 1,
        }

    def _validate_credit_note_request(self, source_invoice):
        if source_invoice.document_type != Invoice.DocumentType.INVOICE:
            raise serializers.ValidationError("Credit notes can only be created from invoices.")

        request = self.context["request"]
        request_user = request.user

        if source_invoice.project:
            if not has_project_permission(
                "PUT",
                request_user,
                source_invoice.project,
                get_product_name_from_request(request),
            ):
                raise serializers.ValidationError("You do not have access to this invoice.")
        elif request_user.is_freelancer:
            if source_invoice.freelancer_id != request_user.id:
                raise serializers.ValidationError("You do not have access to this invoice.")
        elif source_invoice.company_profile_id != request_user.company_id:
            raise serializers.ValidationError("You do not have access to this invoice.")

    def _validate_credit_note_amount(self, source_invoice, amount, *, existing_credit_note=None):
        if amount is None:
            raise serializers.ValidationError({"grand_total": "Credit note amount is required."})

        normalized_amount = self._quantize_currency(amount)
        if normalized_amount >= 0:
            raise serializers.ValidationError({"grand_total": "Credit note amount must be negative."})

        already_credited = self._get_credit_notes_total(
            source_invoice,
            exclude_invoice_id=existing_credit_note.id if existing_credit_note else None,
        )
        remaining_total = self._quantize_currency((source_invoice.grand_total or 0) + already_credited)
        projected_total = self._quantize_currency(remaining_total + normalized_amount)

        if projected_total < 0:
            raise serializers.ValidationError(
                {"grand_total": "Credit note amount cannot exceed the remaining invoice total."}
            )

        return normalized_amount

    def _build_credit_note_payload(self, *, source_invoice, amount, notes=None, documents=None):
        invoice_date = timezone.now().date()
        owner_scope = self._get_invoice_owner_scope(source_invoice)
        payload = {
            "freelancer": source_invoice.freelancer,
            "project": source_invoice.project,
            "company_profile": source_invoice.company_profile,
            "reciepient_user": source_invoice.reciepient_user,
            "reciepient_company": source_invoice.reciepient_company,
            "invoice_date": invoice_date,
            "due_date": invoice_date,
            "status": self._resolve_credit_note_status("open", **owner_scope),
            "notes": notes or "",
            "vat_rate": source_invoice.vat_rate,
            "grand_total": amount,
            "document_type": Invoice.DocumentType.CREDIT_NOTE,
            "source_invoice": source_invoice,
            "purchase_order": source_invoice.purchase_order,
            "documents": documents or [],
            "line_items": [self._build_credit_note_line_item(source_invoice, amount)],
        }
        return self._inject_currency_conversion(payload)

    def _create_credit_note(self, validated_data):
        source_invoice = validated_data.pop("source_invoice")
        documents = validated_data.pop("documents", [])
        grand_total = validated_data.pop("grand_total")
        credit_note_payload = self._build_credit_note_payload(
            source_invoice=source_invoice,
            amount=grand_total,
            notes=validated_data.pop("notes", ""),
            documents=documents,
        )
        documents = credit_note_payload.pop("documents", [])
        line_items_data = credit_note_payload.pop("line_items", [])
        credit_note_payload["created_by"] = self.context["request"].user
        credit_note_payload["reference_number"] = self._generate_reference_number(
            Invoice(
                invoice_date=credit_note_payload["invoice_date"],
                document_type=Invoice.DocumentType.CREDIT_NOTE,
                project=credit_note_payload["project"],
                company_profile=credit_note_payload["company_profile"],
                freelancer=credit_note_payload["freelancer"],
            )
        )
        credit_note = super().create(credit_note_payload)
        if documents:
            credit_note.documents.set(documents)
        for item_data in line_items_data:
            InvoiceLineItem.objects.create(invoice=credit_note, **item_data)

        create_pdf_invoice_by_bill(source_invoice, self.context["request"].user)
        create_pdf_invoice_by_bill(credit_note, self.context["request"].user)
        if credit_note.purchase_order_id:
            sync_purchase_order_status_from_linked_invoices(credit_note.purchase_order_id)

        return credit_note

    def _regenerate_related_invoice_pdfs(self, invoice):
        create_pdf_invoice_by_bill(invoice, self.context['request'].user)
        if invoice.document_type == Invoice.DocumentType.CREDIT_NOTE and invoice.source_invoice_id:
            create_pdf_invoice_by_bill(invoice.source_invoice, self.context['request'].user)

    def _document_matches_invoice_scope(
        self,
        document,
        *,
        project=None,
        company_profile=None,
        freelancer=None,
        request_user=None,
    ):
        folder = document.folder
        folder_project = getattr(folder, "project", None)
        folder_company = getattr(folder, "company_profile", None)
        folder_freelancer = getattr(folder, "freelancer", None)

        if project:
            return bool(folder_project and folder_project.id == project.id)

        if company_profile:
            if folder_company and folder_company.id == company_profile.id:
                return True
            if (
                folder_project
                and folder_project.user.company_id == company_profile.id
            ):
                return True
            return False

        if freelancer:
            if folder_freelancer and folder_freelancer.id == freelancer.id:
                return True
            if folder_project and folder_project.user_id == freelancer.id:
                return True
            return False

        if request_user and request_user.is_freelancer:
            return bool(folder_freelancer and folder_freelancer.id == request_user.id)

        if request_user and request_user.company_id:
            if folder_company and folder_company.id == request_user.company_id:
                return True
            if folder_project and folder_project.user.company_id == request_user.company_id:
                return True

        return False

    
    def validate_documents(self, documents):
        request_user = self.context['request'].user
        instance = self.instance
        project = instance.project if instance else None
        company_profile = instance.company_profile if instance else None
        freelancer = instance.freelancer if instance else None

        for document in documents:
            if self._document_matches_invoice_scope(
                document,
                project=project,
                company_profile=company_profile,
                freelancer=freelancer,
                request_user=request_user,
            ):
                continue

            if project:
                raise serializers.ValidationError(
                    f"Document {document.id} is not linked to the same project as this invoice."
                )

            if request_user.is_freelancer:
                raise serializers.ValidationError(
                    f"Document {document.id} is not linked to this freelancer."
                )

            raise serializers.ValidationError(
                f"Document {document.id} is not linked to your company."
            )
        return documents

    def validate_collaborators(self, collaborators):
        request_user = self.context["request"].user
        allowed_collaborator_ids = set(
            self._get_allowed_collaborators_queryset(request_user).values_list(
                "id",
                flat=True,
            )
        )
        invalid_collaborator_ids = [
            collaborator.id
            for collaborator in collaborators
            if collaborator.id not in allowed_collaborator_ids
        ]
        if invalid_collaborator_ids:
            raise serializers.ValidationError(
                f"Invalid collaborators: {', '.join(map(str, invalid_collaborator_ids))}"
            )
        return collaborators

    def _get_purchase_order_team_member_ids(self, purchase_order):
        if not purchase_order or not purchase_order.project_id:
            return set()
        return set(
            purchase_order.project.team_members.values_list(
                "team_member_id",
                flat=True,
            )
        )

    def validate(self, data):
        instance = self.instance
        request = self.context['request']
        request_user = request.user
        document_type = data.get('document_type', instance.document_type if instance else Invoice.DocumentType.INVOICE)
        source_invoice = data.get('source_invoice', instance.source_invoice if instance else None)
        project = data.get('project', instance.project if instance else None)
        company_profile = data.get('company_profile', instance.company_profile if instance else None)
        freelancer = data.get('freelancer', instance.freelancer if instance else None)
        reciepient_user = data.get('reciepient_user', instance.reciepient_user if instance else None)
        reciepient_company = data.get('reciepient_company', instance.reciepient_company if instance else None)
        status = data.get('status', instance.status if instance else None)
        grand_total = data.get('grand_total', instance.grand_total if instance else None)
        valid_choices = []

        if instance and instance.document_type == Invoice.DocumentType.CREDIT_NOTE:
            if self._is_locked_credit_note_status(instance.status):
                raise serializers.ValidationError(
                    "Issued credit notes cannot be edited after creation."
                )

        if document_type == Invoice.DocumentType.CREDIT_NOTE and instance:
            data.pop('purchase_order', None)
            data.pop('collaborators', None)
            if not instance.source_invoice_id:
                raise serializers.ValidationError("Credit note source invoice is required.")
            self._validate_credit_note_request(instance.source_invoice)
            data['grand_total'] = self._validate_credit_note_amount(
                instance.source_invoice,
                grand_total,
                existing_credit_note=instance,
            )
            if "status" in data and data["status"] is not None:
                data["status"] = self._resolve_credit_note_status(
                    str(data["status"]).strip().lower(),
                    **self._get_invoice_owner_scope(instance),
                )
            return data

        if document_type == Invoice.DocumentType.CREDIT_NOTE:
            data.pop('purchase_order', None)
            data.pop('collaborators', None)
            if instance:
                raise serializers.ValidationError("Credit notes cannot change document type.")
            if not source_invoice:
                raise serializers.ValidationError("source_invoice_id is required for credit notes.")
            self._validate_credit_note_request(source_invoice)
            data['grand_total'] = self._validate_credit_note_amount(source_invoice, grand_total)
            return data

        # Ensure only one of project or (company_profile/freelancer) is set
        if project and (company_profile or freelancer):
            raise serializers.ValidationError(
                "ExpenseCard can have either a project or a company/freelancer, not both."
            )

        if not project and not (company_profile or freelancer):
            raise serializers.ValidationError(
                "ExpenseCard must have either a project or a company/freelancer."
            )
        
        if reciepient_user and reciepient_company:
            raise serializers.ValidationError("Invoice can have either a contact user or contact company.")
        
        if not reciepient_user and not reciepient_company:
            raise serializers.ValidationError("Invoice must have either a contact user or contact company.")

        # Project-specific validation
        if project:
            # TODO: Adjust as per new permission system
            # if not has_project_permission("PUT", request_user, project, get_product_name_from_request(request)):
            #     raise serializers.ValidationError("You do not have access to this project.")
            valid_choices = get_project_owner_valid_choices(
                "invoice_status",
                project,
                request,
            )
            

        # Company/Freelancer-specific validation
        if company_profile:
            valid_choices = get_valid_choices("invoice_status", company_profile.id, request, is_company=True)
            if request_user.company != company_profile:
                raise serializers.ValidationError("You do not have permission to add invoice in this company.")
            
        if freelancer:
            valid_choices = get_valid_choices("invoice_status", freelancer.id, request, is_freelancer=True)
            if freelancer != request_user:
                raise serializers.ValidationError("You do not have permission to add invoice in this freelancer.")

        if request_user.is_freelancer:
            if reciepient_company and reciepient_company.user != request_user:
                raise serializers.ValidationError("Cannot add other freelancer contact in invoice.")
            if reciepient_user and reciepient_user.user != request_user:
                raise serializers.ValidationError(f"Contact {reciepient_user.id} does not belong to this user.")
        else:
            if reciepient_company and reciepient_company.company_profile != request_user.company:
                raise serializers.ValidationError(f"Contact {reciepient_company.id} does not belong to this user.")
            if reciepient_user and reciepient_user.company_profile != request_user.company:
                raise serializers.ValidationError(f"Contact {reciepient_user.id} does not belong to this user.")

        if 'status' in data and valid_choices and status not in valid_choices:
            raise serializers.ValidationError(
                f"Invalid status. Allowed choices are: {', '.join(valid_choices)}"
            )

        purchase_order = data.get(
            "purchase_order",
            instance.purchase_order if instance else None,
        )
        collaborators = data.get(
            "collaborators",
            list(instance.collaborators.all()) if instance else [],
        )
        if document_type == Invoice.DocumentType.INVOICE and not purchase_order and collaborators:
            raise serializers.ValidationError(
                {"collaborators": "Collaborators can only be linked when a purchase order is selected."}
            )
        if document_type == Invoice.DocumentType.INVOICE and purchase_order and collaborators:
            if purchase_order.project_id:
                project_team_member_ids = self._get_purchase_order_team_member_ids(
                    purchase_order
                )
                invalid_project_collaborator_ids = [
                    collaborator.id
                    for collaborator in collaborators
                    if collaborator.id not in project_team_member_ids
                ]
                if invalid_project_collaborator_ids:
                    raise serializers.ValidationError(
                        {
                            "collaborators": (
                                "Collaborators must be members of the purchase order's project: "
                                f"{', '.join(map(str, invalid_project_collaborator_ids))}"
                            )
                        }
                    )
            elif request_user.company:
                # PO without project → internal company collaborators only
                invalid_internal_collaborator_ids = [
                    collaborator.id
                    for collaborator in collaborators
                    if collaborator.company_id != request_user.company_id
                ]
                if invalid_internal_collaborator_ids:
                    raise serializers.ValidationError(
                        {
                            "collaborators": (
                                "Collaborators must be internal company users when the "
                                "purchase order is not linked to a project: "
                                f"{', '.join(map(str, invalid_internal_collaborator_ids))}"
                            )
                        }
                    )
        if document_type == Invoice.DocumentType.INVOICE and purchase_order:
            self._validate_invoice_against_po_limit(
                instance=instance,
                purchase_order=purchase_order,
                grand_total=grand_total,
            )

        return data
    
    def validate_card_belongs_to_invoice_entity(self, invoice, card, item_id=None):
        if invoice.project and card.project != invoice.project:
            raise serializers.ValidationError(f"{card.__class__.__name__} must belong to the associated project.")
        elif invoice.company_profile and card.company_profile != invoice.company_profile:
            raise serializers.ValidationError(f"{card.__class__.__name__} must belong to the associated company.")
        elif invoice.freelancer and card.freelancer != invoice.freelancer:
            raise serializers.ValidationError(f"{card.__class__.__name__} must belong to the associated freelancer.")

        if isinstance(card, TimeCard):
            existing_line_item = InvoiceLineItem.objects.filter(time_card=card).exclude(
                invoice__status='not paid'
            ).exclude(
                invoice__document_type=Invoice.DocumentType.CREDIT_NOTE
            )
        else:
            existing_line_item = InvoiceLineItem.objects.filter(expense_card=card).exclude(
                invoice__status='not paid'
            ).exclude(
                invoice__document_type=Invoice.DocumentType.CREDIT_NOTE
            )

        if item_id:
            existing_line_item = existing_line_item.exclude(id=item_id).first()
        else:
            existing_line_item = existing_line_item.first()
        if existing_line_item:
            raise serializers.ValidationError(
                f"The card {card} is already associated with an invoice (ID: {existing_line_item.invoice.id}) and cannot be reused."
            )

    def validate_timesheet_submission_belongs_to_invoice_entity(
        self,
        invoice,
        submission,
        item_id=None,
    ):
        if submission.status != "approved":
            raise serializers.ValidationError(
                "Only approved monthly timesheets can be invoiced."
            )
        if invoice.freelancer_id and submission.user_id != invoice.freelancer_id:
            raise serializers.ValidationError(
                "This monthly timesheet does not belong to the selected freelancer."
            )

        if (
            invoice.company_profile_id
            and submission.user.company_id != invoice.company_profile_id
        ):
            raise serializers.ValidationError(
                "This monthly timesheet does not belong to the selected company."
            )

        if invoice.purchase_order_id:
            invoiceable_po_hours = self._get_invoice_purchase_order_submission_hours(
                invoice,
                submission,
            )
            if float(invoiceable_po_hours) <= 0:
                raise serializers.ValidationError(
                    "This monthly timesheet has no invoiceable time for the selected purchase order."
                )

        existing_line_items = InvoiceLineItem.objects.filter(
            monthly_timesheet_submission=submission,
        ).exclude(
            invoice__status="not paid"
        ).exclude(
            invoice__document_type=Invoice.DocumentType.CREDIT_NOTE
        )
        if invoice.project_id:
            existing_line_items = existing_line_items.filter(
                invoice__project_id=invoice.project_id,
            )
        elif invoice.company_profile_id:
            existing_line_items = existing_line_items.filter(
                invoice__company_profile_id=invoice.company_profile_id,
            )
        elif invoice.freelancer_id:
            existing_line_items = existing_line_items.filter(
                invoice__freelancer_id=invoice.freelancer_id,
            )
        if invoice.purchase_order_id:
            existing_line_items = existing_line_items.filter(
                invoice__purchase_order_id=invoice.purchase_order_id,
            )

        if item_id:
            existing_line_items = existing_line_items.exclude(id=item_id)

        existing_line_item = existing_line_items.first()
        if existing_line_item:
            raise serializers.ValidationError(
                f"This monthly timesheet is already associated with an invoice (ID: {existing_line_item.invoice.id}) and cannot be reused."
            )

    def _get_invoice_purchase_order_submission_hours(self, invoice, submission):
        if not invoice.purchase_order_id:
            return None

        invoiceable_po_hours = TimesheetActivity.objects.filter(
            user=submission.user,
            project_id=invoice.purchase_order.project_id,
            purchase_order_id=invoice.purchase_order_id,
            is_invoiceable=True,
            entries__entry_date__gte=submission.period_start,
            entries__entry_date__lte=submission.period_end,
        ).aggregate(total=Sum("entries__hours"))["total"] or 0
        return round(float(invoiceable_po_hours), 2)

    def _normalize_purchase_order_timesheet_line_item(self, invoice, item_data):
        submission = item_data.get("monthly_timesheet_submission")
        if not submission or not invoice.purchase_order_id:
            return item_data

        invoiceable_po_hours = self._get_invoice_purchase_order_submission_hours(
            invoice,
            submission,
        )
        if not invoiceable_po_hours or invoiceable_po_hours <= 0:
            return item_data

        item_data["quantity"] = invoiceable_po_hours
        price = float(item_data.get("price") or 0)
        vat_rate = float(invoice.vat_rate or 0)
        item_data["vat_amount"] = round(
            price * invoiceable_po_hours * vat_rate / 100,
            2,
        )
        return item_data


    @transaction.atomic
    def create(self, validated_data):
        if validated_data.get("document_type") == Invoice.DocumentType.CREDIT_NOTE:
            return self._create_credit_note(validated_data)

        validated_data["status"] = get_automatic_invoice_status(
            status=validated_data.get("status", "open"),
            due_date=validated_data.get("due_date"),
            document_type=validated_data.get(
                "document_type",
                Invoice.DocumentType.INVOICE,
            ),
        )
        line_items_data = validated_data.pop('line_items', [])
        manual_documents = validated_data.pop("documents", None)
        collaborators = list(validated_data.pop("collaborators", []))
        validated_data['created_by'] = self.context['request'].user
        # Track seen cards to avoid duplicates
        validated_data = self._inject_currency_conversion(validated_data)
        validated_data['reference_number'] = self._generate_reference_number(
            Invoice(
                invoice_date=validated_data["invoice_date"],
                document_type=validated_data.get("document_type", Invoice.DocumentType.INVOICE),
                project=validated_data.get("project"),
                company_profile=validated_data.get("company_profile"),
                freelancer=validated_data.get("freelancer"),
            )
        )

        invoice = super().create(validated_data)
        invoice.collaborators.set(collaborators)
        seen_cards = set()

        for item_data in line_items_data:
            time_card = item_data.get('time_card')
            expense_card = item_data.get('expense_card')
            monthly_timesheet_submission = item_data.get('monthly_timesheet_submission')

            card_identifier = self._get_line_item_unique_identifier(
                time_card=time_card,
                expense_card=expense_card,
                monthly_timesheet_submission=monthly_timesheet_submission,
            )
            if card_identifier and card_identifier in seen_cards:
                raise serializers.ValidationError(f"{card_identifier} is already added to this invoice.")

            if card_identifier:
                seen_cards.add(card_identifier)
            if time_card or expense_card:
                self.validate_card_belongs_to_invoice_entity(invoice, time_card or expense_card)
            if monthly_timesheet_submission:
                self.validate_timesheet_submission_belongs_to_invoice_entity(
                    invoice,
                    monthly_timesheet_submission,
                )
                item_data = self._normalize_purchase_order_timesheet_line_item(
                    invoice,
                    item_data,
                )
            item_data["description"] = self._get_line_item_description(item_data)

            InvoiceLineItem.objects.create(invoice=invoice, **item_data)
        selected_documents, excluded_auto_documents = (
            self._resolve_invoice_document_selection(
                line_items_data=line_items_data,
                selected_documents=list(manual_documents)
                if manual_documents is not None
                else None,
            )
        )
        invoice.documents.set(selected_documents)
        invoice.excluded_auto_documents.set(excluded_auto_documents)
        self._enforce_invoice_po_limit_after_save(invoice)
        self._regenerate_related_invoice_pdfs(invoice)
        if invoice.purchase_order_id:
            sync_purchase_order_status_from_linked_invoices(invoice.purchase_order_id)
        return invoice

    @transaction.atomic
    def update(self, instance, validated_data):
        if (
            instance.document_type == Invoice.DocumentType.CREDIT_NOTE
            and self._is_locked_credit_note_status(instance.status)
        ):
            raise serializers.ValidationError(
                "Issued credit notes cannot be edited after creation."
            )

        validated_data["status"] = get_automatic_invoice_status(
            status=validated_data.get("status", instance.status),
            due_date=validated_data.get("due_date", instance.due_date),
            document_type=validated_data.get(
                "document_type",
                instance.document_type,
            ),
        )
        line_items_data = validated_data.pop('line_items', None)
        manual_documents = validated_data.pop("documents", None)
        collaborators = validated_data.pop("collaborators", None)
        validated_data = self._inject_currency_conversion(validated_data, instance=instance)
        previous_purchase_order_id = instance.purchase_order_id

        instance = super().update(instance, validated_data)
        if collaborators is not None:
            instance.collaborators.set(collaborators)
        elif not instance.purchase_order_id:
            instance.collaborators.clear()

        if line_items_data is not None:
            # Omitted line items on PATCH must preserve the existing collection.
            existing_ids = {item.id for item in instance.line_items.all()}
            provided_ids = {
                item.get('id') for item in line_items_data if item.get('id')
            }

            for item_id in existing_ids - provided_ids:
                instance.line_items.filter(id=item_id).delete()

            seen_cards = set()

            for item_data in line_items_data:
                item_id = item_data.pop('id', None)
                time_card = item_data.get('time_card')
                expense_card = item_data.get('expense_card')
                monthly_timesheet_submission = item_data.get(
                    'monthly_timesheet_submission'
                )

                card_identifier = self._get_line_item_unique_identifier(
                    time_card=time_card,
                    expense_card=expense_card,
                    monthly_timesheet_submission=monthly_timesheet_submission,
                )
                if card_identifier and card_identifier in seen_cards:
                    raise serializers.ValidationError(
                        f"{card_identifier} is already added to this invoice."
                    )

                if card_identifier:
                    seen_cards.add(card_identifier)
                if time_card or expense_card:
                    self.validate_card_belongs_to_invoice_entity(
                        instance,
                        time_card or expense_card,
                        item_id,
                    )
                if monthly_timesheet_submission:
                    self.validate_timesheet_submission_belongs_to_invoice_entity(
                        instance,
                        monthly_timesheet_submission,
                        item_id,
                    )
                    item_data = self._normalize_purchase_order_timesheet_line_item(
                        instance,
                        item_data,
                    )

                defaults = {
                    key: value
                    for key, value in item_data.items()
                    if key != 'id'
                }
                defaults["description"] = self._get_line_item_description(item_data)
                InvoiceLineItem.objects.update_or_create(
                    id=item_id,
                    defaults={**defaults, 'invoice': instance},
                )

        if line_items_data is not None or manual_documents is not None:
            selected_documents, excluded_auto_documents = (
                self._resolve_invoice_document_selection(
                    line_items_data=line_items_data,
                    selected_documents=list(manual_documents)
                    if manual_documents is not None
                    else None,
                    instance=instance,
                )
            )
            instance.documents.set(selected_documents)
            instance.excluded_auto_documents.set(excluded_auto_documents)
        self._enforce_invoice_po_limit_after_save(instance)
        self._regenerate_related_invoice_pdfs(instance)
        if instance.purchase_order_id:
            sync_purchase_order_status_from_linked_invoices(instance.purchase_order_id)
        if (
            previous_purchase_order_id
            and previous_purchase_order_id != instance.purchase_order_id
        ):
            sync_purchase_order_status_from_linked_invoices(previous_purchase_order_id)
        return instance
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        po = None
        if instance.purchase_order_id:
            po = self.fields["purchase_order"].queryset.filter(
                pk=instance.purchase_order_id
            ).first()
        if po:
            representation["purchase_order"] = {
                "id": po.id,
                "reference": po.reference,
                "document_type": po.document_type,
                "po_type": po.po_type,
            }
        else:
            representation["purchase_order"] = None
        representation["collaborators"] = self._serialize_collaborators(instance)
        representation["display_reference"] = get_invoice_reference_display(instance)
        representation["document_label"] = get_billing_document_label(instance)
        total_credit_notes_amount = self.get_total_credit_notes_amount(instance)
        linked_credit_note_ids = (
            list(instance.credit_notes.order_by('invoice_date', 'id').values_list('id', flat=True))
            if instance.document_type == Invoice.DocumentType.INVOICE
            else []
        )
        representation["credit_note_id"] = (
            linked_credit_note_ids[0] if len(linked_credit_note_ids) == 1 else None
        )
        representation["credit_note_ids"] = linked_credit_note_ids
        representation["has_credit_notes"] = (
            instance.document_type == Invoice.DocumentType.INVOICE and bool(linked_credit_note_ids)
        )
        representation["total_credit_notes_amount"] = total_credit_notes_amount
        representation["net_total_after_credit_notes"] = self.get_net_total_after_credit_notes(instance)
        if instance.reciepient_user:
            representation["reciepient_user"] = {
                "id": instance.reciepient_user.id,
                "name": instance.reciepient_user.name + " " + instance.reciepient_user.surname,
            }
        elif instance.reciepient_company:
            representation["reciepient_company"] = {
                "id": instance.reciepient_company.id,
                "name": instance.reciepient_company.company_name,
            }

        if instance.project:
            representation["project"] = {
                "id": instance.project.id,
                "name": instance.project.project_name,
                "currency": instance.project.currency,
            }

        representation["line_items"] = [
            {
                **InvoiceLineItemSerializer(item).data,
                "description": item.description or self._get_line_item_description({
                    "time_card": item.time_card,
                    "expense_card": item.expense_card,
                    "monthly_timesheet_submission": item.monthly_timesheet_submission,
                }),
            }
            for item in instance.line_items.all()
        ]

        effective_documents = get_effective_invoice_documents(instance)
        if effective_documents:
            representation["documents"] = DocumentSerializer(
                effective_documents,
                many=True,
            ).data

        # Locate generated PDF, supporting both legacy and OFF- reference-based names
        root_folder_name = get_root_folder_name_from_bill(instance)
        root_folder = Folder.objects.filter(name=root_folder_name).first()
        if root_folder:
            invoices_folder = Folder.objects.filter(parent=root_folder, name="invoices").first()
            if invoices_folder:
                reference = getattr(instance, "invoice_reference", None)
                if reference and reference.startswith("OFF-"):
                    base_name = reference
                else:
                    base_name = f"invoice_project_{root_folder_name}_bill_{instance.id}"

                existing_document = Document.objects.filter(
                    folder=invoices_folder,
                    file__icontains=base_name.split(".")[0]
                ).first()
                representation["invoice_pdf"] = existing_document.file.url if existing_document else None
        return representation


class BillingSerializer(serializers.ModelSerializer):
    line_items = LineItemSerializer(many=True, write_only=True)

    class Meta:
        model = Billing
        fields = [
            'id', 'project', 'contact', 'invoice_date', 'invoice_no',
            'invoice_status', 'due_date', 'vat_rate', 'vat_amount',
            'grand_total', 'notes', 'documents', 'line_items'
        ]
    
    def validate_documents(self, documents):
        billing_project = self.instance.project.id if self.instance else int(self.initial_data.get('project'))

        for document in documents:
            if document.folder.project.id != billing_project:
                raise serializers.ValidationError(
                    f"Document {document.file_name} is not linked to the same project as the invoice."
                )
        return documents


    # TODO: Adjust as per new permission system

    # def validate_project(self, value):
    #     request_user = self.context.get('user')
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)
    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]
    #     if value.user == request_user and value.solution in products:
    #         return value
    #     if has_project_permission("PUT", request_user, value, product_name):
    #         return value
    #     raise serializers.ValidationError(f"Project {value} does not belong to this user")
        
    def validate_contact(self, value):
        if value.user != self.context.get('user'):
            raise serializers.ValidationError(f"Contact having {value.id} id does not belong to this user")
        return value

    def validate_invoice_status(self, value):
        if self.instance:
            project_id = self.instance.project.id
        else:
            project_id = self.initial_data.get('project')

        request = self.context.get('request')
        valid_choices = get_project_owner_valid_choices(
            "invoice_status",
            project_id,
            request,
        )
        if valid_choices and value not in valid_choices:
            raise serializers.ValidationError(f"Invalid invoice status. Allowed choices are: {', '.join(valid_choices)}")
        return value

    @transaction.atomic
    def create(self, validated_data):
        line_items_data = validated_data.pop('line_items', [])
        billing = super().create(validated_data)  # Create Billing object
        for item_data in line_items_data:
            LineItem.objects.create(bill=billing, **item_data)  # Create associated LineItems
        create_pdf_invoice_by_bill(billing, self.context['request'].user)
        return billing

    @transaction.atomic
    def update(self, instance, validated_data):
        line_items_data = validated_data.pop('line_items', [])
        instance = super().update(instance, validated_data)  # Update Billing object

        existing_line_item_ids = {item.id for item in instance.line_items.all()}
        incoming_line_item_ids = {item_data.get('id') for item_data in line_items_data if item_data.get('id')}

        # Delete line items not in the incoming data
        for line_item_id in existing_line_item_ids - incoming_line_item_ids:
            LineItem.objects.filter(id=line_item_id).delete()

        # Update or create line items
        for item_data in line_items_data:
            line_item_id = item_data.get('id')
            if line_item_id:
                LineItem.objects.update_or_create(
                    id=line_item_id,
                    defaults={**item_data, 'bill': instance}
                )
            else:
                LineItem.objects.create(bill=instance, **item_data)
        create_pdf_invoice_by_bill(instance, self.context['request'].user)
        return instance

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation['contact'] = ContactSerialier(instance.contact).data
        representation['line_items'] = LineItemSerializer(instance.line_items.all(), many=True).data
        if instance.documents:
            representation["documents"] = DocumentSerializer(instance.documents, many=True).data

        # Locate generated PDF for Billing, mirroring invoice/offer naming
        root_folder_name = get_root_folder_name_from_bill(instance)
        root_folder = Folder.objects.filter(name=root_folder_name).first()
        if root_folder:
            invoices_folder = Folder.objects.filter(parent=root_folder, name="invoices").first()
            if invoices_folder:
                reference = getattr(instance, "invoice_no", None)
                if reference and reference.startswith("OFF-"):
                    base_name = reference
                else:
                    base_name = f"invoice_project_{root_folder_name}_bill_{instance.id}"

                existing_document = Document.objects.filter(
                    folder=invoices_folder,
                    file__icontains=base_name.split(".")[0]
                ).first()
                representation["invoice_pdf"] = existing_document.file.url if existing_document else None
        return representation
