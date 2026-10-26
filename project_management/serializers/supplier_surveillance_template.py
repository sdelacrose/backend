"""L3C: Supplier Surveillance Template editor -- one nested, writable serializer per Template.

Sections/Items are synced from the single PATCH/POST payload (create/update what is present,
delete what was removed) rather than exposing a separate CRUD endpoint per Section/Item -- the
smallest API surface that still lets the editor add/edit/remove freely (L3C section 17). This
never touches any already-created SupplierSurveillance: its own template_snapshot is a frozen
copy, read once at creation, never re-read from these rows.
"""

from rest_framework import serializers

from project_management.models import (
    SupplierSurveillanceTemplate,
    SupplierSurveillanceTemplateItem,
    SupplierSurveillanceTemplateSection,
)


class SupplierSurveillanceTemplateItemSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = SupplierSurveillanceTemplateItem
        fields = ["id", "label", "description", "item_type", "required", "order"]


class SupplierSurveillanceTemplateSectionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    items = SupplierSurveillanceTemplateItemSerializer(many=True, required=False)

    class Meta:
        model = SupplierSurveillanceTemplateSection
        fields = ["id", "title", "description", "order", "items"]


class SupplierSurveillanceTemplateSerializer(serializers.ModelSerializer):
    sections = SupplierSurveillanceTemplateSectionSerializer(many=True, required=False)
    sections_count = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupplierSurveillanceTemplate
        fields = [
            "id",
            "name",
            "description",
            "version",
            "is_active",
            "sections",
            "sections_count",
            "items_count",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_sections_count(self, obj):
        return len(obj.sections.all())

    def get_items_count(self, obj):
        return sum(len(section.items.all()) for section in obj.sections.all())

    def get_created_by_name(self, obj):
        user = obj.created_by
        if not user:
            return None
        return (user.full_name or "").strip() or user.email

    def create(self, validated_data):
        sections_data = validated_data.pop("sections", [])
        template = SupplierSurveillanceTemplate.objects.create(
            company=self.context["company"],
            created_by=self.context["user"],
            **validated_data,
        )
        self._sync_sections(template, sections_data)
        return template

    def update(self, instance, validated_data):
        sections_data = validated_data.pop("sections", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if sections_data is not None:
            self._sync_sections(instance, sections_data)
        return instance

    @staticmethod
    def _sync_sections(template, sections_data):
        existing = {section.id: section for section in template.sections.all()}
        keep_ids = []
        for section_data in sections_data:
            items_data = section_data.pop("items", [])
            section_id = section_data.get("id")
            section = existing.get(section_id)
            if section:
                for field, value in section_data.items():
                    if field != "id":
                        setattr(section, field, value)
                section.save()
            else:
                section = SupplierSurveillanceTemplateSection.objects.create(
                    template=template,
                    **{k: v for k, v in section_data.items() if k != "id"},
                )
            keep_ids.append(section.id)
            SupplierSurveillanceTemplateSerializer._sync_items(section, items_data)
        template.sections.exclude(id__in=keep_ids).delete()

    @staticmethod
    def _sync_items(section, items_data):
        existing = {item.id: item for item in section.items.all()}
        keep_ids = []
        for item_data in items_data:
            item_id = item_data.get("id")
            item = existing.get(item_id)
            if item:
                for field, value in item_data.items():
                    if field != "id":
                        setattr(item, field, value)
                item.save()
            else:
                item = SupplierSurveillanceTemplateItem.objects.create(
                    section=section,
                    **{k: v for k, v in item_data.items() if k != "id"},
                )
            keep_ids.append(item.id)
        section.items.exclude(id__in=keep_ids).delete()
