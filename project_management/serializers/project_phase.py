from rest_framework import serializers

from project_management.models import ProjectPhase, PurchaseOrder
from project_management.permissions import PurchaseOrderPermission
from project_management.models.constants import (
    COLLABORATIVE_SOLUTION
)
from project_management.serializers.purchase_order import (
    raise_if_phase_linked_to_other_po,
    validate_milestone_amount_against_po_limit,
)
from .time_expense import get_valid_choices
from ecpmpapp.constants import UserType
from project_management.utils import get_product_name_from_request


class ProjectPhaseSerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

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
        model = ProjectPhase
        fields = '__all__'

    def to_representation(self, instance):
        data = super().to_representation(instance)
        po = instance.purchase_order
        if po is not None:
            po = self.fields["purchase_order"].queryset.filter(pk=po.pk).first()
        po_id = po.id if po else None
        data["purchase_order"] = po_id
        data["purchase_orders"] = [po_id] if po_id else []
        if po is not None:
            data["linked_purchase_orders"] = [
                {
                    "id": po.id,
                    "reference": po.reference or "",
                    "po_type": po.po_type or "",
                }
            ]
        else:
            data["linked_purchase_orders"] = []
        return data

    def _resolve_purchase_order(self, attrs):
        if "purchase_order" in attrs:
            return attrs.get("purchase_order")

        if "purchase_orders" in self.initial_data:
            raw = self.initial_data.get("purchase_orders")
            if raw is None:
                return None
            if not isinstance(raw, list):
                raw = [raw]
            if len(raw) > 1:
                raise serializers.ValidationError(
                    {
                        "purchase_orders": (
                            "A milestone can only be linked to one purchase order at a time."
                        )
                    }
                )
            if not raw:
                return None
            try:
                po_id = int(raw[0])
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"purchase_orders": "Invalid purchase order id."}
                )
            po = self.fields["purchase_order"].queryset.filter(pk=po_id).first()
            if not po:
                raise serializers.ValidationError(
                    {"purchase_orders": "Purchase order not found."}
                )
            return po

        if self.instance is not None:
            return self.instance.purchase_order
        return None

    # TODO: Adjust as per new permission system
    # def validate_project(self, value):
    #     request_user = self.context.get("request").user
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)

    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]

    #     if value.user == request_user and value.solution in products:
    #         return value


    #     if request_user.groups.filter(name=UserType.ADMIN.value).exists():
    #         if value.user.company == request_user.company:
    #             return value

    #     # Check if the request_user is a project manager of the project
    #     team_member = value.team_members.filter(
    #         team_member=request_user, role="project manager", project_access="RW"
    #     ).first()
    #     if team_member:
    #         return value

    #     raise serializers.ValidationError(f"Project {value} does not belong to this user")

    def validate_status(self, value):
        """
        Validates the status field using the valid status options configured in the project.
        The valid status options are retrieved via the get_valid_choices helper function.
        """
        if self.instance:
            project_id = self.instance.project.id
        else:
            project = self.initial_data.get('project')
            # project might be sent as an ID or an object
            project_id = project.id if hasattr(project, 'id') else project

        request = self.context.get('request')
        valid_choices = get_valid_choices("status_options", project_id, request)
        valid_status = {choice.split(":")[0] for choice in valid_choices}

        if valid_status and value not in valid_status:
            raise serializers.ValidationError(
                f"Invalid project phase status. Allowed choices are: {', '.join(valid_status)}"
            )
        return value

    def validate(self, attrs):
        project = attrs.get("project", self.instance.project if self.instance else None)

        if "amount" in attrs:
            amount = attrs["amount"]
        elif self.instance is not None:
            amount = self.instance.amount
        else:
            amount = None

        po = self._resolve_purchase_order(attrs)
        attrs["purchase_order"] = po

        try:
            amount_positive = float(amount or 0) > 0
        except (TypeError, ValueError):
            amount_positive = False

        if not amount_positive:
            attrs["purchase_order"] = None
            return attrs

        if not po:
            return attrs

        if project is None:
            raise serializers.ValidationError(
                {
                    "purchase_orders": (
                        "Cannot link purchase orders to a milestone without a project."
                    )
                }
            )

        if po.project_id is None:
            raise serializers.ValidationError(
                {
                    "purchase_orders": (
                        "Company-level purchase orders cannot be linked to a project milestone."
                    )
                }
            )
        if po.project_id != project.id:
            raise serializers.ValidationError(
                {
                    "purchase_orders": (
                        "The purchase order must belong to the same project as this milestone."
                    )
                }
            )

        if self.instance:
            raise_if_phase_linked_to_other_po(
                phase=self.instance,
                target_po=po,
                field="purchase_orders",
            )

        validate_milestone_amount_against_po_limit(
            purchase_order=po,
            amount=amount,
            exclude_phase_id=self.instance.pk if self.instance else None,
        )

        return attrs

    def create(self, validated_data):
        request_user = self.context['request'].user
        validated_data['created_by'] = request_user
        return super().create(validated_data)
