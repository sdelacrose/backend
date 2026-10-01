from project_management.models.constants import COLLABORATIVE_SOLUTION
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework import viewsets, filters
from rest_framework.filters import OrderingFilter
from rest_framework.decorators import action
from rest_framework.parsers import JSONParser, MultiPartParser
from django_filters.rest_framework import DjangoFilterBackend
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db.models import Case, CharField, Exists, F, Func, OuterRef, Prefetch, Q, Value, When
from django.db.models.functions import Coalesce, Concat, NullIf
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied

from django.utils.decorators import method_decorator

from models.models import CompanyProfile, User
from project_management.models import ContactCompany, Folder, Document, DocumentWorkflow, Project, ObjectAssessment
from project_management.serializers.document import FolderSerializer, DocumentSerializer
from ecpmpapp.constants import UserType
from ecpmpapp.ordering import TableOrderingFilter
from project_management.utils import get_product_name_from_request, has_project_permission
from project_management.serializers.document import DocumentHistorySerializer
from project_management.permissions import DocumentIssuerLookupPermission, DocumentPermission
from project_management.filters import DocumentFilter
from project_management.services.document_access import get_accessible_document_queryset
from project_management.services.document_workflow import (
    DOCUMENT_EVIDENCE_MESSAGE,
    FOLDER_EVIDENCE_MESSAGE,
    annotate_current_workflow,
    document_has_evidence,
    folder_has_evidence,
)
from project_management.services.document_bulk_update import (
    BulkDocumentUpdateRequestSerializer,
    DocumentBulkUpdateService,
)



from django.db.models import Q
from project_management.models import Folder, Project
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from ecpmpapp.utils import require_any_permission_multiple_modules


def document_display_file_name_annotation():
    file_basename = Func(
        F("file"),
        Value(".*/"),
        Value(""),
        function="REGEXP_REPLACE",
        output_field=CharField(),
    )
    resolved_file_name = Coalesce(
        NullIf(F("file_name"), Value("")),
        file_basename,
        Value(""),
        output_field=CharField(),
    )
    return Case(
        When(
            Q(full_document_number__isnull=False) & ~Q(full_document_number=""),
            then=Concat(
                F("full_document_number"),
                Value("_"),
                resolved_file_name,
                output_field=CharField(),
            ),
        ),
        default=resolved_file_name,
        output_field=CharField(),
    )


class CaseInsensitiveDocumentOrderingFilter(TableOrderingFilter):
    DISPLAY_FILE_NAME_ORDERING_FIELDS = {"file_name"}

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset

        if any(
            (field[1:] if field.startswith("-") else field)
            in self.DISPLAY_FILE_NAME_ORDERING_FIELDS
            for field in ordering
        ):
            queryset = queryset.alias(
                display_file_name=document_display_file_name_annotation(),
            )
            view.ordering_field_aliases = {
                **dict(getattr(view, "ordering_field_aliases", {})),
                "file_name": "display_file_name",
            }

        return super().filter_queryset(request, queryset, view)


class FolderViewSet(viewsets.ModelViewSet):
    queryset = Folder.objects.all()
    serializer_class = FolderSerializer
    filterset_fields = ["name"]
    filter_backends = [filters.SearchFilter, DjangoFilterBackend, OrderingFilter]
    ordering_fields = [
        'id',
        'project',
        'company_profile',
        'freelancer',
        'name',
        'parent',
        'created_at',
        'updated_at',
    ]
    ordering = ['-id']

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.system_key:
            return Response(
                {"detail": "System folders cannot be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)

    def get_serializer(self, *args, **kwargs):
        serializer = super(FolderViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        serializer.context['include_history'] = self.request.query_params.get('include_history', False)
        return serializer

    def get_permissions(self):
        if self.action == 'list':
            # Check if it's a company or project folder based on query params
            name = self.request.query_params.get('name')
            print(f'[DEBUG] FolderViewSet: get_permissions: name: {name} | self.action: {self.action}')
            if name and '_company_root_folder' in name:
                # Company folder - extract company profile id from name
                try:
                    company_profile_id = name.split('_')[0]
                    from models.models import CompanyProfile
                    company_profile = CompanyProfile.objects.get(id=company_profile_id)
                    return [DocumentPermission('view', 'company')]
                except (ValueError, IndexError, CompanyProfile.DoesNotExist):
                    pass
            elif name and '_root_folder' in name:
                # Project folder - extract project id from name
                try:
                    project_id = name.split('_')[0]
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
                    return [DocumentPermission('view', 'project')]
                except (ValueError, IndexError, Project.DoesNotExist):
                    pass
            
            # Default to project permissions if no specific context found
            print(f'[DEBUG] FolderViewSet: get_permissions: name: {name} | self.action: {self.action}')
            return [DocumentPermission('view', 'project')]
        elif self.action == 'retrieve':
            return [DocumentPermission('view', 'project')]
        elif self.action == 'create':
            return [DocumentPermission('create', 'project')]
        elif self.action in ['update', 'partial_update']:
            return [DocumentPermission('update', 'project')]
        elif self.action == 'destroy':
            return [DocumentPermission('delete', 'project')]
        return super().get_permissions()

    def get_queryset(self):
        """
        Filter queryset based on user permissions using the new DocumentPermission system.
        """

        print(f'[DEBUG] FolderViewSet: get_queryset: self.action: {self.action}')
        print(f'[DEBUG] FolderViewSet: get_queryset: self.request.query_params: {self.request.query_params}')
        user = self.request.user
        request = self.request
        project_id = None
        # Parse query params to get project id
        name = self.request.query_params.get('name', None)
        if name and '_' in name:
            name_param = name.split('_', 1)
            if len(name_param) == 2 and name_param[1] == 'root_folder':
                project_id = name_param[0]
                print(f'[DEBUG] FolderViewSet: get_queryset: project_id: {project_id}')

        if user.is_anonymous:
            return Folder.objects.none()
        
        product_name = get_product_name_from_request(request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]
        
        filters = Q()
        
        # Check company-level permissions
        has_documents_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'documents': ['view', 'full_access']}, 
            'company', 
            {'project': project_id}
        )
        
        has_projects_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['full_access', 'view_all_access']}, 
            'company', 
            {'project': project_id}
        )
        
        # Check project-level permissions
        has_project_permissions = require_any_permission_multiple_modules(
            user, 
            {'documents': ['view', 'full_access']}, 
            'project', 
            {'project': project_id}
        )

        has_assigned_created_permissions = require_any_permission_multiple_modules(
            user, 
            {'documents': ['view_created', 'update_created', 'delete_created', 'full_access']}, 
            'documents', 
            {'project': project_id}
        )
        print(f'[DEBUG] FolderViewSet: get_queryset: has_documents_company_permissions: {has_documents_company_permissions}')
        print(f'[DEBUG] FolderViewSet: get_queryset: has_projects_company_permissions: {has_projects_company_permissions}')
        print(f'[DEBUG] FolderViewSet: get_queryset: has_project_permissions: {has_project_permissions}')
        
        # Company-level folders (for users with documents permissions at company level)
        if not user.is_freelancer and has_documents_company_permissions:
            filters |= Q(company_profile=user.company)
        
        # Project-level folders (for users with project permissions)
        if has_project_permissions:
            # Get projects where user has project-level permissions
            user_projects = Project.objects.filter(
                team_members__team_member=user,
                solution__in=products
            )
            if user_projects.exists():
                filters |= Q(project__in=user_projects)
        
        # Company users with projects permissions (can see all company projects)
        if not user.is_freelancer and has_projects_company_permissions:
            company_project_filters = (
                Q(project__solution__in=products) & 
                Q(project__user__company=user.company)
            )
            filters |= company_project_filters
        
        # Freelancer logic
        if user.is_freelancer:
            # Freelancers get their own folders
            freelancer_filters = Q(freelancer=user)
            filters |= freelancer_filters
            
            # Freelancers get project folders where they have project-level permissions
            if has_project_permissions:
                freelancer_projects = Project.objects.filter(
                    team_members__team_member=user,
                    solution__in=products
                )
                if freelancer_projects.exists():
                    filters |= Q(project__in=freelancer_projects)
        
        
        # If no permissions found, return empty queryset
        if not filters:
            return Folder.objects.none()
        
        return Folder.objects.filter(filters) \
            .select_related('project') \
            .select_related('company_profile') \
            .select_related(UserType.FREELANCER.value) \
            .prefetch_related(Prefetch('subfolders', queryset=Folder.objects.filter(filters))) \
            .distinct() \
            .order_by('-id')

    def get_object(self):
        obj = get_object_or_404(Folder, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        request_user = self.request.user

        if obj.company_profile and request_user.company != obj.company_profile:
            raise PermissionDenied("You do not have permission to perform this action.")
        elif obj.freelancer and request_user != obj.freelancer:
            raise PermissionDenied("You do not have permission to perform this action.")
        return obj

    def list(self, request, *args, **kwargs):
        name = request.query_params.get('name', None)
        response = super().list(request, *args, **kwargs)

        if name and '_' in name:
            name_param = name.split('_', 1)
            if len(name_param) == 2 and name_param[1] == 'root_folder':
                project_id = name_param[0]
                if response.data["results"] and response.data["results"][0]["name"] == name:
                    objects_assessment_items = ObjectAssessment.objects.filter(project_id=project_id).prefetch_related('documents', 'author')

                    objects_assessment_dict = {
                        "name": "Object Assessment",
                        "project": project_id,
                        "documents": [],
                        "subfolders": []
                    }

                    sub_folders = {}

                    for item in objects_assessment_items:
                        record_type = item.record_type
                        if record_type not in sub_folders:
                            sub_folders[record_type] = {
                                "name": record_type,
                                "project": project_id,
                                "documents": [],
                                "subfolders": []
                            }
                            objects_assessment_dict["subfolders"].append(sub_folders[record_type])
                        sub_folders[record_type]["documents"].append({
                            "id": item.id,
                            "name": item.record_title,
                            "file": item.file.url if item.file else None,
                            "document_type": item.document_type,
                            "status": item.status,
                            "author": {
                                "id": item.author.id,
                                "name": item.author.full_name,
                            } if item.author else None,
                            "identification_number": item.identification_number,
                            "record_title": item.record_title,
                            "record_type": item.record_type,
                            "description": item.description,
                            "file_name": item.file_name,
                            "revision": item.revision,
                            "created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                            "updated_at": item.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
                            "documents": DocumentSerializer(item.documents.all(), many=True).data,
                            "history": [DocumentHistorySerializer(doc.history.all().order_by('-history_date'), many=True).data 
                                       for doc in item.documents.all()] if request.query_params.get('include_history', False) else None
                        })

                    response.data["results"][0]["subfolders"].append(objects_assessment_dict)

        return Response(response.data)



    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                'project_id',
                openapi.IN_QUERY,
                description="Project id",
                type=openapi.TYPE_INTEGER,
                required=False
            ),
            openapi.Parameter(
                'company_profile_id',
                openapi.IN_QUERY,
                description="Company Profile id",
                type=openapi.TYPE_INTEGER,
                required=False
            ),
            openapi.Parameter(
                'freelancer_id',
                openapi.IN_QUERY,
                description="Freelancer id",
                type=openapi.TYPE_INTEGER,
                required=False
            ),
            openapi.Parameter(
                'search_term',
                openapi.IN_QUERY,
                description="Search term to look for in folder and document names",
                type=openapi.TYPE_STRING,
                required=True
            ),
        ],
        responses={200: FolderSerializer(many=True)}
    )
    @action(detail=False, methods=['get'], url_path='search')
    def search_folders(self, request, *args, **kwargs):
        project_id = request.query_params.get('project_id')
        company_profile_id = request.query_params.get('company_profile_id')
        freelancer_id = request.query_params.get('freelancer_id')
        search_term = request.query_params.get('search_term')
        include_history = request.query_params.get('include_history', False)

        if (not project_id and not company_profile_id and not freelancer_id) or not search_term:
            return Response({'error': 'root_folder_name and search_term are required'}, status=400)

        project = None
        company_profile = None
        freelancer = None
        if project_id:
            try:
                project = get_object_or_404(Project, pk=project_id)
                # TODO: Adjust as per new permission system
                # if not has_project_permission(self.request.method, self.request.user, project, get_product_name_from_request(request)):
                #     raise PermissionDenied("You do not have permission to perform this action.")
            except Project.DoesNotExist:
                return Response({'error': 'Invalid Project ID'}, status=404)
        elif company_profile_id:
            try:
                company_profile = get_object_or_404(CompanyProfile, pk=company_profile_id)
                if self.request.user.is_freelancer or self.request.user.company != company_profile:
                    raise PermissionDenied("You do not have permission to search company documents.")
            except CompanyProfile.DoesNotExist:
                return Response({'error': 'Invalid CompanyProfile ID'}, status=404)
        elif freelancer_id:
            try:
                freelancer = get_object_or_404(User, pk=freelancer_id)
                if not self.request.user.is_freelancer or self.request.user != freelancer:
                    raise PermissionDenied("You do not have permission to search freelancer documents.")
            except User.DoesNotExist:
                return Response({'error': 'Invalid Freelancer ID'}, status=404)
        try:
            if project:
                root_folder = Folder.objects.get(project=project, name=f"{project_id}_root_folder", parent=None)
            elif company_profile:
                root_folder = Folder.objects.get(company_profile=company_profile, name=f"{company_profile_id}_company_root_folder", parent=None)
            elif freelancer:
                root_folder = Folder.objects.get(freelancer=freelancer, name=f"{freelancer_id}_freelancer_root_folder", parent=None)
        except (ValueError, Folder.DoesNotExist):
            return Response({'error': 'Invalid root folder'}, status=404)

        # Search for matching folders and documents
        if project:
            matching_folders = Folder.objects.filter(name__icontains=search_term, project_id=project_id)
            matching_documents = Document.objects.filter(Q(file__icontains=search_term) | Q(file_name__icontains=search_term), folder__project_id=project_id)
        elif company_profile:
            matching_folders = Folder.objects.filter(name__icontains=search_term, company_profile=company_profile)
            matching_documents = Document.objects.filter(Q(file__icontains=search_term) | Q(file_name__icontains=search_term), folder__company_profile=company_profile)
        elif freelancer:
            matching_folders = Folder.objects.filter(name__icontains=search_term, freelancer=freelancer)
            matching_documents = Document.objects.filter(Q(file__icontains=search_term) | Q(file_name__icontains=search_term), folder__freelancer=freelancer)
            
        # Create a structure to store folder hierarchy
        folder_structure = {}
        for folder in matching_folders:
            self.add_folder_to_structure(folder_structure, folder, root_folder, serialize=True)

        for document in matching_documents:
            self.add_folder_to_structure(folder_structure, document.folder, root_folder, document, include_history=include_history)

        # Serialize the folder structure manually
        serialized_structure = self.serialize_folder_structure(
            folder_structure, 
            root_folder, 
            matching_folders.values_list('id', flat=True),
            include_history=include_history
        )

        return Response(serialized_structure)

    def add_folder_to_structure(self, structure, folder, root_folder, document=None, serialize=False, include_history=False):
        """
        Add the folder (and optionally the document) to the structure.
        This will recursively go up to the root folder and build the parent hierarchy.
        """
        if folder == root_folder:
            if root_folder.id not in structure:
                structure[root_folder.id] = {
                    'id': root_folder.id,
                    'name': root_folder.name,
                    'subfolders': {},
                    'documents': []
                }
            if document:
                doc_serializer = DocumentSerializer(document, context={
                    'include_history': include_history,
                    'include_linked_items': self.request.query_params.get('include_linked_items', 'true').lower() == 'true'
                })
                structure[root_folder.id]['documents'].append(doc_serializer.data)
            return structure[root_folder.id]

        # Add parent folders to the structure recursively
        parent_structure = self.add_folder_to_structure(structure, folder.parent, root_folder)

        # Add the current folder to the parent folder's subfolders
        if folder.id not in parent_structure['subfolders']:
            if serialize:
                parent_structure['subfolders'][folder.id] = FolderSerializer(folder).data
            else:
                parent_structure['subfolders'][folder.id] = {
                    'id': folder.id,
                    'name': folder.name,
                    'subfolders': {},
                    'documents': []
                }

        if document:
            doc_serializer = DocumentSerializer(document, context={
                'include_history': include_history,
                'include_linked_items': self.request.query_params.get('include_linked_items', 'true').lower() == 'true'
            })
            parent_structure['subfolders'][folder.id]['documents'].append(doc_serializer.data)

        return parent_structure['subfolders'][folder.id]

    def serialize_folder_structure(self, structure, root_folder, searched_folder_ids, include_history=False):
        """
        Serialize the folder structure, ensuring that documents in the search result are included.
        """
        def serialize_folder(folder_data):
            if folder_data['id'] in searched_folder_ids:
                return folder_data
            return {
                'id': folder_data['id'],
                'name': folder_data['name'],
                'subfolders': [serialize_folder(subfolder) for subfolder in folder_data['subfolders'].values()],
                'documents': [
                    DocumentSerializer(doc, context={
                        'include_history': include_history,
                        'include_linked_items': self.request.query_params.get('include_linked_items', 'true').lower() == 'true'
                    }).data 
                    if isinstance(doc, Document) else doc 
                    for doc in folder_data['documents']
                ]
            }

        return serialize_folder(structure[root_folder.id]) if structure else structure

    def create(self, request, *args, **kwargs):
        try:
            return super().create(request, *args, **kwargs)
        except ValidationError as e:
            return Response({"detail": e.message}, status=status.HTTP_400_BAD_REQUEST)

    def update(self, request, *args, **kwargs):
        try:
            return super().update(request, *args, **kwargs)
        except ValidationError as e:
            return Response({"detail": e.message}, status=status.HTTP_400_BAD_REQUEST)

    def destroy(self, request, *args, **kwargs):
        # Preflight: refuse cleanly BEFORE touching anything when a document below this folder
        # is documentary evidence (the model/signal guards stay as defence in depth).
        folder = self.get_object()
        if folder_has_evidence(folder):
            return Response(
                {"detail": FOLDER_EVIDENCE_MESSAGE},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ValidationError as e:
            return Response(
                {"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST
            )
 
 
class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.select_related('folder').all()
    serializer_class = DocumentSerializer
    parser_classes = [JSONParser, MultiPartParser]
    filterset_class = DocumentFilter
    filter_backends = [
        filters.SearchFilter,
        DjangoFilterBackend,
        CaseInsensitiveDocumentOrderingFilter,
    ]
    ordering_fields = [
        "id",
        "file_name",
        "full_document_number",
        "identification_number",
        "revision",
        "folder__name",
        "author_name",
        "uploaded_at",
        "updated_at",
        "created_at",
        "document_type",
        "status",
        "category",
        "discipline",
    ]
    ordering_text_fields = {
        "file_name",
        "folder__name",
        "author_name",
        "status",
        "identification_number",
        "full_document_number",
        "document_type",
        "category",
        "discipline",
    }
    ordering_natural_fields = ordering_text_fields
    ordering = ["-uploaded_at", "-id"]

    # D13 live review section 14: all three business references (Internal/Supplier/Client) are
    # searchable -- full_document_number/identification_number already were; client_reference
    # added here, same trivial extension, no new search system.
    search_fields = ["file_name", "author__first_name", "author__last_name", "id", "document_type", "full_document_number", "identification_number", "client_reference"]

    def get_serializer(self, *args, **kwargs):
        serializer = super(DocumentViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        serializer.context['include_history'] = self.request.query_params.get('include_history', False)
        serializer.context['include_linked_items'] = self.request.query_params.get('include_linked_items', 'true').lower() == 'true'
        serializer.context['include_control'] = True
        serializer.context['include_detail'] = self.action == 'retrieve'
        return serializer

    def get_permissions(self):
        if self.action == 'bulk_update':
            return [DocumentPermission('update', 'mixed')]
        if self.action == 'issuer_companies':
            return [DocumentIssuerLookupPermission()]
        if self.action == 'list':
            # Check if it's a company or project document based on query params
            company_profile_id = self.request.query_params.get('folder__company_profile')
            project_id = self.request.query_params.get('folder__project')
            name = self.request.query_params.get('name')
            
            if company_profile_id:
                # Company document - check if company profile exists
                try:
                    from models.models import CompanyProfile
                    company_profile = CompanyProfile.objects.get(id=company_profile_id)
                    return [DocumentPermission('view', 'company')]
                except CompanyProfile.DoesNotExist:
                    pass
            elif project_id:
                # Project document - check if project exists
                try:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
                    return [DocumentPermission('view', 'project')]
                except Project.DoesNotExist:
                    pass
            elif name and '_company_root_folder' in name:
                return [DocumentPermission('view', 'company')]
            elif name and '_root_folder' in name:
                return [DocumentPermission('view', 'project')]
            
            # Mixed document queries like `author=...` can span company and project folders.
            return [DocumentPermission('view', 'mixed')]
        elif self.action == 'retrieve':
            # Get the document and check its folder type
            try:
                document = Document.objects.get(pk=self.kwargs['pk'])
                if document.folder.company_profile:
                    return [DocumentPermission('view', 'company')]
                elif document.folder.project:
                    return [DocumentPermission('view', 'project')]
            except Document.DoesNotExist:
                pass
            # Default to project permissions
            return [DocumentPermission('view', 'project')]
        elif self.action == 'create':
            # Check folder from request data
            folder_id = self.request.data.get('folder')
            if folder_id:
                try:
                    folder = Folder.objects.get(id=folder_id)
                    if folder.company_profile:
                        return [DocumentPermission('create', 'company')]
                    elif folder.project:
                        return [DocumentPermission('create', 'project')]
                except Folder.DoesNotExist:
                    pass
            # Default to company permissions
            return [DocumentPermission('create', 'company')]
        elif self.action in ['update', 'partial_update']:
            # Get the document and check its folder type
            try:
                document = Document.objects.get(pk=self.kwargs['pk'])
                if document.folder.company_profile:
                    return [DocumentPermission('update', 'company')]
                elif document.folder.project:
                    return [DocumentPermission('update', 'project')]
            except Document.DoesNotExist:
                pass
            # Default to company permissions
            return [DocumentPermission('update', 'company')]
        elif self.action == 'destroy':
            # Get the document and check its folder type
            try:
                document = Document.objects.get(pk=self.kwargs['pk'])
                if document.folder.company_profile:
                    return [DocumentPermission('delete', 'company')]
                elif document.folder.project:
                    return [DocumentPermission('delete', 'project')]
            except Document.DoesNotExist:
                pass
            # Default to company permissions
            return [DocumentPermission('delete', 'company')]
        return super().get_permissions()

    @action(detail=False, methods=["get"], url_path="issuer-companies")
    def issuer_companies(self, request):
        """Companies selectable as the external issuer of a Document (Client / Supplier origin).

        Minimal by design: only {id, company_name}, live companies of the requester's own
        tenant (same ownership rule as the Network companies). It is not a Network browser.
        """
        user = request.user
        if user.is_freelancer:
            queryset = ContactCompany.objects.filter(user=user)
        elif user.company_id:
            queryset = ContactCompany.objects.filter(company_profile_id=user.company_id)
        else:
            queryset = ContactCompany.objects.none()
        queryset = queryset.filter(is_deleted=False)
        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(company_name__icontains=search)
        return Response(
            list(queryset.order_by("company_name", "id").values("id", "company_name"))
        )

    @action(detail=False, methods=["patch"], url_path="bulk-update")
    def bulk_update(self, request):
        request_serializer = BulkDocumentUpdateRequestSerializer(
            data=request.data
        )
        request_serializer.is_valid(raise_exception=True)
        result = DocumentBulkUpdateService(
            request=request,
            serializer_context=self.get_serializer_context(),
        ).execute(
            document_ids=request_serializer.validated_data["ids"],
            changes=request_serializer.validated_data["changes"],
        )
        return Response(result, status=status.HTTP_200_OK)

    def get_queryset(self):
        """
        Filter queryset based on user permissions using the new DocumentPermission system.
        """
        queryset = get_accessible_document_queryset(self.request)
        if self.action == "list":
            # Workflow of the CURRENT revision (Register indicator + "my validations").
            queryset = annotate_current_workflow(queryset).annotate(
                # Origin becomes immutable once any internal workflow exists.
                has_workflow_history=Exists(
                    DocumentWorkflow.objects.filter(revision__document=OuterRef("pk"))
                )
            )
        return queryset.select_related(
            "control", "control__approver", "created_by", "folder__project", "external_issuer_company"
        ).annotate(
            author_name=Concat(
                Coalesce(F("author__first_name"), Value("")),
                Value(" "),
                Coalesce(F("author__last_name"), Value("")),
                output_field=CharField(),
            ),
        )

    def list(self, request, *args, **kwargs):
        base_queryset = self.get_queryset()
        print(f'[DEBUG] DocumentViewSet: list: base_queryset count: {base_queryset.count()}')
        print(f'[DEBUG] DocumentViewSet: list: request.query_params: {request.query_params}')
        
        # Fix: Handle folder__company_profile filter properly
        if 'folder__company_profile' in request.query_params:
            print(f'[DEBUG] DocumentViewSet: list: handling folder__company_profile filter')
            # For project documents, we need to filter by the company of the project, not the folder
            company_profile_id = request.query_params.get('folder__company_profile')
            if company_profile_id:
                # Add the company filter to the base queryset
                base_queryset = base_queryset.filter(
                    Q(folder__company_profile_id=company_profile_id) |  # Company folders
                    Q(folder__project__user__company_id=company_profile_id)  # Project folders
                )
                print(f'[DEBUG] DocumentViewSet: list: after company filter: {base_queryset.count()}')
        
        # Debug each filter step
        queryset = base_queryset
        print(f'[DEBUG] DocumentViewSet: list: before DjangoFilterBackend: {queryset.count()}')
        
        # Apply DjangoFilterBackend manually, but exclude folder__company_profile
        from django_filters.rest_framework import DjangoFilterBackend
        filter_backend = DjangoFilterBackend()
        
        
        queryset = filter_backend.filter_queryset(request, queryset, self)


        print(f'[DEBUG] DocumentViewSet: list: after DjangoFilterBackend: {queryset.count()}')
        
        
        # Apply SearchFilter manually
        from rest_framework.filters import SearchFilter
        search_backend = SearchFilter()
        queryset = search_backend.filter_queryset(request, queryset, self)
        print(f'[DEBUG] DocumentViewSet: list: after SearchFilter: {queryset.count()}')

        # Apply OrderingFilter (used by company register BE sort + pagination)
        ordering_backend = CaseInsensitiveDocumentOrderingFilter()
        queryset = ordering_backend.filter_queryset(request, queryset, self)
        
        print(f'[DEBUG] DocumentViewSet: list: final queryset count: {queryset.count()}')
        
        # Check pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            print(f'[DEBUG] DocumentViewSet: list: paginated count: {len(page)}')
            serializer = self.get_serializer(page, many=True)
            print(f'[DEBUG] DocumentViewSet: list: serializer data length: {len(serializer.data)}')
            return self.get_paginated_response(serializer.data)
        
        # No pagination
        serializer = self.get_serializer(queryset, many=True)
        print(f'[DEBUG] DocumentViewSet: list: serializer data length: {len(serializer.data)}')
        return Response(serializer.data)

    def get_object(self):
        obj = get_object_or_404(Document, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        request_user = self.request.user

        if obj.folder.company_profile and request_user.company != obj.folder.company_profile:
            raise PermissionDenied("You do not have permission to perform this action.")
        if obj.folder.freelancer and request_user != obj.folder.freelancer:
            raise PermissionDenied("You do not have permission to perform this action.")
        return obj

    def perform_create(self, serializer):
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        # Documentary evidence (workflow history, approved revision) is never deleted.
        instance = self.get_object()
        if document_has_evidence(instance):
            return Response(
                {"detail": DOCUMENT_EVIDENCE_MESSAGE},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ValidationError as error:
            return Response(
                {"detail": " ".join(error.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    def create(self, request, *args, **kwargs):
        # Custom create method to handle file uploads
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            self.perform_create(serializer)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
