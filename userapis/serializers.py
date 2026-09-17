from rest_framework import serializers

from .models import UserDashboardConfig, CalendarEvent
from models.models import User
from project_management.serializers.document import DocumentSerializer
from project_management.serializers.linked_folder import LinkedFolderSerializerMixin
from ecpmpapp.utils import send_event_to_sqs, schedule_reminder_to_sqs, reschedule_event, can_assign_calendar_event
from userapis.signals import notify_calendar_event_created

class UserDashboardConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDashboardConfig
        fields = ['id', 'project_dashboard', 'company_dashboard', 'personal_dashboard']


class PublicUserSerializer(serializers.ModelSerializer):
    company = serializers.CharField(source='company.company_name', allow_null=True)
    company_id = serializers.IntegerField(
        source='company.id', allow_null=True, read_only=True
    )
    company_sharemundo_links_id = serializers.CharField(
        source='company.sharemundo_links_id', allow_null=True, read_only=True
    )

    class Meta:
        model = User
        fields = (
            'id', 'username', 'title',
            'first_name', 'last_name', 'job_title', 'city', 'state',
            'country', 'preferred_language', 'spoken_languages', 'user_type', 'dob',
            'professional_headline', 'industry', 'current_position',
            'qualifications', 'keywords', 'availability_from', 'availability_until',
            'availability_type', 'availability_location', 'availability_hours_per_week',
            'open_to_new_challenges', 'user_image', "user_searchable", "company", "email", "mobile_phone",
            "sharemundo_links_id", "company_id", "company_sharemundo_links_id",
            "main_area_of_expertise", "sub_area_of_expertise",
        )


class CalendarEventSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)
    freelancer = serializers.PrimaryKeyRelatedField(read_only=True)
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    
    class Meta:
        model = CalendarEvent
        fields = '__all__'

    def validate(self, attrs):
        start_date = attrs.get('start_date')
        end_date = attrs.get('end_date')

        if start_date and end_date:
            if start_date >= end_date:
                raise serializers.ValidationError("End date must be after start date")

        return attrs

    def validate_documents(self, documents):
        request = self.context.get('request')
        user = request.user
        for document in documents:
            if user.is_freelancer:
                if document.folder.freelancer!= user:
                    raise serializers.ValidationError(
                        f"Document {document.file_name} is not linked to the same freelancer as the object."
                    )
            else:
                if document.folder.company_profile != user.company:
                    raise serializers.ValidationError(
                        f"Document {document.file_name} is not linked to the same company as the object."
                    )
        return documents

    def get_linked_folder_owner_ids(self):
        request = self.context.get('request')
        user = request.user
        if user.is_freelancer:
            return None, None, user.id
        return None, user.company_id, None

    def validate_assignees(self, value):        
        request = self.context.get('request')
        user = request.user

        for assignee in value:
            if not can_assign_calendar_event(user, assignee):
                if user.is_freelancer:
                    raise serializers.ValidationError(
                        f"Cannot assign calendar event to {assignee.first_name}. "
                        "You can only assign events to yourself, your project team members, "
                        "or connected freelancers."
                    )
                else:
                    raise serializers.ValidationError(
                        f"Cannot assign calendar event to {assignee.first_name}. "
                        "You can only assign events to users from your company, project team members, "
                        "connected freelancers, or connected company users."
                    )

        return value
        

    def validate(self, attrs):
        request = self.context.get('request')
        user = request.user
        instance = self.instance

        start_date = attrs.get('start_date', instance.start_date if instance else None)
        end_date = attrs.get('end_date', instance.end_date if instance else None)

        # Ensure start_date and end_date are provided
        if not start_date or not end_date:
            raise serializers.ValidationError("Start date and End date are required.")

        # Ensure start_date is before end_date
        if start_date > end_date:
            raise serializers.ValidationError("Start date cannot be after end date.")

        return attrs

    def create(self, validated_data):
        """Create a calendar event with the employee."""
        request = self.context.get('request')
        user = request.user
        if user.is_freelancer:
            validated_data['freelancer'] = user
            validated_data['company_profile'] = None
        else:
            validated_data['freelancer'] = None
            validated_data['company_profile'] = user.company

        documents_data = validated_data.pop('documents', [])
        linked_folders_data = validated_data.pop('linked_folders', [])
        assignees_data = validated_data.pop('assignees', [])
        validated_data.setdefault('created_by', user)
        event = CalendarEvent(user=user, **validated_data)
        event._suppress_create_notification = True
        event.save()
        event._bypass_assignee_notification = True
        if documents_data:
            event.documents.set(documents_data)
        self.set_linked_folders(event, linked_folders_data)
        if assignees_data:
            event.assignees.set(assignees_data)
        delattr(event, '_bypass_assignee_notification')
        notify_calendar_event_created(event)

        try:
            schedule_reminder_to_sqs(event)
        except Exception as e:
            # Log the error but don't prevent event creation
            print(f"Error sending event to SQS: {e}")
        
        return event

    def update(self, instance, validated_data):
        """Update a calendar event with the employee."""
        request = self.context.get('request')
        user = request.user
        linked_folders_data = validated_data.pop('linked_folders', None)
        updated_event = super().update(instance, validated_data)
        self.set_linked_folders(updated_event, linked_folders_data)
        try:
            if instance.start_date != validated_data.get('start_date', instance.start_date) \
                or instance.assignees.all() != validated_data.get('assignees', instance.assignees.all()) \
                or instance.name != validated_data.get('name', instance.name):
                reschedule_event(updated_event, is_deleted=False)
        except Exception as e:
            # Log the error but don't prevent event update
            print(f"Error sending event to SQS: {e}")

        return updated_event

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.documents:
            representation["documents"] = DocumentSerializer(instance.documents, many=True).data
        if instance.assignees:
            representation["assignees"] = PublicUserSerializer(instance.assignees, many=True).data
        return representation
