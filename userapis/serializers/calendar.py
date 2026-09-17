from django.db.models import Q
from rest_framework import serializers

from models.models import CompanyConfig
from models.rooms_utils import effective_rooms_list
from userapis.models import CalendarEvent
from userapis.serializers import PublicUserSerializer
from userapis.microsoft_outlook import PRIVATE_OUTLOOK_EVENT_TITLE
from project_management.serializers.document import DocumentSerializer
from project_management.serializers.linked_folder import LinkedFolderSerializerMixin
from project_management.models import Project
from project_management.utils import get_product_name_from_request
from ecpmpapp.utils import schedule_reminder_to_sqs, reschedule_event, can_assign_calendar_event
from userapis.signals import notify_calendar_event_created

class CalendarEventSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)
    freelancer = serializers.PrimaryKeyRelatedField(read_only=True)
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    can_view_private_details = serializers.SerializerMethodField()
    
    class Meta:
        model = CalendarEvent
        fields = '__all__'
        read_only_fields = ('is_private', 'can_view_private_details')

    def _get_project(self, attrs):
        instance = self.instance
        return attrs.get('project', instance.project if instance else None)

    def _get_project_for_field_validation(self):
        if self.instance and self.instance.project_id:
            return self.instance.project

        project_id = self.initial_data.get('project') if hasattr(self, 'initial_data') else None
        if not project_id:
            return None

        try:
            return Project.objects.get(id=project_id)
        except (Project.DoesNotExist, TypeError, ValueError):
            return None

    def _validate_project_scope(self, project):
        request = self.context.get('request')
        user = getattr(request, 'user', None)

        if not project:
            return

        if not self.context.get('allow_project_scope', False):
            raise serializers.ValidationError({
                'project': 'Project-scoped calendar events are not supported by this endpoint.'
            })

        is_project_member = bool(
            user and (
                project.user_id == user.id
                or project.team_members.filter(team_member=user).exists()
                or project.external_users.filter(contact_user__user=user).exists()
            )
        )

        if is_project_member:
            return

        if user and not user.is_freelancer and project.company_profile_id != getattr(user, 'company_id', None):
            raise serializers.ValidationError({
                'project': 'Project must belong to the same company as the calendar event.'
            })

    def validate_documents(self, documents):
        request = self.context.get('request')
        user = request.user
        project = self._get_project_for_field_validation()

        for document in documents:
            if project:
                if document.folder.project_id != project.id:
                    raise serializers.ValidationError(
                        f"Document {document.file_name} is not linked to the same project as the object."
                    )
            elif user.is_freelancer:
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
        project = self._get_project_for_field_validation()

        if project:
            return project.id, None, None
        if user.is_freelancer:
            return None, None, user.id
        return None, user.company_id, None

    def validate_object_assessments(self, object_assessments):
        project = self._get_project_for_field_validation()

        if not project and object_assessments:
            raise serializers.ValidationError(
                "Object assessments can only be linked to project calendar events."
            )

        for object_assessment in object_assessments:
            if object_assessment.project_id != project.id:
                raise serializers.ValidationError(
                    (
                        f"Object assessment {object_assessment.record_title or object_assessment.id} "
                        "is not linked to the same project as the calendar event."
                    )
                )

        return object_assessments

    def _get_config_product_name(self, request, user):
       #  defaults to Energy Consultant Solution via get_product_name_from_request.
        return get_product_name_from_request(request)

    def _get_company_rooms_for_company(self, company_profile, product_name):
        if not company_profile:
            return []

        config = CompanyConfig.objects.filter(
            company_profile=company_profile,
            solution=product_name,
        ).first()
        rooms = effective_rooms_list(config.rooms if config else [])
        return rooms

    def _get_valid_rooms(self):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        project = self._get_project_for_field_validation()

        if not user:
            return []

        product_name = self._get_config_product_name(request, user)

        if project:
            return self._get_company_rooms_for_company(
                project.company_profile,
                product_name,
            )

        if user.is_freelancer:
            config = CompanyConfig.objects.filter(user=user, solution=product_name).first()
        else:
            return self._get_company_rooms_for_company(user.company, product_name)

        return effective_rooms_list(config.rooms if config else [])

    def _get_room_booking_conflicts(self, room, start_date, end_date, project, user):
        if not room or not start_date or not end_date or not user:
            return CalendarEvent.objects.none()

        conflicts = CalendarEvent.objects.filter(
            room=room,
            start_date__lt=end_date,
            end_date__gt=start_date,
        ).select_related("created_by", "user").prefetch_related("assignees")

        if self.instance:
            conflicts = conflicts.exclude(pk=self.instance.pk)

        if project:
            if project.company_profile_id:
                conflicts = conflicts.filter(
                    Q(project__company_profile_id=project.company_profile_id)
                    | Q(company_profile_id=project.company_profile_id)
                )
            elif getattr(user, "company_id", None):
                conflicts = conflicts.filter(
                    Q(project__company_profile_id=user.company_id)
                    | Q(company_profile_id=user.company_id)
                )
            else:
                return CalendarEvent.objects.none()
        elif user.is_freelancer:
            conflicts = conflicts.filter(freelancer=user, project__isnull=True)
        elif getattr(user, "company_id", None):
            conflicts = conflicts.filter(
                Q(project__company_profile_id=user.company_id)
                | Q(company_profile_id=user.company_id)
            )
        else:
            return CalendarEvent.objects.none()

        return conflicts

    def _get_conflict_booked_by_user(self, conflict):
        user = conflict.created_by or conflict.user
        if user:
            return user

        return conflict.assignees.first()

    def _get_conflict_booked_by_name(self, conflict):
        user = self._get_conflict_booked_by_user(conflict)
        if not user:
            return "Another user"

        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        if full_name:
            return full_name

        return user.email or "Another user"

    def _is_self_room_booking_conflict(self, conflict, current_user):
        if not current_user or not getattr(current_user, "is_authenticated", False):
            return False

        if conflict.created_by_id == current_user.id:
            return True

        return conflict.user_id == current_user.id

    def validate_room(self, value):
        if not value:
            return value

        valid_rooms = self._get_valid_rooms()
        if value not in valid_rooms:
            raise serializers.ValidationError(
                f"Room '{value}' is not a valid configured room."
            )
        return value

    def validate_assignees(self, value):        
        request = self.context.get('request')
        user = request.user
        project = self._get_project_for_field_validation()

        if project:
            allowed_assignee_ids = set(
                project.team_members.values_list('team_member_id', flat=True)
            )
            if project.user_id:
                allowed_assignee_ids.add(project.user_id)

            invalid_assignees = [
                assignee.first_name or assignee.email or str(assignee.id)
                for assignee in value
                if assignee.id not in allowed_assignee_ids
            ]
            if invalid_assignees:
                raise serializers.ValidationError(
                    (
                        "Project calendar events can only be assigned to project team members. "
                        f"Invalid assignees: {', '.join(invalid_assignees)}."
                    )
                )
            return value

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
        instance = self.instance

        start_date = attrs.get('start_date', instance.start_date if instance else None)
        end_date = attrs.get('end_date', instance.end_date if instance else None)
        project = self._get_project(attrs)

        self._validate_project_scope(project)

        # Ensure start_date and end_date are provided
        if not start_date or not end_date:
            raise serializers.ValidationError("Start date and End date are required.")

        # Ensure start_date is before end_date
        if start_date > end_date:
            raise serializers.ValidationError("Start date cannot be after end date.")

        if project and project.company_profile_id is None and not request.user.is_freelancer:
            raise serializers.ValidationError({
                'project': 'Project calendar events must belong to a company project for company users.'
            })

        room = attrs.get('room')
        if room is None and instance:
            room = instance.room

        self._room_warning = None
        if room:
            conflicts = self._get_room_booking_conflicts(
                room,
                start_date,
                end_date,
                project,
                request.user,
            )
            if conflicts.exists():
                conflict = conflicts.order_by('start_date').first()
                booked_by_user = self._get_conflict_booked_by_user(conflict)
                is_self_booking = self._is_self_room_booking_conflict(
                    conflict,
                    request.user,
                )
                self._room_warning = {
                    "room": room,
                    "booked_by": self._get_conflict_booked_by_name(conflict),
                    "booked_by_user_id": booked_by_user.id if booked_by_user else None,
                    "is_self_booking": is_self_booking,
                    "start": conflict.start_date.isoformat(),
                    "end": conflict.end_date.isoformat(),
                    "event_name": conflict.name,
                    "project_name": (
                        conflict.project.project_name
                        if conflict.project_id and conflict.project
                        else None
                    ),
                }

        return attrs

    @property
    def room_warning(self):
        return getattr(self, '_room_warning', None)

    def create(self, validated_data):
        """Create a calendar event with the employee."""
        request = self.context.get('request')
        user = request.user
        project = validated_data.get('project')
        if project and project.company_profile_id:
            validated_data['company_profile'] = project.company_profile
            validated_data['freelancer'] = user if user.is_freelancer else None
        elif user.is_freelancer:
            validated_data['freelancer'] = user
            validated_data['company_profile'] = None
        else:
            validated_data['freelancer'] = None
            validated_data['company_profile'] = user.company

        documents_data = validated_data.pop('documents', [])
        linked_folders_data = validated_data.pop('linked_folders', [])
        object_assessments_data = validated_data.pop('object_assessments', [])
        assignees_data = validated_data.pop('assignees', [])
        validated_data['created_by'] = user
        # Keep user in sync for reminder payload (user_email) and ownership.
        validated_data['user'] = user
        event = CalendarEvent(**validated_data)
        # Defer create notify until after M2M assignees are attached.
        event._suppress_create_notification = True
        event.save()
        event._bypass_assignee_notification = True

        if documents_data:
            event.documents.set(documents_data)
        self.set_linked_folders(event, linked_folders_data)
        if object_assessments_data:
            event.object_assessments.set(object_assessments_data)
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

    def get_can_view_private_details(self, instance):
        request = self.context.get('request')
        if not request or not getattr(request, 'user', None):
            return False
        return instance.user == request.user

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        can_view_private_details = self.get_can_view_private_details(instance)

        if instance.is_private and not can_view_private_details:
            representation["name"] = PRIVATE_OUTLOOK_EVENT_TITLE
            representation["description"] = None
            representation["meeting_link"] = None
            representation["room"] = None
            representation["documents"] = []
            representation["linked_folders"] = []
            representation["linked_folder_details"] = []
            representation["assignees"] = []
            return representation

        if instance.documents:
            representation["documents"] = DocumentSerializer(instance.documents, many=True).data
        else:
            representation["documents"] = []

        if instance.object_assessments.exists():
            representation["object_assessments"] = [
                {
                    "id": assessment.id,
                    "record_title": assessment.record_title,
                    "record_type": assessment.record_type,
                    "project": {
                        "id": assessment.project_id,
                        "name": assessment.project.project_name,
                    } if assessment.project_id else None,
                }
                for assessment in instance.object_assessments.all()
            ]
        else:
            representation["object_assessments"] = []

        if instance.assignees:
            representation["assignees"] = PublicUserSerializer(instance.assignees, many=True).data
        else:
            representation["assignees"] = []
        return representation
