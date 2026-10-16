"""MAIL-1.5-B: serializers of the e-mail template library (see views/correspondence_template.py)."""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from project_management.models import CorrespondenceTemplate
from project_management.services.correspondence_template import validate_template_fields


class CorrespondenceTemplateSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CorrespondenceTemplate
        fields = (
            "id", "name", "description", "language", "subject_template", "body_template",
            "is_active", "created_by", "created_by_name", "created_at", "updated_at",
        )
        # The owner company is never accepted from the client: it is the requester's own company.
        read_only_fields = ("id", "created_by", "created_by_name", "created_at", "updated_at")
        extra_kwargs = {
            "name": {"trim_whitespace": True},
            "description": {"required": False, "allow_blank": True},
        }
        # The uniqueness of the name is checked in validate() (case-insensitive, per company).
        validators = []

    def get_created_by_name(self, obj):
        user = obj.created_by
        if user is None:
            return None
        return (getattr(user, "full_name", "") or str(user)).strip() or None

    def _company_id(self):
        request = self.context.get("request")
        user = request.user if request else self.context.get("user")
        return getattr(user, "company_id", None)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        values = {
            field: attrs.get(field, getattr(instance, field, "") if instance else "")
            for field in ("name", "subject_template", "body_template")
        }
        try:
            validate_template_fields(**values)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict)

        company_id = instance.company_profile_id if instance else self._company_id()
        name = (values["name"] or "").strip()
        clash = CorrespondenceTemplate.objects.filter(company_profile_id=company_id, name__iexact=name)
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"name": "A template with this name already exists."})
        if "name" in attrs:
            attrs["name"] = name
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user if request else self.context.get("user")
        validated_data["company_profile_id"] = user.company_id
        validated_data["created_by"] = user
        return super().create(validated_data)


class ApplyCorrespondenceTemplateSerializer(serializers.Serializer):
    """The context a template is applied in. Only identifiers: every object is looked up inside the
    requester's own company (services.correspondence_template.resolve_context)."""

    project = serializers.IntegerField(required=False, allow_null=True)
    recipient_contact = serializers.IntegerField(required=False, allow_null=True)
    recipient_company = serializers.IntegerField(required=False, allow_null=True)
    parent_correspondence = serializers.IntegerField(required=False, allow_null=True)
