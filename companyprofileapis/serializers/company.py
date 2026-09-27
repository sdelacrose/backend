from rest_framework import serializers
from django.utils.timezone import now

from models.models import CompanyProfile, User, CompanyConfig, BankDetail
from models import constants
from models.hierarchical_configurations import (
    AREA_OF_EXPERTISE_KEY,
    DOCUMENT_DISCIPLINES_KEY,
    HierarchicalConfigurationError,
    annotate_nested_configurations,
    get_default_nested_configuration_values,
    prepare_nested_configurations_update,
    resolve_nested_configurations,
)
from models.configuration_visibility import (
    FLAT_VISIBILITY_KEYS,
    ConfigurationVisibilityError,
    annotate_flat_values,
    annotate_nested_visibility,
    clean_configuration_visibility,
    filter_enabled_flat_values,
    filter_enabled_nested_configurations,
    prepare_configuration_visibility_update,
)
from models.rooms_utils import bootstrap_rooms_if_legacy_empty, effective_rooms_list
from models.offer_validity_periods import (
    OfferValidityPeriodError,
    normalize_offer_validity_periods,
)
from companyprofileapis.services.offer_validity import (
    OfferValidityPeriodInUseError,
    validate_offer_validity_period_removal,
)
from companyprofileapis.services.document_type_defaults import (
    merge_missing_document_type_defaults,
)
from companyprofileapis.models import Opportunity, NoticeBoard
from ecpmpapp.constants import (
    VALID_REMINDER_PATTERN, UserType, StatusType,
    ENERGY_CONSULTANT_SOLUTION,
    ENTERPRISE_SOLUTION,
    CORE_SOLUTION,
)
from chat.serializers import ChatRoomSerializer
from project_management.models import ContactCompany, Project
from companyprofileapis.services.hierarchical_configurations import (
    validate_area_of_expertise_visibility,
    validate_removed_document_disciplines,
)
from companyprofileapis.services.configuration_visibility import (
    ProjectConfigurationVisibilityError,
    validate_project_configuration_values,
)
from companyprofileapis.services.area_of_expertise import (
    SubAreaOfExpertiseListField,
    get_company_config_for_request,
    validate_area_of_expertise_fields,
)
from ecpmpapp.utils import get_user_role


class UserNameSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)  # Fetch full name from property
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "full_name", "role"]
    
    def get_role(self, obj):
        scope = self.context.get('scope')
        project = self.context.get('project')
        user_role = get_user_role(obj, 'solution', scope, {'project': project})
        if user_role:
            if hasattr(user_role, 'role'):
                return user_role.role.name
            else:
                return user_role.name
        return None
        

class UserSerializer(serializers.ModelSerializer):
    sub_area_of_expertise = SubAreaOfExpertiseListField(required=False)
    availability = serializers.SerializerMethodField()
    staff_id = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "title", "first_name", "last_name",
            'job_title', 'professional_headline', 'mobile_phone', 'street_address', 'city', 'state',
            'post_code', 'country', 'preferred_language', 'spoken_languages', 'user_image', 'sharemundo_links_id',
            'user_type', 'availability', 'staff_id',
            'main_area_of_expertise', 'sub_area_of_expertise',
        ]

    def get_availability(self, obj):
        return not Project.objects.filter(team_members__team_member=obj.id, project_status='In progress').exists()

    def get_staff_id(self, obj):
        return obj.staff.id if obj.staff else None

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return validate_area_of_expertise_fields(
            attrs,
            instance=self.instance,
            company_config=get_company_config_for_request(
                self.context.get("request")
            ),
            allow_multiple_sub_areas=True,
        )

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        # Determine if user is admin.
        groups = instance.groups.values_list("name", flat=True)
        is_admin = UserType.ADMIN.value in groups
        
        representation["is_admin_user"] = is_admin
        
        # Include company data if available.
        if instance.company:
            company_data = CompanyGetProfileSerializer(instance.company).data
            # For company_admin users, include bank details within the company data.
            if is_admin:
                bank_details = BankDetailSerializer(
                    BankDetail.objects.filter(company=instance.company), many=True
                ).data
                company_data["bank_details"] = bank_details
            representation["company"] = company_data

        # For freelancer users, include bank details as a list.
        if instance.is_freelancer:
            representation["freelancer_chats"] = ChatRoomSerializer(instance.freelancer_chats.all(), many=True).data
            if hasattr(instance, 'freelancer_profile'):
                freelancer_profile = instance.freelancer_profile
                # Create a bank detail dict from freelancer_profile fields.
                bank_detail = {
                    "id": freelancer_profile.id,  # or assign a unique id if available
                    "company": None,  # freelancers might not be linked to a company here
                    "company_bank_name": freelancer_profile.bank_name,
                    "company_iban": freelancer_profile.iban if freelancer_profile.iban else None,
                    "company_bic": freelancer_profile.bic,
                    "company_bank_address": freelancer_profile.bank_address,
                    "company_postcode": "",  # default if not provided
                    "company_town": ""       # default if not provided
                }
                representation['bank_details'] = [bank_detail]
                representation['currency'] = freelancer_profile.currency
            else:
                representation['bank_details'] = []
                representation['currency'] = "€"
            # Include additional freelancer-specific details.
            representation["user_details"] = UserNameSerializer(instance).data
        
        return representation

class BankDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankDetail
        fields = [
            'id', 'company', 'company_iban', 'company_bank_name', 'company_bic', 
            'company_bank_address', 'company_postcode', 'company_town'
        ]

class CompanyGetProfileSerializer(serializers.ModelSerializer):
    company_chats = ChatRoomSerializer(many=True)

    class Meta:
        model = CompanyProfile
        fields = (
            "id", "company_name", "company_address", "company_registration_no",
            "post_code", "company_town", "company_country", "company_tell_no", "company_mob_no",
            "company_web_address", "company_email_address", "company_profile_image", "admin_user",
            "company_chats", "currency", "sharemundo_links_id",
            "main_area_of_expertise", "sub_area_of_expertise")

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        return representation

class CompanyProfileSearchSerializer(serializers.ModelSerializer):
    admin_first_name = serializers.CharField(source='admin_user.first_name', allow_null=True)
    admin_last_name = serializers.CharField(source='admin_user.last_name', allow_null=True)

    class Meta:
        model = CompanyProfile
        fields = (
            "id", "company_name", "sharemundo_links_id", "company_address", "post_code", "company_town",
            "company_country", "company_tell_no", "company_mob_no",
            "company_web_address", "company_email_address", "company_profile_image",
            "products", "services", "expertise", "admin_first_name", "admin_last_name",
            "main_area_of_expertise", "sub_area_of_expertise")


class CompanyConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyConfig
        fields = '__all__'
        read_only_fields = ['company_profile']

    def validate_document_control_configuration(self, value):
        # Reference / revision / workflow settings: validated and stored in canonical form.
        from models.document_control_configuration import (
            DocumentControlConfigurationError,
            validate_document_control_configuration,
        )

        try:
            return validate_document_control_configuration(value)
        except DocumentControlConfigurationError as error:
            raise serializers.ValidationError(str(error))

    def validate_contract_type(self, value):
        return self._validate_with_defaults(value, constants.GC_CONTRACT_TYPES, "contract_type")

    def validate_international_standard(self, value):
        return self._validate_with_defaults(
            value, constants.GC_INTERNATIONAL_STANDARDS, "international_standard"
        )

    def validate_major_project_model(self, value):
        return self._validate_with_defaults(
            value, constants.GC_MAJOR_PROJECT_MODELS, "major_project_model"
        )

    def validate_commercial_model(self, value):
        return self._validate_with_defaults(value, constants.GC_COMMERCIAL_MODELS, "commercial_model")

    def validate_supporting_agreement(self, value):
        return self._validate_with_defaults(value, constants.GC_SUPPORTING_AGREEMENTS, "supporting_agreement")

    def validate_team_members_roles(self, value):
        return self._validate_with_defaults(value, constants.GC_TEAM_MEMBERS_ROLES, "team_members_roles")

    def validate_company_roles(self, value):
        normalized = self._validate_without_defaults(value, "company_roles")

        instance = getattr(self, "instance", None)
        if not instance:
            return normalized

        removed_roles = set(instance.company_roles or []) - set(normalized)
        if not removed_roles:
            return normalized

        if instance.user:
            linked_roles = set(
                ContactCompany.objects.filter(
                    user=instance.user,
                    company_role__in=removed_roles,
                )
                .exclude(company_role__isnull=True)
                .values_list("company_role", flat=True)
            )
        else:
            linked_roles = set(
                ContactCompany.objects.filter(
                    company_profile=instance.company_profile,
                    company_role__in=removed_roles,
                )
                .exclude(company_role__isnull=True)
                .values_list("company_role", flat=True)
            )

        if linked_roles:
            raise serializers.ValidationError(
                "Cannot remove company role(s) currently assigned to companies: "
                + ", ".join(sorted(linked_roles))
            )

        return normalized

    def validate_funding_strategy(self, value):
        return self._validate_with_defaults(value, constants.GC_FUNDING_STRATEGY, "funding_strategy")

    def validate_status_options(self, value):
        return self._validate_with_defaults(value, constants.GC_STATUS_OPTIONS, "status_options")

    def validate_reminder_settings(self, value):

        if not isinstance(value, list):
            raise serializers.ValidationError("Reminder settings must be a list.")
        self._validate_with_defaults(value, constants.GC_REMINDER_SETTINGS, "reminder_settings")

        for item in value:
            if item.lower() == "none":
                continue
            if not VALID_REMINDER_PATTERN.match(item):
                raise serializers.ValidationError(
                    f"Invalid reminder format: '{item}'. Must be like '1 day', '2 weeks', or 'none'."
                )
        return value

    def validate_invoice_status(self, value):
        return self._validate_with_defaults(value, constants.GC_INVOICE_STATUS, "invoice_status")

    def validate_offer_validity_periods(self, value):
        try:
            normalized = normalize_offer_validity_periods(value)
            return validate_offer_validity_period_removal(
                getattr(self, "instance", None),
                normalized,
            )
        except OfferValidityPeriodInUseError as exc:
            raise serializers.ValidationError(
                {
                    "code": "configuration_value_in_use",
                    "values": list(exc.values),
                }
            ) from exc
        except OfferValidityPeriodError as exc:
            raise serializers.ValidationError(str(exc)) from exc


    def validate_expense_types(self, value):
        return self._validate_with_defaults(value, constants.GC_EXPENSE_TYPES, "expense_types")

    def validate_project_types(self, value):
        return self._validate_with_defaults(value, constants.GC_PROJECT_TYPES, "project_types")

    def validate_document_types(self, value):
        return self._validate_with_defaults(
            value, constants.GC_COMPANY_DOCUMENT_TYPES, "document_types"
        )

    def validate_document_categories(self, value):
        return self._validate_with_defaults(
            value, constants.GC_DOCUMENT_CATEGORIES, "document_categories"
        )

    def validate_document_status(self, value):
        return self._validate_with_defaults(value, constants.GC_DOCUMENT_STATUSES, "document_status")

    def validate_document_internal_review_status(self, value):
        return self._validate_with_defaults(
            value,
            constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
            "document_internal_review_status",
        )

    def validate_document_external_review_status(self, value):
        return self._validate_with_defaults(
            value,
            constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
            "document_external_review_status",
        )

    def validate_transmittal_category(self, value):
        return self._validate_with_defaults(
            value,
            constants.GC_TRANSMITTAL_CATEGORIES,
            "transmittal_category",
        )

    def validate_priority_levels(self, value):
        return self._validate_with_defaults(value, constants.GC_PRIORITY_LEVELS, "priority_levels")

    def validate_project_priority_levels(self, value):
        return self._validate_with_defaults(value, constants.GC_PROJECT_PRIORITY_LEVELS, "project_priority_levels")
    
    def validate_leave_types(self, value):
        return self._validate_with_defaults(value, constants.GC_LEAVE_TYPES, "leave_types")
    
    def validate_oppurtunity_sales_phase(self, value):
        return self._validate_with_defaults(value, constants.GC_OPPORTUNITY_SALES_PHASE, "opportunity_sales_phase")

    def validate_project_service_types(self, value):
        return self._validate_with_defaults(value, constants.GC_PROJECT_SERVICE_TYPES, "project_service_types")

    def validate_labels(self, value):
        return self._validate_without_defaults(value, "labels")
    
    def validate_purchase_order_status(self, value):
        return self._validate_with_defaults(value, constants.GC_PURCHASE_ORDER_STATUS, "purchase_order_status")

    def validate_salutation(self, value):
        return self._validate_with_defaults(value, constants.GC_SALUTATIONS, "salutation")

    def validate_rooms(self, value):
        return self._validate_rooms(value)

    def validate_skills(self, value):
        return self._validate_with_defaults(value, constants.GC_SKILLS, "skills")

    def _validate_rooms(self, value):
        normalized = self._validate_without_defaults(value, "rooms")
        instance = getattr(self, "instance", None)
        instance_rooms = getattr(instance, "rooms", None) if instance else None
        normalized = bootstrap_rooms_if_legacy_empty(instance_rooms, normalized)
        min_count = len(constants.GC_ROOMS)
        if len(normalized) < min_count:
            raise serializers.ValidationError(
                f"At least {min_count} default room(s) must remain."
            )
        return normalized

    def _validate_with_defaults(self, value, default_values, field_name):
        missing_defaults = set(default_values) - set(value)
        if missing_defaults:
            raise serializers.ValidationError(
                f"The following default values for {field_name} cannot be removed: {', '.join(missing_defaults)}"
            )
        if len(value) != len(set(value)):
            raise serializers.ValidationError(f"Duplicate values are not allowed in {field_name}.")
        return value

    def _validate_without_defaults(self, value, field_name):
        if not isinstance(value, list):
            raise serializers.ValidationError(f"{field_name} must be a list.")

        normalized = [str(item).strip() for item in value if str(item).strip()]
        if len(normalized) != len(set(normalized)):
            raise serializers.ValidationError(f"Duplicate values are not allowed in {field_name}.")
        return normalized

    def validate(self, attrs):
        nested_updates = attrs.get("nested_configurations")
        if nested_updates is not None:
            current = getattr(self.instance, "nested_configurations", {})
            try:
                merged, flattened = prepare_nested_configurations_update(
                    current,
                    nested_updates,
                )
            except HierarchicalConfigurationError as exc:
                raise serializers.ValidationError(
                    {"nested_configurations": str(exc)}
                ) from exc

            if DOCUMENT_DISCIPLINES_KEY in nested_updates:
                validate_removed_document_disciplines(self.instance, merged)

            attrs["nested_configurations"] = merged
            attrs["document_disciplines"] = flattened[DOCUMENT_DISCIPLINES_KEY]

        visibility_updates = attrs.get("configuration_visibility")
        visibility_related_fields = set(FLAT_VISIBILITY_KEYS) | {
            "nested_configurations"
        }
        if visibility_updates is not None or visibility_related_fields.intersection(attrs):
            nested_configurations = resolve_nested_configurations(
                attrs.get(
                    "nested_configurations",
                    getattr(self.instance, "nested_configurations", {}),
                )
            )
            configured_values = {
                config_key: attrs.get(
                    config_key,
                    getattr(self.instance, config_key, []),
                )
                for config_key in FLAT_VISIBILITY_KEYS
            }
            current_visibility = getattr(
                self.instance,
                "configuration_visibility",
                {},
            )
            try:
                if visibility_updates is not None:
                    visibility = prepare_configuration_visibility_update(
                        current_visibility,
                        visibility_updates,
                        configured_values=configured_values,
                        nested_configurations=nested_configurations,
                    )
                else:
                    visibility = clean_configuration_visibility(
                        current_visibility,
                        configured_values=configured_values,
                        nested_configurations=nested_configurations,
                    )
            except ConfigurationVisibilityError as exc:
                raise serializers.ValidationError(
                    {"configuration_visibility": str(exc)}
                ) from exc

            if not filter_enabled_flat_values(
                configured_values["contract_type"],
                visibility,
                "contract_type",
            ):
                raise serializers.ValidationError(
                    {
                        "configuration_visibility": {
                            "contract_type": "At least one contract type must remain visible."
                        }
                    }
                )

            if self.instance:
                if (
                    isinstance(visibility_updates, dict)
                    and AREA_OF_EXPERTISE_KEY in visibility_updates
                ):
                    validate_area_of_expertise_visibility(
                        self.instance,
                        nested_configurations,
                        current_visibility,
                        visibility,
                    )
                try:
                    validate_project_configuration_values(
                        self.instance,
                        configured_values,
                        visibility,
                    )
                except ProjectConfigurationVisibilityError as exc:
                    raise serializers.ValidationError(
                        {
                            "configuration_visibility": {
                                config_key: {
                                    "code": "configuration_value_in_use",
                                    "values": values,
                                }
                                for config_key, values in exc.blocked_values.items()
                            }
                        }
                    ) from exc
            attrs["configuration_visibility"] = visibility

        return super().validate(attrs)

    def update(self, instance, validated_data):
        request_user = self.context['request'].user
        if request_user.is_freelancer and instance.user != request_user:
            raise serializers.ValidationError("You do not have permission to update this company configuration.")

        if request_user.company:
            if instance.company_profile != request_user.company or not request_user.groups.filter(
                    name=UserType.ADMIN.value).exists():
                raise serializers.ValidationError("You do not have permission to update this company's configuration.")

        return super().update(instance, validated_data)

    def to_representation(self, instance):
        """
        Customizes the output for GET requests to show values as objects with 'value' and 'is_default'
        """
        representation = super().to_representation(instance)
        annotated_nested = annotate_nested_configurations(
            instance.nested_configurations
        )

        include_defaults = self.context['request'].query_params.get('include_defaults', 'false') == 'true'

        if include_defaults:
            if instance.user_id:
                offer_owner_filter = {"freelancer_id": instance.user_id}
            else:
                offer_owner_filter = {
                    "company_profile_id": instance.company_profile_id,
                }
            used_offer_validity_periods = set(
                Opportunity.objects.filter(
                    **offer_owner_filter,
                    offer_expiry_period__isnull=False,
                ).values_list("offer_expiry_period", flat=True)
            )
            representation["nested_configurations"] = annotate_nested_visibility(
                annotated_nested,
                instance.configuration_visibility,
            )
            _, nested_defaults = get_default_nested_configuration_values()
            # Mapping of fields to their respective default values
            default_values_mapping = {
                "contract_type": constants.GC_CONTRACT_TYPES,
                "international_standard": constants.GC_INTERNATIONAL_STANDARDS,
                "major_project_model": constants.GC_MAJOR_PROJECT_MODELS,
                "commercial_model": constants.GC_COMMERCIAL_MODELS,
                "supporting_agreement": constants.GC_SUPPORTING_AGREEMENTS,
                "team_members_roles": constants.GC_TEAM_MEMBERS_ROLES,
                "company_roles": [],
                "funding_strategy": constants.GC_FUNDING_STRATEGY,
                "status_options": constants.GC_STATUS_OPTIONS,
                "reminder_settings": constants.GC_REMINDER_SETTINGS,
                "invoice_status": constants.GC_INVOICE_STATUS,
                "expense_types": constants.GC_EXPENSE_TYPES,
                "project_types": constants.GC_PROJECT_TYPES,
                "document_types": constants.GC_COMPANY_DOCUMENT_TYPES,
                "correspondence_types": constants.GC_CORRESPONDENCE_TYPES,
                "document_status": constants.GC_DOCUMENT_STATUSES,
                "document_internal_review_status": constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
                "document_external_review_status": constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
                "transmittal_category": constants.GC_TRANSMITTAL_CATEGORIES,
                "priority_levels": constants.GC_PRIORITY_LEVELS,
                "project_priority_levels": constants.GC_PROJECT_PRIORITY_LEVELS,
                "leave_types": constants.GC_LEAVE_TYPES,
                "opportunity_sales_phase": constants.GC_OPPORTUNITY_SALES_PHASE,
                "offer_validity_periods": [],
                "purchase_order_status": constants.GC_PURCHASE_ORDER_STATUS,
                "project_service_types": constants.GC_PROJECT_SERVICE_TYPES,
                "labels": [],
                "document_categories": constants.GC_DOCUMENT_CATEGORIES,
                "document_disciplines": nested_defaults[DOCUMENT_DISCIPLINES_KEY],
                "salutation": constants.GC_SALUTATIONS,
                "rooms": constants.GC_ROOMS,
                "skills": constants.GC_SKILLS,
            }

            # document_types: an empty/partial stored list must still offer the canonical
            # types (the Document Type selector and the permanent reference both depend on
            # it) -- missing canonical entries are appended, custom entries are preserved.
            if "document_types" in representation:
                representation["document_types"] = merge_missing_document_type_defaults(
                    representation["document_types"], constants.GC_COMPANY_DOCUMENT_TYPES
                )

            # LOT 3B stabilization: same backfill for Correspondence Type reference data.
            if "correspondence_types" in representation:
                representation["correspondence_types"] = merge_missing_document_type_defaults(
                    representation["correspondence_types"], constants.GC_CORRESPONDENCE_TYPES
                )

            for field, default_values in default_values_mapping.items():
                if field in representation:
                    if field in FLAT_VISIBILITY_KEYS:
                        representation[field] = annotate_flat_values(
                            representation[field],
                            default_values,
                            instance.configuration_visibility,
                            field,
                        )
                    elif field == "rooms":
                        rooms_values = effective_rooms_list(representation[field])
                        representation[field] = [
                            {"value": item, "is_default": index < len(default_values)}
                            for index, item in enumerate(rooms_values)
                        ]
                    elif field == "offer_validity_periods":
                        representation[field] = [
                            {
                                "value": item,
                                "is_default": False,
                                "is_in_use": item in used_offer_validity_periods,
                            }
                            for item in representation[field]
                        ]
                    else:
                        representation[field] = [
                            {"value": item, "is_default": item in default_values}
                            for item in representation[field]
                        ]
        else:
            representation["nested_configurations"] = (
                filter_enabled_nested_configurations(
                    annotated_nested,
                    instance.configuration_visibility,
                )
            )
            for field in FLAT_VISIBILITY_KEYS:
                if field in representation:
                    representation[field] = filter_enabled_flat_values(
                        representation[field],
                        instance.configuration_visibility,
                        field,
                    )
        # delete irrelevant fields based on solution
        if instance.solution == ENTERPRISE_SOLUTION:
            representation.pop("funding_strategy", None)
        elif instance.solution == CORE_SOLUTION:
            representation.pop("project_priority_levels", None)
        return representation
