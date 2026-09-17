from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Max
from rest_framework import serializers

from .models import (
    AcceptanceActionItemProvenance,
    AcceptanceChecklist,
    AcceptanceCriterion,
    AcceptanceCriterionComment,
    AcceptanceCriterionCommentReaction,
    AcceptanceCriterionEvidence,
    AcceptanceGenerationRun,
    CompanyProjectDocumentTemplate,
    GeneratedProjectDocument,
    GeneratedProjectDocumentRevision,
    ProjectContentAuditEvent,
    ProjectDocumentGenerationRun,
)
from .template_validation import TemplateValidationError, validate_template_file


class ProjectContentGenerateSerializer(serializers.Serializer):
    document_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )
    locale = serializers.ChoiceField(
        choices=CompanyProjectDocumentTemplate.Locale.choices,
        default=CompanyProjectDocumentTemplate.Locale.ENGLISH,
    )
    options = serializers.JSONField(required=False, default=dict)


class ProjectDocumentDraftSerializer(serializers.Serializer):
    content = serializers.JSONField()


class AcceptanceDraftSerializer(serializers.Serializer):
    criteria = serializers.ListField(child=serializers.DictField(), required=False)
    proposals = serializers.ListField(child=serializers.DictField(), required=False)

    def validate(self, attrs):
        if "criteria" not in attrs and "proposals" not in attrs:
            raise serializers.ValidationError("A review proposal list is required.")
        return attrs


class AcceptanceApplySerializer(AcceptanceDraftSerializer):
    criteria = serializers.ListField(child=serializers.DictField(), required=False)
    selected_keys = serializers.ListField(
        child=serializers.CharField(max_length=128),
        required=False,
        default=list,
    )

    def validate(self, attrs):
        return attrs


class AcceptanceBulkUpdateSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)
    changes = serializers.DictField()


class AcceptanceReorderSerializer(serializers.Serializer):
    items = serializers.ListField(child=serializers.DictField(), allow_empty=False)


class ProjectContentAuditEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = ProjectContentAuditEvent
        fields = [
            "id",
            "project",
            "event_type",
            "object_type",
            "object_id",
            "actor",
            "actor_name",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields

    def get_actor_name(self, instance):
        if not instance.actor:
            return ""
        full_name = " ".join(
            filter(None, [instance.actor.first_name, instance.actor.last_name])
        ).strip()
        return full_name or instance.actor.email


class CompanyProjectDocumentTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyProjectDocumentTemplate
        fields = [
            "id",
            "company",
            "kind",
            "locale",
            "name",
            "version",
            "template_file",
            "original_file_name",
            "file_hash",
            "is_active",
            "validation_status",
            "validation_report",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "company",
            "version",
            "original_file_name",
            "file_hash",
            "is_active",
            "validation_status",
            "validation_report",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def validate_template_file(self, value):
        if not value.name.lower().endswith(".docx"):
            raise serializers.ValidationError("Only DOCX templates are supported.")
        return value

    def validate(self, attrs):
        if self.instance and set(attrs) - {"name"}:
            raise serializers.ValidationError(
                "Template files, kinds, and locales are immutable. Upload a new version instead."
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        company = request.user.company
        kind = validated_data["kind"]
        locale = validated_data.get("locale", "en")
        version = (
            CompanyProjectDocumentTemplate.objects.filter(
                company=company,
                kind=kind,
                locale=locale,
            ).aggregate(value=Max("version"))["value"]
            or 0
        ) + 1
        uploaded = validated_data["template_file"]
        instance = CompanyProjectDocumentTemplate.objects.create(
            company=company,
            version=version,
            original_file_name=uploaded.name,
            created_by=request.user,
            **validated_data,
        )
        try:
            report = validate_template_file(instance.template_file, instance.original_file_name)
            instance.validation_status = CompanyProjectDocumentTemplate.ValidationStatus.VALID
            instance.file_hash = report["sha256"]
            instance.validation_report = report
        except TemplateValidationError as exc:
            instance.validation_status = CompanyProjectDocumentTemplate.ValidationStatus.INVALID
            instance.validation_report = {
                "valid": False,
                "code": exc.code,
                "message": exc.message,
            }
        instance.save(
            update_fields=[
                "validation_status",
                "validation_report",
                "file_hash",
                "updated_at",
            ]
        )
        return instance


class GeneratedProjectDocumentRevisionSerializer(serializers.ModelSerializer):
    docx_file_name = serializers.CharField(source="docx_document.file_name", read_only=True)
    pdf_file_name = serializers.CharField(source="pdf_document.file_name", read_only=True)

    class Meta:
        model = GeneratedProjectDocumentRevision
        fields = [
            "id",
            "revision",
            "source_hash",
            "template",
            "template_version",
            "structured_content",
            "validation_report",
            "docx_document",
            "docx_file_name",
            "pdf_document",
            "pdf_file_name",
            "docx_hash",
            "pdf_hash",
            "approval_status",
            "created_by",
            "approved_by",
            "approved_at",
            "created_at",
        ]
        read_only_fields = fields


class GeneratedProjectDocumentSerializer(serializers.ModelSerializer):
    current_revision = GeneratedProjectDocumentRevisionSerializer(read_only=True)

    class Meta:
        model = GeneratedProjectDocument
        fields = [
            "id",
            "project",
            "kind",
            "status",
            "source_hash",
            "current_revision",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ProjectDocumentGenerationRunSerializer(serializers.ModelSerializer):
    applied_revision = GeneratedProjectDocumentRevisionSerializer(read_only=True)

    class Meta:
        model = ProjectDocumentGenerationRun
        fields = [
            "id",
            "project",
            "generated_document",
            "kind",
            "locale",
            "status",
            "source_hash",
            "request_options",
            "result",
            "draft_content",
            "warnings",
            "error_code",
            "error_message",
            "attempt_count",
            "hint_only",
            "applied_revision",
            "created_by",
            "modified_by",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "project",
            "generated_document",
            "kind",
            "locale",
            "status",
            "source_hash",
            "request_options",
            "result",
            "warnings",
            "error_code",
            "error_message",
            "attempt_count",
            "hint_only",
            "applied_revision",
            "created_by",
            "modified_by",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]


class AcceptanceCriterionEvidenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = AcceptanceCriterionEvidence
        fields = [
            "id",
            "generation_run",
            "requirement",
            "document",
            "segment_id",
            "location",
            "quote",
            "excerpt_hash",
        ]
        read_only_fields = fields


class AcceptanceCriterionCommentReactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AcceptanceCriterionCommentReaction
        fields = ["id", "user", "emoji", "created_at"]
        read_only_fields = ["id", "user", "created_at"]


class AcceptanceCriterionCommentSerializer(serializers.ModelSerializer):
    reactions = AcceptanceCriterionCommentReactionSerializer(many=True, read_only=True)

    class Meta:
        model = AcceptanceCriterionComment
        fields = [
            "id",
            "criterion",
            "sender",
            "parent",
            "content",
            "attachments",
            "mentions",
            "reactions",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "sender", "reactions", "created_at", "updated_at"]

    def validate(self, attrs):
        criterion = attrs.get("criterion") or getattr(self.instance, "criterion", None)
        if self.instance and attrs.get("criterion") and attrs["criterion"].id != self.instance.criterion_id:
            raise serializers.ValidationError("A comment cannot be moved to another acceptance criterion.")
        parent = attrs.get("parent")
        if parent and criterion and parent.criterion_id != criterion.id:
            raise serializers.ValidationError("A reply must belong to the same acceptance criterion.")
        if self.instance and parent and parent.id == self.instance.id:
            raise serializers.ValidationError("A comment cannot reply to itself.")
        if criterion:
            project = criterion.checklist.project
            for document in attrs.get("attachments", []):
                if document.folder.project_id != project.id:
                    raise serializers.ValidationError(
                        "Comment attachments must belong to the acceptance project."
                    )
            allowed_user_ids = set(
                project.team_members.values_list("team_member_id", flat=True)
            )
            allowed_user_ids.update(
                user_id
                for user_id in (project.user_id, project.created_by_id)
                if user_id
            )
            for user in attrs.get("mentions", []):
                if user.id not in allowed_user_ids:
                    raise serializers.ValidationError(
                        "Mentioned users must belong to the acceptance project."
                    )
        return attrs


class AcceptanceCriterionSerializer(serializers.ModelSerializer):
    evidence = AcceptanceCriterionEvidenceSerializer(many=True, read_only=True)
    comments_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = AcceptanceCriterion
        fields = [
            "id",
            "checklist",
            "parent",
            "stable_key",
            "pair_key",
            "sort_order",
            "name",
            "acceptance_criteria",
            "start_date",
            "end_date",
            "forecast_date",
            "status",
            "priority",
            "labels",
            "assignees",
            "requirements",
            "documents",
            "linked_folders",
            "source_run",
            "manual_fields",
            "evidence",
            "comments_count",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "stable_key",
            "pair_key",
            "source_run",
            "manual_fields",
            "evidence",
            "comments_count",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        checklist = attrs.get("checklist") or getattr(self.instance, "checklist", None)
        if self.instance and attrs.get("checklist") and attrs["checklist"].id != self.instance.checklist_id:
            raise serializers.ValidationError("A criterion cannot be moved to another checklist.")
        if checklist is None:
            raise serializers.ValidationError("An acceptance checklist is required.")
        project = checklist.project if checklist else None
        try:
            configuration = project.configurations
        except ObjectDoesNotExist:
            configuration = None

        def configured_values(field):
            values = getattr(configuration, field, []) if configuration else []
            return {
                str(value).split(":", 1)[0].strip()
                for value in values
                if str(value).split(":", 1)[0].strip()
            }

        status_value = attrs.get("status")
        valid_statuses = configured_values("acceptance_status_options")
        if status_value and valid_statuses and status_value not in valid_statuses:
            raise serializers.ValidationError(
                {"status": "Select a configured acceptance status."}
            )
        priority_value = attrs.get("priority")
        valid_priorities = configured_values("priority_levels")
        if priority_value and valid_priorities and priority_value not in valid_priorities:
            raise serializers.ValidationError(
                {"priority": "Select a configured project priority."}
            )
        labels = attrs.get("labels")
        valid_labels = configured_values("labels")
        if labels is not None:
            normalized_labels = [str(label).strip() for label in labels if str(label).strip()]
            if len(normalized_labels) != len(set(normalized_labels)):
                raise serializers.ValidationError({"labels": "Duplicate labels are not allowed."})
            if valid_labels and not set(normalized_labels).issubset(valid_labels):
                raise serializers.ValidationError(
                    {"labels": "Select only configured project labels."}
                )
            attrs["labels"] = normalized_labels

        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError(
                {"end_date": "End date cannot be before start date."}
            )
        parent = attrs.get("parent")
        if parent and parent.checklist_id != checklist.id:
            raise serializers.ValidationError("Parent criterion must belong to the same checklist.")
        if self.instance and parent and parent.id == self.instance.id:
            raise serializers.ValidationError("A criterion cannot be its own parent.")
        ancestor = parent
        seen_ids = set()
        while ancestor and ancestor.id not in seen_ids:
            if self.instance and ancestor.id == self.instance.id:
                raise serializers.ValidationError("A criterion cannot be nested below its descendant.")
            seen_ids.add(ancestor.id)
            ancestor = ancestor.parent
        for field in ("requirements", "documents", "linked_folders"):
            for item in attrs.get(field, []):
                if field == "requirements" and item.project_id != project.id:
                    raise serializers.ValidationError("Linked requirements must belong to the checklist project.")
                if field == "documents" and item.folder.project_id != project.id:
                    raise serializers.ValidationError("Linked documents must belong to the checklist project.")
                if field == "linked_folders" and item.project_id != project.id:
                    raise serializers.ValidationError("Linked folders must belong to the checklist project.")
        allowed_user_ids = set(
            project.team_members.values_list("team_member_id", flat=True)
        )
        allowed_user_ids.update(
            user_id for user_id in (project.user_id, project.created_by_id) if user_id
        )
        for assignee in attrs.get("assignees", []):
            if assignee.id not in allowed_user_ids:
                raise serializers.ValidationError(
                    "Assignees must belong to the acceptance project."
                )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        checklist = validated_data["checklist"]
        validated_data["stable_key"] = f"manual-{checklist.criteria.count() + 1}-{request.user.id}"
        validated_data["created_by"] = request.user
        validated_data["modified_by"] = request.user
        validated_data["manual_fields"] = [
            "name",
            "acceptance_criteria",
            "start_date",
            "end_date",
            "forecast_date",
            "status",
            "priority",
            "labels",
            "assignees",
            "requirements",
            "documents",
            "linked_folders",
        ]
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context["request"]
        manual_fields = set(instance.manual_fields or [])
        manual_fields.update(validated_data)
        instance.manual_fields = sorted(manual_fields)
        instance.modified_by = request.user
        instance.save(update_fields=["manual_fields", "modified_by", "updated_at"])
        return super().update(instance, validated_data)


class AcceptanceChecklistSerializer(serializers.ModelSerializer):
    criteria_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = AcceptanceChecklist
        fields = [
            "id",
            "project",
            "phase",
            "display_name",
            "is_active",
            "source_revision",
            "criteria_count",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "project", "phase", "source_revision", "created_by", "created_at", "updated_at"]


class AcceptanceActionItemProvenanceSerializer(serializers.ModelSerializer):
    action_item_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = AcceptanceActionItemProvenance
        fields = [
            "action_item_id",
            "proposal_key",
            "phase_role",
            "pairing_key",
            "applied_fields",
            "source_evidence",
        ]
        read_only_fields = fields


class AcceptanceGenerationRunSerializer(serializers.ModelSerializer):
    review_required = serializers.SerializerMethodField()

    def get_review_required(self, instance):
        if (
            instance.status != AcceptanceGenerationRun.Status.APPLIED
            or instance.schema_version != "2"
            or not instance.fat_phase_id
        ):
            return False
        from .snapshots import build_acceptance_source_snapshot

        # List responses share a serializer, so read each project's sources once.
        if not hasattr(self, "_current_acceptance_sources"):
            self._current_acceptance_sources = {}
        if instance.project_id not in self._current_acceptance_sources:
            try:
                _, source_hash, prerequisites = build_acceptance_source_snapshot(
                    instance.project, request=self.context.get("request"),
                )
                current = source_hash if prerequisites["ready"] else None
            except (PermissionError, ValueError):
                current = None
            self._current_acceptance_sources[instance.project_id] = current
        return self._current_acceptance_sources[instance.project_id] != instance.source_hash

    applied_action_items = AcceptanceActionItemProvenanceSerializer(
        source="action_item_provenance",
        many=True,
        read_only=True,
    )

    class Meta:
        model = AcceptanceGenerationRun
        fields = [
            "id",
            "project",
            "fat_phase",
            "cat_phase",
            "status",
            "schema_version",
            "locale",
            "source_hash",
            "request_options",
            "result",
            "draft_content",
            "warnings",
            "error_code",
            "error_message",
            "attempt_count",
            "hint_only",
            "applied_action_items",
            "review_required",
            "created_by",
            "modified_by",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "project",
            "fat_phase",
            "cat_phase",
            "status",
            "schema_version",
            "locale",
            "source_hash",
            "request_options",
            "result",
            "warnings",
            "error_code",
            "error_message",
            "attempt_count",
            "hint_only",
            "created_by",
            "modified_by",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]


class AcceptanceGenerateSerializer(serializers.Serializer):
    schema_version = serializers.ChoiceField(choices=["2"], required=True)
    locale = serializers.ChoiceField(choices=["en", "de", "fr"], default="en")
    fat_phase_id = serializers.IntegerField(required=False, allow_null=False)
    cat_phase_id = serializers.IntegerField(required=False, allow_null=False)
    options = serializers.JSONField(required=False, default=dict)
