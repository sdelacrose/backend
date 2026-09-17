from project_management.models.constants import COLLABORATIVE_SOLUTION

from rest_framework import viewsets
from rest_framework.response import Response
from django.contrib.auth.models import AnonymousUser
from rest_framework.parsers import JSONParser, MultiPartParser

from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from project_management.models import (
    ObjectAssessment,
)
from project_management.filters import ObjectAssessmentFilter
from project_management.serializers import (
    ObjectAssessmentSerializer
)
from ecpmpapp.constants import UserType
from project_management.utils import (
    generate_object_assessment_record_title,
    get_product_name_from_request,
)
from project_management.permissions import ObjectAssessmentPermission
from project_management.services.object_assessment_bulk_update import (
    BulkObjectAssessmentUpdateRequestSerializer,
    ObjectAssessmentBulkUpdateService,
)
from ecpmpapp.utils import require_any_permission_multiple_modules


class ObjectAssessmentViewSet(viewsets.ModelViewSet):
    queryset = ObjectAssessment.objects.all()
    serializer_class = ObjectAssessmentSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    filterset_class = ObjectAssessmentFilter
    search_fields = ['record_type', 'record_title', "author__first_name", "author__last_name", "document_type", "status", "file_name"]
    parser_classes = [JSONParser, MultiPartParser]

    def get_permissions(self):
        if self.action == 'bulk_update':
            return [ObjectAssessmentPermission(action='update', scope='project')]
        if self.action in ['list', 'retrieve', 'next_record_title']:
            return [ObjectAssessmentPermission(action='view', scope='project')]
        elif self.action == 'create':
            return [ObjectAssessmentPermission(action='create', scope='project')]
        elif self.action in ['update', 'partial_update']:
            return [ObjectAssessmentPermission(action='update', scope='project')]
        elif self.action == 'destroy':
            return [ObjectAssessmentPermission(action='delete', scope='project')]
        return super().get_permissions()

    @action(detail=False, methods=['patch'], url_path='bulk-update')
    def bulk_update(self, request):
        request_serializer = BulkObjectAssessmentUpdateRequestSerializer(
            data=request.data
        )
        request_serializer.is_valid(raise_exception=True)
        result = ObjectAssessmentBulkUpdateService(
            request=request,
            serializer_context=self.get_serializer_context(),
        ).execute(
            object_assessment_ids=request_serializer.validated_data['ids'],
            changes=request_serializer.validated_data['changes'],
        )
        return Response(result, status=status.HTTP_200_OK)

    def get_serializer(self, *args, **kwargs):
        serializer = super(ObjectAssessmentViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        serializer.context['include_history'] = self.request.query_params.get('include_history', False)
        return serializer

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ObjectAssessment.objects.none()
        
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]

        has_projects_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['full_access', 'view_all_access']}, 
            'company', 
            self.request.query_params
        )


        has_object_assessments_project_permissions = require_any_permission_multiple_modules(
            user, 
            {'object_assessments': ['view', 'full_access']}, 
            'project', 
            self.request.query_params
        )
        query_filters = Q()

        has_object_assessments_created_permissions = require_any_permission_multiple_modules(
            user, 
            {'object_assessments': ['view_assigned_created', 'update_assigned_created', 'delete_assigned_created']}, 
            'object_assessments', 
            self.request.query_params
        )
        if not user.is_freelancer and has_projects_company_permissions:
            query_filters = (
                Q(project__solution__in=products) & 
                (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))
            )
        elif has_object_assessments_project_permissions:
            query_filters = Q(project__team_members__team_member=user, project__solution__in=products)
        elif has_object_assessments_created_permissions:
            query_filters = query_filters | Q(created_by=user) | Q(assignee=user)
        else:
            return ObjectAssessment.objects.none()
        
        return ObjectAssessment.objects.filter(query_filters).distinct().prefetch_related(
            'documents',
            'solution_demands',
            'requirements',
            'available_product_parts',
            'bom_parts',
            'transports',
            'project_solution_space_items',
        ).order_by('-id')

    def get_object(self):
        obj = get_object_or_404(ObjectAssessment, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj

    @action(detail=False, methods=['get'])
    def next_record_title(self, request):
        project_id = request.query_params.get('project')
        record_type = request.query_params.get('record_type')

        if not project_id or not record_type:
            return Response(
                {"detail": "Both 'project' and 'record_type' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            "record_title": generate_object_assessment_record_title(
                self.get_queryset(),
                project_id,
                record_type,
            )
        })

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter('created_at_after', openapi.IN_QUERY, description="Filter by start date", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            openapi.Parameter('created_at_before', openapi.IN_QUERY, description="Filter by end date", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            openapi.Parameter('get_folder_like_structure', openapi.IN_QUERY, description="Get folder like structure", type=openapi.TYPE_BOOLEAN),
        ]
    )
    def list(self, request, *args, **kwargs):
        search = request.query_params.get('search', None)
        project_id = request.query_params.get('project', None)

        get_folder_like_structure = request.query_params.get('get_folder_like_structure', None)
        response = super().list(request, *args, **kwargs)

        if search and get_folder_like_structure == "true":
            if search.lower() in "object assessment" and project_id:
                queryset = self.get_queryset()
                objects_assessment_items = ObjectAssessmentSerializer(queryset.filter(project_id=int(project_id)), many=True).data
            else:
                objects_assessment_items = response.data["results"]
            if len(objects_assessment_items) > 0:
                project_id = objects_assessment_items[0]["project"]
                objects_assessment_dict = {
                    "name": "Object Assessment",
                    "project": project_id,
                    "documents": [],
                    "subfolders": []
                }
                sub_folders = {}

                for item in objects_assessment_items:
                    record_type = item["record_type"]
                    if record_type not in sub_folders:
                        sub_folders[record_type] = {
                            "name": record_type,
                            "project": project_id,
                            "documents": [],
                            "subfolders": []
                        }
                        objects_assessment_dict["subfolders"].append(sub_folders[record_type])
                    sub_folders[record_type]["documents"].append({
                        "id": item["id"],
                        "name": item["record_title"],
                        "file": item["file"],
                        "document_type": item["document_type"],
                        "status": item["status"],
                        "author": item["author"],
                        "assignee": item["assignee"],
                        "created_by": item["created_by"],
                        "record_title": item["record_title"],
                        "record_type": item["record_type"],
                        "description": item["description"],
                        "identification_number": item["identification_number"],
                        "file_name": item["file_name"],
                        "revision": item["revision"],
                        "created_at": item["created_at"],
                        "updated_at": item["updated_at"]
                    })

            else:
                return Response({})

            return Response(objects_assessment_dict)
        else:
            return response
