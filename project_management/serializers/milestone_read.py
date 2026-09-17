from rest_framework import serializers

from project_management.models import Document, Milestone


class PhaseDocumentSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = (
            "id",
            "file_name",
            "document_type",
            "author",
            "status",
            "identification_number",
        )

    def get_author(self, document):
        if not document.author_id:
            return None
        return {"id": document.author_id, "name": document.author.full_name}


class MilestoneListSerializer(serializers.ModelSerializer):
    project = serializers.SerializerMethodField()
    closed_by = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()
    status_color = serializers.SerializerMethodField()
    booked_hour = serializers.FloatField(read_only=True)
    booked_cost = serializers.FloatField(read_only=True)
    num_documents = serializers.IntegerField(read_only=True)
    num_action_items = serializers.IntegerField(read_only=True)
    num_sub_action_items = serializers.IntegerField(read_only=True)
    has_open_action_items = serializers.BooleanField(read_only=True)
    all_closed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Milestone
        fields = (
            "id",
            "sort_order",
            "project",
            "name",
            "description",
            "colour",
            "status",
            "status_color",
            "reminder_setting",
            "allocated_budget",
            "allocated_time",
            "phase_title",
            "start_date",
            "due_date",
            "documents",
            "num_documents",
            "num_action_items",
            "num_sub_action_items",
            "has_open_action_items",
            "all_closed",
            "closed_at",
            "closed_by",
            "booked_hour",
            "booked_cost",
            "created_at",
            "updated_at",
        )

    def get_project(self, milestone):
        return {
            "id": milestone.project_id,
            "project_name": milestone.project.project_name,
        }

    def get_closed_by(self, milestone):
        if not milestone.closed_by_id:
            return None
        return {
            "id": milestone.closed_by_id,
            "name": milestone.closed_by.full_name,
        }

    def get_documents(self, milestone):
        return PhaseDocumentSerializer(milestone.phase_documents, many=True).data

    def get_status_color(self, milestone):
        configuration = getattr(milestone.project, "configurations", None)
        options = getattr(configuration, "status_options", None) or []
        colors = {}
        for option in options:
            value, separator, color = option.partition(":")
            if separator:
                colors[value] = color
        return colors.get(milestone.status, "#00FF00")


class MilestoneDetailSerializer(MilestoneListSerializer):
    linked_folders = serializers.SerializerMethodField()
    linked_folder_details = serializers.SerializerMethodField()

    class Meta(MilestoneListSerializer.Meta):
        fields = MilestoneListSerializer.Meta.fields + (
            "linked_folders",
            "linked_folder_details",
        )

    def get_linked_folders(self, milestone):
        return [folder.id for folder in milestone.phase_linked_folders]

    def get_linked_folder_details(self, milestone):
        return [self._serialize_folder(folder) for folder in milestone.phase_linked_folders]

    def _serialize_folder(self, folder):
        path = []
        current = folder
        while current:
            path.append(current.name)
            current = current.parent
        path.reverse()
        if len(path) > 1:
            path = path[1:]
        return {
            "id": folder.id,
            "name": folder.name,
            "path": " / ".join(path),
            "parent": folder.parent_id,
            "project": folder.project_id,
            "company_profile": folder.company_profile_id,
            "freelancer": folder.freelancer_id,
        }


class MilestoneOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Milestone
        fields = (
            "id",
            "name",
            "colour",
            "status",
            "sort_order",
            "start_date",
            "due_date",
        )
