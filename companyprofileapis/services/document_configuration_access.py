"""Role-based updates limited to documentary company configuration fields."""

DOCUMENT_CONFIGURATION_FIELDS = frozenset({
    "document_types", "document_categories", "document_status",
    "document_internal_review_status", "document_external_review_status",
    "transmittal_category", "document_control_configuration",
    "document_disciplines", "nested_configurations",
})


def can_update_document_configuration(user, validated_data):
    if not validated_data or not set(validated_data).issubset(DOCUMENT_CONFIGURATION_FIELDS):
        return False
    nested = validated_data.get("nested_configurations")
    if nested is not None and (
        not isinstance(nested, dict) or not nested or set(nested) != {"document_disciplines"}
    ):
        return False
    from ecpmpapp.utils import require_any_permission_multiple_modules
    return require_any_permission_multiple_modules(
        user, {"company_configurations": ["update", "full_access"]}, "company", {}
    )
