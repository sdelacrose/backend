"""QUALITY Q2: nested, writable serializer for the evaluation template builder.

Same approach as the Supplier Surveillance template editor: one POST/PATCH payload carries the whole
Template -> Section -> Criterion tree; items present are created/updated, items absent are deleted.
`order` is taken from the position in the payload, so it is always a clean 0..n-1 sequence.
Existing evaluations are unaffected: they only read their own snapshot.
"""

from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from project_management.models import (
    EvaluationTemplate,
    EvaluationTemplateCriterion,
    EvaluationTemplateSection,
)
from project_management.models.supplier_evaluation_template import SCALE_MAX, SCALE_MIN

MAX_WEIGHT = Decimal("100")


def _validate_weight(value):
    if value is not None and not (Decimal("0") < value <= MAX_WEIGHT):
        raise serializers.ValidationError("A weight must be greater than 0 and at most 100.")
    return value


def _validate_threshold(value):
    # A threshold of 1 can never trigger; above the maximum it would force a comment on every score.
    if value is not None and not (SCALE_MIN < value <= SCALE_MAX):
        raise serializers.ValidationError(
            f"The comment threshold must be between {SCALE_MIN + 1} and {SCALE_MAX}."
        )
    return value


class CriterionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = EvaluationTemplateCriterion
        fields = [
            "id", "label", "description", "order", "weight", "required", "critical",
            "allow_na", "comment_required_below",
        ]
        read_only_fields = ["order"]

    def validate_weight(self, value):
        return _validate_weight(value)

    def validate_comment_required_below(self, value):
        return _validate_threshold(value)

    def validate(self, attrs):
        if attrs.get("critical"):
            attrs["required"] = True  # a critical criterion can never be optional
        return attrs


class SectionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    criteria = CriterionSerializer(many=True, required=False)

    class Meta:
        model = EvaluationTemplateSection
        fields = ["id", "title", "description", "order", "weight", "criteria"]
        read_only_fields = ["order"]

    def validate_weight(self, value):
        return _validate_weight(value)


class EvaluationTemplateSerializer(serializers.ModelSerializer):
    sections = SectionSerializer(many=True, required=False)
    sections_count = serializers.SerializerMethodField()
    criteria_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = EvaluationTemplate
        fields = [
            "id", "name", "description", "version", "is_active", "comment_required_below",
            "sections", "sections_count", "criteria_count", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = ["version", "created_at", "updated_at"]

    def get_sections_count(self, obj):
        return len(obj.sections.all())

    def get_criteria_count(self, obj):
        return sum(len(section.criteria.all()) for section in obj.sections.all())

    def get_created_by_name(self, obj):
        user = obj.created_by
        return None if not user else ((user.full_name or "").strip() or user.email)

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A name is required.")
        clash = EvaluationTemplate.objects.filter(
            company=self.context["company"], name__iexact=value
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("A template with this name already exists.")
        return value

    def validate_comment_required_below(self, value):
        return _validate_threshold(value)

    def validate_sections(self, sections):
        for section in sections:
            if not (section.get("title") or "").strip():
                raise serializers.ValidationError("Every section needs a title.")
            for criterion in section.get("criteria", []):
                if not (criterion.get("label") or "").strip():
                    raise serializers.ValidationError("Every criterion needs a label.")
        return sections

    @transaction.atomic
    def create(self, validated_data):
        sections = validated_data.pop("sections", [])
        template = EvaluationTemplate.objects.create(
            company=self.context["company"], created_by=self.context["user"], **validated_data
        )
        self._sync(template, sections)
        return template

    @transaction.atomic
    def update(self, instance, validated_data):
        sections = validated_data.pop("sections", None)
        content_changed = sections is not None or any(
            getattr(instance, key) != value
            for key, value in validated_data.items()
            if key != "is_active"
        )
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if content_changed:
            instance.version += 1
        instance.save()
        if sections is not None:
            self._sync(instance, sections)
        return instance

    def _sync(self, template, sections_data):
        existing = {s.id: s for s in template.sections.all()}
        keep = set()
        for position, data in enumerate(sections_data):
            criteria = data.pop("criteria", [])
            section_id = data.pop("id", None)
            data.pop("order", None)
            section = existing.get(section_id)
            if section is None:  # unknown or foreign ids are treated as new rows, never trusted
                section = EvaluationTemplateSection(template=template)
            for key, value in data.items():
                setattr(section, key, value)
            section.order = position
            section.save()
            keep.add(section.id)
            self._sync_criteria(section, criteria)
        template.sections.exclude(id__in=keep).delete()

    def _sync_criteria(self, section, criteria_data):
        existing = {c.id: c for c in section.criteria.all()}
        keep = set()
        for position, data in enumerate(criteria_data):
            criterion_id = data.pop("id", None)
            data.pop("order", None)
            criterion = existing.get(criterion_id)
            if criterion is None:
                criterion = EvaluationTemplateCriterion(section=section)
            for key, value in data.items():
                setattr(criterion, key, value)
            criterion.order = position
            criterion.save()
            keep.add(criterion.id)
        section.criteria.exclude(id__in=keep).delete()
