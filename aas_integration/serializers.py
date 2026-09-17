from rest_framework import serializers

from project_management.models import PartsListConfig

from .models import (
    AASFieldProvenance,
    AASPublication,
    AASSubmodelFieldMapping,
    AASSubmodelTemplate,
    ExternalAASGrant,
    FestoHandover,
    ProductAASSubmodelAssignment,
    ProductAASProfile,
)
from .profiles import is_product_aas_column, product_aas_field_contract
from .services import current_publication
from .submodel_templates import template_element_value


class AASSubmodelFieldMappingSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField()

    class Meta:
        model = AASSubmodelFieldMapping
        fields = [
            "id",
            "element_path",
            "element_id_short",
            "model_type",
            "semantic_id",
            "value_type",
            "source",
            "source_key",
            "editable",
            "required",
            "sort_order",
        ]
        read_only_fields = [
            "id",
            "element_path",
            "element_id_short",
            "model_type",
            "semantic_id",
            "value_type",
            "sort_order",
        ]

    def validate(self, attrs):
        source = attrs.get("source", getattr(self.instance, "source", None))
        source_key = attrs.get(
            "source_key",
            getattr(self.instance, "source_key", ""),
        )
        if source in {
            AASSubmodelFieldMapping.Source.PROFILE_FIELD,
            AASSubmodelFieldMapping.Source.CUSTOM_FIELD,
        } and not source_key:
            raise serializers.ValidationError(
                {"source_key": "A source key is required for this mapping."}
            )
        return attrs


class AASSubmodelTemplateSerializer(serializers.ModelSerializer):
    field_mappings = AASSubmodelFieldMappingSerializer(many=True, required=False)
    field_count = serializers.IntegerField(source="field_mappings.count", read_only=True)
    can_delete = serializers.SerializerMethodField()
    concept_description_count = serializers.SerializerMethodField()
    supplementary_files = serializers.SerializerMethodField()

    class Meta:
        model = AASSubmodelTemplate
        fields = [
            "id",
            "template_identifier",
            "semantic_id",
            "id_short",
            "name",
            "version",
            "status",
            "source_format",
            "definition",
            "concept_description_count",
            "supplementary_files",
            "profile_keys",
            "validation_report",
            "checksum",
            "field_mappings",
            "field_count",
            "can_delete",
            "created_by",
            "activated_by",
            "activated_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "template_identifier",
            "semantic_id",
            "id_short",
            "status",
            "source_format",
            "definition",
            "validation_report",
            "checksum",
            "created_by",
            "activated_by",
            "activated_at",
            "created_at",
            "updated_at",
        ]

    def get_can_delete(self, template):
        return template.status in {
            AASSubmodelTemplate.Status.DRAFT,
            AASSubmodelTemplate.Status.INVALID,
        }

    def get_concept_description_count(self, template):
        return len(template.concept_descriptions or [])

    def get_supplementary_files(self, template):
        return [
            {
                "path": item.path,
                "content_type": item.content_type,
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in template.supplementary_files.all()
        ]

    def validate_profile_keys(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Use a list of product profile keys.")
        cleaned = []
        for item in value:
            key = str(item or "").strip()
            if not key or len(key) > 100:
                raise serializers.ValidationError(
                    "Every product profile key must contain 1 to 100 characters."
                )
            if key not in cleaned:
                cleaned.append(key)
        return cleaned

    def update(self, instance, validated_data):
        if instance.status not in {
            AASSubmodelTemplate.Status.DRAFT,
            AASSubmodelTemplate.Status.INVALID,
        }:
            raise serializers.ValidationError(
                "Create a new template version instead of modifying an active or deprecated version."
            )
        mappings = validated_data.pop("field_mappings", None)
        instance = super().update(instance, validated_data)
        if mappings is not None:
            existing = {mapping.id: mapping for mapping in instance.field_mappings.all()}
            incoming_ids = set()
            for mapping_data in mappings:
                mapping_id = mapping_data.pop("id", None)
                if not mapping_id or mapping_id not in existing:
                    raise serializers.ValidationError(
                        {"field_mappings": "Mappings must reference fields imported with this template."}
                    )
                incoming_ids.add(mapping_id)
                mapping = existing[mapping_id]
                for key, value in mapping_data.items():
                    setattr(mapping, key, value)
                if mapping_data:
                    mapping.save(update_fields=[*mapping_data.keys()])
            if incoming_ids != set(existing):
                raise serializers.ValidationError(
                    {"field_mappings": "Submit every imported field mapping when updating a template."}
                )
        return instance


class ProductAASProfileSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", max_length=255)
    article_number = serializers.CharField(source="product.part_number", allow_blank=True, allow_null=True)
    editable = serializers.SerializerMethodField()
    field_contract = serializers.SerializerMethodField()
    technical_fields = serializers.SerializerMethodField()
    technical_values = serializers.DictField(
        child=serializers.JSONField(),
        required=False,
        write_only=True,
    )
    files = serializers.SerializerMethodField()
    registered_submodels = serializers.SerializerMethodField()
    available_submodels = serializers.SerializerMethodField()
    assigned_submodel_template_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
    )
    submodel_values = serializers.DictField(
        child=serializers.DictField(child=serializers.JSONField()),
        required=False,
        write_only=True,
    )

    class Meta:
        model = ProductAASProfile
        fields = [
            "product",
            "origin",
            "editable",
            "product_name",
            "article_number",
            "manufacturer_name",
            "supplier_name",
            "supplier_aas_id",
            "global_asset_id",
            "order_code",
            "profile_key",
            "profile_version",
            "standard_fields",
            "source_payload_hash",
            "field_contract",
            "technical_fields",
            "technical_values",
            "files",
            "registered_submodels",
            "available_submodels",
            "assigned_submodel_template_ids",
            "submodel_values",
            "updated_at",
        ]
        read_only_fields = [
            "product",
            "origin",
            "supplier_name",
            "supplier_aas_id",
            "global_asset_id",
            "order_code",
            "profile_key",
            "profile_version",
            "standard_fields",
            "source_payload_hash",
            "updated_at",
        ]

    def get_editable(self, profile):
        return bool(
            self.context.get("can_write")
            and profile.origin == ProductAASProfile.Origin.MANUFACTURED
        )

    def get_field_contract(self, _profile):
        return product_aas_field_contract()

    def _technical_columns(self, profile):
        config = PartsListConfig.objects.filter(
            company_profile=profile.product.company_profile,
            project__isnull=True,
        ).first()
        return [
            column
            for column in (config.available_product_columns if config else [])
            if column.get("key")
            and column.get("key") != "manufacturer"
            and is_product_aas_column(column)
        ]

    def get_technical_fields(self, profile):
        custom_fields = profile.product.custom_fields or {}
        editable = self.get_editable(profile)
        return [
            {
                "key": column["key"],
                "label": column.get("label") or column["key"],
                "label_en": column.get("label_en") or "",
                "label_de": column.get("label_de") or "",
                "label_fr": column.get("label_fr") or "",
                "data_type": column.get("type") or "text",
                "unit": column.get("unit") or "",
                "options": column.get("options") or [],
                "semantic_id": column.get("idta_semantic_id") or "",
                "value": custom_fields.get(column["key"]),
                "editable": editable,
            }
            for column in self._technical_columns(profile)
        ]

    def get_files(self, profile):
        return [
            {
                "id": document.id,
                "name": document.file_name or "",
                "document_type": document.document_type or "",
                "status": document.status or "",
            }
            for document in profile.product.documents.all().order_by("file_name", "id")
        ]

    def _registered_templates(self, profile):
        return (
            AASSubmodelTemplate.objects.filter(
                company=profile.product.company_profile,
                status=AASSubmodelTemplate.Status.ACTIVE,
                product_assignments__profile=profile,
            )
            .prefetch_related("field_mappings")
            .order_by("name", "template_identifier")
        )

    def get_available_submodels(self, profile):
        assigned_ids = set(
            profile.submodel_assignments.values_list("template_id", flat=True)
        )
        templates = (
            AASSubmodelTemplate.objects.filter(
                company=profile.product.company_profile,
                status=AASSubmodelTemplate.Status.ACTIVE,
            )
            .order_by("name", "template_identifier")
        )
        return [
            {
                "id": template.id,
                "template_identifier": template.template_identifier,
                "semantic_id": template.semantic_id,
                "id_short": template.id_short,
                "name": template.name,
                "version": template.version,
                "assigned": template.id in assigned_ids,
            }
            for template in templates
            if not template.profile_keys
            or profile.profile_key in template.profile_keys
        ]

    @staticmethod
    def _mapping_value(profile, mapping):
        product = profile.product
        if mapping.source == AASSubmodelFieldMapping.Source.PRODUCT_NAME:
            return product.name
        if mapping.source == AASSubmodelFieldMapping.Source.ARTICLE_NUMBER:
            return product.part_number or ""
        if mapping.source == AASSubmodelFieldMapping.Source.MANUFACTURER_NAME:
            return profile.manufacturer_name
        if mapping.source == AASSubmodelFieldMapping.Source.CUSTOM_FIELD:
            return (product.custom_fields or {}).get(mapping.source_key)
        if mapping.source == AASSubmodelFieldMapping.Source.PROFILE_FIELD:
            stored = (profile.standard_fields or {}).get("registered_submodels", {})
            template_values = stored.get(mapping.template.template_identifier, {})
            if isinstance(template_values, dict):
                return (template_values.get("values") or {}).get(
                    mapping.source_key or mapping.element_path
                )
        if mapping.source == AASSubmodelFieldMapping.Source.TEMPLATE_DEFAULT:
            return template_element_value(
                mapping.template.definition,
                mapping.element_path,
            )
        return None

    def get_registered_submodels(self, profile):
        editable_profile = self.get_editable(profile)
        result = []
        for template in self._registered_templates(profile):
            if template.profile_keys and profile.profile_key not in template.profile_keys:
                continue
            result.append(
                {
                    "id": template.id,
                    "template_identifier": template.template_identifier,
                    "semantic_id": template.semantic_id,
                    "id_short": template.id_short,
                    "name": template.name,
                    "version": template.version,
                    "fields": [
                        {
                            "element_path": mapping.element_path,
                            "element_id_short": mapping.element_id_short,
                            "model_type": mapping.model_type,
                            "semantic_id": mapping.semantic_id,
                            "value_type": mapping.value_type,
                            "required": mapping.required,
                            "source": mapping.source,
                            "value": self._mapping_value(profile, mapping),
                            "editable": bool(
                                editable_profile
                                and mapping.editable
                                and mapping.source
                                == AASSubmodelFieldMapping.Source.PROFILE_FIELD
                            ),
                        }
                        for mapping in template.field_mappings.all()
                    ],
                }
            )
        return result

    @staticmethod
    def _validate_registered_value(mapping, value):
        if value in (None, ""):
            return
        if mapping.model_type == "MultiLanguageProperty":
            if not isinstance(value, dict) or not all(
                isinstance(language, str)
                and language.strip()
                and isinstance(text, str)
                for language, text in value.items()
            ):
                raise serializers.ValidationError(
                    "Use a language-to-text object for this multilingual field."
                )
            return
        if mapping.model_type == "Range":
            if not isinstance(value, dict) or set(value) - {"min", "max"}:
                raise serializers.ValidationError(
                    "Use an object containing min and max values for this range."
                )
            for range_value in value.values():
                ProductAASProfileSerializer._validate_scalar_value(
                    mapping.value_type,
                    range_value,
                )
            return
        ProductAASProfileSerializer._validate_scalar_value(
            mapping.value_type,
            value,
        )

    @staticmethod
    def _validate_scalar_value(value_type, value):
        if value in (None, ""):
            return
        normalized = str(value_type or "").lower().replace("xsd:", "xs:")
        local_name = normalized.rsplit(":", 1)[-1]
        if local_name == "boolean":
            serializers.BooleanField().run_validation(value)
        elif local_name in {
            "byte",
            "int",
            "integer",
            "long",
            "negativeinteger",
            "nonnegativeinteger",
            "nonpositiveinteger",
            "positiveinteger",
            "short",
            "unsignedbyte",
            "unsignedint",
            "unsignedlong",
            "unsignedshort",
        }:
            serializers.IntegerField().run_validation(value)
        elif local_name in {"decimal", "double", "float"}:
            serializers.DecimalField(
                max_digits=50,
                decimal_places=20,
            ).run_validation(value)
        elif local_name == "date":
            serializers.DateField().run_validation(value)
        elif local_name in {"datetime", "datetimestamp"}:
            serializers.DateTimeField().run_validation(value)

    def validate(self, attrs):
        profile = self.instance
        if profile and profile.origin != ProductAASProfile.Origin.MANUFACTURED:
            raise serializers.ValidationError(
                {"origin": "Only manufactured-product AAS fields can be edited in Links."}
            )
        if not self.context.get("can_write"):
            raise serializers.ValidationError("You do not have permission to edit this AAS profile.")
        assigned_template_ids = attrs.get("assigned_submodel_template_ids")
        if assigned_template_ids is not None:
            if len(assigned_template_ids) != len(set(assigned_template_ids)):
                raise serializers.ValidationError(
                    {
                        "assigned_submodel_template_ids": (
                            "Select each submodel template only once."
                        )
                    }
                )
            templates = AASSubmodelTemplate.objects.filter(
                id__in=assigned_template_ids,
                company=profile.product.company_profile,
                status=AASSubmodelTemplate.Status.ACTIVE,
            )
            allowed_ids = {
                template.id
                for template in templates
                if not template.profile_keys
                or profile.profile_key in template.profile_keys
            }
            if allowed_ids != set(assigned_template_ids):
                raise serializers.ValidationError(
                    {
                        "assigned_submodel_template_ids": (
                            "Every selected submodel must be an active company "
                            "template for this product profile."
                        )
                    }
                )
        technical_values = attrs.get("technical_values")
        if technical_values is not None:
            columns = {
                column["key"]: column for column in self._technical_columns(profile)
            }
            allowed_keys = set(columns)
            unknown_keys = sorted(set(technical_values) - allowed_keys)
            if unknown_keys:
                raise serializers.ValidationError(
                    {
                        "technical_values": {
                            key: "This field is not configured for the company product list."
                            for key in unknown_keys
                        }
                    }
                )
            value_errors = {}
            for key, value in technical_values.items():
                if value in (None, ""):
                    continue
                column = columns[key]
                try:
                    if column.get("type") == "number":
                        serializers.DecimalField(
                            max_digits=30,
                            decimal_places=10,
                        ).run_validation(value)
                    elif column.get("type") == "date":
                        serializers.DateField().run_validation(value)
                    elif column.get("type") == "enum" and value not in (
                        column.get("options") or []
                    ):
                        raise serializers.ValidationError(
                            "Choose one of the configured values."
                        )
                except serializers.ValidationError as exc:
                    value_errors[key] = exc.detail
            if value_errors:
                raise serializers.ValidationError(
                    {"technical_values": value_errors}
                )
        submodel_values = attrs.get("submodel_values")
        if submodel_values is not None:
            registered_templates = self._registered_templates(profile)
            if assigned_template_ids is not None:
                registered_templates = AASSubmodelTemplate.objects.filter(
                    id__in=assigned_template_ids,
                    company=profile.product.company_profile,
                    status=AASSubmodelTemplate.Status.ACTIVE,
                ).prefetch_related("field_mappings")
            templates = {
                template.template_identifier: template
                for template in registered_templates
                if not template.profile_keys
                or profile.profile_key in template.profile_keys
            }
            errors = {}
            for template_identifier, values in submodel_values.items():
                template = templates.get(template_identifier)
                if template is None:
                    errors[template_identifier] = "This submodel template is not active for the product profile."
                    continue
                editable_fields = {
                    mapping.element_path: mapping
                    for mapping in template.field_mappings.all()
                    if mapping.editable
                    and mapping.source
                    == AASSubmodelFieldMapping.Source.PROFILE_FIELD
                }
                unknown_paths = sorted(set(values) - set(editable_fields))
                if unknown_paths:
                    errors[template_identifier] = {
                        path: "This registered submodel field is not editable."
                        for path in unknown_paths
                    }
                    continue
                value_errors = {}
                for path, value in values.items():
                    try:
                        self._validate_registered_value(
                            editable_fields[path],
                            value,
                        )
                    except serializers.ValidationError as exc:
                        value_errors[path] = exc.detail
                if value_errors:
                    errors[template_identifier] = value_errors
            if errors:
                raise serializers.ValidationError({"submodel_values": errors})
        return attrs

    def update(self, instance, validated_data):
        product_data = validated_data.pop("product", {})
        technical_values = validated_data.pop("technical_values", None)
        submodel_values = validated_data.pop("submodel_values", None)
        assigned_template_ids = validated_data.pop(
            "assigned_submodel_template_ids",
            None,
        )
        product = instance.product
        if assigned_template_ids is not None:
            selected_templates = {
                template.id: template
                for template in AASSubmodelTemplate.objects.filter(
                    id__in=assigned_template_ids,
                    company=instance.product.company_profile,
                    status=AASSubmodelTemplate.Status.ACTIVE,
                )
            }
            current_ids = set(
                instance.submodel_assignments.values_list("template_id", flat=True)
            )
            selected_ids = set(assigned_template_ids)
            instance.submodel_assignments.filter(
                template_id__in=current_ids - selected_ids
            ).delete()
            request = self.context.get("request")
            ProductAASSubmodelAssignment.objects.bulk_create(
                [
                    ProductAASSubmodelAssignment(
                        profile=instance,
                        template=selected_templates[template_id],
                        template_identifier=selected_templates[
                            template_id
                        ].template_identifier,
                        assigned_by=getattr(request, "user", None),
                    )
                    for template_id in selected_ids - current_ids
                ]
            )
        product_fields = []
        if "name" in product_data and product.name != product_data["name"]:
            product.name = product_data["name"]
            product_fields.append("name")
        if "part_number" in product_data and product.part_number != product_data["part_number"]:
            product.part_number = product_data["part_number"]
            product_fields.append("part_number")
        if product_fields:
            product_fields.append("updated_at")
            product.save(update_fields=product_fields)
        if technical_values is not None:
            product.custom_fields = {
                **(product.custom_fields or {}),
                **technical_values,
            }
            product.save(update_fields=["custom_fields", "updated_at"])
        if submodel_values is not None:
            standard_fields = dict(instance.standard_fields or {})
            registered = dict(standard_fields.get("registered_submodels") or {})
            templates = {
                template.template_identifier: template
                for template in self._registered_templates(instance)
            }
            for template_identifier, values in submodel_values.items():
                current = dict(registered.get(template_identifier) or {})
                current_values = dict(current.get("values") or {})
                mappings_by_path = {
                    mapping.element_path: mapping
                    for mapping in templates[template_identifier].field_mappings.all()
                }
                for path, value in values.items():
                    mapping = mappings_by_path[path]
                    current_values[mapping.source_key or path] = value
                current["values"] = current_values
                registered[template_identifier] = current
            standard_fields["registered_submodels"] = registered
            instance.standard_fields = standard_fields
            validated_data["standard_fields"] = standard_fields
        updated = super().update(instance, validated_data)
        from .services import mark_product_out_of_date

        request = self.context.get("request")
        user = getattr(request, "user", None)
        mark_product_out_of_date(product, getattr(user, "id", None))
        return updated


class ProductOriginClassificationSerializer(serializers.Serializer):
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=500,
    )
    origin = serializers.ChoiceField(choices=ProductAASProfile.Origin.choices)

    def validate_origin(self, value):
        if value == ProductAASProfile.Origin.UNCLASSIFIED:
            raise serializers.ValidationError("Choose manufactured or purchased.")
        return value


class AASPublicationSerializer(serializers.ModelSerializer):
    is_active = serializers.SerializerMethodField()

    def get_is_active(self, publication):
        active_publication_id = self.context.get("active_publication_id")
        if active_publication_id is not None:
            return publication.id == active_publication_id
        active_publication = current_publication(publication.resource)
        return bool(active_publication and active_publication.id == publication.id)

    class Meta:
        model = AASPublication
        fields = [
            "id",
            "revision",
            "source_hash",
            "status",
            "is_active",
            "endpoint",
            "validation_report",
            "published_by",
            "published_at",
            "created_at",
        ]
        read_only_fields = fields


class ExternalAASGrantSerializer(serializers.ModelSerializer):
    partner = serializers.CharField(source="client.name", read_only=True)

    class Meta:
        model = ExternalAASGrant
        fields = ["id", "partner", "publication", "can_read", "expires_at", "revoked_at", "created_at"]
        read_only_fields = fields


class AASFieldProvenanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = AASFieldProvenance
        fields = [
            "field_path",
            "owner",
            "supplier",
            "source_aas_id",
            "last_value",
            "manual_override",
            "refresh_state",
            "proposed_value",
            "updated_at",
        ]
        read_only_fields = fields


class FestoHandoverSerializer(serializers.ModelSerializer):
    publication_detail = AASPublicationSerializer(source="publication", read_only=True)
    launch_url = serializers.URLField(source="external_url", read_only=True)

    class Meta:
        model = FestoHandover
        fields = [
            "id",
            "project",
            "publication",
            "publication_detail",
            "item_snapshot",
            "context_snapshot",
            "status",
            "aas_endpoint",
            "launch_url",
            "grant_expires_at",
            "attempts",
            "failure_code",
            "failure_message",
            "confirmed_at",
            "launched_at",
            "created_at",
        ]
        read_only_fields = fields
