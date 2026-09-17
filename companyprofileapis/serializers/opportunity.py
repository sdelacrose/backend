from rest_framework import serializers
from django.db import transaction
from django.utils import timezone

from companyprofileapis.models import OfferLineItem, Opportunity
from companyprofileapis.services.offer_line_items import (
    calculate_line_amounts,
    calculate_offer_totals,
)
from companyprofileapis.services.offer_validity import (
    OfferValiditySelectionError,
    calculate_offer_expiry_date,
    offer_created_date_for_record,
    validate_offer_validity_selection,
)
from companyprofileapis.services.opportunity_history import (
    LIFECYCLE_REASON_BY_ACTION,
    OpportunityHistoryReason,
    annotate_latest_opportunity_history,
    prepare_opportunity_history,
)
from companyprofileapis.services.opportunity_lifecycle import (
    FULLY_EDITABLE_OFFER_STATUSES,
    TERMINAL_STATUSES,
    OpportunityLifecycleAction,
    OpportunityLifecycleError,
    target_status_for_action,
)
from companyprofileapis.services.opportunity_references import (
    allocate_opportunity_reference,
    convert_opportunity_reference,
)
from ecpmpapp.constants import UserType
from project_management.models import (
    ActionItem,
    AvailableProductPart,
    Document,
    Project,
)
from project_management.serializers.linked_folder import LinkedFolderSerializerMixin


class OfferLineItemSerializer(serializers.ModelSerializer):
    available_product = serializers.PrimaryKeyRelatedField(
        queryset=AvailableProductPart.objects.all(),
        required=False,
        allow_null=True,
    )
    rate = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
    )
    quantity = serializers.DecimalField(
        max_digits=12,
        decimal_places=4,
        min_value=0,
    )
    vat_rate = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
    )
    vat_amount = serializers.SerializerMethodField()
    total_incl_vat = serializers.SerializerMethodField()

    class Meta:
        model = OfferLineItem
        fields = (
            "id",
            "line_number",
            "available_product",
            "item_name",
            "rate",
            "quantity",
            "vat_rate",
            "total",
            "vat_amount",
            "total_incl_vat",
        )
        read_only_fields = (
            "id",
            "line_number",
            "total",
            "vat_amount",
            "total_incl_vat",
        )

    def validate(self, data):
        if not data.get("available_product") and not data.get("item_name", "").strip():
            raise serializers.ValidationError(
                "Select an Available Product or enter an item name."
            )
        return data

    def get_vat_amount(self, obj):
        _, vat_amount, _ = calculate_line_amounts(
            obj.rate,
            obj.quantity,
            obj.vat_rate,
        )
        return vat_amount

    def get_total_incl_vat(self, obj):
        _, _, total = calculate_line_amounts(
            obj.rate,
            obj.quantity,
            obj.vat_rate,
        )
        return total


class OfferPdfDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ("id", "file", "file_name")
        read_only_fields = fields


class OpportunitySerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    reference = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(
        choices=Opportunity.Status.choices,
        read_only=True,
    )
    value = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        allow_null=True,
        read_only=True,
    )
    offer_created_date = serializers.DateField(read_only=True)
    offer_expiry_period = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    offer_expiry_date = serializers.DateField(read_only=True)
    offer_pdf_document = OfferPdfDocumentSerializer(read_only=True)
    line_items = OfferLineItemSerializer(
        source="offer_line_items",
        many=True,
        required=False,
    )
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    freelancer = serializers.PrimaryKeyRelatedField(read_only=True)
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    approver = serializers.PrimaryKeyRelatedField(read_only=True)
    action_items = serializers.PrimaryKeyRelatedField(
        queryset=ActionItem.objects.all(),
        many=True,
        required=False,
        write_only=True,
    )
    history = serializers.SerializerMethodField()
    linked_projects_count = serializers.SerializerMethodField()
    linked_action_items_count = serializers.SerializerMethodField()

    class Meta:
        model = Opportunity
        fields = "__all__"

    def get_fields(self):
        fields = super().get_fields()
        view = self.context.get("view")
        if getattr(view, "action", None) == "list":
            fields.pop("line_items", None)
        return fields

    def _project_is_accessible(self, project: Project, request_user) -> bool:
        if request_user.is_freelancer:
            if project.user_id == request_user.id:
                return True
        else:
            if project.company_profile_id == request_user.company.id:
                return True

        return project.team_members.filter(team_member=request_user).exists()

    def _action_item_is_accessible(self, action_item: ActionItem, request_user) -> bool:
        if action_item.company_profile_id:
            if request_user.is_freelancer:
                return action_item.freelancer_id == request_user.id
            return action_item.company_profile_id == request_user.company.id

        project = getattr(action_item.milestone, "project", None)
        if not project:
            return False

        return self._project_is_accessible(project, request_user)

    def get_linked_folder_owner_ids(self):
        request_user = self.context["request"].user
        if request_user.is_freelancer:
            return None, None, request_user.id
        return None, request_user.company_id, None

    def validate_sales_phase(self, value):
        from project_management.serializers.time_expense import get_valid_choices
        request = self.context.get('request')
        request_user = request.user
        if request_user.is_freelancer:
            valid_choices = get_valid_choices("opportunity_sales_phase", request_user.id, request, is_freelancer=True)
        else:
            valid_choices = get_valid_choices("opportunity_sales_phase", request_user.company.id, request, is_company=True)

        if valid_choices and value not in valid_choices:
            raise serializers.ValidationError(
                {"sales_phase": f"Invalid sales phase. Allowed choices are: {', '.join(valid_choices)}"}
            )
        return value

    def validate_type_of_service(self, value):
        if not value:
            return value

        from project_management.serializers.time_expense import get_valid_choices

        request = self.context.get('request')
        request_user = request.user
        if request_user.is_freelancer:
            valid_choices = get_valid_choices(
                "project_service_types",
                request_user.id,
                request,
                is_freelancer=True,
            )
        else:
            valid_choices = get_valid_choices(
                "project_service_types",
                request_user.company.id,
                request,
                is_company=True,
            )

        if value not in valid_choices:
            raise serializers.ValidationError(
                {
                    "type_of_service": (
                        "Invalid service type. Allowed choices are: "
                        f"{', '.join(valid_choices)}"
                    )
                }
            )
        return value

    def validate_responsible_salesperson(self, value):
        if not value:
            return value

        request = self.context.get('request')
        request_user = request.user

        if request_user.is_freelancer:
            if value != request_user:
                raise serializers.ValidationError(
                    {"responsible_salesperson": "Freelancers can only assign themselves as responsible salesperson."}
                )
        elif value.company != request_user.company:
            raise serializers.ValidationError(
                {"responsible_salesperson": "The selected responsible salesperson must belong to the same company."}
            )

        return value

    def validate_projects(self, value):
        if not value:
            return value

        request_user = self.context["request"].user

        for project in value:
            if not self._project_is_accessible(project, request_user):
                raise serializers.ValidationError(
                    {
                        "projects": (
                            f"Project {project.project_name} is not accessible to the current user."
                        )
                    }
                )

        return value

    def validate_action_items(self, value):
        if not value:
            return value

        request_user = self.context["request"].user

        for action_item in value:
            if not self._action_item_is_accessible(action_item, request_user):
                raise serializers.ValidationError(
                    {
                        "action_items": (
                            f"Action item {action_item.name} is not accessible to the current user."
                        )
                    }
                )

        return value

    def _validate_offer_line_items(self, data, *, required):
        line_items = data.get("offer_line_items")
        line_items_submitted = "line_items" in (self.initial_data or {})

        if required and not line_items:
            raise serializers.ValidationError(
                {"line_items": "At least one line item is required."}
            )
        if line_items_submitted and not line_items:
            raise serializers.ValidationError(
                {"line_items": "At least one line item is required."}
            )

    def _validate_line_item_ownership(self, data, request_user):
        line_items = data.get("offer_line_items") or []
        for item in line_items:
            product = item.get("available_product")
            if not product:
                continue
            if request_user.is_freelancer:
                raise serializers.ValidationError(
                    {
                        "line_items": (
                            "Available Products cannot be selected for a "
                            "freelancer-owned Offer."
                        )
                    }
                )
            if product.company_profile_id != request_user.company_id:
                raise serializers.ValidationError(
                    {
                        "line_items": (
                            "The selected Available Product must belong to "
                            "the Offer owner's company."
                        )
                    }
                )

    def _validate_client_status_input(self):
        if "status" in (self.initial_data or {}):
            raise serializers.ValidationError(
                {
                    "status": (
                        "Status is controlled by Opportunity and Offer actions "
                        "and cannot be changed directly."
                    )
                }
            )

    def _validate_offer_expiry_input(self, data, instance, lifecycle_action):
        initial_data = self.initial_data or {}
        if "offer_expiry_date" in initial_data or "offer_created_date" in initial_data:
            raise serializers.ValidationError(
                {
                    "offer_expiry_date": {
                        "code": "read_only",
                        "message": "Offer Expiry Date is calculated by the system.",
                    }
                }
            )

        requires_selection = lifecycle_action in {
            OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            OpportunityLifecycleAction.CONVERT_TO_OFFER,
        }
        selection_submitted = "offer_expiry_period" in initial_data

        if not requires_selection and not selection_submitted:
            return

        if instance and instance.status == Opportunity.Status.OPEN and not requires_selection:
            raise serializers.ValidationError(
                {
                    "offer_expiry_period": {
                        "code": "offer_only",
                        "message": "Offer Expiry Period can only be set for an Offer.",
                    }
                }
            )

        try:
            data["offer_expiry_period"] = validate_offer_validity_selection(
                self.context["request"].user,
                self.context.get("request"),
                data.get("offer_expiry_period"),
            )
        except OfferValiditySelectionError as exc:
            raise serializers.ValidationError(
                {
                    "offer_expiry_period": {
                        "code": exc.code,
                        "message": str(exc),
                    }
                }
            ) from exc

    def _apply_lifecycle_action(self, data, instance, lifecycle_action):
        try:
            target_status = target_status_for_action(
                instance.status,
                lifecycle_action,
            )
        except OpportunityLifecycleError as exc:
            raise serializers.ValidationError({"status": str(exc)})

        if lifecycle_action == OpportunityLifecycleAction.CONVERT_TO_OFFER:
            self._validate_offer_line_items(data, required=True)
        elif lifecycle_action == OpportunityLifecycleAction.DECLINE_OFFER:
            if data:
                raise serializers.ValidationError(
                    "Decline Offer does not accept editable fields."
                )
        elif lifecycle_action == OpportunityLifecycleAction.CONVERT_TO_PROJECT:
            if data:
                raise serializers.ValidationError(
                    "Convert to Project does not accept editable fields."
                )
        elif lifecycle_action == OpportunityLifecycleAction.CLOSE:
            unsupported_fields = set(data) - {"close_reason"}
            if unsupported_fields:
                raise serializers.ValidationError(
                    "Close accepts only the close reason."
                )

        data["status"] = target_status

    def _validate_generic_update(self, data, instance):
        if instance.status in TERMINAL_STATUSES:
            raise serializers.ValidationError(
                "Closed and declined records cannot be updated."
            )

        if instance.status == Opportunity.Status.CONVERTED_TO_PROJECT:
            unsupported_fields = set(data) - {
                "sent_date",
                "offer_expiry_period",
            }
            if unsupported_fields:
                raise serializers.ValidationError(
                    "Only Offer Sent Date and Offer Expiry Period can be edited "
                    "after conversion to a project."
                )
            return

        if (
            instance.status == Opportunity.Status.OPEN
            and "line_items" in (self.initial_data or {})
        ):
            raise serializers.ValidationError(
                {"line_items": "Line items can only be added to an Offer."}
            )

        if instance.status in FULLY_EDITABLE_OFFER_STATUSES:
            self._validate_offer_line_items(data, required=False)

    def validate(self, data):
        request_user = self.context['request'].user
        instance = self.instance
        lifecycle_action = self.context.get("lifecycle_action")

        self._validate_client_status_input()
        self._validate_offer_expiry_input(data, instance, lifecycle_action)

        customer_user = data.get('customer_user', getattr(instance, 'customer_user', None))
        customer_company = data.get('customer_company', getattr(instance, 'customer_company', None))

        if customer_user and customer_company:
            raise serializers.ValidationError("Either 'customer_user' or 'customer_company' must be provided.")

        if not customer_user and not customer_company:
            raise serializers.ValidationError("Either 'customer_user' or 'customer_company' must be provided.")

        if instance:
            if lifecycle_action:
                self._apply_lifecycle_action(data, instance, lifecycle_action)
            else:
                self._validate_generic_update(data, instance)
        elif lifecycle_action == OpportunityLifecycleAction.CREATE_DIRECT_OFFER:
            self._validate_offer_line_items(data, required=True)
            data["status"] = Opportunity.Status.DRAFT
        elif lifecycle_action:
            raise serializers.ValidationError(
                "Unsupported lifecycle action during creation."
            )
        else:
            if data.get("offer_line_items") is not None:
                raise serializers.ValidationError(
                    {"line_items": "Line items can only be added to an Offer."}
                )
            data["status"] = Opportunity.Status.OPEN

        self._validate_line_item_ownership(data, request_user)

        # Additional validations for freelancers
        if request_user.is_freelancer:
            if customer_user and customer_user.user != request_user:
                raise serializers.ValidationError("The selected customer_user does not belong to the freelancer.")
            if customer_company and customer_company.user != request_user:
                raise serializers.ValidationError("The selected customer_company does not belong to the freelancer.")

            if 'documents' in data:
                for doc in data['documents']:
                    if doc.folder.freelancer != request_user:
                        raise serializers.ValidationError(f"Document {doc.id} does not belong to the freelancer.")

        # Additional validations for company users
        if not request_user.is_freelancer:
            if customer_user and customer_user.company_profile != request_user.company:
                raise serializers.ValidationError(
                    "The selected customer_user does not belong to the request user's company.")
            if customer_company and customer_company.company_profile != request_user.company:
                raise serializers.ValidationError(
                    "The selected customer_company does not belong to the request user's company.")
            if 'documents' in data:
                for doc in data['documents']:
                    if doc.folder.company_profile != request_user.company:
                        raise serializers.ValidationError(
                            f"Document {doc.id} does not belong to the request user's company.")

        return data

    @transaction.atomic
    def create(self, validated_data):
        request_user = self.context['request'].user
        action_items = validated_data.pop("action_items", None)
        line_items = validated_data.pop("offer_line_items", None)
        validated_data['created_by'] = request_user

        if request_user.is_freelancer:
            validated_data[UserType.FREELANCER.value] = request_user
        else:
            validated_data['company_profile'] = request_user.company
        status = validated_data.get("status", Opportunity.Status.OPEN)
        if status in Opportunity.OFFER_STATUSES:
            offer_created_date = timezone.localdate()
            validated_data["offer_created_date"] = offer_created_date
            validated_data["offer_expiry_date"] = calculate_offer_expiry_date(
                offer_created_date,
                validated_data["offer_expiry_period"],
            )
        validated_data["reference"] = allocate_opportunity_reference(
            prefix=Opportunity.reference_prefix_for_status(status),
            company_profile=validated_data.get("company_profile"),
            freelancer=validated_data.get("freelancer"),
        )
        instance = super().create(validated_data)
        creation_reason = (
            OpportunityHistoryReason.OFFER_CREATED
            if status in Opportunity.OFFER_STATUSES
            else OpportunityHistoryReason.OPPORTUNITY_CREATED
        )
        annotate_latest_opportunity_history(
            instance,
            creation_reason,
            request_user,
        )
        if line_items is not None:
            self._sync_offer_line_items(instance, line_items)
        if action_items is not None:
            instance.action_items.set(action_items)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        action_items = validated_data.pop("action_items", None)
        line_items = validated_data.pop("offer_line_items", None)
        lifecycle_action = self.context.get("lifecycle_action")
        history_reason = LIFECYCLE_REASON_BY_ACTION.get(lifecycle_action)
        if history_reason:
            prepare_opportunity_history(
                instance,
                history_reason,
                self.context["request"].user,
            )
        if (
            validated_data.get("status") == Opportunity.Status.CONVERTED
            and instance.status != Opportunity.Status.CONVERTED
        ):
            offer_created_date = timezone.localdate()
            validated_data["offer_created_date"] = offer_created_date
            validated_data["offer_expiry_date"] = calculate_offer_expiry_date(
                offer_created_date,
                validated_data["offer_expiry_period"],
            )
            validated_data["reference"] = convert_opportunity_reference(
                instance.reference,
            )
        elif "offer_expiry_period" in validated_data:
            offer_created_date = offer_created_date_for_record(instance)
            if not offer_created_date:
                raise serializers.ValidationError(
                    {
                        "offer_expiry_period": {
                            "code": "missing_offer_created_date",
                            "message": (
                                "The Offer creation date is unavailable, so the "
                                "expiry date cannot be calculated."
                            ),
                        }
                    }
                )
            validated_data["offer_created_date"] = offer_created_date
            validated_data["offer_expiry_date"] = calculate_offer_expiry_date(
                offer_created_date,
                validated_data["offer_expiry_period"],
            )
        instance = super().update(instance, validated_data)
        if line_items is not None:
            self._sync_offer_line_items(instance, line_items)
        if action_items is not None:
            instance.action_items.set(action_items)
        return instance

    def _sync_offer_line_items(self, opportunity, line_items):
        _, _, offer_value = calculate_offer_totals(line_items)
        opportunity.offer_line_items.all().delete()

        rows = []
        for line_number, item in enumerate(line_items, start=1):
            available_product = item.get("available_product")
            item_name = (
                available_product.name
                if available_product
                else item.get("item_name", "").strip()
            )
            subtotal, _, _ = calculate_line_amounts(
                item["rate"],
                item["quantity"],
                item["vat_rate"],
            )
            rows.append(
                OfferLineItem(
                    opportunity=opportunity,
                    available_product=available_product,
                    line_number=line_number,
                    item_name=item_name,
                    rate=item["rate"],
                    quantity=item["quantity"],
                    vat_rate=item["vat_rate"],
                    total=subtotal,
                )
            )

        OfferLineItem.objects.bulk_create(rows)
        opportunity.value = offer_value
        opportunity.updated_at = timezone.now()
        Opportunity.objects.filter(pk=opportunity.pk).update(
            value=offer_value,
            updated_at=opportunity.updated_at,
        )

    def get_linked_projects_count(self, obj):
        return getattr(obj, "linked_projects_count", obj.projects.count())

    def get_linked_action_items_count(self, obj):
        return getattr(
            obj,
            "linked_action_items_count",
            obj.action_items.count(),
        )

    def to_representation(self, instance):
        from project_management.serializers import DocumentSerializer

        representation = super().to_representation(instance)
        if instance.documents:
            representation["documents"] = DocumentSerializer(instance.documents, many=True).data
        if instance.customer_user:
            representation["customer_user"] = {
                "id": instance.customer_user.id,
                "name": instance.customer_user.name
            }
        if instance.customer_company:
            representation["customer_company"] = {
                "id": instance.customer_company.id,
                "name": instance.customer_company.company_name
            }
        if instance.created_by:
            representation["created_by"] = {
                "id": instance.created_by.id,
                "name": instance.created_by.full_name
            }
        if instance.responsible_salesperson:
            representation["responsible_salesperson"] = {
                "id": instance.responsible_salesperson.id,
                "name": instance.responsible_salesperson.full_name,
            }
        if instance.approver:
            representation["approver"] = {
                "id": instance.approver.id,
                "name": instance.approver.full_name
            }

        view = self.context.get("view")
        action = getattr(view, "action", None)
        include_linked_lists = action != "list"

        representation["linked_projects_count"] = self.get_linked_projects_count(
            instance
        )
        representation["linked_action_items_count"] = (
            self.get_linked_action_items_count(instance)
        )

        if include_linked_lists:
            representation["projects"] = [
                {
                    "id": project.id,
                    "project_name": project.project_name,
                    "project_status": project.project_status,
                    "project_start_date": project.project_start_date,
                    "project_end_date": project.project_end_date,
                }
                for project in instance.projects.all()
            ]
            representation["action_items"] = [
                {
                    "id": action_item.id,
                    "name": action_item.name,
                    "status": action_item.status,
                    "target_date": action_item.target_date,
                    "opportunity_ids": list(
                        action_item.opportunities.values_list("id", flat=True)
                    ),
                    "assignees": [
                        {
                            "id": assignee.id,
                            "name": assignee.full_name,
                        }
                        for assignee in action_item.assignees.all()
                    ],
                }
                for action_item in instance.action_items.all()
            ]

        return representation

    def get_history(self, obj):
        include_history = self.context.get('include_history', False)
        if not include_history:
            return None
            
        history = obj.history.all().order_by('-history_date')
        return OpportunityHistorySerializer(history, many=True).data

class OpportunityHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Opportunity.history.model
        fields = [
            'id',
            'reference',
            'history_date',
            'history_change_reason',
            'history_type',
            'history_user',
            'approver',
            'title',
            'details',
            'status',
            'country',
            'revenue',
            'probability',
            'contract_date',
            'planned_delivery_start_date',
            'planned_delivery_end_date',
            'purchase_order',
            'type_of_service',
            'responsible_salesperson',
            'sent_date',
            'value',
            'sales_phase',
            'customer_user',
            'customer_company',
            'offer_created_date',
            'offer_expiry_period',
            'offer_expiry_date',
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        if instance.history_user:
            representation['history_user'] = {
                "id": instance.history_user.id,
                "name": instance.history_user.full_name
            }
        
        if instance.created_by:
            representation['created_by'] = {
                "id": instance.created_by.id,
                "name": instance.created_by.full_name
            }

        if instance.responsible_salesperson:
            representation['responsible_salesperson'] = {
                "id": instance.responsible_salesperson.id,
                "name": instance.responsible_salesperson.full_name,
            }

        if instance.approver:
            representation['approver'] = {
                "id": instance.approver.id,
                "name": instance.approver.full_name,
            }

        if instance.customer_user:
            representation['customer_user'] = {
                "id": instance.customer_user.id,
                "name": instance.customer_user.name,
            }

        if instance.customer_company:
            representation['customer_company'] = {
                "id": instance.customer_company.id,
                "name": instance.customer_company.company_name,
            }
        
        if instance.planned_delivery_start_date:
            representation['planned_delivery_start_date'] = instance.planned_delivery_start_date.strftime("%d-%m-%Y")
        
        if instance.planned_delivery_end_date:
            representation['planned_delivery_end_date'] = instance.planned_delivery_end_date.strftime("%d-%m-%Y")
        
        if instance.contract_date:
            representation['contract_date'] = instance.contract_date.strftime("%d-%m-%Y")
        
        if instance.sent_date:
            representation['sent_date'] = instance.sent_date.strftime("%d-%m-%Y")

        return representation
