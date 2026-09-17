import os

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework import serializers

from chat.object_links import (
    extract_linked_objects_from_text,
    filter_accessible_linked_objects,
    merge_linked_objects,
    normalize_linked_object_refs,
    normalize_linked_objects,
    resolve_linked_objects,
)
from project_management.matrix_comment_mentions import get_matrix_mention_user_snapshots
from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanyAssemblyProductionLink,
    CompanySolutionSpaceItem,
    Document,
    Equipment,
    EquipmentConfig,
    ECPMPConfig,
    Machine,
    MachineConfig,
    MatrixComment,
    MatrixCommentReaction,
    ObjectAssessment,
    PartsListConfig,
    ProjectMatrixConfig,
    ProjectSolutionSpaceItem,
    Requirement,
    SolutionDemand,
    SolutionSpaceConfig,
    Transport,
)
from models.models import CompanyConfig
from project_management.models.project import (
    Project,
    default_parts_columns,
    default_solution_space_columns,
)
from project_management.serializers.document import DocumentSerializer
from project_management.serializers.linked_folder import LinkedFolderSerializerMixin
from project_management.serializers.object_assessment import ObjectAssessmentSerializer
from project_management.utils import get_product_name_from_request


MAX_TREE_LEVEL = 10
MANDATORY_DEMAND_COLUMNS = {"sequence", "subject", "demand_number", "status"}
MANDATORY_REQUIREMENT_COLUMNS = {"sequence", "name", "object_number", "skill", "status"}
BUILT_IN_TRANSPORT_COLUMNS = {
    "sequence",
    "name",
    "transport_start_date",
    "transport_end_date",
    "transportation_lanes",
    "carrier_per_transportation_lane_and_means_of_transport",
    "location",
    "location_with_address",
    "equipment",
    "freight_settlement_document",
    "unlocode",
    "unlocode_text",
    "stops_for_transportation_stages",
    "freight_order_freight_booking_freight_unit_root_or_header",
    "freight_order_freight_booking_freight_unit_item_or_line_item",
    "document_references",
    "freight_order_types",
    "freight_unit_types",
    "transportation_unit_types",
    "order_based_transportation_requirement",
    "delivery_based_transportation_requirement",
    "forwarding_order_types",
    "freight_agreement_types",
    "freight_agreement_root",
    "freight_agreement_positions",
    "block_reason_codes",
    "texts_for_block_reason_codes",
    "freight_unit_formation_rule_header",
    "tcc_solution_basis",
    "rate_tables",
    "transportation_charges",
    "freight_order_freight_booking_freight_unit",
    "transportation_requirement",
    "forwarding_settlement_document",
    "vehicle_resource",
    "driver",
}
MANDATORY_PART_COLUMNS = {"sequence", "name", "part_number"}
MANDATORY_BOM_COLUMNS = MANDATORY_PART_COLUMNS | {"status"}
MANDATORY_MACHINE_COLUMNS = {"sequence", "name"}
MANDATORY_EQUIPMENT_COLUMNS = {"sequence", "name"}
SOLUTION_SPACE_SYSTEM_COLUMN_KEYS = {
    "assembly_name",
    "assembly_type",
    "required_skills",
    "provided_skills",
    "responsible_engineer",
    "projects",
    "customers",
    "year_of_construction",
}


def normalize_colored_option(value):
    return str(value).rsplit(":", 1)[0].strip()


def normalize_optional_columns(value, reserved_keys):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError("Columns must be a list.")

    normalized = []
    seen_keys = set()
    for index, column in enumerate(value):
        if not isinstance(column, dict):
            raise serializers.ValidationError("Each column must be an object.")
        key = str(column.get("key") or column.get("id") or "").strip()
        label_en = str(column.get("label_en") or "").strip()
        label_de = str(column.get("label_de") or "").strip()
        label_fr = str(column.get("label_fr") or "").strip()
        label = str(column.get("label") or label_en or label_de or label_fr or "").strip()
        if not key or not label:
            raise serializers.ValidationError("Each column must include key and label.")
        if key in reserved_keys:
            raise serializers.ValidationError(f"{key} is a mandatory column and cannot be configured as optional.")
        if key in seen_keys:
            raise serializers.ValidationError(f"Duplicate column key: {key}.")
        seen_keys.add(key)
        data_type = str(column.get("type") or "text").strip().lower()
        allowed_types = {"text", "number", "date", "enum", "language", "unit"}
        if data_type not in allowed_types:
            raise serializers.ValidationError(
                f"Unsupported column type for {key}: {data_type}."
            )
        normalized_column = {
            "key": key,
            "label": label,
            "type": data_type,
            "order": int(column.get("order", index)),
        }
        unit = str(column.get("unit") or "").strip()
        if unit:
            normalized_column["unit"] = unit
        options = column.get("options") or []
        if data_type == "enum":
            if not isinstance(options, list):
                raise serializers.ValidationError(
                    f"Enum options for {key} must be a list."
                )
            normalized_options = [
                str(option).strip()
                for option in options
                if option is not None and str(option).strip()
            ]
            if len(set(normalized_options)) != len(normalized_options):
                raise serializers.ValidationError(
                    f"Enum options for {key} must be unique."
                )
            normalized_column["options"] = normalized_options
        if label_en:
            normalized_column["label_en"] = label_en
        if label_de:
            normalized_column["label_de"] = label_de
        if label_fr:
            normalized_column["label_fr"] = label_fr
        normalized.append(normalized_column)

    return sorted(normalized, key=lambda item: item["order"])


def normalize_idta_columns(value, reserved_keys):
    normalized = normalize_optional_columns(value, reserved_keys)
    idta_by_key = {
        str(column.get("key") or column.get("id") or "").strip(): str(column.get("idta_semantic_id") or "").strip()
        for column in value or []
        if isinstance(column, dict)
    }
    for column in normalized:
        column["idta_semantic_id"] = idta_by_key.get(column["key"], "")
    return normalized


def normalize_available_product_columns(value):
    value = value or []
    manufacturer_input = next(
        (
            column
            for column in value
            if isinstance(column, dict)
            and str(column.get("key") or column.get("id") or "").strip()
            == "manufacturer"
        ),
        {},
    )
    custom_columns = [
        column
        for column in value
        if not (
            isinstance(column, dict)
            and str(column.get("key") or column.get("id") or "").strip()
            == "manufacturer"
        )
    ]
    normalized = normalize_idta_columns(custom_columns, MANDATORY_PART_COLUMNS)
    for column in normalized:
        column["aas_ownership"] = (
            "links"
            if column["key"] in {"description", "quantity"}
            else "aas"
        )
    manufacturer = default_parts_columns()[0]
    manufacturer["order"] = int(
        manufacturer_input.get("order", manufacturer.get("order", 0))
    )
    normalized.append(manufacturer)
    return sorted(normalized, key=lambda item: item["order"])


def normalize_solution_space_columns(value):
    if value in (None, ""):
        value = []
    if not isinstance(value, list):
        raise serializers.ValidationError("Columns must be a list.")

    defaults = {column["key"]: column for column in default_solution_space_columns()}
    normalized = []
    seen_keys = set()

    for index, column in enumerate(value):
        if not isinstance(column, dict):
            raise serializers.ValidationError("Each column must be an object.")
        key = str(column.get("key") or column.get("id") or "").strip()
        if not key or key in seen_keys:
            raise serializers.ValidationError("Column keys must be present and unique.")
        seen_keys.add(key)

        if key in defaults:
            default = defaults[key]
            normalized.append(
                {
                    **default,
                    "label": str(column.get("label") or default["label"]).strip(),
                    "label_en": str(column.get("label_en") or default.get("label_en") or "").strip(),
                    "label_de": str(column.get("label_de") or default.get("label_de") or "").strip(),
                    "label_fr": str(column.get("label_fr") or default.get("label_fr") or "").strip(),
                    "order": int(column.get("order", index)),
                    "visible": True if key == "assembly_name" else bool(column.get("visible", True)),
                    "idta_semantic_id": str(column.get("idta_semantic_id") or "").strip(),
                }
            )
            continue

        label_en = str(column.get("label_en") or "").strip()
        label_de = str(column.get("label_de") or "").strip()
        label_fr = str(column.get("label_fr") or "").strip()
        label = str(column.get("label") or label_en or label_de or label_fr or "").strip()
        if not label:
            raise serializers.ValidationError("Each custom column must include a label.")
        normalized.append(
            {
                "key": key,
                "label": label,
                "label_en": label_en,
                "label_de": label_de,
                "label_fr": label_fr,
                "type": "text",
                "order": int(column.get("order", index)),
                "visible": bool(column.get("visible", True)),
                "system": False,
                "scope": "both",
                "idta_semantic_id": str(column.get("idta_semantic_id") or "").strip(),
            }
        )

    for key, default in defaults.items():
        if key not in seen_keys:
            normalized.append({**default, "order": len(normalized)})

    return sorted(normalized, key=lambda item: item["order"])


def get_next_sort_order(model, project, parent):
    max_order = model.objects.filter(project=project, parent=parent).aggregate(
        max_order=Max("sort_order")
    )["max_order"]
    return (max_order or 0) + 1


def get_next_company_sort_order(model, company_profile, parent):
    max_order = model.objects.filter(company_profile=company_profile, parent=parent).aggregate(
        max_order=Max("sort_order")
    )["max_order"]
    return (max_order or 0) + 1


def update_descendant_levels(instance):
    children = instance.__class__.objects.filter(parent=instance)
    for child in children:
        child.level = instance.level + 1
        child.save(update_fields=["level", "updated_at"])
        update_descendant_levels(child)


class ProjectMatrixConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMatrixConfig
        fields = "__all__"
        read_only_fields = ["created_by", "modified_by", "created_at", "updated_at"]

    def validate_demand_columns(self, value):
        return normalize_optional_columns(value, MANDATORY_DEMAND_COLUMNS)

    def validate_requirement_columns(self, value):
        return normalize_optional_columns(value, MANDATORY_REQUIREMENT_COLUMNS)

    def validate_transport_columns(self, value):
        return normalize_optional_columns(value, BUILT_IN_TRANSPORT_COLUMNS)

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        return super().update(instance, validated_data)


class PartsListConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartsListConfig
        fields = "__all__"
        read_only_fields = ["created_by", "modified_by", "created_at", "updated_at"]

    def validate(self, attrs):
        company_profile = attrs.get("company_profile", self.instance.company_profile if self.instance else None)
        project = attrs.get("project", self.instance.project if self.instance else None)
        if project and company_profile and project.company_profile_id != company_profile.id:
            raise serializers.ValidationError(
                "The parts list configuration company must own the project."
            )
        if not company_profile and not project:
            raise serializers.ValidationError("A company or project is required.")
        return attrs

    def validate_available_product_columns(self, value):
        return normalize_available_product_columns(value)

    def validate_bom_columns(self, value):
        return normalize_idta_columns(value, MANDATORY_BOM_COLUMNS)

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        return super().update(instance, validated_data)


class SolutionSpaceConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = SolutionSpaceConfig
        fields = "__all__"
        read_only_fields = ["created_by", "modified_by", "created_at", "updated_at"]

    def validate(self, attrs):
        company_profile = attrs.get(
            "company_profile",
            self.instance.company_profile if self.instance else None,
        )
        project = attrs.get("project", self.instance.project if self.instance else None)
        if project and company_profile and project.company_profile_id != company_profile.id:
            raise serializers.ValidationError(
                "The Solution Space configuration company must own the project."
            )
        if not company_profile and not project:
            raise serializers.ValidationError("A company or project is required.")
        return attrs

    def validate_columns(self, value):
        return normalize_solution_space_columns(value)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["columns"] = normalize_solution_space_columns(
            representation.get("columns")
        )
        return representation

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        return super().update(instance, validated_data)

class MachineConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = MachineConfig
        fields = "__all__"
        read_only_fields = ["created_by", "modified_by", "created_at", "updated_at"]

    def validate_machine_columns(self, value):
        return normalize_optional_columns(value, MANDATORY_MACHINE_COLUMNS)

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        return super().update(instance, validated_data)


class EquipmentConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = EquipmentConfig
        fields = "__all__"
        read_only_fields = ["created_by", "modified_by", "created_at", "updated_at"]

    def validate_equipment_columns(self, value):
        return normalize_optional_columns(value, MANDATORY_EQUIPMENT_COLUMNS)

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        return super().update(instance, validated_data)


class MatrixTreeSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    status_options_config_field = None

    sequence = serializers.SerializerMethodField()
    object_assessment_details = serializers.SerializerMethodField()
    documents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Document.objects.all(),
        required=False,
    )

    class Meta:
        fields = "__all__"

    def get_sequence(self, obj):
        return obj.get_sequence()

    def get_object_assessment_details(self, obj):
        return ObjectAssessmentSerializer(obj.object_assessments.all(), many=True, context=self.context).data

    def validate_parent(self, value):
        if not value:
            return value
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        if project_id and int(value.project_id) != int(project_id):
            raise serializers.ValidationError("Parent must belong to the same project.")
        if self.instance and value.id == self.instance.id:
            raise serializers.ValidationError("An item cannot be its own parent.")
        if value.level >= MAX_TREE_LEVEL:
            raise serializers.ValidationError(f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels.")
        return value

    def validate_object_assessments(self, value):
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        for item in value:
            if int(item.project_id) != int(project_id):
                raise serializers.ValidationError("Object Assessment links must belong to the same project.")
        return value

    def validate_documents(self, value):
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        for document in value:
            if int(document.folder.project_id or 0) != int(project_id):
                raise serializers.ValidationError("Linked documents must belong to the same project.")
        return value

    def validate_status(self, value):
        if value in (None, ""):
            return None
        if not self.status_options_config_field:
            raise serializers.ValidationError("Status is not supported for this module.")

        project = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        project_id = getattr(project, "id", project)
        if not project_id and self.instance:
            project_id = self.instance.project_id

        normalized_value = normalize_colored_option(value)
        configured_options = ECPMPConfig.objects.filter(
            project_id=project_id,
        ).values_list(
            self.status_options_config_field,
            flat=True,
        ).first() or []
        normalized_options = [
            normalize_colored_option(option)
            for option in configured_options
        ]
        valid_statuses = {
            option.lower(): option
            for option in normalized_options
            if option
        }

        configured_value = valid_statuses.get(normalized_value.lower())
        if configured_value:
            return configured_value

        existing_value = getattr(self.instance, "status", None)
        if (
            existing_value
            and normalized_value.lower() == str(existing_value).strip().lower()
        ):
            return existing_value

        raise serializers.ValidationError("Invalid configured status option.")

    def validate(self, attrs):
        parent = attrs.get("parent", self.instance.parent if self.instance else None)
        project = attrs.get("project", self.instance.project if self.instance else None)
        if parent and project and parent.project_id != project.id:
            raise serializers.ValidationError({"parent": "Parent must belong to the same project."})
        level = (parent.level + 1) if parent else 1
        if level > MAX_TREE_LEVEL:
            raise serializers.ValidationError({"parent": f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels."})
        attrs["level"] = level
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        object_assessments = validated_data.pop("object_assessments", [])
        documents = validated_data.pop("documents", [])
        linked_folders = validated_data.pop("linked_folders", [])
        parent = validated_data.get("parent")
        project = validated_data["project"]
        validated_data["sort_order"] = get_next_sort_order(self.Meta.model, project, parent)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        instance = super().create(validated_data)
        instance.object_assessments.set(object_assessments)
        instance.documents.set(documents)
        self.set_linked_folders(instance, linked_folders)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        object_assessments = validated_data.pop("object_assessments", None)
        documents = validated_data.pop("documents", None)
        linked_folders = validated_data.pop("linked_folders", None)
        parent_changed = "parent" in validated_data and validated_data["parent"] != instance.parent
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        updated = super().update(instance, validated_data)
        if object_assessments is not None:
            updated.object_assessments.set(object_assessments)
        if documents is not None:
            updated.documents.set(documents)
        self.set_linked_folders(updated, linked_folders)
        if parent_changed:
            updated.sort_order = get_next_sort_order(self.Meta.model, updated.project, updated.parent)
            updated.save(update_fields=["sort_order", "updated_at"])
            update_descendant_levels(updated)
        return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["documents"] = DocumentSerializer(
            instance.documents.all(),
            many=True,
            context=self.context,
        ).data
        return representation


class AvailableProductPartSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    sequence = serializers.SerializerMethodField()
    object_assessment_details = serializers.SerializerMethodField()
    documents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Document.objects.all(),
        required=False,
    )

    class Meta:
        model = AvailableProductPart
        fields = "__all__"
        read_only_fields = [
            "company_profile",
            "level",
            "sort_order",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]

    def get_sequence(self, obj):
        return obj.get_sequence()

    def get_object_assessment_details(self, obj):
        return ObjectAssessmentSerializer(obj.object_assessments.all(), many=True, context=self.context).data

    def _get_company_profile_id(self):
        company_profile_id = self.initial_data.get("company_profile") if hasattr(self, "initial_data") else None
        if not company_profile_id and self.instance:
            company_profile_id = self.instance.company_profile_id
        if not company_profile_id:
            request = self.context.get("request")
            user = getattr(request, "user", None)
            company_profile_id = getattr(user, "company_id", None)
        return company_profile_id

    def get_linked_folder_owner_ids(self):
        return None, self._get_company_profile_id(), None

    def validate_parent(self, value):
        if not value:
            return value
        company_profile_id = self._get_company_profile_id()
        if company_profile_id and int(value.company_profile_id) != int(company_profile_id):
            raise serializers.ValidationError("Parent must belong to the same company.")
        if self.instance and value.id == self.instance.id:
            raise serializers.ValidationError("An item cannot be its own parent.")
        if value.level >= MAX_TREE_LEVEL:
            raise serializers.ValidationError(f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels.")
        return value

    def validate_object_assessments(self, value):
        company_profile_id = self._get_company_profile_id()
        for item in value:
            if int(item.project.company_profile_id) != int(company_profile_id):
                raise serializers.ValidationError("Object Assessment links must belong to projects from the same company.")
        return value

    def validate_documents(self, value):
        company_profile_id = self._get_company_profile_id()
        for document in value:
            if int(document.folder.company_profile_id or 0) != int(company_profile_id):
                raise serializers.ValidationError("Linked documents must belong to the same company.")
        return value

    def validate(self, attrs):
        parent = attrs.get("parent", self.instance.parent if self.instance else None)
        company_profile = attrs.get("company_profile", self.instance.company_profile if self.instance else None)
        company_profile_id = company_profile.id if company_profile else self._get_company_profile_id()
        if parent and company_profile_id and int(parent.company_profile_id) != int(company_profile_id):
            raise serializers.ValidationError({"parent": "Parent must belong to the same company."})
        level = (parent.level + 1) if parent else 1
        if level > MAX_TREE_LEVEL:
            raise serializers.ValidationError({"parent": f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels."})
        if self.instance:
            from aas_integration.models import ProductAASProfile
            from aas_integration.services import infer_product_aas_origin

            profile = getattr(self.instance, "aas_profile", None)
            origin = (
                profile.origin
                if profile and profile.origin != ProductAASProfile.Origin.UNCLASSIFIED
                else infer_product_aas_origin(self.instance, profile)
            )
            if origin == ProductAASProfile.Origin.PURCHASED:
                protected_errors = {}
                if "name" in attrs and attrs["name"] != self.instance.name:
                    protected_errors["name"] = "Supplier-owned AAS fields are read-only."
                if "part_number" in attrs and attrs["part_number"] != self.instance.part_number:
                    protected_errors["part_number"] = "Supplier-owned AAS fields are read-only."
                if "custom_fields" in attrs:
                    from aas_integration.profiles import is_product_aas_column

                    config = PartsListConfig.objects.filter(
                        company_profile=self.instance.company_profile,
                        project__isnull=True,
                    ).first()
                    configured_keys = {
                        str(column.get("key"))
                        for column in (config.available_product_columns if config else [])
                        if column.get("key") and is_product_aas_column(column)
                    }
                    protected_keys = configured_keys | {
                        "supplier",
                        "manufacturer",
                        "article_number",
                        "global_asset_id",
                        "asset_id",
                        "aas_id",
                        "supplier_aas_id",
                        "order_code",
                    }
                    previous = self.instance.custom_fields or {}
                    changed_keys = sorted(
                        key
                        for key in protected_keys
                        if key in attrs["custom_fields"]
                        and attrs["custom_fields"].get(key) != previous.get(key)
                    )
                    if changed_keys:
                        protected_errors["custom_fields"] = {
                            key: "Supplier-owned AAS fields are read-only."
                            for key in changed_keys
                        }
                    attrs["custom_fields"] = {**previous, **attrs["custom_fields"]}
                if protected_errors:
                    raise serializers.ValidationError(protected_errors)
        attrs["level"] = level
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        object_assessments = validated_data.pop("object_assessments", [])
        documents = validated_data.pop("documents", [])
        linked_folders = validated_data.pop("linked_folders", [])
        parent = validated_data.get("parent")
        company_profile = validated_data["company_profile"]
        validated_data["sort_order"] = get_next_company_sort_order(self.Meta.model, company_profile, parent)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        instance = super().create(validated_data)
        from aas_integration.services import synchronize_product_aas_profile

        synchronize_product_aas_profile(instance)
        instance.object_assessments.set(object_assessments)
        instance.documents.set(documents)
        self.set_linked_folders(instance, linked_folders)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        from aas_integration.models import ProductAASProfile
        from aas_integration.profiles import is_product_aas_column

        profile = getattr(instance, "aas_profile", None)
        config = PartsListConfig.objects.filter(
            company_profile=instance.company_profile,
            project__isnull=True,
        ).first()
        aas_custom_keys = {
            str(column.get("key"))
            for column in (config.available_product_columns if config else [])
            if column.get("key") and is_product_aas_column(column)
        }
        next_custom_fields = validated_data.get("custom_fields")
        aas_fields_changed = bool(
            ("name" in validated_data and validated_data["name"] != instance.name)
            or (
                "part_number" in validated_data
                and validated_data["part_number"] != instance.part_number
            )
            or (
                next_custom_fields is not None
                and any(
                    (instance.custom_fields or {}).get(key)
                    != next_custom_fields.get(key)
                    for key in aas_custom_keys
                )
            )
        )
        object_assessments = validated_data.pop("object_assessments", None)
        documents = validated_data.pop("documents", None)
        linked_folders = validated_data.pop("linked_folders", None)
        validated_data.pop("parent", None)
        validated_data.pop("level", None)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        updated = super().update(instance, validated_data)
        from aas_integration.services import synchronize_product_aas_profile

        profile = synchronize_product_aas_profile(updated)
        if object_assessments is not None:
            updated.object_assessments.set(object_assessments)
        if documents is not None:
            updated.documents.set(documents)
        self.set_linked_folders(updated, linked_folders)
        if (
            aas_fields_changed
            and profile
            and profile.origin == ProductAASProfile.Origin.MANUFACTURED
        ):
            from aas_integration.services import mark_product_out_of_date

            request = self.context.get("request")
            mark_product_out_of_date(
                updated,
                getattr(getattr(request, "user", None), "id", None),
            )
        return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        from aas_integration.models import ProductAASProfile
        from aas_integration.services import infer_product_aas_origin

        profile = getattr(instance, "aas_profile", None)
        identity = getattr(instance, "aas_identity", None)
        origin = (
            profile.origin
            if profile and profile.origin != ProductAASProfile.Origin.UNCLASSIFIED
            else infer_product_aas_origin(instance, profile)
        )
        representation["aas_origin"] = origin
        representation["aas_eligible"] = origin != "unclassified"
        representation["aas_fields_supplier_owned"] = (
            origin == "purchased"
        )
        representation["aas_publication_status"] = (
            "out_of_date"
            if identity and identity.out_of_date
            else "current"
            if identity
            else "unpublished"
        )
        representation["documents"] = DocumentSerializer(
            instance.documents.all(),
            many=True,
            context=self.context,
        ).data
        return representation


class MachineSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    sequence = serializers.SerializerMethodField()
    documents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Document.objects.all(),
        required=False,
    )
    projects = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Project.objects.all(),
        required=False,
    )
    project_details = serializers.SerializerMethodField()

    class Meta:
        model = Machine
        fields = "__all__"
        read_only_fields = [
            "company_profile",
            "level",
            "sort_order",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]

    def get_sequence(self, obj):
        return obj.get_sequence()

    def get_project_details(self, obj):
        return [
            {
                "id": project.id,
                "project_name": project.project_name,
                "project_status": project.project_status,
                "project_start_date": project.project_start_date,
                "project_end_date": project.project_end_date,
            }
            for project in obj.projects.all().order_by("project_name", "id")
        ]

    def _get_company_profile_id(self):
        company_profile_id = self.initial_data.get("company_profile") if hasattr(self, "initial_data") else None
        if not company_profile_id and self.instance:
            company_profile_id = self.instance.company_profile_id
        if not company_profile_id:
            request = self.context.get("request")
            user = getattr(request, "user", None)
            company_profile_id = getattr(user, "company_id", None)
        return company_profile_id

    def get_linked_folder_owner_ids(self):
        return None, self._get_company_profile_id(), None

    def validate_parent(self, value):
        if not value:
            return value
        company_profile_id = self._get_company_profile_id()
        if company_profile_id and int(value.company_profile_id) != int(company_profile_id):
            raise serializers.ValidationError("Parent must belong to the same company.")
        if self.instance and value.id == self.instance.id:
            raise serializers.ValidationError("An item cannot be its own parent.")
        if value.level >= MAX_TREE_LEVEL:
            raise serializers.ValidationError(f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels.")
        return value

    def validate_documents(self, value):
        company_profile_id = self._get_company_profile_id()
        for document in value:
            if int(document.folder.company_profile_id or 0) != int(company_profile_id):
                raise serializers.ValidationError("Linked documents must belong to the same company.")
        return value

    def validate_projects(self, value):
        company_profile_id = self._get_company_profile_id()
        for project in value:
            if not project.company_profile_id or int(project.company_profile_id) != int(company_profile_id):
                raise serializers.ValidationError("Linked projects must belong to the same company.")
        return value

    def validate(self, attrs):
        parent = attrs.get("parent", self.instance.parent if self.instance else None)
        company_profile = attrs.get("company_profile", self.instance.company_profile if self.instance else None)
        company_profile_id = company_profile.id if company_profile else self._get_company_profile_id()
        if parent and company_profile_id and int(parent.company_profile_id) != int(company_profile_id):
            raise serializers.ValidationError({"parent": "Parent must belong to the same company."})
        level = (parent.level + 1) if parent else 1
        if level > MAX_TREE_LEVEL:
            raise serializers.ValidationError({"parent": f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels."})
        attrs["level"] = level
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        documents = validated_data.pop("documents", [])
        projects = validated_data.pop("projects", [])
        linked_folders = validated_data.pop("linked_folders", [])
        parent = validated_data.get("parent")
        company_profile = validated_data["company_profile"]
        validated_data["sort_order"] = get_next_company_sort_order(self.Meta.model, company_profile, parent)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        instance = super().create(validated_data)
        instance.documents.set(documents)
        instance.projects.set(projects)
        self.set_linked_folders(instance, linked_folders)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        documents = validated_data.pop("documents", None)
        projects = validated_data.pop("projects", None)
        linked_folders = validated_data.pop("linked_folders", None)
        validated_data.pop("parent", None)
        validated_data.pop("level", None)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        updated = super().update(instance, validated_data)
        if documents is not None:
            updated.documents.set(documents)
        if projects is not None:
            updated.projects.set(projects)
        self.set_linked_folders(updated, linked_folders)
        return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["documents"] = DocumentSerializer(
            instance.documents.all(),
            many=True,
            context=self.context,
        ).data
        return representation


class EquipmentSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    sequence = serializers.SerializerMethodField()
    documents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Document.objects.all(),
        required=False,
    )

    class Meta:
        model = Equipment
        fields = "__all__"
        read_only_fields = [
            "company_profile",
            "level",
            "sort_order",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]

    def get_sequence(self, obj):
        return obj.get_sequence()

    def _get_company_profile_id(self):
        company_profile_id = self.initial_data.get("company_profile") if hasattr(self, "initial_data") else None
        if not company_profile_id and self.instance:
            company_profile_id = self.instance.company_profile_id
        if not company_profile_id:
            request = self.context.get("request")
            user = getattr(request, "user", None)
            company_profile_id = getattr(user, "company_id", None)
        return company_profile_id

    def get_linked_folder_owner_ids(self):
        return None, self._get_company_profile_id(), None

    def validate_parent(self, value):
        if not value:
            return value
        company_profile_id = self._get_company_profile_id()
        if company_profile_id and int(value.company_profile_id) != int(company_profile_id):
            raise serializers.ValidationError("Parent must belong to the same company.")
        if self.instance and value.id == self.instance.id:
            raise serializers.ValidationError("An item cannot be its own parent.")
        if value.level >= MAX_TREE_LEVEL:
            raise serializers.ValidationError(f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels.")
        return value

    def validate_documents(self, value):
        company_profile_id = self._get_company_profile_id()
        for document in value:
            if int(document.folder.company_profile_id or 0) != int(company_profile_id):
                raise serializers.ValidationError("Linked documents must belong to the same company.")
        return value

    def validate(self, attrs):
        parent = attrs.get("parent", self.instance.parent if self.instance else None)
        company_profile = attrs.get("company_profile", self.instance.company_profile if self.instance else None)
        company_profile_id = company_profile.id if company_profile else self._get_company_profile_id()
        if parent and company_profile_id and int(parent.company_profile_id) != int(company_profile_id):
            raise serializers.ValidationError({"parent": "Parent must belong to the same company."})
        level = (parent.level + 1) if parent else 1
        if level > MAX_TREE_LEVEL:
            raise serializers.ValidationError({"parent": f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels."})
        attrs["level"] = level
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        documents = validated_data.pop("documents", [])
        linked_folders = validated_data.pop("linked_folders", [])
        parent = validated_data.get("parent")
        company_profile = validated_data["company_profile"]
        validated_data["sort_order"] = get_next_company_sort_order(self.Meta.model, company_profile, parent)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        instance = super().create(validated_data)
        instance.documents.set(documents)
        self.set_linked_folders(instance, linked_folders)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        documents = validated_data.pop("documents", None)
        linked_folders = validated_data.pop("linked_folders", None)
        validated_data.pop("parent", None)
        validated_data.pop("level", None)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        updated = super().update(instance, validated_data)
        if documents is not None:
            updated.documents.set(documents)
        self.set_linked_folders(updated, linked_folders)
        return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["documents"] = DocumentSerializer(
            instance.documents.all(),
            many=True,
            context=self.context,
        ).data
        return representation


class SolutionDemandSerializer(MatrixTreeSerializer):
    status_options_config_field = "demand_status_options"

    class Meta:
        model = SolutionDemand
        fields = "__all__"
        read_only_fields = ["level", "sort_order", "created_by", "modified_by", "created_at", "updated_at"]


class RequirementSerializer(MatrixTreeSerializer):
    status_options_config_field = "requirement_status_options"

    solution_demand_details = serializers.SerializerMethodField()

    class Meta:
        model = Requirement
        fields = "__all__"
        read_only_fields = ["level", "sort_order", "created_by", "modified_by", "created_at", "updated_at"]

    def get_solution_demand_details(self, obj):
        return [
            {
                "id": demand.id,
                "subject": demand.subject,
                "demand_number": demand.demand_number,
                "sequence": demand.get_sequence(),
            }
            for demand in obj.solution_demands.all()
        ]

    def validate_skill(self, value):
        if value in (None, ""):
            return value
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        config = ECPMPConfig.objects.filter(project_id=project_id).first()
        valid_options = config.skills if config else []
        existing_value = getattr(self.instance, "skill", None)
        if valid_options and value not in valid_options and value != existing_value:
            raise serializers.ValidationError("Invalid requirement skill option.")
        return value

    def validate_solution_demands(self, value):
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        for demand in value:
            if int(demand.project_id) != int(project_id):
                raise serializers.ValidationError("Linked demands must belong to the same project.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        solution_demands = validated_data.pop("solution_demands", [])
        instance = super().create(validated_data)
        instance.solution_demands.set(solution_demands)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        solution_demands = validated_data.pop("solution_demands", None)
        updated = super().update(instance, validated_data)
        if solution_demands is not None:
            updated.solution_demands.set(solution_demands)
        return updated


class TransportSerializer(MatrixTreeSerializer):
    class Meta:
        model = Transport
        fields = "__all__"
        read_only_fields = ["level", "sort_order", "created_by", "modified_by", "created_at", "updated_at"]

    def validate_parent(self, value):
        if value:
            raise serializers.ValidationError("Transport items do not support hierarchy.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        start_date = attrs.get(
            "transport_start_date",
            self.instance.transport_start_date if self.instance else None,
        )
        end_date = attrs.get(
            "transport_end_date",
            self.instance.transport_end_date if self.instance else None,
        )
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError(
                {
                    "transport_end_date": (
                        "Transport End Date cannot be earlier than Transport Start Date."
                    )
                }
            )
        attrs["parent"] = None
        attrs["level"] = 1
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data["level"] = 1
        return super().update(instance, validated_data)


class BillOfMaterialPartSerializer(MatrixTreeSerializer):
    purchase_block_reason = serializers.SerializerMethodField()

    def get_purchase_block_reason(self, obj):
        from project_management.services.engineering_approval import purchase_block_reason
        return purchase_block_reason(obj, self.context.setdefault("engineering_approval_cache", {}))

    status_options_config_field = "bom_status_options"

    requirement_details = serializers.SerializerMethodField()
    available_product_detail = serializers.SerializerMethodField()

    class Meta:
        model = BillOfMaterialPart
        fields = "__all__"
        read_only_fields = [
            "level",
            "sort_order",
            "source_project_assembly",
            "source_workstation",
            "source_workstation_snapshot",
            "source_occurrence_id",
            "is_purchasable",
            "engineering_detached",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]

    def get_requirement_details(self, obj):
        return [
            {
                "id": requirement.id,
                "name": requirement.name,
                "object_number": requirement.object_number,
                "sequence": requirement.get_sequence(),
            }
            for requirement in obj.requirements.all()
        ]

    def get_available_product_detail(self, obj):
        if not obj.available_product:
            return None
        return {
            "id": obj.available_product.id,
            "name": obj.available_product.name,
            "part_number": obj.available_product.part_number,
            "sequence": obj.available_product.get_sequence(),
        }

    def validate_available_product(self, value):
        if not value:
            return value
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        from project_management.models import Project
        project = Project.objects.filter(id=project_id).first()
        if project and value.company_profile_id != project.company_profile_id:
            raise serializers.ValidationError("Available Product must belong to the project company.")
        return value

    def validate_requirements(self, value):
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        for requirement in value:
            if int(requirement.project_id) != int(project_id):
                raise serializers.ValidationError("Linked requirements must belong to the same project.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        if settings.PROJECT_WORKSTATION_COMPOSITION_ENABLED:
            raise serializers.ValidationError("Create and agree parts in Engineering Desk, then use Copy to BoM.")
        requirements = validated_data.pop("requirements", [])
        instance = super().create(validated_data)
        instance.requirements.set(requirements)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        requirements = validated_data.pop("requirements", None)
        updated = super().update(instance, validated_data)
        if requirements is not None:
            updated.requirements.set(requirements)
        return updated


class SolutionSpaceSerializerMixin:
    def _get_company_profile_id(self):
        if self.instance:
            if hasattr(self.instance, "company_profile_id"):
                return self.instance.company_profile_id
            return self.instance.project.company_profile_id

        request = self.context.get("request")
        company_id = getattr(getattr(request, "user", None), "company_id", None)
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if project_id:
            project = Project.objects.filter(id=project_id).only("company_profile_id").first()
            return project.company_profile_id if project else company_id
        return company_id

    def _validate_skills(self, values, field_name):
        if values in (None, ""):
            return []
        if not isinstance(values, list):
            raise serializers.ValidationError("Skills must be a list.")
        normalized = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        is_project_scope = self.Meta.model is ProjectSolutionSpaceItem
        if is_project_scope:
            project_id = (
                self.instance.project_id
                if self.instance
                else self.initial_data.get("project")
            )
            config = ECPMPConfig.objects.filter(project_id=project_id).first()
        else:
            request = self.context.get("request")
            product_name = get_product_name_from_request(request) if request else None
            config_query = CompanyConfig.objects.filter(
                company_profile_id=self._get_company_profile_id(),
            )
            config = (
                config_query.filter(solution=product_name).first()
                if product_name
                else config_query.first()
            )
        valid_options = set(config.skills if config else [])
        existing_values = set(getattr(self.instance, field_name, []) or [])
        invalid_values = [
            value for value in normalized if valid_options and value not in valid_options and value not in existing_values
        ]
        if invalid_values:
            raise serializers.ValidationError("Invalid Solution Space skill option.")
        return normalized

    def validate_required_skills(self, value):
        return self._validate_skills(value, "required_skills")

    def validate_provided_skills(self, value):
        return self._validate_skills(value, "provided_skills")

    def validate_year_of_construction(self, value):
        if value is not None and (value < 1000 or value > 9999):
            raise serializers.ValidationError("Enter a four-digit year.")
        return value

    def validate_assembly_type(self, value):
        if value in (None, ""):
            return None
        valid_values = {
            choice[0] for choice in CompanySolutionSpaceItem.ASSEMBLY_TYPE_CHOICES
        }
        if value not in valid_values:
            raise serializers.ValidationError("Invalid assembly type.")
        return value

    def get_responsible_engineer_detail(self, obj):
        user = obj.responsible_engineer
        if not user:
            return None
        return {
            "id": user.id,
            "name": user.full_name,
            "email": user.email,
        }

    def get_customer_details(self, obj):
        return [
            {
                "id": customer.id,
                "company_name": customer.company_name,
            }
            for customer in obj.customers.all().order_by("company_name", "id")
        ]

    @staticmethod
    def get_related_assembly_details(obj):
        if obj.assembly_type == CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS:
            details = [
                {
                    "id": link.process_assembly_id,
                    "assembly_name": link.process_assembly.assembly_name,
                    "assembly_type": link.process_assembly.assembly_type,
                }
                for link in obj.process_assembly_links.all()
            ]
            return sorted(
                details,
                key=lambda detail: (detail["assembly_name"].casefold(), detail["id"]),
            )
        if obj.assembly_type == CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES:
            details = [
                {
                    "id": link.product_assembly_id,
                    "assembly_name": link.product_assembly.assembly_name,
                    "assembly_type": link.product_assembly.assembly_type,
                }
                for link in obj.product_assembly_links.all()
            ]
            return sorted(
                details,
                key=lambda detail: (detail["assembly_name"].casefold(), detail["id"]),
            )
        return []


class CompanySolutionSpaceItemSerializer(
    SolutionSpaceSerializerMixin,
    LinkedFolderSerializerMixin,
    serializers.ModelSerializer,
):
    related_assembly_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        write_only=True,
    )
    related_process_assembly_ids = serializers.SerializerMethodField()
    related_product_assembly_ids = serializers.SerializerMethodField()
    related_assembly_details = serializers.SerializerMethodField()
    sequence = serializers.SerializerMethodField()
    project_details = serializers.SerializerMethodField()
    customer_details = serializers.SerializerMethodField()
    responsible_engineer_detail = serializers.SerializerMethodField()
    aas_product_detail = serializers.SerializerMethodField()
    documents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Document.objects.all(),
        required=False,
    )

    class Meta:
        model = CompanySolutionSpaceItem
        fields = "__all__"
        read_only_fields = [
            "company_profile",
            "aas_product",
            "level",
            "sort_order",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]

    def get_aas_product_detail(self, obj):
        product = obj.aas_product
        if not product:
            return None
        profile = getattr(product, "aas_profile", None)
        identity = getattr(product, "aas_identity", None)
        return {
            "id": product.id,
            "name": product.name,
            "part_number": product.part_number,
            "origin": getattr(profile, "origin", "unclassified"),
            "aas_status": (
                "out_of_date"
                if identity and identity.out_of_date
                else "current"
                if identity
                else "unpublished"
            ),
        }

    def get_related_process_assembly_ids(self, obj):
        if obj.assembly_type != CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS:
            return []
        return list(
            obj.process_assembly_links.order_by("process_assembly_id").values_list(
                "process_assembly_id",
                flat=True,
            )
        )

    def get_related_product_assembly_ids(self, obj):
        if obj.assembly_type != CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES:
            return []
        return list(
            obj.product_assembly_links.order_by("product_assembly_id").values_list(
                "product_assembly_id",
                flat=True,
            )
        )

    def get_related_assembly_details(self, obj):
        return super().get_related_assembly_details(obj)

    def get_sequence(self, obj):
        return obj.get_sequence()

    def get_project_details(self, obj):
        return [
            {
                "id": project.id,
                "project_name": project.project_name,
                "project_status": project.project_status,
            }
            for project in obj.projects.all().order_by("project_name", "id")
        ]

    def get_linked_folder_owner_ids(self):
        return None, self._get_company_profile_id(), None

    def validate_parent(self, value):
        if not value:
            return value
        company_id = self._get_company_profile_id()
        if company_id and value.company_profile_id != company_id:
            raise serializers.ValidationError("Parent must belong to the same company.")
        if self.instance and value.id == self.instance.id:
            raise serializers.ValidationError("An item cannot be its own parent.")
        if value.level >= MAX_TREE_LEVEL:
            raise serializers.ValidationError(
                f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels."
            )
        return value

    def validate_projects(self, value):
        company_id = self._get_company_profile_id()
        for project in value:
            if project.company_profile_id != company_id:
                raise serializers.ValidationError("Projects must belong to the same company.")
        return value

    def validate_customers(self, value):
        company_id = self._get_company_profile_id()
        for customer in value:
            if customer.company_profile_id != company_id:
                raise serializers.ValidationError("Customers must belong to the company network.")
        return value

    def validate_responsible_engineer(self, value):
        if value and (
            value.company_id != self._get_company_profile_id()
            or not value.is_active
            or value.is_deleted
        ):
            raise serializers.ValidationError("Select an active company user.")
        return value

    def validate_documents(self, value):
        company_id = self._get_company_profile_id()
        for document in value:
            if int(document.folder.company_profile_id or 0) != int(company_id or 0):
                raise serializers.ValidationError("Documents must belong to the same company.")
        return value

    def validate(self, attrs):
        parent = attrs.get("parent", self.instance.parent if self.instance else None)
        level = parent.level + 1 if parent else 1
        if level > MAX_TREE_LEVEL:
            raise serializers.ValidationError(
                {"parent": f"Cannot create hierarchy deeper than {MAX_TREE_LEVEL} levels."}
            )
        related_ids = attrs.get("related_assembly_ids")
        next_type = attrs.get(
            "assembly_type",
            self.instance.assembly_type if self.instance else None,
        )
        if parent and next_type and parent.assembly_type != next_type:
            raise serializers.ValidationError(
                {"parent": "Parent and child must belong to the same assembly catalog."}
            )
        if (
            self.instance
            and next_type != self.instance.assembly_type
            and (
                self.instance.process_assembly_links.exists()
                or self.instance.product_assembly_links.exists()
            )
        ):
            raise serializers.ValidationError(
                {"assembly_type": "Remove linked assemblies before changing the catalog type."}
            )
        if related_ids is not None:
            expected_type = (
                CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES
                if next_type == CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS
                else CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS
                if next_type == CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES
                else None
            )
            if not expected_type and related_ids:
                raise serializers.ValidationError(
                    {"related_assembly_ids": "Choose a Product or Process Assembly catalog type first."}
                )
            company_id = self._get_company_profile_id()
            related = list(
                CompanySolutionSpaceItem.objects.filter(
                    id__in=set(related_ids),
                    company_profile_id=company_id,
                    assembly_type=expected_type,
                ).order_by("id")
            )
            if len(related) != len(set(related_ids)):
                raise serializers.ValidationError(
                    {
                        "related_assembly_ids": (
                            "Linked assemblies must belong to the same company and the opposite catalog."
                        )
                    }
                )
            attrs["related_assembly_ids"] = [item.id for item in related]
        attrs["level"] = level
        return attrs

    @staticmethod
    def _sync_related_assemblies(instance, related_ids, user):
        if related_ids is None:
            return
        if instance.assembly_type == CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS:
            previous_ids = set(
                instance.process_assembly_links.values_list(
                    "process_assembly_id",
                    flat=True,
                )
            )
            instance.process_assembly_links.exclude(
                process_assembly_id__in=related_ids,
            ).delete()
            existing_ids = set(
                instance.process_assembly_links.values_list(
                    "process_assembly_id",
                    flat=True,
                )
            )
            CompanyAssemblyProductionLink.objects.bulk_create(
                [
                    CompanyAssemblyProductionLink(
                        product_assembly=instance,
                        process_assembly_id=related_id,
                        created_by=user,
                    )
                    for related_id in related_ids
                    if related_id not in existing_ids
                ],
                ignore_conflicts=True,
            )
            instance._changed_related_assembly_ids = previous_ids.symmetric_difference(
                set(related_ids)
            )
            return
        if instance.assembly_type == CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES:
            previous_ids = set(
                instance.product_assembly_links.values_list(
                    "product_assembly_id",
                    flat=True,
                )
            )
            instance.product_assembly_links.exclude(
                product_assembly_id__in=related_ids,
            ).delete()
            existing_ids = set(
                instance.product_assembly_links.values_list(
                    "product_assembly_id",
                    flat=True,
                )
            )
            CompanyAssemblyProductionLink.objects.bulk_create(
                [
                    CompanyAssemblyProductionLink(
                        product_assembly_id=related_id,
                        process_assembly=instance,
                        created_by=user,
                    )
                    for related_id in related_ids
                    if related_id not in existing_ids
                ],
                ignore_conflicts=True,
            )
            instance._changed_related_assembly_ids = previous_ids.symmetric_difference(
                set(related_ids)
            )

    @transaction.atomic
    def create(self, validated_data):
        related_ids = validated_data.pop("related_assembly_ids", None)
        projects = validated_data.pop("projects", [])
        customers = validated_data.pop("customers", [])
        documents = validated_data.pop("documents", [])
        linked_folders = validated_data.pop("linked_folders", [])
        parent = validated_data.get("parent")
        company_profile = validated_data["company_profile"]
        validated_data["sort_order"] = get_next_company_sort_order(
            self.Meta.model,
            company_profile,
            parent,
        )
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user
            validated_data["modified_by"] = request.user
        instance = super().create(validated_data)
        instance.projects.set(projects)
        instance.customers.set(customers)
        instance.documents.set(documents)
        self.set_linked_folders(instance, linked_folders)
        self._sync_related_assemblies(
            instance,
            related_ids,
            request.user if request and request.user.is_authenticated else None,
        )
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        related_ids = validated_data.pop("related_assembly_ids", None)
        projects = validated_data.pop("projects", None)
        customers = validated_data.pop("customers", None)
        documents = validated_data.pop("documents", None)
        linked_folders = validated_data.pop("linked_folders", None)
        validated_data.pop("parent", None)
        validated_data.pop("level", None)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["modified_by"] = request.user
        updated = super().update(instance, validated_data)
        if projects is not None:
            updated.projects.set(projects)
        if customers is not None:
            updated.customers.set(customers)
        if documents is not None:
            updated.documents.set(documents)
        self.set_linked_folders(updated, linked_folders)
        self._sync_related_assemblies(
            updated,
            related_ids,
            request.user if request and request.user.is_authenticated else None,
        )
        return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["documents"] = DocumentSerializer(
            instance.documents.all(),
            many=True,
            context=self.context,
        ).data
        return representation


class ProjectSolutionSpaceItemSerializer(
    SolutionSpaceSerializerMixin,
    MatrixTreeSerializer,
):
    requirement_details = serializers.SerializerMethodField()
    customer_details = serializers.SerializerMethodField()
    responsible_engineer_detail = serializers.SerializerMethodField()
    source_company_item_detail = serializers.SerializerMethodField()

    class Meta:
        model = ProjectSolutionSpaceItem
        fields = "__all__"
        read_only_fields = [
            "level",
            "sort_order",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {"assembly_name": {"required": False}}

    def _get_project(self):
        if self.instance:
            return self.instance.project
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        return Project.objects.filter(id=project_id).first()

    def get_requirement_details(self, obj):
        return [
            {
                "id": requirement.id,
                "name": requirement.name,
                "object_number": requirement.object_number,
                "sequence": requirement.get_sequence(),
            }
            for requirement in obj.requirements.all().order_by("sort_order", "id")
        ]

    def get_source_company_item_detail(self, obj):
        source = obj.source_company_item
        if not source:
            return None
        product = source.aas_product
        profile = getattr(product, "aas_profile", None) if product else None
        identity = getattr(product, "aas_identity", None) if product else None
        return {
            "id": source.id,
            "assembly_name": source.assembly_name,
            "sequence": source.get_sequence(),
            "related_assembly_details": self.get_related_assembly_details(source),
            "aas_product_detail": (
                {
                    "id": product.id,
                    "name": product.name,
                    "part_number": product.part_number,
                    "origin": getattr(profile, "origin", "unclassified"),
                    "aas_status": (
                        "out_of_date"
                        if identity and identity.out_of_date
                        else "current"
                        if identity
                        else "unpublished"
                    ),
                }
                if product
                else None
            ),
        }

    def validate_requirements(self, value):
        project = self._get_project()
        for requirement in value:
            if not project or requirement.project_id != project.id:
                raise serializers.ValidationError("Requirements must belong to the same project.")
        return value

    def validate_customers(self, value):
        project = self._get_project()
        if not project:
            return value
        for customer in value:
            same_company = (
                project.company_profile_id
                and customer.company_profile_id == project.company_profile_id
            )
            same_freelancer = (
                not project.company_profile_id and customer.user_id == project.user_id
            )
            if not same_company and not same_freelancer:
                raise serializers.ValidationError("Customers must belong to the project owner network.")
        return value

    def validate_responsible_engineer(self, value):
        if not value:
            return value
        project = self._get_project()
        is_project_member = project and (
            project.user_id == value.id
            or project.team_members.filter(team_member=value).exists()
        )
        if not is_project_member or not value.is_active or value.is_deleted:
            raise serializers.ValidationError("Select an active project team member.")
        return value

    def validate_source_company_item(self, value):
        if not value:
            return value
        project = self._get_project()
        if not project or value.company_profile_id != project.company_profile_id:
            raise serializers.ValidationError("The source assembly must belong to the project company.")
        if value.assembly_type not in {
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        }:
            raise serializers.ValidationError(
                "Classify the source in the Product or Process Assembly Catalog first."
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        source = attrs.get(
            "source_company_item",
            self.instance.source_company_item if self.instance else None,
        )
        if not self.instance and not source:
            raise serializers.ValidationError(
                {
                    "source_company_item": (
                        "Select an assembly approved in the company catalog. "
                        "Project-specific assemblies are not allowed."
                    )
                }
            )
        if (
            self.instance
            and "source_company_item" in attrs
            and attrs["source_company_item"] != self.instance.source_company_item
        ):
            raise serializers.ValidationError(
                {"source_company_item": "The approved company source cannot be changed."}
            )
        canonical_fields = {
            "assembly_name",
            "assembly_type",
            "required_skills",
            "provided_skills",
            "responsible_engineer",
            "year_of_construction",
            "custom_fields",
            "parent",
        }
        if self.instance:
            changed = sorted(
                field
                for field in canonical_fields
                if field in attrs and attrs[field] != getattr(self.instance, field)
            )
            if changed:
                raise serializers.ValidationError(
                    {
                        field: "This value is controlled by the approved company catalog."
                        for field in changed
                    }
                )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        requirements = validated_data.pop("requirements", [])
        customers = validated_data.pop("customers", [])
        source = validated_data["source_company_item"]
        project = validated_data["project"]
        if ProjectSolutionSpaceItem.objects.filter(
            project=project,
            source_company_item=source,
        ).exists():
            raise serializers.ValidationError(
                {"source_company_item": "This company assembly is already selected for the project."}
            )
        validated_data.update(
            {
                "assembly_name": source.assembly_name,
                "assembly_type": source.assembly_type,
                "required_skills": list(source.required_skills or []),
                "provided_skills": list(source.provided_skills or []),
                "responsible_engineer": (
                    source.responsible_engineer
                    if source.responsible_engineer_id
                    and (
                        project.user_id == source.responsible_engineer_id
                        or project.team_members.filter(
                            team_member_id=source.responsible_engineer_id,
                        ).exists()
                    )
                    else None
                ),
                "year_of_construction": source.year_of_construction,
                "custom_fields": dict(source.custom_fields or {}),
                "parent": ProjectSolutionSpaceItem.objects.filter(
                    project=project,
                    source_company_item=source.parent,
                ).first()
                if source.parent_id
                else None,
            }
        )
        instance = super().create(validated_data)
        instance.requirements.set(requirements)
        instance.customers.set(customers)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        requirements = validated_data.pop("requirements", None)
        customers = validated_data.pop("customers", None)
        updated = super().update(instance, validated_data)
        if requirements is not None:
            updated.requirements.set(requirements)
        if customers is not None:
            updated.customers.set(customers)
        return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        source = instance.source_company_item
        if not source:
            representation["approval_state"] = "legacy_unlinked"
            return representation
        representation.update(
            {
                "assembly_name": source.assembly_name,
                "assembly_type": source.assembly_type,
                "required_skills": list(source.required_skills or []),
                "provided_skills": list(source.provided_skills or []),
                "responsible_engineer": source.responsible_engineer_id,
                "year_of_construction": source.year_of_construction,
                "custom_fields": dict(source.custom_fields or {}),
                "approval_state": "company_approved",
            }
        )
        return representation


class MatrixCommentReactionSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = MatrixCommentReaction
        fields = ["id", "key", "user", "user_name", "created_at"]
        read_only_fields = ["user", "user_name", "created_at"]

    def get_user_name(self, obj):
        return obj.user.full_name if obj.user else None


class MatrixCommentSerializer(serializers.ModelSerializer):
    content = serializers.CharField(required=False, allow_blank=True)
    sender = serializers.IntegerField(source="sender_id", read_only=True)
    sender_name = serializers.SerializerMethodField()
    sender_username = serializers.SerializerMethodField()
    sender_image = serializers.SerializerMethodField()
    reply_to = serializers.SerializerMethodField()
    reactions = serializers.SerializerMethodField()
    linked_object_refs = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        write_only=True,
    )

    class Meta:
        model = MatrixComment
        fields = [
            "id",
            "target_type",
            "solution_demand",
            "requirement",
            "transport",
            "available_product_part",
            "machine",
            "equipment",
            "bom_part",
            "company_solution_space_item",
            "project_solution_space_item",
            "sender",
            "sender_name",
            "sender_username",
            "sender_image",
            "content",
            "attachments",
            "linked_objects",
            "mentions",
            "linked_object_refs",
            "replied_to",
            "reply_to",
            "is_deleted",
            "is_edited",
            "edited_at",
            "deleted_at",
            "deleted_by",
            "created_at",
            "updated_at",
            "reactions",
        ]
        read_only_fields = [
            "sender",
            "sender_name",
            "sender_username",
            "sender_image",
            "reply_to",
            "is_deleted",
            "is_edited",
            "edited_at",
            "deleted_at",
            "deleted_by",
            "created_at",
            "updated_at",
            "reactions",
        ]

    def _get_request_user(self):
        request = self.context.get("request")
        if request is not None:
            return getattr(request, "user", None)
        return self.context.get("user")

    def _get_target_from_attrs(self, attrs, instance=None):
        target_type = attrs.get("target_type") or getattr(instance, "target_type", None)
        if target_type == MatrixComment.TARGET_SOLUTION_DEMAND:
            return (
                attrs.get("solution_demand")
                or getattr(instance, "solution_demand", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_REQUIREMENT:
            return (
                attrs.get("requirement") or getattr(instance, "requirement", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_TRANSPORT:
            return (
                attrs.get("transport") or getattr(instance, "transport", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_AVAILABLE_PRODUCT_PART:
            return (
                attrs.get("available_product_part")
                or getattr(instance, "available_product_part", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_MACHINE:
            return (
                attrs.get("machine") or getattr(instance, "machine", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_EQUIPMENT:
            return (
                attrs.get("equipment") or getattr(instance, "equipment", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_BOM_PART:
            return (
                attrs.get("bom_part") or getattr(instance, "bom_part", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_COMPANY_SOLUTION_SPACE:
            return (
                attrs.get("company_solution_space_item")
                or getattr(instance, "company_solution_space_item", None),
                target_type,
            )
        if target_type == MatrixComment.TARGET_PROJECT_SOLUTION_SPACE:
            return (
                attrs.get("project_solution_space_item")
                or getattr(instance, "project_solution_space_item", None),
                target_type,
            )
        return None, target_type

    def get_sender_name(self, obj):
        sender = getattr(obj, "sender", None)
        return sender.full_name if sender else ""

    def get_sender_username(self, obj):
        return self.get_sender_name(obj)

    def get_sender_image(self, obj):
        sender = getattr(obj, "sender", None)
        return sender.user_image if sender else ""

    def get_reply_to(self, obj):
        target = getattr(obj, "replied_to", None)
        if not target:
            return None

        sender = getattr(target, "sender", None)
        content = "" if target.is_deleted else target.content
        return {
            "id": target.id,
            "sender": target.sender_id,
            "sender_username": sender.full_name if sender else "",
            "sender_name": sender.full_name if sender else "",
            "content": content,
            "is_deleted": target.is_deleted,
            "created_at": target.created_at.isoformat() if target.created_at else None,
        }

    def get_reactions(self, obj):
        if getattr(obj, "is_deleted", False):
            return []

        viewer = self._get_request_user()
        viewer_id = getattr(viewer, "id", None)

        grouped = {}
        for reaction in obj.reactions.all():
            bucket = grouped.get(reaction.key)
            if not bucket:
                bucket = {
                    "emoji": reaction.key,
                    "count": 0,
                    "users": [],
                    "reacted_by_me": False,
                }
                grouped[reaction.key] = bucket

            bucket["count"] += 1
            bucket["users"].append(
                {
                    "id": reaction.user.id,
                    "name": reaction.user.full_name,
                    "user_image": reaction.user.user_image,
                }
            )
            if viewer_id is not None and reaction.user_id == viewer_id:
                bucket["reacted_by_me"] = True

        reactions = list(grouped.values())
        reactions.sort(key=lambda item: (-item["count"], item["emoji"]))
        return reactions

    def validate_attachments(self, value):
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("Attachments must be a list.")
        return value

    def validate_mentions(self, value):
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("Mentions must be a list.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)

        request_user = self._get_request_user()
        instance = self.instance

        if instance and instance.is_deleted:
            raise serializers.ValidationError("Deleted comments cannot be updated.")

        target, target_type = self._get_target_from_attrs(attrs, instance)
        replied_to = attrs.get("replied_to")
        content = str(attrs.get("content", getattr(instance, "content", "")) or "").strip()
        attachments_raw = attrs.get("attachments", getattr(instance, "attachments", [])) or []
        linked_objects_raw = attrs.get(
            "linked_objects",
            getattr(instance, "linked_objects", []),
        ) or []
        mentions_raw = attrs.get(
            "mentions",
            getattr(instance, "mentions", []) if instance else [],
        ) or []
        linked_object_refs_raw = attrs.get("linked_object_refs") or []

        if instance:
            immutable_target_fields = (
                "target_type",
                "solution_demand",
                "requirement",
                "transport",
                "available_product_part",
                "machine",
                "equipment",
                "bom_part",
                "company_solution_space_item",
                "project_solution_space_item",
            )
            for field_name in immutable_target_fields:
                if field_name not in attrs:
                    continue
                current_value = getattr(instance, field_name)
                next_value = attrs[field_name]
                current_id = getattr(current_value, "id", current_value)
                next_id = getattr(next_value, "id", next_value)
                if current_id != next_id:
                    raise serializers.ValidationError(
                        {field_name: "Comment target cannot be changed."}
                    )

            if "replied_to" in attrs and replied_to and instance.replied_to_id != replied_to.id:
                raise serializers.ValidationError(
                    {"replied_to": "Reply target cannot be changed after creation."}
                )
            if "attachments" in attrs and attrs["attachments"] != instance.attachments:
                raise serializers.ValidationError(
                    {"attachments": "Attachments cannot be changed after send."}
                )

        if replied_to is not None:
            if replied_to.target_type != target_type:
                raise serializers.ValidationError(
                    {"replied_to": "Reply target must belong to the same matrix item."}
                )
            reply_target_id = (
                replied_to.solution_demand_id
                or replied_to.requirement_id
                or replied_to.transport_id
                or replied_to.available_product_part_id
                or replied_to.machine_id
                or replied_to.equipment_id
                or replied_to.bom_part_id
                or replied_to.company_solution_space_item_id
                or replied_to.project_solution_space_item_id
            )
            current_target_id = getattr(target, "id", None)
            if reply_target_id != current_target_id:
                raise serializers.ValidationError(
                    {"replied_to": "Reply target must belong to the same matrix item."}
                )
            if replied_to.replied_to_id is not None:
                raise serializers.ValidationError(
                    {"replied_to": "Replies can only be added to top-level comments."}
                )

        try:
            normalized_linked_objects = normalize_linked_objects(linked_objects_raw)
        except ValueError as exc:
            raise serializers.ValidationError({"linked_objects": str(exc)})

        normalized_linked_object_refs = normalize_linked_object_refs(linked_object_refs_raw)
        normalized_linked_object_refs_from_text = extract_linked_objects_from_text(content)
        merged_linked_objects = merge_linked_objects(
            normalized_linked_objects,
            normalized_linked_object_refs,
            normalized_linked_object_refs_from_text,
        )
        accessible_linked_objects = filter_accessible_linked_objects(
            merged_linked_objects,
            request_user,
        )

        max_linked_objects = int(getattr(settings, "CHAT_MAX_LINKED_OBJECTS", 5) or 5)
        if len(accessible_linked_objects) > max_linked_objects:
            raise serializers.ValidationError(
                {
                    "linked_objects": (
                        f"Maximum {max_linked_objects} linked objects are allowed per comment."
                    )
                }
            )

        normalized_attachments = self._normalize_attachments(attachments_raw)
        if "mentions" in attrs or not instance:
            attrs["mentions"] = self._normalize_mentions(
                mentions_raw,
                target,
                target_type,
            )

        if not content and not normalized_attachments and not accessible_linked_objects:
            raise serializers.ValidationError(
                {"content": "Comment content cannot be empty."}
            )

        attrs["content"] = content
        attrs["attachments"] = normalized_attachments
        attrs["linked_objects"] = accessible_linked_objects
        attrs.pop("linked_object_refs", None)
        return attrs

    def _normalize_attachments(self, attachments_raw):
        max_attachment_files = int(getattr(settings, "CHAT_ATTACHMENT_MAX_FILES", 5) or 5)
        max_total_attachment_size_mb = int(
            getattr(settings, "CHAT_ATTACHMENT_MAX_TOTAL_SIZE_MB", 25) or 25
        )
        max_total_attachment_size = max_total_attachment_size_mb * 1024 * 1024
        blocked_extensions = {
            str(ext).lower()
            for ext in getattr(
                settings,
                "CHAT_BLOCKED_ATTACHMENT_EXTENSIONS",
                [
                    ".exe",
                    ".bat",
                    ".cmd",
                    ".msi",
                    ".sql",
                    ".com",
                    ".scr",
                    ".pif",
                    ".vb",
                    ".vbs",
                    ".jar",
                ],
            )
        }

        if len(attachments_raw) > max_attachment_files:
            raise serializers.ValidationError(
                {
                    "attachments": (
                        f"Maximum {max_attachment_files} attachments are allowed per comment."
                    )
                }
            )

        normalized_attachments = []
        total_attachment_size = 0

        for index, attachment in enumerate(attachments_raw, start=1):
            if not isinstance(attachment, dict):
                raise serializers.ValidationError(
                    {"attachments": f"Attachment #{index} must be an object."}
                )

            attachment_url = str(
                attachment.get("url")
                or attachment.get("file_url")
                or attachment.get("attachment_url")
                or ""
            ).strip()
            if not attachment_url:
                raise serializers.ValidationError(
                    {"attachments": f"Attachment #{index} URL is required."}
                )

            attachment_name = str(
                attachment.get("file_name") or attachment.get("name") or ""
            ).strip()
            if not attachment_name:
                attachment_name = os.path.basename(attachment_url.split("?")[0])
            if not attachment_name:
                attachment_name = f"attachment-{index}"

            attachment_extension = str(attachment.get("extension") or "").strip().lower()
            if not attachment_extension:
                attachment_extension = os.path.splitext(attachment_name)[1].lower()
            if attachment_extension and not attachment_extension.startswith("."):
                attachment_extension = f".{attachment_extension}"

            if attachment_extension and attachment_extension in blocked_extensions:
                raise serializers.ValidationError(
                    {
                        "attachments": (
                            f"Attachment #{index} uses a blocked file type ({attachment_extension})."
                        )
                    }
                )

            raw_size = attachment.get("size")
            if raw_size in (None, ""):
                attachment_size = 0
            else:
                try:
                    attachment_size = int(raw_size)
                except (TypeError, ValueError):
                    raise serializers.ValidationError(
                        {"attachments": f"Attachment #{index} size must be an integer."}
                    )

            if attachment_size < 0:
                raise serializers.ValidationError(
                    {"attachments": f"Attachment #{index} size must be non-negative."}
                )

            total_attachment_size += attachment_size
            normalized_attachments.append(
                {
                    "url": attachment_url,
                    "file_name": attachment_name,
                    "mime_type": str(attachment.get("mime_type") or "").strip().lower() or None,
                    "size": attachment_size,
                    "extension": attachment_extension or None,
                }
            )

        if total_attachment_size > max_total_attachment_size:
            raise serializers.ValidationError(
                {
                    "attachments": (
                        f"Total attachment size cannot exceed {max_total_attachment_size_mb}MB."
                    )
                }
            )

        return normalized_attachments

    def _normalize_mentions(self, mentions_raw, target, target_type):
        if not mentions_raw:
            return []

        if target is None or not target_type:
            raise serializers.ValidationError(
                {"mentions": "Matrix comment target is required for mentions."}
            )

        mentioned_user_ids = []
        seen_user_ids = set()
        for index, mention in enumerate(mentions_raw, start=1):
            if not isinstance(mention, dict):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} must be an object."}
                )

            raw_user_id = mention.get("user_id", mention.get("id"))
            if raw_user_id in (None, ""):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} user_id is required."}
                )

            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} user_id must be an integer."}
                )

            if user_id in seen_user_ids:
                continue

            seen_user_ids.add(user_id)
            mentioned_user_ids.append(user_id)

        mention_snapshots = get_matrix_mention_user_snapshots(
            target,
            target_type,
            mentioned_user_ids,
        )
        invalid_user_ids = [
            user_id
            for user_id in mentioned_user_ids
            if user_id not in mention_snapshots
        ]
        if invalid_user_ids:
            raise serializers.ValidationError(
                {
                    "mentions": (
                        "Invalid or out-of-scope mention user IDs: "
                        f"{', '.join(str(user_id) for user_id in invalid_user_ids)}."
                    )
                }
            )

        return [mention_snapshots[user_id] for user_id in mentioned_user_ids]

    def create(self, validated_data):
        request_user = self._get_request_user()
        if request_user is not None and getattr(request_user, "is_authenticated", False):
            validated_data["sender"] = request_user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        for field in (
            "target_type",
            "solution_demand",
            "requirement",
            "transport",
            "available_product_part",
            "machine",
            "equipment",
            "bom_part",
            "company_solution_space_item",
            "project_solution_space_item",
            "attachments",
            "replied_to",
        ):
            validated_data.pop(field, None)

        content_changed = (
            "content" in validated_data and validated_data["content"] != instance.content
        )
        linked_objects_changed = (
            "linked_objects" in validated_data
            and validated_data["linked_objects"] != (instance.linked_objects or [])
        )
        mentions_changed = (
            "mentions" in validated_data
            and validated_data["mentions"] != (instance.mentions or [])
        )

        if content_changed or linked_objects_changed or mentions_changed:
            validated_data["is_edited"] = True
            validated_data["edited_at"] = timezone.now()

        return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.is_deleted:
            representation["content"] = ""
            representation["attachments"] = []
            representation["linked_objects"] = []
            representation["mentions"] = []
            representation["reactions"] = []
            return representation

        request_user = self._get_request_user()
        try:
            representation["linked_objects"] = resolve_linked_objects(
                getattr(instance, "linked_objects", None) or [],
                request_user,
            )
        except Exception:
            representation["linked_objects"] = []

        return representation


class MatrixCommentThreadSerializer(MatrixCommentSerializer):
    replies = serializers.SerializerMethodField()

    class Meta(MatrixCommentSerializer.Meta):
        fields = MatrixCommentSerializer.Meta.fields + ["replies"]
        read_only_fields = list(MatrixCommentSerializer.Meta.read_only_fields) + ["replies"]

    def get_replies(self, obj):
        replies = obj.replies.order_by("created_at")
        return MatrixCommentSerializer(replies, many=True, context=self.context).data
