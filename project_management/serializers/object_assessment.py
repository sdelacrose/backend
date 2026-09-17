import json

from django.utils.text import slugify
from rest_framework import serializers

from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    ObjectAssessment,
    Project,
    ProjectSolutionSpaceItem,
    Requirement,
    SolutionDemand,
    Transport,
)
from project_management.models.constants import COLLABORATIVE_SOLUTION
from project_management.serializers import DocumentSerializer
from project_management.serializers.file_name import validate_display_file_name
from .time_expense import get_valid_choices
from project_management.utils import (
    generate_object_assessment_record_title,
    get_product_name_from_request,
    has_project_permission,
)


class RichTextJSONField(serializers.JSONField):
    def to_internal_value(self, data):
        if data in (None, ""):
            return None
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                raise serializers.ValidationError("Invalid rich text JSON.")
        return super().to_internal_value(data)


def get_rich_note_file_name(record_title):
    return slugify(str(record_title or '')).replace('-', '_') or 'notes'


class ObjectAssessmentSerializer(serializers.ModelSerializer):
    history = serializers.SerializerMethodField()
    solution_demands = serializers.SerializerMethodField()
    requirements = serializers.SerializerMethodField()
    available_product_parts = serializers.SerializerMethodField()
    bom_parts = serializers.SerializerMethodField()
    transports = serializers.SerializerMethodField()
    project_solution_space_items = serializers.SerializerMethodField()
    solution_demand_details = serializers.SerializerMethodField()
    requirement_details = serializers.SerializerMethodField()
    available_product_part_details = serializers.SerializerMethodField()
    bom_part_details = serializers.SerializerMethodField()
    transport_details = serializers.SerializerMethodField()
    project_solution_space_item_details = serializers.SerializerMethodField()
    solution_demand_ids = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    requirement_ids = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    available_product_part_ids = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    bom_part_ids = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    transport_ids = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    project_solution_space_item_ids = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    notes_content_json = RichTextJSONField(required=False, allow_null=True)
    notes_content_html = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    class Meta:
        model = ObjectAssessment
        fields = '__all__'
        read_only_fields = ['created_by', 'assignee']
    # TODO: Adjust as per new permission system
    # def validate_project(self, value):
    #     request_user = self.context.get('user')
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
    #     raise serializers.ValidationError(f"Project {value} does not belong to this user")
    
    def validate_documents(self, documents):
        project = self.instance.project.id if self.instance else int(self.initial_data.get('project'))

        for document in documents:
            if document.folder.project.id != project:
                raise serializers.ValidationError(
                    f"Document {document.file_name} is not linked to the same project as the object."
                )
        return documents

    def validate_file_name(self, value):
        incoming_file = self.initial_data.get('file') if hasattr(self, 'initial_data') else None
        current_file_name = self.instance.file_name if self.instance else None
        source_file = incoming_file or (self.instance.file if self.instance else None)
        return validate_display_file_name(
            value,
            source_file=source_file,
            current_file_name=current_file_name,
        )

    def validate_status(self, value):
        if value is None:
            return
        project_id = self.instance.project.id if self.instance else int(self.initial_data.get('project'))
        request = self.context.get('request')
        valid_choices = get_valid_choices("document_status", project_id, request)
        valid_status = {choice.split(":")[0] for choice in valid_choices}

        if valid_status and value not in valid_status:
            raise serializers.ValidationError(f"Invalid object assessment status. Allowed choices are: {', '.join(valid_status)}")
        return value
    
    def validate_document_type(self, value):
        if value is None:
            return
        project_id = self.instance.project.id if self.instance else int(self.initial_data.get('project'))
        request = self.context.get('request')
        valid_choices = get_valid_choices("document_types", project_id, request)

        valid_status = {choice.split(":")[0] for choice in valid_choices}

        if valid_status and value not in valid_status:
            raise serializers.ValidationError(f"Invalid document type. Allowed choices are: {', '.join(valid_status)}")
        return value

    def validate_author(self, value):
        """
        Validate that the author is a valid team member and belongs to the same project as the folder.
        """
        if not value:
            return
        request_user = self.context.get('user')
        project_instance = self.instance.project if self.instance else None
        if hasattr(self, 'initial_data') and 'project' in self.initial_data:
            project_id = self.initial_data.get('project')
            project_instance = Project.objects.filter(id=project_id).first()
        
        if not project_instance:
            raise serializers.ValidationError("Invalid project specified.")

        # TODO: Adjust as per new permission system
        # if not has_project_permission("PUT", value, project_instance, get_product_name_from_request(self.context.get('request'))):
        #     raise serializers.ValidationError("The author must belong to the same project as the object assessment.")

        # TODO: Adjust as per new permission system
        # if not has_project_permission("PUT", request_user, project_instance, get_product_name_from_request(self.context.get('request'))):
        #     raise serializers.ValidationError("The request user must have access to the same project as the object assessment.")

        return value

    def _parse_matrix_link_ids(self, value, label):
        if value is None:
            return None
        if value == "":
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise serializers.ValidationError(f"Invalid {label} links.")
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError(f"{label} links must be a list.")

        ids = []
        for item in value:
            if item in (None, ""):
                continue
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                raise serializers.ValidationError(f"Invalid {label} link id.")
        return ids

    def validate(self, attrs):
        attrs = super().validate(attrs)
        project = attrs.get('project') or (self.instance.project if self.instance else None)
        record_type = attrs.get('record_type') or (
            self.instance.record_type if self.instance else None
        )

        file_obj = attrs.get('file')
        existing_file = bool(self.instance and self.instance.file)
        incoming_notes_json = attrs.get(
            'notes_content_json',
            self.instance.notes_content_json if self.instance else None,
        )
        incoming_notes_html = attrs.get(
            'notes_content_html',
            self.instance.notes_content_html if self.instance else None,
        )

        has_note_json = incoming_notes_json not in (None, "")
        has_note_html = bool(
            isinstance(incoming_notes_html, str) and incoming_notes_html.strip()
        )

        if record_type == 'Notes':
            if not (file_obj or existing_file or has_note_json or has_note_html):
                raise serializers.ValidationError(
                    {
                        'notes_content_json': (
                            'Notes must include rich text content or a legacy file.'
                        )
                    }
                )
        elif not (file_obj or existing_file):
            raise serializers.ValidationError({'file': 'This field is required.'})

        if 'solution_demand_ids' in attrs:
            demand_ids = self._parse_matrix_link_ids(
                attrs.pop('solution_demand_ids'),
                'solution demand',
            )
            if project and demand_ids is not None:
                matched_count = SolutionDemand.objects.filter(
                    id__in=set(demand_ids),
                    project=project,
                ).count()
                if matched_count != len(set(demand_ids)):
                    raise serializers.ValidationError({
                        'solution_demand_ids': (
                            'Linked demands must belong to the same project.'
                        )
                    })
            attrs['_solution_demand_ids'] = demand_ids

        if 'requirement_ids' in attrs:
            requirement_ids = self._parse_matrix_link_ids(
                attrs.pop('requirement_ids'),
                'requirement',
            )
            if project and requirement_ids is not None:
                matched_count = Requirement.objects.filter(
                    id__in=set(requirement_ids),
                    project=project,
                ).count()
                if matched_count != len(set(requirement_ids)):
                    raise serializers.ValidationError({
                        'requirement_ids': (
                            'Linked requirements must belong to the same project.'
                        )
                    })
            attrs['_requirement_ids'] = requirement_ids

        if 'available_product_part_ids' in attrs:
            available_part_ids = self._parse_matrix_link_ids(
                attrs.pop('available_product_part_ids'),
                'available product part',
            )
            if project and available_part_ids is not None:
                matched_count = AvailableProductPart.objects.filter(
                    id__in=set(available_part_ids),
                    company_profile=project.company_profile,
                ).count()
                if matched_count != len(set(available_part_ids)):
                    raise serializers.ValidationError({
                        'available_product_part_ids': (
                            'Linked available product parts must belong to the same company.'
                        )
                    })
            attrs['_available_product_part_ids'] = available_part_ids

        if 'bom_part_ids' in attrs:
            bom_part_ids = self._parse_matrix_link_ids(
                attrs.pop('bom_part_ids'),
                'bill of material part',
            )
            if project and bom_part_ids is not None:
                matched_count = BillOfMaterialPart.objects.filter(
                    id__in=set(bom_part_ids),
                    project=project,
                ).count()
                if matched_count != len(set(bom_part_ids)):
                    raise serializers.ValidationError({
                        'bom_part_ids': (
                            'Linked bill of material parts must belong to the same project.'
                        )
                    })
            attrs['_bom_part_ids'] = bom_part_ids

        if 'transport_ids' in attrs:
            transport_ids = self._parse_matrix_link_ids(
                attrs.pop('transport_ids'),
                'transport',
            )
            if project and transport_ids is not None:
                matched_count = Transport.objects.filter(
                    id__in=set(transport_ids),
                    project=project,
                ).count()
                if matched_count != len(set(transport_ids)):
                    raise serializers.ValidationError({
                        'transport_ids': (
                            'Linked transports must belong to the same project.'
                        )
                    })
            attrs['_transport_ids'] = transport_ids

        if 'project_solution_space_item_ids' in attrs:
            solution_space_item_ids = self._parse_matrix_link_ids(
                attrs.pop('project_solution_space_item_ids'),
                'project solution space item',
            )
            if project and solution_space_item_ids is not None:
                matched_count = ProjectSolutionSpaceItem.objects.filter(
                    id__in=set(solution_space_item_ids),
                    project=project,
                ).count()
                if matched_count != len(set(solution_space_item_ids)):
                    raise serializers.ValidationError({
                        'project_solution_space_item_ids': (
                            'Linked project solution space items must belong to the same project.'
                        )
                    })
            attrs['_project_solution_space_item_ids'] = solution_space_item_ids

        return attrs

    def _set_matrix_links(
        self,
        instance,
        solution_demand_ids=None,
        requirement_ids=None,
        available_product_part_ids=None,
        bom_part_ids=None,
        transport_ids=None,
        project_solution_space_item_ids=None,
    ):
        if solution_demand_ids is not None:
            instance.solution_demands.set(
                SolutionDemand.objects.filter(
                    id__in=solution_demand_ids,
                    project=instance.project,
                )
            )
        if requirement_ids is not None:
            instance.requirements.set(
                Requirement.objects.filter(
                    id__in=requirement_ids,
                    project=instance.project,
                )
            )
        if available_product_part_ids is not None:
            instance.available_product_parts.set(
                AvailableProductPart.objects.filter(
                    id__in=available_product_part_ids,
                    company_profile=instance.project.company_profile,
                )
            )
        if bom_part_ids is not None:
            instance.bom_parts.set(
                BillOfMaterialPart.objects.filter(
                    id__in=bom_part_ids,
                    project=instance.project,
                )
            )
        if transport_ids is not None:
            instance.transports.set(
                Transport.objects.filter(
                    id__in=transport_ids,
                    project=instance.project,
                )
            )
        if project_solution_space_item_ids is not None:
            instance.project_solution_space_items.set(
                ProjectSolutionSpaceItem.objects.filter(
                    id__in=project_solution_space_item_ids,
                    project=instance.project,
                )
            )

    def create(self, validated_data):
        solution_demand_ids = validated_data.pop('_solution_demand_ids', None)
        requirement_ids = validated_data.pop('_requirement_ids', None)
        available_product_part_ids = validated_data.pop('_available_product_part_ids', None)
        bom_part_ids = validated_data.pop('_bom_part_ids', None)
        transport_ids = validated_data.pop('_transport_ids', None)
        project_solution_space_item_ids = validated_data.pop(
            '_project_solution_space_item_ids',
            None,
        )
        file_obj = validated_data.get('file')
        if file_obj and not validated_data.get('file_name'):
            validated_data['file_name'] = file_obj.name
        validated_data['created_by'] = self.context['request'].user
        author = validated_data.get('author') if "author" in validated_data else None
        validated_data['assignee'] = author
        record_title = validated_data.get('record_title')
        if not record_title or not str(record_title).strip():
            validated_data['record_title'] = generate_object_assessment_record_title(
                ObjectAssessment.objects,
                validated_data.get('project'),
                validated_data.get('record_type'),
            )
        if validated_data.get('record_type') == 'Notes' and not file_obj:
            validated_data['file_name'] = get_rich_note_file_name(
                validated_data.get('record_title')
            )
        instance = super().create(validated_data)
        self._set_matrix_links(
            instance,
            solution_demand_ids,
            requirement_ids,
            available_product_part_ids,
            bom_part_ids,
            transport_ids,
            project_solution_space_item_ids,
        )
        return instance

    def update(self, instance, validated_data):
        solution_demand_ids = validated_data.pop('_solution_demand_ids', None)
        requirement_ids = validated_data.pop('_requirement_ids', None)
        available_product_part_ids = validated_data.pop('_available_product_part_ids', None)
        bom_part_ids = validated_data.pop('_bom_part_ids', None)
        transport_ids = validated_data.pop('_transport_ids', None)
        project_solution_space_item_ids = validated_data.pop(
            '_project_solution_space_item_ids',
            None,
        )
        file_obj = validated_data.get('file')
        author = validated_data.get('author') if "author" in validated_data else instance.author
        if file_obj and not validated_data.get('file_name'):
            validated_data['file_name'] = file_obj.name
        effective_record_type = validated_data.get('record_type', instance.record_type)
        effective_file = validated_data.get('file', instance.file)
        if effective_record_type == 'Notes' and not effective_file:
            validated_data['file_name'] = get_rich_note_file_name(
                validated_data.get('record_title', instance.record_title)
            )
        validated_data['assignee'] = author
        updated = super().update(instance, validated_data)
        self._set_matrix_links(
            updated,
            solution_demand_ids,
            requirement_ids,
            available_product_part_ids,
            bom_part_ids,
            transport_ids,
            project_solution_space_item_ids,
        )
        return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.record_type == 'Notes' and not instance.file:
            representation['file_name'] = get_rich_note_file_name(
                instance.record_title
            )
        if instance.documents.exists():
            representation["documents"] = DocumentSerializer(instance.documents.all(), many=True).data
        if instance.author:
            representation["author"] = {
                "id": instance.author.id,
                "name": instance.author.full_name
            }
        return representation

    def get_solution_demands(self, obj):
        return list(obj.solution_demands.values_list('id', flat=True))

    def get_requirements(self, obj):
        return list(obj.requirements.values_list('id', flat=True))

    def get_available_product_parts(self, obj):
        return list(obj.available_product_parts.values_list('id', flat=True))

    def get_bom_parts(self, obj):
        return list(obj.bom_parts.values_list('id', flat=True))

    def get_transports(self, obj):
        return list(obj.transports.values_list('id', flat=True))

    def get_project_solution_space_items(self, obj):
        return list(obj.project_solution_space_items.values_list('id', flat=True))

    def get_solution_demand_details(self, obj):
        return [
            {
                "id": demand.id,
                "name": demand.subject,
                "demand_number": demand.demand_number,
                "sequence": demand.get_sequence(),
            }
            for demand in obj.solution_demands.all()
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

    def get_available_product_part_details(self, obj):
        return [
            {
                "id": part.id,
                "name": part.name,
                "part_number": part.part_number,
                "sequence": part.get_sequence(),
            }
            for part in obj.available_product_parts.all()
        ]

    def get_bom_part_details(self, obj):
        return [
            {
                "id": part.id,
                "name": part.name,
                "part_number": part.part_number,
                "sequence": part.get_sequence(),
            }
            for part in obj.bom_parts.all()
        ]

    def get_transport_details(self, obj):
        return [
            {
                "id": transport.id,
                "name": transport.name,
                "transport_start_date": transport.transport_start_date,
                "transport_end_date": transport.transport_end_date,
                "sequence": transport.get_sequence(),
            }
            for transport in obj.transports.all()
        ]

    def get_project_solution_space_item_details(self, obj):
        return [
            {
                "id": item.id,
                "name": item.assembly_name,
                "sequence": item.get_sequence(),
            }
            for item in obj.project_solution_space_items.all()
        ]

    def get_history(self, obj):
        include_history = self.context.get('include_history', False)
        if not include_history:
            return None
            
        history = obj.history.all().order_by('-history_date')
        return ObjectAssessmentHistorySerializer(history, many=True).data

class ObjectAssessmentHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ObjectAssessment.history.model
        fields = [
            'id',
            'history_date',
            'history_change_reason',
            'history_type',
            'history_user',
            'file_name',
            'document_type',
            'identification_number',
            'status',
            'author',
            'description',
            'record_title'
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        if instance.history_user:
            representation['history_user'] = {
                "id": instance.history_user.id,
                "name": instance.history_user.full_name
            }
        
        if instance.author:
            representation['author'] = {
                "id": instance.author.id,
                "name": instance.author.full_name
            }
        
        return representation
