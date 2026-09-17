from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Q
from rest_framework import serializers

from models.models import CompanyProfile, User
from project_management.models import (
    AvailableProductPart,
    ContactCompany,
    ContactUser,
    Document,
    Invoice,
    POReferenceCounter,
    Project,
    ProjectPhase,
    PurchaseOrder,
    PurchaseOrderLineItem,
)
from project_management.models.constants import (
    INCOMING_PURCHASE_ORDER_STATUSES,
    OUTGOING_PURCHASE_ORDER_STATUSES,
)
from project_management.permissions import PurchaseOrderPermission
from project_management.serializers.linked_folder import LinkedFolderSerializerMixin
from project_management.utils import (
    create_pdf_change_notice_standalone,
    create_pdf_purchase_order,
    get_stored_change_notice_pdf_document,
    get_stored_purchase_order_pdf_document,
)

# Stored on POReferenceCounter.period; sequence never resets when the calendar month changes.
_PO_REFERENCE_COUNTER_GLOBAL_PERIOD = "global"

MILESTONE_AMOUNT_PO_INCONSISTENCY = (
    "Milestone amount is inconsistent with the PO amount. "
    "Please adjust the milestone amount accordingly."
)

MILESTONE_ALREADY_LINKED_TO_ANOTHER_PO = (
    "This milestone is already linked with another PO."
)


def _quantize_money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def raise_if_phase_linked_to_other_po(*, phase, target_po, field="purchase_orders"):
    if not target_po or not phase:
        return
    existing_po_id = getattr(phase, "purchase_order_id", None)
    if existing_po_id and existing_po_id != target_po.pk:
        raise serializers.ValidationError({field: MILESTONE_ALREADY_LINKED_TO_ANOTHER_PO})


def is_change_notice_row_effective(po: PurchaseOrder) -> bool:
    if po.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
        return True
    if po.po_type != PurchaseOrder.POType.OUTGOING:
        return False
    return po.source_po_id is not None


def get_change_notice_delta_excl_vat(po: PurchaseOrder):
    if not is_change_notice_row_effective(po):
        return None
    if po.change_amount_excl_vat is None:
        return None
    return _quantize_money(po.change_amount_excl_vat)


def get_purchase_order_display_total_incl_vat(po: PurchaseOrder) -> Decimal:
    delta = get_change_notice_delta_excl_vat(po)
    if delta is None:
        return _quantize_money(po.amount_incl_vat or 0)

    prior = PurchaseOrderSerializer._tip_before_change_notice(po)
    if prior is not None and po.amount_incl_vat is not None:
        prior_incl = prior.amount_incl_vat or Decimal("0")
        return _quantize_money((po.amount_incl_vat or 0) - prior_incl)

    return _quantize_money(delta)


class PurchaseOrderLineItemSerializer(serializers.ModelSerializer):
    available_product = serializers.PrimaryKeyRelatedField(
        queryset=AvailableProductPart.objects.all(),
        required=False,
        allow_null=True,
    )
    vat_rate = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = PurchaseOrderLineItem
        fields = (
            "id",
            "line_number",
            "available_product",
            "item_name",
            "rate",
            "quantity",
            "vat_rate",
            "total",
        )
        read_only_fields = ("id",)


class PurchaseOrderSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.project_name", read_only=True)
    linked_payment_milestones = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text="Project payment milestone IDs (ProjectPhase PKs).",
    )
    documents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Document.objects.all(),
        required=False,
    )
    source_po = serializers.PrimaryKeyRelatedField(
        queryset=PurchaseOrder.objects.filter(
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        ),
        required=False,
        allow_null=True,
    )
    change_amount_excl_vat = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    change_vat_amount = serializers.SerializerMethodField()
    change_incl_vat = serializers.SerializerMethodField()
    linked_payment_milestone_labels = serializers.SerializerMethodField()
    linked_payment_milestone_count = serializers.SerializerMethodField()
    line_item_labels = serializers.SerializerMethodField()
    line_item_count = serializers.SerializerMethodField()
    po_pdf = serializers.SerializerMethodField()
    linked_po_documents = serializers.SerializerMethodField()
    linked_invoices = serializers.SerializerMethodField()
    line_items = PurchaseOrderLineItemSerializer(many=True, required=False)
    effective_amount_excl_vat = serializers.SerializerMethodField()
    effective_line_items = serializers.SerializerMethodField()
    can_view_via_source_po = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        source_queryset = PurchaseOrder.objects.filter(
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        self.fields["source_po"].queryset = PurchaseOrderPermission(
            action="view",
            scope="company",
        ).filter_queryset_for_view(source_queryset, user)

    class Meta:
        model = PurchaseOrder
        fields = (
            "id",
            "project",
            "project_name",
            "po_type",
            "document_type",
            "source_po",
            "change_amount_excl_vat",
            "change_vat_amount",
            "change_incl_vat",
            "documents",
            "linked_folders",
            "linked_payment_milestones",
            "linked_payment_milestone_labels",
            "linked_payment_milestone_count",
            "line_item_labels",
            "line_item_count",
            "reference",
            "customer_reference",
            "po_date",
            "currency",
            "status",
            "amount_excl_vat",
            "vat_rate",
            "vat_amount",
            "amount_incl_vat",
            "notes",
            "receiving_user",
            "receiving_company",
            "emitting_user",
            "emitting_company",
            "po_pdf",
            "linked_po_documents",
            "linked_invoices",
            "line_items",
            "effective_amount_excl_vat",
            "effective_line_items",
            "can_view_via_source_po",
            "created_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "created_by",
            "created_at",
            "updated_at",
            "reference",
            "vat_amount",
            "amount_incl_vat",
            "po_pdf",
        )

    def get_can_view_via_source_po(self, obj):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        user_id = getattr(user, "id", None)
        return bool(
            user_id
            and obj.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE
            and obj.created_by_id != user_id
            and obj.source_po_id
            and obj.source_po.created_by_id == user_id
        )

    def _get_owner_scope(self, attrs=None):
        attrs = attrs or {}
        instance = self.instance
        context_project = self.context.get("project")

        project = attrs.get("project")
        if project is None:
            project = context_project or (instance.project if instance else None)

        company_profile = attrs.get(
            "company_profile",
            instance.company_profile if instance else None,
        )
        freelancer = attrs.get(
            "freelancer",
            instance.freelancer if instance else None,
        )

        if project:
            project_user = getattr(project, "user", None)
            if project_user and getattr(project_user, "is_freelancer", False):
                return project, None, project_user
            if project_user:
                return project, project_user.company, None

        return project, company_profile, freelancer

    def get_linked_folder_owner_ids(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user and getattr(user, "is_freelancer", False):
            return None, None, user.id

        company_profile = self.context.get("company_profile")
        company_profile_id = getattr(company_profile, "id", None)
        if not company_profile_id and self.instance:
            company_profile_id = self.instance.company_profile_id
        if not company_profile_id and user:
            company_profile_id = getattr(user, "company_id", None)

        return None, company_profile_id, None

    def _get_valid_status_choices(self, attrs=None):
        attrs = attrs or {}
        instance = self.instance
        po_type = attrs.get(
            "po_type",
            instance.po_type if instance else PurchaseOrder.POType.INCOMING,
        )
        if po_type == PurchaseOrder.POType.OUTGOING:
            return list(OUTGOING_PURCHASE_ORDER_STATUSES)
        return list(INCOMING_PURCHASE_ORDER_STATUSES)

    def _scope_attrs_from_initial_for_status(self):
        """Field validators run before `validate()`; resolve scope FKs from raw input for status checks."""
        attrs = {}
        initial = getattr(self, "initial_data", None)
        if not initial:
            return attrs
        if "po_type" in initial:
            attrs["po_type"] = initial.get("po_type")
        raw_project = initial.get("project")
        if raw_project is not None:
            try:
                attrs["project"] = Project.objects.get(pk=raw_project)
            except (Project.DoesNotExist, TypeError, ValueError):
                pass
        raw_company = initial.get("company_profile")
        if raw_company is not None:
            try:
                attrs["company_profile"] = CompanyProfile.objects.get(pk=raw_company)
            except (CompanyProfile.DoesNotExist, TypeError, ValueError):
                pass
        raw_freelancer = initial.get("freelancer")
        if raw_freelancer is not None:
            try:
                attrs["freelancer"] = User.objects.get(pk=raw_freelancer)
            except (User.DoesNotExist, TypeError, ValueError):
                pass
        return attrs

    def _resolved_contact(self, attrs, instance, user_field, company_field):
        initial = getattr(self, "initial_data", None) or {}
        user = attrs.get(user_field) if user_field in attrs else None
        company = attrs.get(company_field) if company_field in attrs else None

        if user_field not in attrs and company_field not in attrs:
            if user_field in initial:
                raw = initial.get(user_field)
                user = (
                    ContactUser.objects.filter(pk=raw).first()
                    if raw not in (None, "")
                    else None
                )
            if company_field in initial:
                raw = initial.get(company_field)
                company = (
                    ContactCompany.objects.filter(pk=raw).first()
                    if raw not in (None, "")
                    else None
                )

        if user is None and company is None and instance is not None:
            user = getattr(instance, user_field, None)
            company = getattr(instance, company_field, None)

        return user, company

    def _validate_contact_pair(
        self,
        *,
        user,
        company,
        user_field,
        company_field,
        label,
        required,
        request_user,
    ):
        if user and company:
            raise serializers.ValidationError(
                {
                    user_field: (
                        f"{label} can have either a contact user or contact company."
                    )
                }
            )

        if required and not user and not company:
            raise serializers.ValidationError(
                {
                    user_field: (
                        f"{label} must have either a contact user or contact company."
                    )
                }
            )

        if not request_user:
            return

        if getattr(request_user, "is_freelancer", False):
            if company and company.user != request_user:
                raise serializers.ValidationError(
                    {company_field: "Cannot add other freelancer contact on purchase order."}
                )
            if user and user.user != request_user:
                raise serializers.ValidationError(
                    {
                        user_field: (
                            f"Contact {user.id} does not belong to this user."
                        )
                    }
                )
        else:
            if company and company.company_profile != request_user.company:
                raise serializers.ValidationError(
                    {
                        company_field: (
                            f"Contact {company.id} does not belong to this user."
                        )
                    }
                )
            if user and user.company_profile != request_user.company:
                raise serializers.ValidationError(
                    {
                        user_field: (
                            f"Contact {user.id} does not belong to this user."
                        )
                    }
                )

    def _serialize_contact_ref(self, instance, user_field, company_field):
        user = getattr(instance, user_field, None)
        company = getattr(instance, company_field, None)
        if user:
            return {
                user_field: {
                    "id": user.id,
                    "name": f"{user.name} {user.surname}".strip(),
                },
                company_field: None,
            }
        if company:
            return {
                user_field: None,
                company_field: {
                    "id": company.id,
                    "name": company.company_name,
                },
            }
        return {user_field: None, company_field: None}

    def validate_status(self, value):
        if not value:
            return value
        attrs = self._scope_attrs_from_initial_for_status()
        valid_choices = self._get_valid_status_choices(attrs)
        if value not in valid_choices:
            raise serializers.ValidationError(f"Invalid Purchase Order Status. Allowed choices are: {', '.join(valid_choices)}")
        return value

    def _resolved_po_project_id_for_documents(self):
        """PO project PK from this request (or existing instance). Used to allow project-folder documents."""
        initial = getattr(self, "initial_data", None) or {}
        if "project" in initial:
            raw = initial.get("project")
            if raw in (None, ""):
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        if self.instance is not None and self.instance.project_id:
            return self.instance.project_id
        return None

    def validate_documents(self, documents):
        request_user = self.context["request"].user
        if not documents:
            return documents
        if request_user.is_freelancer:
            for document in documents:
                if document.folder.freelancer and document.folder.freelancer.id != request_user.id:
                    raise serializers.ValidationError(
                        f"Document {document.id} is not linked to this freelancer."
                    )
        else:
            company_id = getattr(request_user.company, "id", None)
            if not company_id:
                raise serializers.ValidationError(
                    "Cannot validate documents without a company on the current user."
                )
            po_project_id = self._resolved_po_project_id_for_documents()
            for document in documents:
                folder = document.folder
                if folder.company_profile_id and folder.company_profile_id == company_id:
                    continue
                if po_project_id and folder.project_id == po_project_id:
                    project = folder.project
                    if (
                        project
                        and project.company_profile_id
                        and project.company_profile_id == company_id
                    ):
                        continue
                raise serializers.ValidationError(
                    f"Document {document.id} is not linked to your company."
                )
        return documents

    def get_linked_payment_milestone_labels(self, obj):
        return list(
            self._get_linked_payment_project_phases(obj).values_list("name", flat=True)
        )

    def get_linked_payment_milestone_count(self, obj):
        return self._get_linked_payment_project_phases(obj).count()

    def _get_line_items(self, obj):
        return obj.line_items.all()

    def get_line_item_labels(self, obj):
        labels = []
        for item in self._get_line_items(obj):
            name = (item.item_name or "").strip()
            if not name and item.available_product_id:
                product = item.available_product
                name = (
                    getattr(product, "name", None)
                    or getattr(product, "part_number", None)
                    or ""
                ).strip()
            labels.append(name or f"Line {item.line_number}")
        return labels

    def get_line_item_count(self, obj):
        return self._get_line_items(obj).count()

    def get_po_pdf(self, obj):
        if obj.po_type != PurchaseOrder.POType.OUTGOING:
            return None

        if (
            obj.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE
            and obj.source_po_id
        ):
            doc = get_stored_change_notice_pdf_document(obj)
            if not doc or not doc.file:
                doc = (
                    obj.documents.filter(file__isnull=False)
                    .exclude(file="")
                    .first()
                )
        else:
            doc = get_stored_purchase_order_pdf_document(obj)
            if not doc or not doc.file:
                doc = (
                    obj.documents.filter(file__isnull=False)
                    .exclude(file="")
                    .first()
                )

        if not doc or not doc.file:
            return None
        url = doc.file.url
        request = self.context.get("request")
        if request and url and not url.startswith(("http://", "https://")):
            return request.build_absolute_uri(url)
        return url

    def _serialize_po_link(self, po, relation_type):
        if not po:
            return None
        po_date = po.po_date.isoformat() if po.po_date else None
        chg = po.change_amount_excl_vat
        return {
            "id": po.id,
            "reference_number": po.reference,
            "document_type": po.document_type,
            "status": po.status,
            "grand_total": float(po.amount_incl_vat)
            if po.amount_incl_vat is not None
            else None,
            "invoice_date": po_date,
            "relation_type": relation_type,
            "amount_excl_vat": float(po.amount_excl_vat)
            if po.amount_excl_vat is not None
            else None,
            "vat_rate": float(po.vat_rate) if po.vat_rate is not None else None,
            "vat_amount": float(po.vat_amount) if po.vat_amount is not None else None,
            "change_amount_excl_vat": float(chg) if chg is not None else None,
        }

    def _ancestor_chain_root_first(self, instance):
        chain_rev = []
        cur_id = instance.source_po_id
        guard = 0
        while cur_id and guard < 64:
            try:
                src = PurchaseOrder.objects.get(pk=cur_id)
            except PurchaseOrder.DoesNotExist:
                break
            chain_rev.append(src)
            cur_id = src.source_po_id
            guard += 1
        return list(reversed(chain_rev))

    @staticmethod
    def _descendants_change_notices(root):
        out = []
        seen = {root.id}
        queue = list(root.change_notices.all().order_by("po_date", "id"))
        while queue:
            child = queue.pop(0)
            if child.id in seen:
                continue
            seen.add(child.id)
            out.append(child)
            queue.extend(
                ch
                for ch in child.change_notices.all().order_by("po_date", "id")
                if ch.id not in seen
            )
        return out

    @staticmethod
    def _amount_base_for_new_change_notice(source_po: PurchaseOrder) -> PurchaseOrder:
        """
        PO/CN row whose excl-VAT total we extend when applying a new delta.

        If the client selects the original PO as source for every notice, we still
        stack on the latest change notice under that root (chronological tip).
        If they select a specific change notice as source, that row is the base.
        """
        if source_po.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
            return source_po
        descendants = PurchaseOrderSerializer._descendants_change_notices(source_po)
        if not descendants:
            return source_po
        return max(descendants, key=lambda p: (p.po_date or date.min, p.id))

    @staticmethod
    def _root_purchase_order(po: PurchaseOrder) -> PurchaseOrder:
        root = po
        guard = 0
        while root.source_po_id and guard < 64:
            next_root = getattr(root, "source_po", None)
            if next_root is None:
                next_root = PurchaseOrder.objects.filter(pk=root.source_po_id).first()
            if next_root is None:
                break
            root = next_root
            guard += 1
        return root

    @staticmethod
    def _tip_before_change_notice(cn: PurchaseOrder) -> PurchaseOrder | None:
        """
        Totals tip immediately before this change notice was applied
        (prior CN chronologically, else the root PO).
        """
        if cn.document_type != PurchaseOrder.DocumentType.CHANGE_NOTICE:
            return None
        source = getattr(cn, "source_po", None)
        if source is None and cn.source_po_id:
            source = PurchaseOrder.objects.filter(pk=cn.source_po_id).first()
        if source is None:
            return None

        root = PurchaseOrderSerializer._root_purchase_order(source)
        earlier = [
            d
            for d in PurchaseOrderSerializer._descendants_change_notices(root)
            if d.id != cn.id
            and (d.po_date or date.min, d.id) < (cn.po_date or date.min, cn.id)
        ]
        if not earlier:
            return root
        return max(earlier, key=lambda p: (p.po_date or date.min, p.id))

    def get_change_vat_amount(self, obj):
        """Delta VAT vs prior tip (items-based)"""
        if obj.document_type != PurchaseOrder.DocumentType.CHANGE_NOTICE:
            return None
        prior = PurchaseOrderSerializer._tip_before_change_notice(obj)
        if prior is None:
            return None
        return float(
            _quantize_money((obj.vat_amount or 0) - (prior.vat_amount or 0))
        )

    def get_change_incl_vat(self, obj):
        """Delta incl. VAT vs prior tip (items-based)."""
        if obj.document_type != PurchaseOrder.DocumentType.CHANGE_NOTICE:
            return None
        prior = PurchaseOrderSerializer._tip_before_change_notice(obj)
        if prior is None:
            return None
        return float(
            _quantize_money(
                (obj.amount_incl_vat or 0) - (prior.amount_incl_vat or 0)
            )
        )

    @staticmethod
    def _get_effective_line_items_for_change_notice(source_po: PurchaseOrder):
        """Line items reflecting current PO item state before a new change notice."""
        base = PurchaseOrderSerializer._amount_base_for_new_change_notice(source_po)
        items = list(base.line_items.order_by("line_number", "id"))
        if items:
            return items
        root = source_po
        guard = 0
        while root.source_po_id and guard < 64:
            root = (
                PurchaseOrder.objects.select_related("source_po")
                .prefetch_related("line_items")
                .get(pk=root.source_po_id)
            )
            guard += 1
        return list(root.line_items.order_by("line_number", "id"))

    def get_effective_amount_excl_vat(self, obj):
        if obj.po_type != PurchaseOrder.POType.OUTGOING:
            return float(obj.amount_excl_vat or 0)
        if obj.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
            return float(obj.amount_excl_vat or 0)
        base = PurchaseOrderSerializer._amount_base_for_new_change_notice(obj)
        return float(base.amount_excl_vat or 0)

    def get_effective_line_items(self, obj):
        if obj.po_type != PurchaseOrder.POType.OUTGOING:
            return []
        if obj.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
            items = obj.line_items.order_by("line_number", "id")
        else:
            items = PurchaseOrderSerializer._get_effective_line_items_for_change_notice(
                obj
            )
        return PurchaseOrderLineItemSerializer(items, many=True).data

    def get_linked_invoices(self, obj):
        """Invoices and credit notes linked to this PO (billing.Invoice.purchase_order)."""
        if not getattr(obj, "pk", None):
            return []
        qs = obj.invoices.filter(
            document_type__in=[
                Invoice.DocumentType.INVOICE,
                Invoice.DocumentType.CREDIT_NOTE,
            ],
        ).order_by("-invoice_date", "-id")
        return [
            {
                "id": inv.id,
                "reference_number": inv.reference_number or str(inv.id),
                "document_type": inv.document_type,
                "status": inv.status,
                "grand_total": float(inv.grand_total)
                if inv.grand_total is not None
                else None,
                "invoice_date": inv.invoice_date.isoformat()
                if inv.invoice_date
                else None,
                "relation_type": None,
            }
            for inv in qs
        ]

    def get_linked_po_documents(self, obj):
        """Related PO / change notices (ancestors toward root + descendants), for outgoing docs only."""
        if obj.po_type != PurchaseOrder.POType.OUTGOING:
            return []

        candidates = []
        seen = set()
        for anc in self._ancestor_chain_root_first(obj):
            if anc.id in seen:
                continue
            rel = (
                "source_purchase_order"
                if anc.document_type == PurchaseOrder.DocumentType.PURCHASE_ORDER
                else "prior_change_notice"
            )
            candidates.append((anc, rel))
            seen.add(anc.id)
        for desc in self._descendants_change_notices(obj):
            if desc.id in seen:
                continue
            candidates.append((desc, "change_notice"))
            seen.add(desc.id)

        request = self.context.get("request")
        user = getattr(request, "user", None)
        permission = PurchaseOrderPermission(action="view", scope="company")
        view_scope = getattr(self, "_purchase_order_view_scope", None)
        if view_scope is None:
            view_scope = permission.get_view_scope(user)
            self._purchase_order_view_scope = view_scope
        visible_ids = set(
            permission.filter_queryset_for_view(
                PurchaseOrder.objects.filter(
                    pk__in=[purchase_order.pk for purchase_order, _ in candidates]
                ),
                user,
                view_scope=view_scope,
            ).values_list("pk", flat=True)
        )

        linked = []
        for purchase_order, relation_type in candidates:
            if purchase_order.pk not in visible_ids:
                continue
            serialized = self._serialize_po_link(
                purchase_order,
                relation_type,
            )
            if serialized:
                linked.append(serialized)
        return linked

    def _get_linked_payment_project_phases(self, instance):
        try:
            return instance.linked_payment_project_phases.all()
        except (ProgrammingError, OperationalError):
            return ProjectPhase.objects.none()

    def _sync_linked_payment_project_phases(self, po, phase_ids):
        """Link ProjectPhase rows to this PO via M2M; clears legacy payment_milestones."""
        normalized = []
        for x in phase_ids or []:
            try:
                normalized.append(int(x))
            except (TypeError, ValueError):
                continue
        normalized = list(dict.fromkeys(normalized))
        project = po.project
        if not project:
            if normalized:
                raise serializers.ValidationError(
                    {
                        "linked_payment_milestones": (
                            "Company-level purchase orders cannot link payment milestones."
                        )
                    }
                )
            return

        try:
            current_ids = set(
                po.linked_payment_project_phases.values_list("id", flat=True)
            )
        except (ProgrammingError, OperationalError):
            current_ids = set()

        desired = set(normalized)
        to_detach = current_ids - desired
        to_attach = desired - current_ids

        if to_detach:
            ProjectPhase.objects.filter(
                id__in=to_detach,
                purchase_order_id=po.id,
            ).update(purchase_order=None)

        if to_attach:
            phases = list(ProjectPhase.objects.filter(id__in=to_attach))
            wrong = [p.id for p in phases if p.project_id != project.id]
            if wrong or len(phases) != len(to_attach):
                raise serializers.ValidationError(
                    {
                        "linked_payment_milestones": (
                            "All linked payment milestones must belong to the selected project."
                        )
                    }
                )
            for phase in phases:
                raise_if_phase_linked_to_other_po(
                    phase=phase,
                    target_po=po,
                    field="linked_payment_milestones",
                )
                phase.purchase_order = po
                phase.save(update_fields=["purchase_order"])

        try:
            po.payment_milestones.clear()
        except (ProgrammingError, OperationalError):
            pass

    def validate(self, attrs):
        instance = self.instance
        po_type = attrs.get("po_type", instance.po_type if instance else PurchaseOrder.POType.INCOMING)
        if po_type == PurchaseOrder.POType.INCOMING:
            attrs["document_type"] = PurchaseOrder.DocumentType.PURCHASE_ORDER

        document_type = attrs.get(
            "document_type",
            instance.document_type if instance else PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        source_po = attrs.get("source_po")
        change_delta = attrs.get("change_amount_excl_vat")

        if document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
            if po_type != PurchaseOrder.POType.OUTGOING:
                raise serializers.ValidationError(
                    {"document_type": "Change notices are only valid for outgoing purchase orders."}
                )
            if not source_po:
                raise serializers.ValidationError(
                    {"source_po": "Source PO is required for a change notice."}
                )

            effective_items = (
                PurchaseOrderSerializer._get_effective_line_items_for_change_notice(
                    source_po
                )
            )
            initial = getattr(self, "initial_data", None) or {}
            line_items_in_payload = "line_items" in initial
            line_items_data = attrs.get("line_items") if line_items_in_payload else None
            amount_base = PurchaseOrderSerializer._amount_base_for_new_change_notice(
                source_po
            )

            if effective_items:
                if not line_items_data:
                    raise serializers.ValidationError(
                        {
                            "line_items": (
                                "Line items are required when the source PO has items."
                            )
                        }
                    )
                self._validate_change_notice_line_items(
                    effective_items, line_items_data
                )
            else:
                if line_items_data:
                    raise serializers.ValidationError(
                        {
                            "line_items": (
                                "Line items cannot be provided when the source PO has no items."
                            )
                        }
                    )

            # Change amount is derived from items when present (items are source of truth).
            if line_items_data:
                items_excl = self._sum_line_items_excl_vat(line_items_data)
                change_delta = _quantize_money(
                    items_excl - amount_base.amount_excl_vat
                )
            elif change_delta is None:
                raise serializers.ValidationError(
                    {
                        "change_amount_excl_vat": (
                            "Change amount (excl. VAT) is required."
                        )
                    }
                )
            else:
                change_delta = _quantize_money(change_delta)

            new_excl = amount_base.amount_excl_vat + change_delta
            if new_excl < 0:
                raise serializers.ValidationError(
                    {
                        "change_amount_excl_vat": (
                            "The change would make the PO amount excluding VAT negative."
                        )
                    }
                )

            ctx_project = self.context.get("project")
            ctx_company = self.context.get("company_profile")
            request_user = self.context["request"].user

            if source_po.project_id:
                if not ctx_project or source_po.project_id != ctx_project.id:
                    raise serializers.ValidationError(
                        {
                            "source_po": (
                                "Source PO must belong to the same project you are creating this change notice for."
                            )
                        }
                    )
            else:
                if ctx_project is not None:
                    raise serializers.ValidationError(
                        {
                            "source_po": (
                                "This source PO is company-level. Create the change notice as a company-level PO "
                                "(no project)."
                            )
                        }
                    )
                if source_po.company_profile_id:
                    if not ctx_company or source_po.company_profile_id != ctx_company.id:
                        raise serializers.ValidationError(
                            {"source_po": "Source PO must belong to your company."}
                        )
                elif source_po.freelancer_id:
                    if (
                        not getattr(request_user, "is_freelancer", False)
                        or source_po.freelancer_id != request_user.id
                    ):
                        raise serializers.ValidationError(
                            {"source_po": "Invalid source PO for this account."}
                        )

            attrs["receiving_user"] = source_po.receiving_user
            attrs["receiving_company"] = source_po.receiving_company
            attrs["amount_excl_vat"] = new_excl
            attrs["change_amount_excl_vat"] = change_delta
            attrs["vat_rate"] = amount_base.vat_rate
        else:
            attrs.pop("source_po", None)
            attrs.pop("change_amount_excl_vat", None)

            # Normal purchase orders: line items are mandatory (totals are derived from them).
            initial = getattr(self, "initial_data", None) or {}
            line_items_payload = attrs.get("line_items")
            if line_items_payload is None and "line_items" in initial:
                line_items_payload = initial.get("line_items")
            if instance is None:
                if not line_items_payload:
                    raise serializers.ValidationError(
                        {"line_items": "At least one line item is required."}
                    )
            elif "line_items" in initial and not line_items_payload:
                raise serializers.ValidationError(
                    {"line_items": "At least one line item is required."}
                )

        if "project" in attrs:
            project = attrs["project"]
        else:
            project = self.context.get("project") or (instance.project if instance else None)
        amount_excl_vat = attrs.get(
            "amount_excl_vat",
            instance.amount_excl_vat if instance else None,
        )
        vat_rate = attrs.get("vat_rate", instance.vat_rate if instance else Decimal("12"))
        if amount_excl_vat is not None and amount_excl_vat < 0:
            raise serializers.ValidationError({"amount_excl_vat": "Amount excluding VAT cannot be negative."})

        if vat_rate is not None and vat_rate < 0:
            raise serializers.ValidationError({"vat_rate": "VAT rate cannot be negative."})

        if vat_rate is not None and vat_rate > 100:
            raise serializers.ValidationError({"vat_rate": "VAT rate cannot be greater than 100."})

        phase_ids = attrs.get("linked_payment_milestones", None)
        if phase_ids is not None:
            if not isinstance(phase_ids, list):
                raise serializers.ValidationError(
                    {"linked_payment_milestones": "Expected a list of payment milestone IDs."}
                )
            if not project and phase_ids:
                raise serializers.ValidationError(
                    {
                        "linked_payment_milestones": (
                            "Company-level purchase orders cannot link payment milestones."
                        )
                    }
                )
            if project and phase_ids:
                phases = ProjectPhase.objects.filter(id__in=phase_ids)
                found = set(phases.values_list("id", flat=True))
                missing = [pk for pk in phase_ids if pk not in found]
                if missing:
                    raise serializers.ValidationError(
                        {
                            "linked_payment_milestones": (
                                f"Invalid payment milestone IDs: {missing}"
                            )
                        }
                    )
                invalid = [p.id for p in phases if p.project_id != project.id]
                if invalid:
                    raise serializers.ValidationError(
                        {
                            "linked_payment_milestones": (
                                "All linked payment milestones must belong to the selected project. "
                                f"Invalid IDs: {invalid}"
                            )
                        }
                    )
                current_po_id = instance.pk if instance else None
                for phase in phases:
                    if phase.purchase_order_id and (
                        not current_po_id
                        or phase.purchase_order_id != current_po_id
                    ):
                        raise serializers.ValidationError(
                            {
                                "linked_payment_milestones": (
                                    MILESTONE_ALREADY_LINKED_TO_ANOTHER_PO
                                )
                            }
                        )
                line_items_for_cap = attrs.get("line_items")
                if line_items_for_cap is None:
                    initial_for_cap = getattr(self, "initial_data", None) or {}
                    if "line_items" in initial_for_cap:
                        line_items_for_cap = initial_for_cap.get("line_items")

                if line_items_for_cap:
                    po_cap = float(self._sum_line_items_incl_vat(line_items_for_cap))
                elif instance is not None and instance.amount_incl_vat is not None:
                    po_cap = float(
                        get_po_revised_amount_incl_vat(instance)
                        if instance.document_type
                        == PurchaseOrder.DocumentType.PURCHASE_ORDER
                        else _quantize_money(instance.amount_incl_vat)
                    )
                else:
                    po_cap = None

                if po_cap is not None:
                    total_milestone_amount = sum(
                        float(phase.amount or 0) for phase in phases
                    )
                    if total_milestone_amount > po_cap:
                        raise serializers.ValidationError(
                            {
                                "linked_payment_milestones": (
                                    "Payment milestones total amount exceeds associated PO value."
                                )
                            }
                        )

        status = attrs.get("status", instance.status if instance else None)
        if status:
            valid_choices = self._get_valid_status_choices(attrs)
            if status not in valid_choices:
                raise serializers.ValidationError(
                    {"status": f"Invalid Purchase Order Status. Allowed choices are: {', '.join(valid_choices)}"}
                )

        request = self.context.get("request")
        request_user = getattr(request, "user", None)
        initial = getattr(self, "initial_data", None) or {}
        contact_fields_in_payload = any(
            key in attrs or key in initial
            for key in (
                "receiving_user",
                "receiving_company",
                "emitting_user",
                "emitting_company",
            )
        )
        is_create = instance is None

        receiving_user, receiving_company = self._resolved_contact(
            attrs,
            instance,
            "receiving_user",
            "receiving_company",
        )
        emitting_user, emitting_company = self._resolved_contact(
            attrs,
            instance,
            "emitting_user",
            "emitting_company",
        )

        if po_type == PurchaseOrder.POType.OUTGOING:
            if is_create or contact_fields_in_payload:
                attrs["emitting_user"] = None
                attrs["emitting_company"] = None
            self._validate_contact_pair(
                user=receiving_user,
                company=receiving_company,
                user_field="receiving_user",
                company_field="receiving_company",
                label="Receiving company",
                required=is_create or contact_fields_in_payload,
                request_user=request_user,
            )
        else:
            if is_create or contact_fields_in_payload:
                attrs["receiving_user"] = None
                attrs["receiving_company"] = None
            self._validate_contact_pair(
                user=emitting_user,
                company=emitting_company,
                user_field="emitting_user",
                company_field="emitting_company",
                label="Emitting company",
                required=is_create or contact_fields_in_payload,
                request_user=request_user,
            )

        return attrs

    def _get_po_reference_owner(self, project):
        project_user = getattr(project, "user", None)
        if not project_user:
            raise serializers.ValidationError(
                "Unable to generate PO reference: project has no owner."
            )
        if getattr(project_user, "is_freelancer", False):
            return None, project_user
        company = getattr(project_user, "company", None)
        if not company:
            raise serializers.ValidationError(
                "Unable to generate PO reference without a company."
            )
        return company, None

    def _get_global_po_reference_max_sequence(
        self,
        *,
        company_profile=None,
        freelancer=None,
    ):
        """Largest numeric suffix among all PO references for this billing owner (any month)."""
        if company_profile:
            pos = PurchaseOrder.objects.filter(
                Q(
                    company_profile=company_profile,
                    project__isnull=True,
                    freelancer__isnull=True,
                )
                | Q(project__user__company=company_profile)
                | Q(project__company_profile=company_profile)
            ).only("reference")
        elif freelancer:
            pos = PurchaseOrder.objects.filter(
                Q(
                    freelancer=freelancer,
                    project__isnull=True,
                    company_profile__isnull=True,
                )
                | Q(project__user=freelancer)
            ).only("reference")
        else:
            return 0
        max_sequence = 0
        for po in pos:
            reference = str(po.reference or "").strip()
            if not reference:
                continue
            try:
                sequence = int(reference.split("-")[-1])
            except (TypeError, ValueError):
                continue
            if sequence > max_sequence:
                max_sequence = sequence
        return max_sequence

    def _generate_po_reference(
        self,
        *,
        po_date,
        prefix=None,
        project=None,
        company_profile=None,
        freelancer=None,
    ):
        if project:
            company_profile, freelancer = self._get_po_reference_owner(project)
        if not company_profile and not freelancer:
            raise serializers.ValidationError(
                "Unable to generate PO reference without a billing owner."
            )
        if prefix is None:
            prefix = POReferenceCounter.Prefix.PO
        display_period = po_date.strftime("%Y%m")
        counter_defaults = {
            "company_profile": company_profile,
            "freelancer": freelancer,
            "period": _PO_REFERENCE_COUNTER_GLOBAL_PERIOD,
            "last_sequence": 0,
        }
        lookup = {
            "company_profile": company_profile,
            "freelancer": freelancer,
            "period": _PO_REFERENCE_COUNTER_GLOBAL_PERIOD,
        }
        with transaction.atomic():
            counter, _ = POReferenceCounter.objects.select_for_update().get_or_create(
                defaults=counter_defaults,
                **lookup,
            )
            existing_max = self._get_global_po_reference_max_sequence(
                company_profile=company_profile,
                freelancer=freelancer,
            )
            next_sequence = max(counter.last_sequence, existing_max) + 1
            counter.last_sequence = next_sequence
            counter.save(update_fields=["last_sequence", "updated_at"])
        return f"{prefix}-{display_period}-{next_sequence:05d}"

    def _normalize_line_item_data(self, item_data, default_vat_rate=None):
        normalized = dict(item_data)
        rate = _quantize_money(normalized.get("rate", 0))
        quantity = _quantize_money(normalized.get("quantity", 0))
        vat_rate = normalized.get("vat_rate")
        if vat_rate is None:
            vat_rate = (
                _quantize_money(default_vat_rate)
                if default_vat_rate is not None
                else Decimal("12")
            )
        else:
            vat_rate = _quantize_money(vat_rate)
        total = normalized.get("total")
        if total is None:
            total = _quantize_money(rate * quantity)
        else:
            total = _quantize_money(total)
        normalized["rate"] = rate
        normalized["quantity"] = quantity
        normalized["vat_rate"] = vat_rate
        normalized["total"] = total
        return normalized

    def _line_item_product_id(self, item_data):
        available_product = item_data.get("available_product")
        if available_product is None:
            return None
        return getattr(available_product, "pk", available_product)

    def _validate_change_notice_line_items(self, effective_items, line_items_data):
        if len(line_items_data) != len(effective_items):
            raise serializers.ValidationError(
                {"line_items": "Line item count must match the source PO."}
            )

        effective_by_line = {item.line_number: item for item in effective_items}
        for index, raw in enumerate(line_items_data):
            item_data = self._normalize_line_item_data(raw)
            line_num = raw.get("line_number") or (index + 1)
            source_item = effective_by_line.get(line_num)
            if not source_item:
                raise serializers.ValidationError(
                    {
                        "line_items": (
                            f"Invalid line number {line_num} for change notice items."
                        )
                    }
                )

            new_product_id = self._line_item_product_id(raw)
            if source_item.available_product_id:
                if new_product_id != source_item.available_product_id:
                    raise serializers.ValidationError(
                        {
                            "line_items": (
                                "Change notices cannot add or replace PO line items."
                            )
                        }
                    )
            else:
                new_name = str(item_data.get("item_name") or "").strip()
                source_name = str(source_item.item_name or "").strip()
                if new_name != source_name:
                    raise serializers.ValidationError(
                        {
                            "line_items": (
                                "Change notices cannot add or replace PO line items."
                            )
                        }
                    )

            if item_data["quantity"] < 0:
                raise serializers.ValidationError(
                    {"line_items": "Line item quantity cannot be negative."}
                )
            if item_data["rate"] < 0:
                raise serializers.ValidationError(
                    {"line_items": "Line item unit price cannot be negative."}
                )
            if item_data["total"] < 0:
                raise serializers.ValidationError(
                    {"line_items": "Line item amount cannot be negative."}
                )

    def _sum_line_items_excl_vat(self, line_items_data):
        total = Decimal("0")
        for raw in line_items_data:
            item_data = self._normalize_line_item_data(raw)
            total += item_data["total"]
        return _quantize_money(total)

    def _sum_line_items_incl_vat(self, line_items_data, default_vat_rate=None):
        total = Decimal("0")
        for raw in line_items_data:
            item_data = self._normalize_line_item_data(
                raw,
                default_vat_rate=default_vat_rate,
            )
            subtotal = item_data["total"]
            vat_rate = item_data.get("vat_rate", default_vat_rate or Decimal("12"))
            vat_amount = _quantize_money((subtotal * vat_rate) / Decimal("100"))
            total += _quantize_money(subtotal + vat_amount)
        return _quantize_money(total)

    def _line_items_payload_from_instance(self, instance):
        if instance is None:
            return None
        existing = list(instance.line_items.all())
        if not existing:
            return None
        return [
            {
                "rate": item.rate,
                "quantity": item.quantity,
                "vat_rate": item.vat_rate,
                "total": item.total,
            }
            for item in existing
        ]

    def _apply_vat_calculation(
        self, validated_data, instance=None, line_items_data=None
    ):
        # Ignore client-sent computed fields; backend is source of truth.
        validated_data.pop("vat_amount", None)
        validated_data.pop("amount_incl_vat", None)

        items_for_totals = line_items_data
        if items_for_totals is None:
            items_for_totals = self._line_items_payload_from_instance(instance)

        if not items_for_totals:
            amount_excl_vat = validated_data.get(
                "amount_excl_vat",
                instance.amount_excl_vat if instance else Decimal("0"),
            )
            validated_data["amount_excl_vat"] = _quantize_money(amount_excl_vat or 0)
            if instance is not None and instance.vat_amount is not None:
                validated_data["vat_amount"] = _quantize_money(instance.vat_amount)
                validated_data["amount_incl_vat"] = _quantize_money(
                    instance.amount_incl_vat
                    if instance.amount_incl_vat is not None
                    else validated_data["amount_excl_vat"]
                )
            else:
                validated_data["vat_amount"] = Decimal("0")
                validated_data["amount_incl_vat"] = validated_data["amount_excl_vat"]
            return validated_data

        amount_excl_vat = self._sum_line_items_excl_vat(items_for_totals)
        amount_incl_vat = self._sum_line_items_incl_vat(items_for_totals)
        vat_amount = _quantize_money(amount_incl_vat - amount_excl_vat)
        validated_data["amount_excl_vat"] = amount_excl_vat
        validated_data["vat_amount"] = vat_amount
        validated_data["amount_incl_vat"] = amount_incl_vat
        return validated_data

    def _sync_line_items(self, purchase_order, line_items_data):
        purchase_order.line_items.all().delete()
        for index, raw in enumerate(line_items_data, start=1):
            item_data = self._normalize_line_item_data(raw)
            available_product = item_data.pop("available_product", None)
            item_data.pop("id", None)
            PurchaseOrderLineItem.objects.create(
                purchase_order=purchase_order,
                available_product=available_product,
                line_number=item_data.pop("line_number", index),
                item_name=item_data.get("item_name") or "",
                rate=item_data["rate"],
                quantity=item_data["quantity"],
                vat_rate=item_data.get("vat_rate", Decimal("12")),
                total=item_data["total"],
            )

    def _regenerate_po_pdf_change_notice(self, po):
        create_pdf_change_notice_standalone(po, self.context["request"].user)
        if po.source_po_id:
            create_pdf_purchase_order(po.source_po, self.context["request"].user)

    def _regenerate_po_pdf_root_only(self, po):
        create_pdf_purchase_order(po, self.context["request"].user)

    def _regenerate_po_pdf(self, instance):
        po = (
            PurchaseOrder.objects.select_related(
                "project__user__company",
                "company_profile",
                "freelancer",
                "source_po",
            )
            .prefetch_related("linked_payment_project_phases")
            .get(pk=instance.pk)
        )
        if po.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
            self._regenerate_po_pdf_change_notice(po)
        else:
            self._regenerate_po_pdf_root_only(po)

    @transaction.atomic
    def create(self, validated_data):
        line_items_data = validated_data.pop("line_items", None)
        phase_ids = validated_data.pop("linked_payment_milestones", None)
        linked_folders = validated_data.pop("linked_folders", [])
        if phase_ids is None:
            phase_ids = []

        document_type = validated_data.get("document_type")
        po_date = validated_data.get("po_date")
        validated_data.pop("reference", None)

        if document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
            source = validated_data["source_po"]
            delta = validated_data.pop("change_amount_excl_vat")
            amount_base = PurchaseOrderSerializer._amount_base_for_new_change_notice(
                source
            )
            validated_data["amount_excl_vat"] = amount_base.amount_excl_vat + delta
            validated_data["change_amount_excl_vat"] = delta
            validated_data["currency"] = source.currency
            validated_data["vat_rate"] = source.vat_rate
            validated_data["project"] = source.project
            validated_data["company_profile"] = source.company_profile
            validated_data["freelancer"] = source.freelancer
            validated_data = self._apply_vat_calculation(
                validated_data, line_items_data=line_items_data
            )

            chn_prefix = POReferenceCounter.Prefix.CHN
            if source.project_id:
                validated_data["reference"] = self._generate_po_reference(
                    project=source.project,
                    po_date=po_date,
                    prefix=chn_prefix,
                )
            elif source.company_profile_id:
                validated_data["reference"] = self._generate_po_reference(
                    po_date=po_date,
                    company_profile=source.company_profile,
                    prefix=chn_prefix,
                )
            elif source.freelancer_id:
                validated_data["reference"] = self._generate_po_reference(
                    po_date=po_date,
                    freelancer=source.freelancer,
                    prefix=chn_prefix,
                )
            else:
                raise serializers.ValidationError(
                    "Unable to determine PO reference owner for this change notice."
                )
            validated_data["created_by"] = self.context["request"].user
            po = super().create(validated_data)
            self.set_linked_folders(po, linked_folders)
            if line_items_data is not None:
                self._sync_line_items(po, line_items_data)
            self._regenerate_po_pdf(po)
            root = (
                PurchaseOrder.objects.prefetch_related("linked_payment_project_phases")
                .get(pk=source.pk)
            )
            if is_po_payment_milestones_inconsistent_with_revised_amount(root):
                self._response_warnings = [
                    {
                        "code": PAYMENT_MILESTONE_PO_INCONSISTENCY_WARNING,
                        "message": PAYMENT_MILESTONE_PO_INCONSISTENCY_MESSAGE,
                    }
                ]
            return po

        validated_data = self._apply_vat_calculation(
            validated_data, line_items_data=line_items_data
        )
        project = self.context.get("project")
        company_profile_ctx = self.context.get("company_profile")

        if project:
            validated_data["reference"] = self._generate_po_reference(
                project=project,
                po_date=po_date,
            )
            validated_data["project"] = project
            validated_data["company_profile"] = None
            validated_data["freelancer"] = None
        else:
            if company_profile_ctx is None:
                raise serializers.ValidationError(
                    "Company profile is required for company-level purchase orders."
                )
            validated_data["reference"] = self._generate_po_reference(
                po_date=po_date,
                company_profile=company_profile_ctx,
            )
            validated_data["project"] = None
            validated_data["company_profile"] = company_profile_ctx
            validated_data["freelancer"] = None

        validated_data["created_by"] = self.context["request"].user
        instance = super().create(validated_data)
        self.set_linked_folders(instance, linked_folders)
        self._sync_linked_payment_project_phases(instance, phase_ids)
        if line_items_data is not None:
            self._sync_line_items(instance, line_items_data)
        self._regenerate_po_pdf(instance)
        return instance

    def update(self, instance, validated_data):
        line_items_data = validated_data.pop("line_items", None)
        phase_ids = validated_data.pop("linked_payment_milestones", None)
        linked_folders = validated_data.pop("linked_folders", None)
        validated_data.pop("reference", None)
        validated_data.pop("source_po", None)
        validated_data.pop("change_amount_excl_vat", None)
        if validated_data.get("project") is not None:
            validated_data["company_profile"] = None
            validated_data["freelancer"] = None
        validated_data = self._apply_vat_calculation(
            validated_data, instance=instance, line_items_data=line_items_data
        )
        instance = super().update(instance, validated_data)
        if phase_ids is not None:
            self._sync_linked_payment_project_phases(instance, phase_ids)
        self.set_linked_folders(instance, linked_folders)
        if line_items_data is not None:
            self._sync_line_items(instance, line_items_data)
        self._regenerate_po_pdf(instance)
        return instance

    def to_representation(self, instance):
        try:
            representation = super().to_representation(instance)
        except (ProgrammingError, OperationalError):
            # linked_payment_milestones field access can fail before migration is applied.
            representation = {
                "id": instance.id,
                "project": instance.project_id,
                "project_name": instance.project.project_name if instance.project_id else None,
                "po_type": getattr(instance, "po_type", None),
                "document_type": getattr(instance, "document_type", None),
                "source_po": getattr(instance, "source_po_id", None),
                "change_amount_excl_vat": getattr(instance, "change_amount_excl_vat", None),
                "documents": [],
                "linked_payment_milestones": [],
                "linked_payment_milestone_labels": [],
                "linked_payment_milestone_count": 0,
                "line_item_labels": [],
                "line_item_count": 0,
                "linked_payment_project_phases": [],
                "reference": instance.reference,
                "po_date": instance.po_date,
                "currency": instance.currency,
                "status": instance.status,
                "notes": getattr(instance, "notes", None),
                "amount_excl_vat": instance.amount_excl_vat,
                "vat_rate": instance.vat_rate,
                "vat_amount": instance.vat_amount,
                "amount_incl_vat": instance.amount_incl_vat,
                "po_pdf": None,
                "linked_po_documents": [],
                "linked_folders": [],
                "linked_folder_details": [],
                "linked_invoices": [],
                "created_by": instance.created_by_id,
                "created_at": instance.created_at,
                "updated_at": instance.updated_at,
            }
            warnings = getattr(self, "_response_warnings", None)
            if warnings:
                representation["warnings"] = warnings
            return representation

        from project_management.serializers.document import DocumentSerializer

        representation["documents"] = DocumentSerializer(
            instance.documents.all(),
            many=True,
            context=self.context,
        ).data

        phase_qs = instance.linked_payment_project_phases.all().select_related(
            "project", "created_by"
        )
        po_vat = float(instance.vat_rate) if instance.vat_rate is not None else 0.0
        representation["linked_payment_milestones"] = [
            {
                "id": phase.id,
                "name": phase.name,
                "due_date": phase.date.date().isoformat() if phase.date else None,
                "allocated_budget": phase.amount,
                "vat_rate": po_vat,
                "status": phase.status,
                "created_by": (
                    {
                        "id": phase.created_by_id,
                        "name": phase.created_by.full_name,
                    }
                    if phase.created_by_id and phase.created_by
                    else None
                ),
                "project": {
                    "id": phase.project_id,
                    "project_name": phase.project.project_name,
                },
            }
            for phase in phase_qs
        ]

        representation["linked_payment_project_phases"] = [
            {
                "id": phase.id,
                "name": phase.name,
                "date": phase.date,
                "amount": phase.amount,
                "status": phase.status,
                "project": {
                    "id": phase.project_id,
                    "project_name": phase.project.project_name,
                },
            }
            for phase in instance.linked_payment_project_phases.all().select_related(
                "project"
            )
        ]

        representation["linked_invoices"] = self.get_linked_invoices(instance)

        representation.update(
            self._serialize_contact_ref(
                instance, "receiving_user", "receiving_company"
            )
        )
        representation.update(
            self._serialize_contact_ref(instance, "emitting_user", "emitting_company")
        )

        warnings = getattr(self, "_response_warnings", None)
        if warnings:
            representation["warnings"] = warnings

        return representation


PAYMENT_MILESTONE_PO_INCONSISTENCY_WARNING = "payment_milestones_inconsistent_with_po"
PAYMENT_MILESTONE_PO_INCONSISTENCY_MESSAGE = (
    "Inconsistency between PO and Payment Milestone amounts. "
    "Please ensure that the Payment Milestone amounts are adjusted accordingly."
)


def is_po_payment_milestones_inconsistent_with_revised_amount(
    po: PurchaseOrder,
) -> bool:
    """True when linked payment milestones exist but no longer match revised PO TTC."""
    if po.po_type != PurchaseOrder.POType.OUTGOING:
        return False
    if po.document_type != PurchaseOrder.DocumentType.PURCHASE_ORDER:
        return False

    phases = list(po.linked_payment_project_phases.all())
    if not phases:
        return False

    milestone_sum = sum(float(phase.amount or 0) for phase in phases)
    revised = float(get_po_revised_amount_incl_vat(po))
    return float(_quantize_money(milestone_sum)) > revised


def get_po_revised_amount_incl_vat(po: PurchaseOrder) -> Decimal:
    """Root PO total incl. VAT after stacking linked change notices (outgoing POs)."""
    if po.po_type != PurchaseOrder.POType.OUTGOING:
        return _quantize_money(po.amount_incl_vat or 0)

    descendants = PurchaseOrderSerializer._descendants_change_notices(po)
    if not descendants:
        return _quantize_money(po.amount_incl_vat or 0)

    sorted_notices = sorted(
        descendants, key=lambda row: (row.po_date or date.min, row.id)
    )
    latest = sorted_notices[-1]
    if latest.amount_incl_vat is not None:
        return _quantize_money(latest.amount_incl_vat)
    return _quantize_money(po.amount_incl_vat or 0)


def validate_milestone_amount_against_po_limit(
    *,
    purchase_order,
    amount,
    exclude_phase_id=None,
):
    if not purchase_order:
        return

    try:
        milestone_amount = float(amount or 0)
    except (TypeError, ValueError):
        return

    if milestone_amount <= 0:
        return

    po_max = float(get_po_revised_amount_incl_vat(purchase_order))
    others_qs = ProjectPhase.objects.filter(purchase_order_id=purchase_order.pk)
    if exclude_phase_id:
        others_qs = others_qs.exclude(pk=exclude_phase_id)

    other_sum = sum(float(phase.amount or 0) for phase in others_qs)
    projected = float(_quantize_money(other_sum + milestone_amount))

    if projected > po_max:
        raise serializers.ValidationError({"amount": MILESTONE_AMOUNT_PO_INCONSISTENCY})
