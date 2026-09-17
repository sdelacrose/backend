from rest_framework import serializers


class SupplierTechnicalFieldSerializer(serializers.Serializer):
    key = serializers.RegexField(
        regex=r"^[A-Za-z][A-Za-z0-9_]{0,127}$",
        max_length=128,
    )
    label = serializers.CharField(max_length=255)
    value = serializers.CharField(max_length=2000)
    semantic_id = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
    )


class SupplierProductImportSerializer(serializers.Serializer):
    source = serializers.ChoiceField(choices=("ai_search",))
    source_product_id = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
    )
    name = serializers.CharField(max_length=255)
    article_number = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )
    supplier = serializers.CharField(max_length=255)
    supplier_aas_id = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
    )
    global_asset_id = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
    )
    asset_kind = serializers.ChoiceField(
        choices=("Type", "Instance"),
        required=False,
        default="Type",
    )
    product_url = serializers.URLField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
    )
    image_url = serializers.URLField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
    )
    technical_fields = SupplierTechnicalFieldSerializer(
        many=True,
        required=False,
        default=list,
        max_length=50,
    )

    def validate(self, attrs):
        if not attrs["supplier_aas_id"] and not attrs["global_asset_id"]:
            raise serializers.ValidationError(
                {
                    "supplier_aas_id": (
                        "A verified supplier AAS ID or Global Asset ID is required."
                    )
                }
            )

        seen_keys = set()
        for field in attrs["technical_fields"]:
            normalized_key = field["key"].strip().casefold()
            if normalized_key in seen_keys:
                raise serializers.ValidationError(
                    {"technical_fields": "Technical field keys must be unique."}
                )
            seen_keys.add(normalized_key)
        return attrs
