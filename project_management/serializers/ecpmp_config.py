from rest_framework import serializers

from project_management.models import (
    ECPMPConfig
)
from project_management.models.constants import (
    ECPMP_BOM_STATUS_OPTIONS,
    ECPMP_DEMAND_STATUS_OPTIONS,
    ECPMP_REQUIREMENT_STATUS_OPTIONS,
    ECPMP_STATUS_OPTIONS,
)
from ecpmpapp.constants import VALID_REMINDER_PATTERN
from models import constants
from models.hierarchical_configurations import (
    AREA_OF_EXPERTISE_KEY,
    DOCUMENT_DISCIPLINES_KEY,
    HierarchicalConfigurationError,
    annotate_nested_configurations,
    get_default_nested_configuration_values,
    prepare_nested_configurations_update,
)
from models.rooms_utils import bootstrap_rooms_if_legacy_empty, effective_rooms_list
from companyprofileapis.services.hierarchical_configurations import (
    validate_removed_document_disciplines,
)

class ECPMPCONFIGSerializer(serializers.ModelSerializer):
    class Meta:
        model = ECPMPConfig
        exclude = ("acceptance_status_options",)

    def validate_document_control_configuration(self, value):
        # Reference / revision / workflow settings: validated and stored in canonical form.
        from models.document_control_configuration import (
            DocumentControlConfigurationError,
            validate_document_control_configuration,
        )

        try:
            resolved = validate_document_control_configuration(value)
        except DocumentControlConfigurationError as error:
            raise serializers.ValidationError(str(error))

        # A started project (first permanent reference issued) keeps its reference convention.
        project = getattr(self.instance, "project", None)
        if project is not None:
            from django.core.exceptions import ValidationError as DjangoValidationError

            from project_management.services.project_codes import (
                assert_conventions_unchanged,
            )

            try:
                assert_conventions_unchanged(
                    project, self.instance.document_control_configuration, resolved
                )
            except DjangoValidationError as error:
                raise serializers.ValidationError(error.messages[0])
        return resolved

    def validate_reminder_settings(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Reminder settings must be a list.")

        for item in value:
            if item.lower() == "none":
                continue
            if not VALID_REMINDER_PATTERN.match(item):
                raise serializers.ValidationError(
                    f"Invalid reminder format: '{item}'. Must be like '1 day', '2 weeks', or 'none'."
                )
        return value

    def validate_labels(self, value):
        return self._validate_without_defaults(value, "labels")

    def validate_rooms(self, value):
        return self._validate_rooms(value)

    def validate_skills(self, value):
        return self._validate_with_defaults(value, constants.GC_SKILLS, "skills")

    def validate_document_types(self, value):
        return self._validate_with_defaults(
            value, constants.GC_PROJECT_DOCUMENT_TYPES, "document_types"
        )

    def validate_document_categories(self, value):
        return self._validate_with_defaults(
            value, constants.GC_DOCUMENT_CATEGORIES, "document_categories"
        )

    def validate_demand_status_options(self, value):
        return self._validate_with_defaults(
            value,
            ECPMP_DEMAND_STATUS_OPTIONS,
            "demand_status_options",
        )

    def validate_requirement_status_options(self, value):
        return self._validate_with_defaults(
            value,
            ECPMP_REQUIREMENT_STATUS_OPTIONS,
            "requirement_status_options",
        )

    def validate_bom_status_options(self, value):
        return self._validate_with_defaults(
            value,
            ECPMP_BOM_STATUS_OPTIONS,
            "bom_status_options",
        )

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

    def _validate_without_defaults(self, value, field_name):
        if not isinstance(value, list):
            raise serializers.ValidationError(f"{field_name} must be a list.")

        normalized = [str(item).strip() for item in value if str(item).strip()]
        if len(normalized) != len(set(normalized)):
            raise serializers.ValidationError(f"Duplicate values are not allowed in {field_name}.")
        return normalized

    def _validate_with_defaults(self, value, default_values, field_name):
        normalized = self._validate_without_defaults(value, field_name)
        missing_defaults = set(default_values) - set(normalized)
        if missing_defaults:
            raise serializers.ValidationError(
                f"The following default values for {field_name} cannot be removed: {', '.join(sorted(missing_defaults))}"
            )
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

            merged.pop(AREA_OF_EXPERTISE_KEY, None)

            if (
                self.instance
                and DOCUMENT_DISCIPLINES_KEY in nested_updates
            ):
                validate_removed_document_disciplines(self.instance, merged)

            attrs["nested_configurations"] = merged
            attrs["document_disciplines"] = flattened[
                DOCUMENT_DISCIPLINES_KEY
            ]

        if 'status_options' in attrs:
            provided_statuses = attrs['status_options']

            if not isinstance(provided_statuses, list):
                raise serializers.ValidationError("Status options must be a list.")

            if not all(status in provided_statuses for status in ECPMP_STATUS_OPTIONS):
                raise serializers.ValidationError("You cannot edit default status options.")

            seen_statuses = {}
            seen_color_codes = {}

            for status in provided_statuses:
                if ':' in status:
                    status_name, color_code = status.rsplit(':', 1)
                    status_name = status_name.strip()
                    color_code = color_code.strip()
                    if status_name in seen_statuses and seen_statuses[status_name] != color_code:
                        raise serializers.ValidationError(f"Duplicate default status '{status_name}' with different color codes is not allowed.")
                    
                    if color_code in seen_color_codes:
                        raise serializers.ValidationError(f"Duplicate color code '{color_code}' with different status option is not allowed.")
                    
                    seen_color_codes[color_code] = status_name
                    seen_statuses[status_name] = color_code
                else:
                    raise serializers.ValidationError("Status must include a color code.")

            deduped_statuses = []
            seen_values = set()
            for status in provided_statuses:
                if status not in seen_values:
                    deduped_statuses.append(status)
                    seen_values.add(status)

            attrs['status_options'] = deduped_statuses

        return attrs

    def to_representation(self, instance):
        """
        Customizes the output for GET requests to show values as objects with 'value' and 'is_default'
        """
        representation = super().to_representation(instance)
        annotated_nested = annotate_nested_configurations(
            instance.nested_configurations
        )
        annotated_nested.pop(AREA_OF_EXPERTISE_KEY, None)

        include_defaults = self.context['request'].query_params.get('include_defaults', 'false') == 'true'

        if include_defaults:
            representation["nested_configurations"] = annotated_nested
            _, nested_defaults = get_default_nested_configuration_values()
            # Mapping of fields to their respective default values
            default_values_mapping = {
                "team_members_roles": constants.GC_TEAM_MEMBERS_ROLES,
                "funding_strategy": constants.GC_FUNDING_STRATEGY,
                "status_options": constants.GC_STATUS_OPTIONS,
                "demand_status_options": ECPMP_DEMAND_STATUS_OPTIONS,
                "requirement_status_options": ECPMP_REQUIREMENT_STATUS_OPTIONS,
                "bom_status_options": ECPMP_BOM_STATUS_OPTIONS,
                "reminder_settings": constants.GC_REMINDER_SETTINGS,
                "document_types": constants.GC_PROJECT_DOCUMENT_TYPES,
                "document_status": constants.GC_DOCUMENT_STATUSES,
                "document_internal_review_status": constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
                "document_external_review_status": constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
                "transmittal_category": constants.GC_TRANSMITTAL_CATEGORIES,
                "priority_levels": constants.GC_PRIORITY_LEVELS,
                "project_priority_levels": constants.GC_PROJECT_PRIORITY_LEVELS,
                "project_service_types": constants.GC_PROJECT_SERVICE_TYPES,
                "labels": [],
                "document_categories": constants.GC_DOCUMENT_CATEGORIES,
                "document_disciplines": nested_defaults[DOCUMENT_DISCIPLINES_KEY],
                "rooms": constants.GC_ROOMS,
                "skills": constants.GC_SKILLS,
            }

            for field, default_values in default_values_mapping.items():
                if field in representation:
                    if field == "rooms":
                        rooms_values = effective_rooms_list(representation[field])
                        representation[field] = [
                            {"value": item, "is_default": index < len(default_values)}
                            for index, item in enumerate(rooms_values)
                        ]
                    else:
                        representation[field] = [
                            {"value": item, "is_default": item in default_values}
                            for item in representation[field]
                        ]

        else:
            representation["nested_configurations"] = annotated_nested

        return representation
