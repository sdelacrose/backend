from rest_framework import serializers

from project_management.models import Folder


class LinkedFolderSerializerMixin:
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["linked_folder_details"] = self.get_linked_folder_details(
            instance
        )
        return representation

    def get_linked_folder_details(self, obj):
        linked_folders = getattr(obj, "linked_folders", None)
        if linked_folders is None:
            return []

        return [
            self.serialize_linked_folder(folder)
            for folder in linked_folders.select_related(
                "parent",
                "project",
                "company_profile",
                "freelancer",
            ).all()
        ]

    def serialize_linked_folder(self, folder):
        return {
            "id": folder.id,
            "name": folder.name,
            "path": self.get_folder_path(folder),
            "parent": folder.parent_id,
            "project": folder.project_id,
            "company_profile": folder.company_profile_id,
            "freelancer": folder.freelancer_id,
        }

    def get_folder_path(self, folder):
        path = []
        current = folder
        while current:
            path.append(current.name)
            current = current.parent
        path = list(reversed(path))
        if len(path) > 1:
            path = path[1:]
        return " / ".join(path)

    def validate_linked_folders(self, folders):
        if not folders:
            return folders

        project_id, company_profile_id, freelancer_id = (
            self.get_linked_folder_owner_ids()
        )

        if project_id:
            return self.validate_project_linked_folders(folders, project_id)
        if company_profile_id:
            return self.validate_company_linked_folders(
                folders, company_profile_id
            )
        if freelancer_id:
            return self.validate_freelancer_linked_folders(folders, freelancer_id)

        raise serializers.ValidationError(
            "Unable to determine owner for linked folders."
        )

    def get_linked_folder_owner_ids(self):
        resolver = getattr(
            self, "get_related_project_id_or_company_id_or_freelancer", None
        )
        if callable(resolver):
            return resolver()

        instance = getattr(self, "instance", None)
        initial_data = getattr(self, "initial_data", {}) or {}

        project_id = initial_data.get("project") or getattr(
            instance, "project_id", None
        )
        company_profile_id = initial_data.get("company_profile") or getattr(
            instance, "company_profile_id", None
        )
        freelancer_id = initial_data.get("freelancer") or getattr(
            instance, "freelancer_id", None
        )

        return project_id, company_profile_id, freelancer_id

    def validate_project_linked_folders(self, folders, project_id):
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                "Unable to determine project for linked folders."
            )

        for folder in folders:
            if int(folder.project_id or 0) != project_id:
                raise serializers.ValidationError(
                    f"Folder {folder.id} does not belong to the same project."
                )
        return folders

    def validate_company_linked_folders(self, folders, company_profile_id):
        try:
            company_profile_id = int(company_profile_id)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                "Unable to determine company for linked folders."
            )

        for folder in folders:
            if int(folder.company_profile_id or 0) != company_profile_id:
                raise serializers.ValidationError(
                    f"Folder {folder.id} does not belong to the same company."
                )
        return folders

    def validate_freelancer_linked_folders(self, folders, freelancer_id):
        try:
            freelancer_id = int(freelancer_id)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                "Unable to determine freelancer for linked folders."
            )

        for folder in folders:
            if int(folder.freelancer_id or 0) != freelancer_id:
                raise serializers.ValidationError(
                    f"Folder {folder.id} does not belong to the same freelancer."
                )
        return folders

    def set_linked_folders(self, instance, linked_folders):
        if linked_folders is not None:
            instance.linked_folders.set(linked_folders)
        return instance
