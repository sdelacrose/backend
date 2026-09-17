from decimal import Decimal, InvalidOperation

from django.db.models import Q
from rest_framework import serializers

from models.models import SupplierPaymentMethod
from project_management.models import PartPurchaseIntent


class SupplierPaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierPaymentMethod
        fields = [
            "id",
            "name",
            "method_type",
            "display_details",
            "reference",
        ]


class PartPurchaseIntentSerializer(serializers.ModelSerializer):
    buyer_name = serializers.SerializerMethodField()
    buyer_email = serializers.SerializerMethodField()
    buyer_company_name = serializers.SerializerMethodField()

    class Meta:
        model = PartPurchaseIntent
        fields = [
            "id",
            "reference",
            "buyer",
            "buyer_name",
            "buyer_email",
            "buyer_company",
            "buyer_company_name",
            "status",
            "source",
            "source_product_id",
            "project",
            "supplier",
            "currency",
            "price_status",
            "unit_price",
            "total_amount",
            "items",
            "product_snapshot",
            "general_information",
            "delivery_method",
            "delivery_address",
            "invoice_address",
            "payment_snapshot",
            "code_sent_at",
            "code_expires_at",
            "code_verified_at",
            "confirmed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "reference",
            "buyer",
            "buyer_name",
            "buyer_email",
            "buyer_company",
            "buyer_company_name",
            "status",
            "code_sent_at",
            "code_expires_at",
            "code_verified_at",
            "confirmed_at",
            "created_at",
            "updated_at",
        ]

    def get_buyer_name(self, obj):
        first_name = getattr(obj.buyer, "first_name", "") or ""
        last_name = getattr(obj.buyer, "last_name", "") or ""
        return f"{first_name} {last_name}".strip() or obj.buyer.email

    def get_buyer_email(self, obj):
        return obj.buyer.email

    def get_buyer_company_name(self, obj):
        return getattr(obj.buyer_company, "company_name", None)

    def validate_items(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError("At least one cart item is required.")
        for item in value:
            if not isinstance(item, dict):
                raise serializers.ValidationError("Each cart item must be an object.")
            quantity = item.get("quantity", 1)
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                raise serializers.ValidationError("Cart item quantity must be a positive number.")
            if quantity < 1:
                raise serializers.ValidationError("Cart item quantity must be at least 1.")
        return value

    def validate_product_snapshot(self, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("Product snapshot must be an object.")
        return value

    def validate_general_information(self, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("General purchasing information must be an object.")
        return value

    def validate_delivery_address(self, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("Delivery address must be an object.")
        return value

    def validate_invoice_address(self, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("Invoice address must be an object.")
        return value

    def validate_payment_snapshot(self, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("Payment snapshot must be an object.")
        payment_method_id = value.get("payment_method_id")
        if payment_method_id in (None, ""):
            return value

        request = self.context.get("request")
        company = getattr(getattr(request, "user", None), "company", None)
        queryset = SupplierPaymentMethod.objects.filter(is_active=True)
        if company:
            queryset = queryset.filter(
                Q(company_profile=company) | Q(company_profile__isnull=True)
            )
        else:
            queryset = queryset.filter(company_profile__isnull=True)

        if not queryset.filter(id=payment_method_id).exists():
            raise serializers.ValidationError("Selected payment method is not available.")
        return value

    def validate(self, attrs):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from project_management.services.engineering_approval import validate_purchase_items
        project = attrs.get("project", getattr(self.instance, "project", None))
        from project_management.project_access import accessible_projects
        user = getattr(self.context.get("request"), "user", None)
        if not user or not project or not accessible_projects(user).filter(pk=project.pk).exists():
            raise serializers.ValidationError({"project": "Project is unavailable."})
        items = attrs.get("items", getattr(self.instance, "items", []))
        try:
            validate_purchase_items(project=project, items=items)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"items": exc.messages})
        unit_price = attrs.get("unit_price", getattr(self.instance, "unit_price", None))
        items = attrs.get("items", getattr(self.instance, "items", []))
        if unit_price is not None and items:
            try:
                total_quantity = sum(int(item.get("quantity", 1)) for item in items)
                attrs["total_amount"] = Decimal(unit_price) * Decimal(total_quantity)
            except (InvalidOperation, TypeError, ValueError):
                pass
        elif items:
            total_amount = Decimal("0")
            has_priced_items = False
            try:
                for item in items:
                    item_price = item.get("unit_price")
                    if item_price in (None, ""):
                        continue
                    has_priced_items = True
                    item_quantity = int(item.get("quantity", 1))
                    total_amount += Decimal(item_price) * Decimal(item_quantity)
                if has_priced_items:
                    attrs["total_amount"] = total_amount
            except (InvalidOperation, TypeError, ValueError):
                pass
        return attrs


class PartPurchaseCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=6, min_length=6)
