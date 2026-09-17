import datetime

from rest_framework import viewsets
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser

from companyprofileapis.models import NoticeBoard
from companyprofileapis.serializers import (
    NoticeBoardSerializer
)
from companyprofileapis.permissions import NoticeBoardPermission

class NoticeBoardViewSet(viewsets.ModelViewSet):
    queryset = NoticeBoard.objects.all()
    serializer_class = NoticeBoardSerializer
    filterset_fields = ['freelancer', 'company_profile']

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [NoticeBoardPermission(action='view', scope='company')]
        elif self.action == 'create':
            return [NoticeBoardPermission(action='create', scope='company')]
        elif self.action in ['update', 'partial_update']:
            return [NoticeBoardPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            return [NoticeBoardPermission(action='delete', scope='company')]
        return super().get_permissions()

    def get_queryset(self):
        """
        Filter queryset based on user context.
        Permission checks are handled by the permission classes.
        """
        request_user = self.request.user
        if isinstance(request_user, AnonymousUser):
            return NoticeBoard.objects.none()

        # Filter based on user context (freelancer vs company user)
        if request_user.is_freelancer:
            queryset = NoticeBoard.objects.filter(freelancer=request_user)
        else:
            queryset = NoticeBoard.objects.filter(company_profile=request_user.company)

        # Filter out expired notices
        return queryset.filter(expires_at__gte=datetime.datetime.today().date())

    def get_object(self):
        obj = super().get_object()
        request_user = self.request.user

        self.check_object_permissions(self.request, obj)
        return obj

