import io
import csv
import ast
import pandas as pd
from datetime import datetime, timedelta

from rest_framework import viewsets, filters
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from django.db.models import CharField, Count, F, Func, Min, Q, Value
from django.db.models.functions import Lower, Trim

from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser
from rest_framework import status

from project_management.models import Contact, Project, ContactCompany, ContactUser
from models.models import User
from project_management.serializers import ContactSerialier, CSVUploadSerializer, ContactUserSerializer, ContactCompanySerializer
from project_management.serializers.contact import get_allowed_company_roles_for_request
from project_management.filters import ContactUserFilter, ContactCompanyFilter
from project_management.permissions import ContactPermission
from ecpmpapp.ordering import TableOrderingFilter


def parse_birthday(birthday_value):
    """
    Try to parse birthday value using multiple date formats.
    Returns a date object if successful, None if all formats fail.
    """
    if not birthday_value:
        return None
    
    # Handle Excel date numbers (days since 1900-01-01)
    if birthday_value.isdigit():
        try:
            return (datetime(1899, 12, 30) + timedelta(days=int(birthday_value))).date()
        except (ValueError, OverflowError):
            pass
    
    # List of date formats to try
    date_formats = [
        "%Y-%m-%d",           # 2023-12-25
        "%d-%m-%Y",           # 25-12-2023
        "%d/%m/%Y",           # 25/12/2023
        "%d/%m/%y",           # 25/12/23
        "%Y-%m-%d %H:%M:%S",  # 2023-12-25 14:30:00
        "%d-%m-%Y %H:%M:%S",  # 25-12-2023 14:30:00
        "%d/%m/%Y %H:%M:%S",  # 25/12/2023 14:30:00
        "%d/%m/%y %H:%M:%S",  # 25/12/23 14:30:00
        "%Y-%m-%d %H:%M:%S %z",  # 2023-12-25 14:30:00 +0000
        "%d/%m/%y %H:%M:%S %z",  # 25/12/23 14:30:00 +0000
        "%Y-%m-%d %H:%M:%S %Z",  # 2023-12-25 14:30:00 UTC
        "%d/%m/%y %H:%M:%S %Z",  # 25/12/23 14:30:00 UTC
        "%m/%d/%Y",           # 12/25/2023 (US format)
        "%m/%d/%y",           # 12/25/23 (US format)
        "%Y/%m/%d",           # 2023/12/25
        "%d.%m.%Y",           # 25.12.2023 (European format)
        "%d.%m.%y",           # 25.12.23 (European format)
    ]
    
    for date_format in date_formats:
        try:
            return datetime.strptime(birthday_value, date_format).date()
        except ValueError:
            continue
    
    # If all formats fail, return None
    return None


def validate_existing_contact(request_user, existing_contact):
    if request_user.is_freelancer and (existing_contact.user != request_user or existing_contact.company_profile is not None):
        raise ValidationError("You are not allowed to update this contact.")
    elif not request_user.is_freelancer and (existing_contact.user is not None or existing_contact.company_profile != request_user.company):
        raise ValidationError("You are not allowed to update this contact.")


def tenant_contacts(model, user):
    """SEC-02: the contacts of `model` owned by the requester's tenant (company XOR freelancer).

    Independent of the module permissions: it is the base of every by-id lookup, so a record of
    another tenant is simply not found, whatever the requester's contact_management rights.
    """
    if isinstance(user, AnonymousUser):
        return model.objects.none()
    if user.is_freelancer:
        return model.objects.filter(user=user)
    if user.company is None:
        return model.objects.none()
    return model.objects.filter(company_profile=user.company)


class ContactUserViewSet(viewsets.ModelViewSet):
    queryset = ContactUser.objects.all()
    serializer_class = ContactUserSerializer
    filterset_class = ContactUserFilter
    filter_backends = [
        filters.SearchFilter,
        DjangoFilterBackend,
        TableOrderingFilter,
    ]
    search_fields = ['name', 'surname']
    ordering_fields = [
        'id',
        'user',
        'user__email',
        'company_profile',
        'name',
        'surname',
        'telephone_no',
        'email',
        'keyword',
        'qualification',
        'country',
        'city',
        'postcode',
        'competencies',
        'main_area_of_expertise',
        'sub_area_of_expertise',
        'company_name',
        'salutation',
        'open_to_new_challenges',
        'availability_from',
        'availability_until',
        'availability_location',
        'spoken_languages',
        'num_projects',
    ]
    ordering = ['-id']
    ordering_field_aliases = {
        'spoken_languages': 'spoken_languages_sort',
    }
    ordering_natural_fields = {
        'user__email',
        'name',
        'surname',
        'telephone_no',
        'email',
        'keyword',
        'qualification',
        'country',
        'city',
        'postcode',
        'competencies',
        'company_name',
        'salutation',
        'availability_location',
        'spoken_languages',
    }
    ordering_localized_fields = {
        'main_area_of_expertise',
        'sub_area_of_expertise',
    }
    ordering_localized_array_fields = {'sub_area_of_expertise'}

    @staticmethod
    def _with_ordering_aliases(queryset):
        return queryset.alias(
            spoken_languages_sort=Func(
                F('spoken_languages'),
                Value(', '),
                function='ARRAY_TO_STRING',
                output_field=CharField(),
            )
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'companies'):
            return [ContactPermission('view', 'company')]
        elif self.action == 'create':
            return [ContactPermission('create', 'company')]
        elif self.action == 'update':
            return [ContactPermission('update', 'company')]
        elif self.action == 'destroy':
            print(f'[DEBUG] ContactUserViewSet: get_permissions: destroy | request.user: {self.request.user} | action: {self.action}')
            return [ContactPermission('delete', 'company')]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return self._with_ordering_aliases(ContactUser.objects.none())

        if user.is_freelancer:
            queryset = ContactUser.objects.filter(user=user)
        else:
            permission = ContactPermission('view', 'company')
            if permission.has_permission(self.request, self):
                queryset = ContactUser.objects.filter(company_profile=user.company)
            else:
                queryset = ContactUser.objects.none()

        # Annotate with number of projects if you want to sort by it
        queryset = self._with_ordering_aliases(queryset).annotate(
            num_projects=Count('external_project_users', distinct=True)
        )
        return queryset

    def get_object(self):
        obj = get_object_or_404(tenant_contacts(ContactUser, self.request.user), pk=self.kwargs['pk'])
        user = self.request.user
        permission = ContactPermission('view', 'company')
        if not permission.has_object_permission(self.request, self, obj):
            raise PermissionDenied("You do not have permission to access this contact.")
        
        return obj
    
    @transaction.atomic
    @action(detail=False, methods=['post'], serializer_class=CSVUploadSerializer, parser_classes=[MultiPartParser])
    def import_excel_file(self, request):
        """Import contacts from CSV using bulk creation."""
        expected_headers = [
            'Id', 'Name', 'Surname', 'Telephone No', 'Mobile Number',
            'Linkedin Url', 'Email', 'Birthday', 'Headline',
            'Industry', 'Education', 'Position', 'Keyword',
            'Qualification', 'Country', 'City', 'Competencies',
            'Company Name', 'Salutation'
        ]

        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            file = serializer.validated_data['file']
            df = pd.read_excel(file, dtype=str)     # Read as strings to avoid type errors
            df = df.where(pd.notna(df), None)

            if set(df.columns) != set(expected_headers):
                return Response(
                    {"error": f"File headers do not match the expected format. Headers must be: {', '.join(expected_headers)}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            contacts_to_create = []
            contacts_to_update = []
            for _, row in df.iterrows():
                contact_id = row.get('Id')
                birthday_value = row.get('Birthday')
                birthday = parse_birthday(birthday_value)

                contact_data = {
                    'name': row.get('Name'),
                    'surname': row.get('Surname'),
                    'telephone_no': row.get('Telephone No'),
                    'mobile_number': row.get('Mobile Number'),
                    'linkedin_url': row.get('Linkedin Url'),
                    'email': row.get('Email'),
                    'birthday': birthday,
                    'headline': row.get('Headline'),
                    'industry': row.get('Industry'),
                    'education': row.get('Education'),
                    'position': row.get('Position'),
                    'keyword': row.get('Keyword'),
                    'qualification': row.get('Qualification'),
                    'country': row.get('Country'),
                    'city': row.get('City'),
                    'competencies': row.get('Competencies'),
                    'company_name': row.get('Company Name'),
                    'salutation': row.get('Salutation'),
                }

                serializer = ContactUserSerializer(data=contact_data, partial=True, context={'request': request})
                if serializer.is_valid(raise_exception=True):
                    existing_contact = ContactUser.objects.filter(id=contact_id).first() if contact_id else None
                    # Ensure the user has the right to modify the contact
                    if existing_contact:
                        validate_existing_contact(request.user, existing_contact)
                        # Update existing contact
                        for key, value in contact_data.items():
                            setattr(existing_contact, key, value)
                        contacts_to_update.append(existing_contact)
                    else:
                        # Assign user or company_profile based on user type
                        serializer.validated_data["user"] = request.user if request.user.is_freelancer else None
                        serializer.validated_data["company_profile"] = request.user.company if not request.user.is_freelancer else None
                        
                        contacts_to_create.append(ContactUser(**serializer.validated_data))

            if contacts_to_create:
                ContactUser.objects.bulk_create(contacts_to_create, ignore_conflicts=True)
            if contacts_to_update:
                ContactUser.objects.bulk_update(contacts_to_update, fields=contact_data.keys())

            return Response({"message": "Contacts imported successfully"}, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        data = serializer.data

        ordering = request.query_params.get("ordering")
        if ordering:
            reverse = ordering.startswith("-")
            sort_by = ordering.lstrip("-")

            # Computed fields that need Python-side sorting
            computed_fields = {
                "is_sharemundo_user"
            }

            if sort_by in computed_fields:
                data = sorted(data, key=lambda x: x.get(sort_by, False), reverse=reverse)

        page = self.paginate_queryset(data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(data)

    @action(detail=False, methods=['get'])
    def export_csv(self, request):
        """Export contacts as a CSV file."""
        contacts = self.filter_queryset(self.get_queryset())
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="contacts_user_export.csv"'

        writer = csv.writer(response)
        headers = [
            'id', 'name', 'surname', 'telephone_no', 'mobile_number',
            'linkedin_url', 'email', 'birthday', 'headline',
            'industry', 'education', 'position', 'keyword',
            'qualification', 'country', 'city', 'competencies',
            'company_name', 'salutation'
        ]
        writer.writerow(headers)

        for contact in contacts:
            writer.writerow([
                contact.id, contact.name, contact.surname, contact.telephone_no, contact.mobile_number,
                contact.linkedin_url, contact.email, contact.birthday.strftime("%Y-%m-%d") if contact.birthday else None, contact.headline,
                contact.industry, contact.education, contact.position, contact.keyword,
                contact.qualification, contact.country, contact.city, contact.competencies,
                contact.company_name, contact.salutation,
            ])

        return response

    @action(detail=False, methods=['get'], url_path='companies')
    def companies(self, request):
        """Return distinct companies from non-deleted contacts."""
        contacts = self.get_queryset().filter(is_deleted=False)

        linked = [
            {
                'id': row['company_id'],
                'company_name': row['linked_company_name'],
            }
            for row in contacts.filter(
                contact_company_id__isnull=False,
                contact_company__is_deleted=False,
            )
            .values(
                company_id=F('contact_company_id'),
                linked_company_name=F('contact_company__company_name'),
            )
            .distinct()
        ]

        registered_names = {
            (row['company_name'] or '').strip().lower()
            for row in linked
            if row.get('company_name')
        }

        text_only_qs = (
            contacts.filter(contact_company_id__isnull=True)
            .exclude(company_name__isnull=True)
            .exclude(company_name='')
            .annotate(normalized_name=Lower(Trim('company_name')))
            .exclude(normalized_name='')
        )
        if registered_names:
            text_only_qs = text_only_qs.exclude(normalized_name__in=registered_names)

        text_only = [
            {'id': None, 'company_name': row['display_name']}
            for row in text_only_qs.values('normalized_name')
            .annotate(display_name=Min('company_name'))
            .order_by('normalized_name')
        ]

        data = sorted(
            linked + text_only,
            key=lambda item: item['company_name'].lower(),
        )
        return Response(data)


class ContactCompanyViewSet(viewsets.ModelViewSet):
    queryset = ContactCompany.objects.all()
    serializer_class = ContactCompanySerializer
    filterset_class = ContactCompanyFilter
    filter_backends = [
        filters.SearchFilter,
        DjangoFilterBackend,
        TableOrderingFilter,
    ]
    search_fields = ["company_name", "contact_name"]
    ordering_fields = [
        "id",
        "user",
        "user__email",
        "company_profile",
        "company_name",
        "company_role",
        "contact_name",
        "contact_surname",
        "company_tel_no",
        "company_address",
        "company_postcode",
        "company_town",
        "company_country",
        "area_of_competence",
        "main_area_of_expertise",
        "sub_area_of_expertise",
        "contact_email",
        "num_projects",
    ]
    ordering = ["-id"]
    ordering_natural_fields = {
        "user__email",
        "company_name",
        "company_role",
        "contact_name",
        "contact_surname",
        "company_tel_no",
        "company_address",
        "company_postcode",
        "company_town",
        "company_country",
        "area_of_competence",
        "contact_email",
    }
    ordering_localized_fields = {
        "main_area_of_expertise",
        "sub_area_of_expertise",
    }
    ordering_localized_array_fields = {"sub_area_of_expertise"}

    def get_permissions(self):
        if self.action == 'list':
            return [ContactPermission('view', 'company')]
        elif self.action == 'retrieve':
            return [ContactPermission('view', 'company')]
        elif self.action == 'create':
            return [ContactPermission('create', 'company')]
        elif self.action == 'update':
            return [ContactPermission('update', 'company')]
        elif self.action == 'destroy':
            return [ContactPermission('delete', 'company')]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ContactCompany.objects.none()

        if user.is_freelancer:
            queryset = ContactCompany.objects.filter(user=user)
        else:
            permission = ContactPermission('view', 'company')
            if permission.has_permission(self.request, self):
                queryset = ContactCompany.objects.filter(company_profile=user.company)
            else:
                queryset = ContactCompany.objects.none()

        # Annotate with number of projects if you want to sort by it
        queryset = queryset.annotate(num_projects=Count('external_project_users', distinct=True))
        queryset = queryset.annotate(
            linked_contact_users_count=Count(
                "linked_contact_users",
                filter=Q(linked_contact_users__is_deleted=False),
                distinct=True,
            )
        )

        return queryset
    
    def get_object(self):
        obj = get_object_or_404(tenant_contacts(ContactCompany, self.request.user), pk=self.kwargs['pk'])
        user = self.request.user
        permission = ContactPermission('view', 'company')
        if not permission.has_object_permission(self.request, self, obj):
            raise PermissionDenied("You do not have permission to access this contact.")
        
        return obj
    
    @transaction.atomic
    @action(detail=False, methods=['post'], serializer_class=CSVUploadSerializer, parser_classes=[MultiPartParser])
    def import_excel_file(self, request):
        """Import company contacts from Excel using bulk creation."""
        expected_headers = [
            'Id', 'Company Name', 'Company Role', 'Company Logo',
            'Contact Name', 'Contact Surname', 'Company Address', 'Company Postcode',
            'Company Town', 'Company Tel No', 'Company Website', 'Company Country',
            'Area Of Competence', 'Contact Email', 'Products', 'Services'
        ]

        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            file = serializer.validated_data['file']
            df = pd.read_excel(file, dtype=str)  # Read as strings to avoid type errors
            df = df.where(pd.notna(df), None)

            actual_headers = set(df.columns)
            missing_required_headers = [
                header for header in expected_headers
                if header not in actual_headers
            ]
            unexpected_headers = [
                header for header in actual_headers
                if header not in set(expected_headers)
            ]

            if missing_required_headers or unexpected_headers:
                return Response(
                    {"error": f"File headers do not match the expected format. Headers must be: {', '.join(expected_headers)}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            contacts_to_create = []
            contacts_to_update = []
            allowed_roles = get_allowed_company_roles_for_request(request)
            if not allowed_roles:
                return Response(
                    {"error": "Company role configuration is not available."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            for _, row in df.iterrows():
                contact_id = row.get('Id')
                company_role = row.get('Company Role')
                normalized_company_role = (
                    company_role.strip() if isinstance(company_role, str) else company_role
                )
                if normalized_company_role and normalized_company_role not in allowed_roles:
                    return Response(
                        {
                            "error": (
                                "Invalid Company Role. Allowed choices are: "
                                + ", ".join(allowed_roles)
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                
                contact_data = {
                    'company_name': row.get('Company Name'),
                    'company_role': normalized_company_role,
                    'company_logo': row.get('Company Logo'),
                    'contact_name': row.get('Contact Name'),
                    'contact_surname': row.get('Contact Surname'),
                    'company_address': row.get('Company Address'),
                    'company_postcode': row.get('Company Postcode'),
                    'company_town': row.get('Company Town'),
                    'company_tel_no': row.get('Company Tel No'),
                    'company_website': row.get('Company Website'),
                    'company_country': row.get('Company Country'),
                    'area_of_competence': row.get('Area Of Competence'),
                    'contact_email': row.get('Contact Email'),
                    'products': row.get('Products'),
                    'services': row.get('Services'),
                }
                
                serializer = ContactCompanySerializer(data=contact_data, partial=True, context={'request': request})
                if serializer.is_valid(raise_exception=True):
                    existing_contact = ContactCompany.objects.filter(id=contact_id).first() if contact_id else None

                    # Ensure the user has the right to modify the contact
                    if existing_contact:
                        validate_existing_contact(request.user, existing_contact)

                        # Update existing contact
                        for key, value in contact_data.items():
                            setattr(existing_contact, key, value)
                        contacts_to_update.append(existing_contact)

                    else:
                        # Assign user or company_profile based on user type
                        serializer.validated_data["user"] = request.user if request.user.is_freelancer else None
                        serializer.validated_data["company_profile"] = request.user.company if not request.user.is_freelancer else None
                        
                        contacts_to_create.append(ContactCompany(**serializer.validated_data))

            if contacts_to_create:
                ContactCompany.objects.bulk_create(contacts_to_create, ignore_conflicts=True)
            if contacts_to_update:
                ContactCompany.objects.bulk_update(contacts_to_update, fields=contact_data.keys())

            return Response({"message": "Company contacts imported successfully"}, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        data = serializer.data

        ordering = request.query_params.get("ordering")
        if ordering:
            reverse = ordering.startswith("-")
            sort_by = ordering.lstrip("-")

            # Computed fields that need Python-side sorting
            computed_fields = {
                "is_sharemundo_company"
            }

            if sort_by in computed_fields:
                data = sorted(data, key=lambda x: x.get(sort_by, False), reverse=reverse)

        page = self.paginate_queryset(data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(data)

    @action(detail=False, methods=['get'])
    def export_csv(self, request):
        """Export company contacts as a CSV file."""
        contacts = self.filter_queryset(self.get_queryset())
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="contact_companies_export.csv"'

        writer = csv.writer(response)
        headers = [
            'id', 'company_name', 'company_role', 'company_logo',
            'contact_name', 'contact_surname', 'company_address', 'company_postcode',
            'company_town', 'company_tel_no', 'company_website', 'company_country',
            'area_of_competence', 'contact_email', 'products', 'services'
        ]
        writer.writerow(headers)

        for contact in contacts:
            row = [
                contact.id, contact.company_name, contact.company_role, contact.company_logo,
                contact.contact_name, contact.contact_surname, contact.company_address, contact.company_postcode,
                contact.company_town, contact.company_tel_no, contact.company_website, contact.company_country,
                contact.area_of_competence, contact.contact_email, contact.products, contact.services,
            ]

            writer.writerow(row)
        return response

class ContactViewset(viewsets.ModelViewSet):
    queryset = Contact.objects.all()
    serializer_class = ContactSerialier
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    filterset_fields = ['contact_type']
    search_fields = ['contact_type', 'area_of_competence', 'surname', 'town', 'name']

    def get_serializer(self, *args, **kwargs):
        serializer = super(ContactViewset, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        return serializer

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return Contact.objects.none()
        return Contact.objects.filter(user=self.request.user).order_by('-id')

    def get_serializer_class(self):
        if self.action == 'import_csv':
            return CSVUploadSerializer
        return super().get_serializer_class()

    def get_object(self):
        obj = get_object_or_404(Contact, pk=self.kwargs['pk'])
        if obj.user != self.request.user:
            raise PermissionDenied("You do not have permission to access this contact.")
        return obj

    def get_parsers(self):
        if hasattr(self, 'action') and self.action == 'import_csv':
            return [MultiPartParser()]
        return super().get_parsers()
    
    def perform_destroy(self, instance):
        # Check if the contact is a 'client' and is linked to any project
        if instance.contact_type == 'client':
            linked_projects = Project.objects.filter(client=instance)
            if linked_projects.exists():
                raise ValidationError("Cannot delete this contact as it is assigned as a client in one or more projects.")
        
        # Proceed with deletion if no linked projects are found
        super().perform_destroy(instance)

    @action(detail=False, methods=['get'], url_path='export-csv')
    def export_csv(self, request):
        # Define the HTTP response content
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="contacts.csv"'

        # Create CSV writer
        writer = csv.writer(response)
        headers = [
            'id', 'title', 'name', 'surname',
            'address_line_1', 'town', 'country', 'postcode',
            'contact_type', 'email_address', 'user', 'telephone_number',
            'mobile_number', 'area_of_competence', 'web_address']
        if not request.user.is_freelancer:
            headers.append('company_name')
        # Write the header row
        writer.writerow(headers)

        filtered_queryset = self.filter_queryset(self.get_queryset())

        # Write the data rows
        for contact in filtered_queryset:
            attributes = [
                contact.id, contact.title, contact.name, contact.surname,
                contact.address_line_1, contact.town, 
                contact.country, contact.postcode, contact.contact_type,
                contact.email_address, contact.user.id, contact.telephone_number,
                contact.mobile_number, contact.area_of_competence, contact.web_address
            ]
            if not request.user.is_freelancer:
                attributes.append(contact.company_name),
            writer.writerow(attributes)

        return response

    @transaction.atomic
    @action(detail=False, methods=['post'], serializer_class=CSVUploadSerializer,  parser_classes=[MultiPartParser])
    def import_csv(self, request):
        # Expected headers that match the fields in the Contact model
        expected_headers = [
            'id', 'title', 'name', 'surname',
            'address_line_1', 'town', 'country', 'postcode',
            'contact_type', 'email_address', 'user', 'telephone_number',
            'mobile_number', 'area_of_competence', 'web_address'
        ]
        if not request.user.is_freelancer:
            expected_headers.append('company_name')

        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            file = serializer.validated_data['file']
            csv_file = io.TextIOWrapper(file, encoding='utf-8')
            reader = csv.DictReader(csv_file)

            if set(reader.fieldnames) != set(expected_headers):
                return Response(
                    {"error": "CSV headers do not match the expected format. Headers must be: " + ", ".join(expected_headers)},
                    status=status.HTTP_400_BAD_REQUEST
                )

            for row in reader:
                contact_id = row.get('id')
                contact_data = {
                    'title': row.get('title'),
                    'name': row.get('name'),
                    'surname': row.get('surname'),
                    'address_line_1': row.get('address_line_1'),
                    'town': row.get('town'),
                    'country': row.get('country'),
                    'postcode': row.get('postcode'),
                    'contact_type': row.get('contact_type'),
                    'email_address': row.get('email_address'),
                    'user_id': int(row.get('user')),
                    'telephone_number': row.get('telephone_number'),
                    'mobile_number': row.get('mobile_number'),
                    'area_of_competence': row.get('area_of_competence'),
                    'web_address': row.get('web_address')
                }
                if not request.user.is_freelancer:
                    contact_data['company_name'] = row.get('company_name')

                serializer = ContactSerialier(data=contact_data, partial=True, context={'user': request.user})
                if serializer.is_valid() and self.validate_user(int(row.get('user')), self.request.user):
                    if contact_id:
                        Contact.objects.update_or_create(id=contact_id, defaults=contact_data)
                    else:
                        Contact.objects.create(**contact_data)
                else:
                    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

            return Response({"message": "CSV file imported successfully"}, status=status.HTTP_200_OK)

    def validate_user(self, user_id, signed_user):
        if user_id != signed_user.id:
            raise ValidationError(f"Cannot add contacts for any other users.")
        if not User.objects.filter(pk=user_id).exists():
            raise ValidationError(f"User having {user_id} id does not exist.")
        return True
