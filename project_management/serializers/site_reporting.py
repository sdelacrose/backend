from django.db import transaction
from django.conf import settings
from project_management.models.constants import COLLABORATIVE_SOLUTION
from rest_framework import serializers

from project_management.models import SiteReport, Observation, ProjectTeamMembers, Folder, Document
from project_management.serializers import ProjectTeamSerializer
from project_management.utils import get_product_name_from_request, has_project_permission, create_pdf_invoice_by_site_report



class ObservationSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    contractor = serializers.PrimaryKeyRelatedField(
        queryset=ProjectTeamMembers.objects.all()
    )

    class Meta:
        model = Observation
        fields = [
            "id", "contractor", "observation", "role",
            "rectification", "timescale", "agreed", "photos", "deadline",
        ]
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["contractor"] = ProjectTeamSerializer(instance.contractor).data
        return representation


class SiteReportSerializer(serializers.ModelSerializer):
    observations = ObservationSerializer(many=True, required=False)
    present = serializers.PrimaryKeyRelatedField(
        many=True, queryset=ProjectTeamMembers.objects.all(), required=False
    )
    distribution = serializers.PrimaryKeyRelatedField(
        many=True, queryset=ProjectTeamMembers.objects.all(), required=False
    )

    class Meta:
        model = SiteReport
        fields = [
            "id", "project", "date_time", "observer", "weather_condition",
            "location", "present", "distribution", "observations", 
            "created_at", "updated_at", "name", "created_by", "assignees"
        ]
        read_only_fields = ['created_by', 'assignees']
    # TODO: Adjust as per new permission system

    # def validate_project(self, value):
    #     request_user = self.context['request'].user
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)
    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]
    #     if value.user == request_user and value.solution in products:
    #         return value
    #     if has_project_permission("PUT", request_user, value, product_name):
    #         return value
    #     raise serializers.ValidationError("You do not have Read&Write permission of this project.")
    
    def validate_present(self, value):
        project = self.instance.project.id if self.instance else int(self.initial_data.get("project"))
        if not all(member.project_id == project for member in value):
            raise serializers.ValidationError("All 'present' members must belong to the same project as the site report.")
        return value

    def validate_distribution(self, value):
        project = self.instance.project.id if self.instance else int(self.initial_data.get("project"))
        if not all(member.project_id == project for member in value):
            raise serializers.ValidationError("All 'distribution' members must belong to the same project as the site report.")
        return value
    
    def validate_observer(self, value):
        request_user = self.context['request'].user
        project = self.instance.project.id if self.instance else int(self.initial_data.get("project"))
        if value.project_id != project:
            raise serializers.ValidationError("Only project team members can be added as observer.")
        if value.team_member != request_user:
            raise serializers.ValidationError("Cannot add any other user as observer.")
        return value

    def validate_contractor(self, site_report, contractor):
        """Ensure the contractor's project matches the SiteReport's project."""
        if contractor.project_id != site_report.project_id:
            raise serializers.ValidationError(
                "The contractor must belong to the same project as the associated site report."
            )

    @transaction.atomic
    def create(self, validated_data):
        observations_data = validated_data.pop("observations", [])
        present_members = validated_data.pop("present", [])
        distribution_members = validated_data.pop("distribution", [])
        validated_data['created_by'] = self.context['request'].user
        observer = validated_data.get('observer')
        site_report = super().create(validated_data)
        site_report.present.set(present_members)
        site_report.distribution.set(distribution_members)
        
        # Set assignees to include the observer's team member and all present team members
        if observer:
            site_report.assignees.add(observer.team_member)
        
        # Add all present team members to assignees
        for present_member in present_members:
            site_report.assignees.add(present_member.team_member)

        for observation_data in observations_data:
            contractor = observation_data.get("contractor")
            if contractor:
                self.validate_contractor(site_report, contractor)
                Observation.objects.create(site_report=site_report, **observation_data)
        create_pdf_invoice_by_site_report(site_report, self.context['request'].user)
        return site_report

    @transaction.atomic
    def update(self, instance, validated_data):
        observations_data = validated_data.pop("observations", None)
        present_members = validated_data.pop("present", None)
        distribution_members = validated_data.pop("distribution", None)
        observer_changed = "observer" in validated_data

        instance = super().update(instance, validated_data)
        instance._bypass_assignee_notification = True
        if present_members is not None:
            instance.present.set(present_members)
        if distribution_members is not None:
            instance.distribution.set(distribution_members)

        if present_members is not None or observer_changed:
            effective_present_members = (
                present_members
                if present_members is not None
                else list(instance.present.all())
            )
            instance.assignees.clear()
            if instance.observer:
                instance.assignees.add(instance.observer.team_member)
            for present_member in effective_present_members:
                instance.assignees.add(present_member.team_member)

        if observations_data is not None:
            existing_observation_ids = {
                obs.id for obs in instance.observations.all()
            }
            incoming_observation_ids = {
                obs_data.get("id")
                for obs_data in observations_data
                if obs_data.get("id")
            }

            for obs_id in existing_observation_ids - incoming_observation_ids:
                Observation.objects.filter(id=obs_id).delete()

            for obs_data in observations_data:
                contractor = obs_data.get("contractor")
                if contractor:
                    self.validate_contractor(instance, contractor)
                    obs_id = obs_data.get("id")
                    if obs_id:
                        Observation.objects.update_or_create(
                            id=obs_id,
                            defaults={**obs_data, "site_report": instance}
                        )
                    else:
                        Observation.objects.create(
                            site_report=instance,
                            **obs_data
                        )
        create_pdf_invoice_by_site_report(instance, self.context['request'].user)
        delattr(instance, '_bypass_assignee_notification')
        return instance

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["observer"] = ProjectTeamSerializer(instance.observer).data
        representation["present"] = ProjectTeamSerializer(instance.present.all(), many=True).data
        representation["distribution"] = ProjectTeamSerializer(instance.distribution.all(), many=True).data
        representation["observations"] = ObservationSerializer(instance.observations.all(), many=True).data
        
        # Add assignees data to representation
        assignees_data = []
        for assignee in instance.assignees.all():
            # Find the ProjectTeamMembers entry for this assignee in the current project
            team_member = ProjectTeamMembers.objects.filter(
                project=instance.project, 
                team_member=assignee
            ).first()
            if team_member:
                assignees_data.append(ProjectTeamSerializer(team_member).data)
        representation["assignees"] = assignees_data
        root_folder_name = f"{instance.project.id}_root_folder"
        root_folder = Folder.objects.filter(name=root_folder_name).first()
        if root_folder:
            invoices_folder = Folder.objects.filter(parent=root_folder, name="site_reports").first()
            if invoices_folder:
                file_name = f"site_reports_project_{instance.project.id}_site_report_{instance.id}.pdf"
                existing_document = Document.objects.filter(folder=invoices_folder, file__icontains=file_name.split(".")[0]).first()
                representation["site_report_pdf"] = existing_document.file.url if existing_document else None
        return representation
