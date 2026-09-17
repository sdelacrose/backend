from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny, BasePermission
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models.functions import Coalesce, Concat, NullIf
from django.db.models import CharField, Count, F, Func, IntegerField, Value
from django.conf import settings
import logging
from django.contrib.contenttypes.models import ContentType
from ecpmpapp.utils import create_notification_with_recipients, format_notification_message, get_actor_name
from ecpmpapp.ordering import TableOrderingFilter

from userapis.serializers.feedback import FeedbackSerializer
from userapis.filters import FeedbackFilter
from models.models import User, Feedback, FeedbackResponse
from models.constants import FeedbackStatusType
from django.contrib.auth.models import AnonymousUser
from rest_framework.exceptions import PermissionDenied
from userapis.utils import send_feedback_notification_email

logger = logging.getLogger(__name__)

class FeedbackViewSet(viewsets.ModelViewSet):
    serializer_class = FeedbackSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [
        filters.SearchFilter,
        DjangoFilterBackend,
        TableOrderingFilter,
    ]
    filterset_class = FeedbackFilter
    search_fields = ['description', 'id']
    ordering_fields = [
        "id",
        "company_profile",
        "company_profile__company_name",
        "project",
        "project_name",
        "solution_name",
        "user",
        "user__email",
        "user_full_name",
        "feedback_type",
        "role",
        "description",
        "status",
        "answer_required",
        "project_team_role",
        "freelancer",
        "response_sort_count",
        "attachment_count",
        "created_at",
    ]
    ordering = ["-id"]
    ordering_text_fields = {
        "company_profile__company_name",
        "project_name",
        "solution_name",
        "user_full_name",
        "feedback_type",
        "description",
        "status",
        "project_team_role",
    }
    ordering_natural_fields = {
        "company_profile__company_name",
        "project_name",
        "solution_name",
        "user_full_name",
        "description",
    }
    
    def get_queryset(self):
        user = self.request.user

        if isinstance(user, AnonymousUser):
            return Feedback.objects.none()
        
        return Feedback.objects.filter(user=user).annotate(
            user_full_name=Concat(
                F('user__first_name'),
                Value(' '),
                F('user__last_name'),
                output_field=CharField()
            ),
            response_sort_count=NullIf(
                Count("responses", distinct=True),
                Value(0),
                output_field=IntegerField(),
            ),
            attachment_count=Coalesce(
                Func(
                    F("attachments"),
                    function="CARDINALITY",
                    output_field=IntegerField(),
                ),
                Value(0),
                output_field=IntegerField(),
            ),
        )
    
    def get_object(self):
        obj = super().get_object()
        if obj.user != self.request.user and not self.request.user.is_superuser:
            raise PermissionDenied(detail="You are not allowed to access this feedback")
        return obj

    def perform_create(self, serializer):
        feedback = serializer.save()
        # Notify admins about new feedback
        self._notify_admins(feedback)

    def _notify_admins(self, feedback):
        # Get all super admins
        super_admins = User.objects.filter(is_superuser=True)
        for admin in super_admins:
            send_feedback_notification_email(admin, feedback)

    def update(self, request, *args, **kwargs):
        return Response(
            {"error": "Update operation is not allowed for feedback"},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def partial_update(self, request, *args, **kwargs):
        return Response(
            {"error": "Update operation is not allowed for feedback"},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class FeedbackNotificationView(APIView):
    """
    API endpoint to receive notifications from admin server about feedback updates.
    This endpoint is called by Django signals from the admin server when:
    - Feedback status is changed
    - Admin responds to feedback
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        try:
            token = request.data.get('SECRET_TOKEN')
            
            if not token:
                return Response(
                    {"error": "Authorization token is required."},
                    status=status.HTTP_401_UNAUTHORIZED
                )

            if token != settings.API_SECRET_KEY:
                return Response(
                    {"error": "Invalid authorization token."},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            feedback_id = request.data.get('feedback_id')
            event_type = request.data.get('event')

            if not feedback_id or not event_type:
                return Response(
                    {"error": "feedback_id and event are required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                feedback = Feedback.objects.get(id=feedback_id)
            except Feedback.DoesNotExist:
                return Response(
                    {"error": "Feedback not found"},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            if not feedback.user:
                return Response(
                    {"error": "Feedback has no associated user"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if event_type == 'status_update':
                new_status = request.data.get('status')
                if not new_status:
                    return Response(
                        {"error": "status is required for status_update event"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                status_messages = {
                    'incident_created': 'Your feedback has been converted to an incident and is being investigated.',
                    'improvement_created': 'Your feedback has been converted to an improvement request and is under review.',
                    'no_action': 'Your feedback has been reviewed and no action is required at this time.',
                    'closed': 'Your feedback has been resolved and closed.',
                    'archived': 'Your feedback has been archived.'
                }
                
                message = status_messages.get(new_status, f'Your feedback status has been updated to: {new_status}')
                
            elif event_type == 'response':
                response_text = request.data.get('response')
                if not response_text:
                    return Response(
                        {"error": "response is required for response event"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                message = f"Sharemundo admin has responded to your feedback: '{response_text[:100]}{'...' if len(response_text) > 100 else ''}'"
            
            else:
                return Response(
                    {"error": f"Unknown event type: {event_type}"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                create_notification_with_recipients(
                    message=message,
                    content_type=ContentType.objects.get_for_model(Feedback),
                    object_id=feedback.id,
                    content_object=feedback,
                    notification_type='update' if event_type == 'status_update' else 'create',
                    recipients={feedback.user},
                    important=True,
                )
                                
                return Response({
                    "success": True,
                    "message": "User notification sent successfully",
                    "feedback_id": feedback.id,
                    "user_id": feedback.user.id
                }, status=status.HTTP_200_OK)
                
            except Exception as notif_error:
                return Response({
                    "success": False,
                    "error": "Failed to send notification",
                    "details": str(notif_error)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
        except Exception as e:
            return Response({
                "success": False,
                "error": "Internal server error",
                "details": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


