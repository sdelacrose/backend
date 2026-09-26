from django.db.models import OuterRef, Exists, CharField
from django.db.models.functions import Cast
from django_filters import rest_framework as filters
from ecpmpapp.filters import ArrayOverlapFilter
import django_filters
from django.db.models import Q
import re
from django.db.models import Prefetch, Q

from project_management.models import (
    ProjectExternalUsers,
    Milestone, ActionItem, ObjectAssessment, Project, ProjectPhase,
    ContactUser, ContactCompany, Invoice, TimeCard, ExpenseCard, Document,
    PurchaseOrder,
)

class CharInFilter(filters.BaseInFilter, filters.CharFilter):
    pass

class NumberInFilter(filters.BaseInFilter, filters.NumberFilter):
    pass


class DocumentFilter(filters.FilterSet):
    document_type = CharInFilter(field_name="document_type", lookup_expr="in")
    category = CharInFilter(field_name="category", lookup_expr="in")
    discipline_group_id = CharInFilter(field_name="discipline_group_id", lookup_expr="in")
    discipline = CharInFilter(field_name="discipline", lookup_expr="in")
    status = CharInFilter(field_name="status", lookup_expr="in")
    internal_review_status = CharInFilter(
        field_name="internal_review_status", lookup_expr="in"
    )
    external_review_status = CharInFilter(
        field_name="external_review_status", lookup_expr="in"
    )
    transmittal_category = CharInFilter(
        field_name="transmittal_category", lookup_expr="in"
    )
    document_origin = CharInFilter(field_name="document_origin", lookup_expr="in")
    author = NumberInFilter(field_name="author", lookup_expr="in")
    folder = NumberInFilter(field_name="folder", lookup_expr="in")
    folder_name = CharInFilter(field_name="folder__name", lookup_expr="in")
    created_by = NumberInFilter(field_name="created_by", lookup_expr="in")
    # Supplementary Document Control data (no approver concept on Document).
    approver = NumberInFilter(field_name="control__approver", lookup_expr="in")
    project = NumberInFilter(field_name="folder__project", lookup_expr="in")
    # Workflow: documents whose CURRENT revision waits for an action from the connected user.
    my_validation = filters.BooleanFilter(method="filter_my_validation")
    date_from = filters.DateFilter(field_name="uploaded_at", lookup_expr="date__gte")
    date_to = filters.DateFilter(field_name="uploaded_at", lookup_expr="date__lte")

    class Meta:
        model = Document
        fields = [
            "folder__project",
            "folder__company_profile",
            "folder__freelancer",
            "author",
            "document_type",
            "document_origin",
            "category",
            "discipline_group_id",
            "discipline",
            "status",
            "internal_review_status",
            "external_review_status",
            "transmittal_category",
            "folder",
            "folder_name",
            "created_by",
            "date_from",
            "date_to",
        ]

    def filter_my_validation(self, queryset, name, value):
        if not value:
            return queryset
        from project_management.services.document_workflow import (
            filter_awaiting_user,
        )

        return filter_awaiting_user(queryset, self.request.user)

class MilestoneFilter(filters.FilterSet):
    due_date = filters.DateFromToRangeFilter(field_name='due_date', label='Due Date Range')
    status = filters.CharFilter(method='filter_status', label='Status (prefix with - for exclusion)')
    search   = filters.CharFilter(method='filter_search', label='Search milestone or action-item name')
    action_item_assignee = filters.NumberFilter(method='filter_action_item_assignee')

    class Meta:
        model = Milestone
        fields = ['project', 'due_date', 'status', 'search', 'action_item_assignee']

    def filter_action_item_assignee(self, queryset, name, value):
        return queryset.filter(
            Q(action_items__assignees=value)
            | Q(action_items__sub_action_items__assignees=value)
        ).distinct()

    def filter_status(self, queryset, name, value):
        if not value:
            return queryset

        is_negative = value.startswith('-')
        raw_value = value[1:] if is_negative else value
        criteria = [c.strip() for c in raw_value.split(',') if c.strip()]

        if not criteria:
            return queryset

        if is_negative:
            # Exclude milestones whose status is in the provided list
            return queryset.exclude(status__in=criteria)

        # Include milestones whose status is in the provided list
        return queryset.filter(status__in=criteria)

    def filter_search(self, queryset, name, value):
        # Step 1: Check for milestone name matches
        matching_milestones = queryset.filter(name__icontains=value)
        matching_milestone_ids = list(matching_milestones.values_list('id', flat=True))

        if matching_milestone_ids:
        # If any milestone name matches, return those milestones
            return queryset.filter(id__in=matching_milestone_ids)

        # Step 2: No milestone name match — search in action items
        matching_action_items = ActionItem.objects.filter(name__icontains=value)
        related_milestone_ids = list(matching_action_items.values_list('milestone_id', flat=True))

        if not related_milestone_ids:
        # No matches found at all
            return queryset.none()

        # Step 3: Return only milestones related to matching action items,
        # and prefetch just those matching items using `to_attr`
        return (
            queryset
            .filter(id__in=related_milestone_ids)
            .prefetch_related(
                Prefetch(
                    'action_items',
                    queryset=matching_action_items,
                    to_attr='filtered_action_items'
                )
            )
        )
    
class ActionItemFilter(filters.FilterSet):
    target_date = filters.DateFromToRangeFilter(field_name='target_date', label='Target date Range')
    forecast_date = filters.DateFromToRangeFilter(field_name='forecast_date', label='Forecast date Range')
    project = filters.NumberFilter(field_name='milestone__project', label='Project ID')
    assignee = filters.NumberFilter(method='filter_assignee')
    status = filters.CharFilter(method='filter_status', label='Status (prefix with - for exclusion)')

    class Meta:
        model = ActionItem
        fields = ['id', 'freelancer', 'company_profile', 'target_date', 'forecast_date', 'milestone', 'project', 'assignee', 'status']

    def filter_assignee(self, queryset, name, value):
        if value in (None, ""):
            return queryset
        return queryset.filter(Q(assignees=value)).distinct()

    def filter_status(self, queryset, name, value):
        if not value:
            return queryset

        is_negative = value.startswith('-')
        raw_value = value[1:] if is_negative else value
        criteria = [c.strip() for c in raw_value.split(',') if c.strip()]

        if not criteria:
            return queryset

        if is_negative:
            # Exclude action items whose status is in the provided list
            return queryset.exclude(status__in=criteria)

        # Include action items whose status is in the provided list
        return queryset.filter(status__in=criteria)

class ObjectAssessmentFilter(filters.FilterSet):
    created_at = filters.DateFromToRangeFilter(field_name='created_at', label='Record Date Range')
    date_from = filters.DateFilter(field_name='created_at', lookup_expr='date__gte')
    date_to = filters.DateFilter(field_name='created_at', lookup_expr='date__lte')
    document_type = CharInFilter(field_name='document_type', lookup_expr='in')
    status = CharInFilter(field_name='status', lookup_expr='in')
    author = NumberInFilter(field_name='author', lookup_expr='in')

    folder_name = filters.CharFilter(method='filter_folder_name')

    class Meta:
        model = ObjectAssessment
        fields = ['project', 'created_at', 'date_from', 'date_to', 'author', 'document_type', 'status', 'folder_name']

    def filter_folder_name(self, queryset, name, value):
        if not value:
            return queryset

        folders = [f.strip() for f in value.split(',') if f.strip()]
        if not folders:
            return queryset

        # If "Object Assessment" (the root) is selected, we don't filter by record_type (show all)
        if any(f.lower() == "object assessment" for f in folders):
            return queryset

        return queryset.filter(record_type__in=folders)


class ProjectFilter(filters.FilterSet):
    # Custom filtering for project_status with a default value of 'all'
    project_status = filters.CharFilter(method='filter_project_status')
    service = filters.CharFilter(field_name="type_of_service", lookup_expr="icontains")
    start_year = filters.NumberFilter(field_name='project_start_date', lookup_expr='year')
    end_year = filters.NumberFilter(field_name='project_end_date', lookup_expr='year')
    
    # New client filters
    client_name = filters.CharFilter(method='filter_client_name')
    client_type = filters.CharFilter(method='filter_client_type')

    class Meta:
        model = Project
        fields = [
            'contract_type', 
            'project_name', 
            'funding_strategy', 
            'project_status', 
            'service', 
            'start_year',
            'end_year',
            'client_name',
            'client_type',
        ]

    def filter_project_status(self, queryset, name, value):
        if not value:
            return queryset

        if value.lower() == 'all':
            return queryset

        is_negative = value.startswith('-')
        raw_value = value[1:] if is_negative else value
        criteria = [c.strip() for c in raw_value.split(",") if c.strip()]

        if not criteria:
            return queryset

        if is_negative:
            return queryset.exclude(project_status__in=criteria)

        return queryset.filter(project_status__in=criteria)


class ProjectPhaseFilter(filters.FilterSet):
    status = filters.CharFilter(method='filter_status', label='Status (prefix with - for exclusion)')
    is_payment = filters.BooleanFilter(method="filter_is_payment")

    class Meta:
        model = ProjectPhase
        fields = ['project', 'status', 'is_payment']

    def filter_status(self, queryset, name, value):
        if not value:
            return queryset
        if value.startswith('-'):
            status_value = value[1:].strip()
            if status_value:
                return queryset.exclude(status__iexact=status_value)
            return queryset
        return queryset.filter(status__iexact=value)

    def filter_is_payment(self, queryset, name, value):
        if value is True:
            return queryset.filter(amount__gt=0)
        if value is False:
            return queryset.filter(Q(amount__isnull=True) | Q(amount__lte=0))
        return queryset


    def filter_client_name(self, queryset, name, value):
        if value:
            return queryset.filter(
                Q(client__contact_user__name__icontains=value) |
                Q(client__contact_company__company_name__icontains=value)
            )
        return queryset

    def filter_client_type(self, queryset, name, value):
        if value:
            return queryset.filter(
                Q(client__contact_user__isnull=False) if value.lower() == 'user' else
                Q(client__contact_company__isnull=False) if value.lower() == 'company' else
                Q()
            )
        return queryset


class ContactUserFilter(filters.FilterSet):
    name = filters.CharFilter(field_name='name', lookup_expr='icontains')
    email = filters.CharFilter(field_name='email', lookup_expr='icontains')
    city = filters.CharFilter(field_name="city", lookup_expr="icontains")
    country = filters.CharFilter(field_name="country", lookup_expr="icontains")
    qualifications = filters.CharFilter(field_name="qualification", lookup_expr="icontains")
    keywords = filters.CharFilter(field_name="keyword", lookup_expr="icontains")
    competencies = filters.CharFilter(field_name="competencies", lookup_expr="icontains")
    main_area_of_expertise = CharInFilter(
        field_name="main_area_of_expertise",
        lookup_expr="in",
    )
    sub_area_of_expertise = ArrayOverlapFilter(
        field_name="sub_area_of_expertise",
    )
    contact_company = filters.NumberFilter(field_name="contact_company")
    company_name = CharInFilter(field_name="company_name", lookup_expr="in")
    spoken_languages = filters.CharFilter(method="filter_by_spoken_languages")
    availability_location = filters.CharFilter(field_name="availability_location", lookup_expr="icontains")
    open_to_new_challenges = filters.BooleanFilter(field_name="open_to_new_challenges")
    in_progress = filters.BooleanFilter(method='filter_in_progress')

    class Meta:
        model = ContactUser
        fields = [
            'is_deleted',
            'user',
            'company_profile',
            'city',
            "country",
            "qualifications",
            "keywords",
            "competencies",
            "main_area_of_expertise",
            "sub_area_of_expertise",
            "contact_company",
            "company_name",
            "spoken_languages",
            "availability_location",
            "open_to_new_challenges",
        ]

    def filter_by_spoken_languages(self, queryset, name, value):
        normalized_values = [
            item.strip()
            for item in str(value).split(",")
            if item.strip()
        ]
        if not normalized_values:
            return queryset

        query = Q()
        for language in normalized_values:
            query |= Q(spoken_languages_text__icontains=language)

        return queryset.annotate(
            spoken_languages_text=Cast("spoken_languages", output_field=CharField())
        ).filter(query)

    def filter_in_progress(self, queryset, name, value):
        external_qs = ProjectExternalUsers.objects.filter(
            contact_user=OuterRef('pk'),
            contact_company__isnull=True
        )
        queryset = queryset.annotate(has_external=Exists(external_qs))

        if value:
            return queryset.filter(
                has_external=True
            ).distinct()
        else:
            return queryset.filter(
                has_external=False
            ).distinct()

class ContactCompanyFilter(filters.FilterSet):
    name = filters.CharFilter(field_name='company_name', lookup_expr='icontains')
    email = filters.CharFilter(field_name='contact_email', lookup_expr='icontains')   
    company_town = filters.CharFilter(field_name="company_town", lookup_expr="icontains")
    company_country = filters.CharFilter(field_name="company_country", lookup_expr="icontains")
    area_of_competence = filters.CharFilter(field_name="area_of_competence", lookup_expr="icontains")
    main_area_of_expertise = CharInFilter(
        field_name="main_area_of_expertise",
        lookup_expr="in",
    )
    sub_area_of_expertise = ArrayOverlapFilter(
        field_name="sub_area_of_expertise",
    )
    company_role = CharInFilter(field_name="company_role", lookup_expr="in")
    in_progress = filters.BooleanFilter(method='filter_in_progress')

    class Meta:
        model = ContactCompany
        fields = [
            'is_deleted', 'user', 'company_profile', 'company_town',
            "company_country", "area_of_competence", "company_role",
            "main_area_of_expertise", "sub_area_of_expertise",
        ]

    def filter_in_progress(self, queryset, name, value):
        external_qs = ProjectExternalUsers.objects.filter(
            contact_user__isnull=True,
            contact_company=OuterRef('pk')
        )
        queryset = queryset.annotate(has_external=Exists(external_qs))

        if value:
            return queryset.filter(
                has_external=True
            ).distinct()
        else:
            return queryset.filter(
                has_external=False
            ).distinct()


class BillingInvoiceFilter(filters.FilterSet):
    status = filters.CharFilter(method='filter_status', label='Status (prefix with - for exclusion)')
    due_date = filters.DateFromToRangeFilter(field_name='due_date', label='Due Date Range')
    invoice_date = filters.DateFromToRangeFilter(field_name='invoice_date', label='Invoice Date Range')
    document_type = filters.CharFilter(method="filter_document_type")

    def filter_document_type(self, queryset, name, value):
        normalized_value = (value or "").lower()
        if normalized_value in {"invoice", "credit_note"}:
            return queryset.filter(document_type=normalized_value)
        return queryset

    class Meta:
        model = Invoice
        fields = ['id', 'freelancer', 'project', 'company_profile', 'grand_total', "status", "due_date", "invoice_date", "document_type"]

    def filter_status(self, queryset, name, value):
        """
        Support:
        - Simple status filter:       status=open
        - Negated single status:      status=-closed
        - Multiple statuses:          status=open,in_progress
        - Negated multiple statuses:  status=-closed,cancelled
        
        Behaviour mirrors ActionItemFilter.filter_status.
        """
        if not value:
            return queryset

        is_negative = value.startswith('-')
        raw_value = value[1:] if is_negative else value
        criteria = [c.strip() for c in raw_value.split(',') if c.strip()]

        if not criteria:
            return queryset

        if is_negative:
            # Exclude invoices whose status is in the provided list
            return queryset.exclude(status__in=criteria)

        # Include invoices whose status is in the provided list
        return queryset.filter(status__in=criteria)


class PurchaseOrderFilter(filters.FilterSet):
    status = filters.CharFilter(method="filter_status", label="Status (prefix with - for exclusion)")
    project = NumberInFilter(field_name="project", lookup_expr="in", label="Projects")
    po_date = filters.DateFilter(field_name="po_date", lookup_expr="exact", label="PO date (exact day)")
    po_date_range = filters.DateFromToRangeFilter(field_name="po_date", label="PO date range")
    document_type = filters.CharFilter(method="filter_document_type")

    def filter_document_type(self, queryset, name, value):
        normalized_value = (value or "").lower()
        if normalized_value in {"purchase_order", "change_notice"}:
            return queryset.filter(document_type=normalized_value)
        return queryset

    class Meta:
        model = PurchaseOrder
        fields = [
            "id",
            "company_profile",
            "freelancer",
            "po_type",
            "status",
            "po_date",
            "document_type",
        ]

    def filter_status(self, queryset, name, value):
        if not value:
            return queryset

        if value.startswith("-"):
            raw_value = value[1:]
            criteria = [c.strip() for c in raw_value.split(",") if c.strip()]
            if not criteria:
                return queryset
            return queryset.exclude(status__in=criteria)

        includes: list[str] = []
        excludes: list[str] = []
        for part in value.split(","):
            token = (part or "").strip()
            if not token:
                continue
            if token.startswith("-") and len(token) > 1:
                excludes.append(token[1:].strip())
            else:
                includes.append(token)

        qs = queryset
        if includes:
            qs = qs.filter(status__in=includes)
        if excludes:
            qs = qs.exclude(status__in=excludes)
        return qs


class TimeCardFilter(filters.FilterSet):
    status = CharInFilter(field_name="status", lookup_expr="in")
    created_at = filters.DateFromToRangeFilter(field_name='created_at', label='Created At Range')
    date = filters.DateFromToRangeFilter(field_name='date', label='Date Range')  
    project_name = filters.CharFilter(field_name='project__project_name', lookup_expr='icontains', label='Project Name contains')
    users = NumberInFilter(field_name='created_by', lookup_expr='in', label='Created By Users')
    projects = NumberInFilter(field_name='project', lookup_expr='in', label='Projects')

    type = filters.CharFilter(method='filter_by_type', label='Filter Type (project/company)')

    class Meta:
        model = TimeCard
        fields = ['freelancer', 'company_profile', 'status', 'date', 'users', 'projects', 'type']


    def filter_by_type(self, queryset, name, value):
        """
        This method handles the type parameter but doesn't modify the queryset directly.
        It's used as a flag in filter_queryset method.
        """
        return queryset
    
    def filter_queryset(self, queryset):
        """
        Apply filters in specific order: projects -> users -> created_at -> status
        Each filter narrows down the results from the previous filter
        """


        # Start with the base queryset
        filtered_queryset = queryset
        filter_type = self.data.get('type', '').lower()
        
        if filter_type:
            types = filter_type.split(',')
            type_q = Q()
            if 'project' in types:
                type_q |= Q(project_id__isnull=False)
            if 'company' in types:
                type_q |= Q(company_profile_id__isnull=False)
            if type_q:
                filtered_queryset = filtered_queryset.filter(type_q)

        else:
        # 1. Filter by projects first (if provided) or company profile
            projects_filter = self.data.get('projects')
            company_profile_filter = self.data.get('company_profile')
            if projects_filter:
                project_ids = projects_filter.split(',') if isinstance(projects_filter, str) else projects_filter

                try:
                    project_ids = [int(pid) for pid in project_ids]
                    filtered_queryset = filtered_queryset.filter(project_id__in=project_ids)
                except (ValueError, TypeError):
                    pass
            elif company_profile_filter:
                filtered_queryset = filtered_queryset.filter(company_profile_id=company_profile_filter)
            
        # 2. Filter by users from the filtered results (if provided)
        users_filter = self.data.get('users')
        if users_filter:
            user_ids = users_filter.split(',') if isinstance(users_filter, str) else users_filter
            try:
                user_ids = [int(uid) for uid in user_ids]
                filtered_queryset = filtered_queryset.filter(created_by_id__in=user_ids)
            except (ValueError, TypeError):
                pass
        
        # In your filter_queryset method:
        date_after = self.data.get('date_after')
        date_before = self.data.get('date_before')

        if date_after:
            try:
                # This will include records with date >= date_after
                filtered_queryset = filtered_queryset.filter(date__gte=date_after)
            except (ValueError, TypeError) as e:
                pass

        if date_before:
            try:
                # This will include records with date <= date_before
                filtered_queryset = filtered_queryset.filter(date__lte=date_before)
            except (ValueError, TypeError) as e:
                pass
        
        # 4. Filter by status from the created_at-filtered results (if provided)
        status_filter = self.data.get('status')
        if status_filter:
            is_negative = status_filter.startswith('-')
            raw_value = status_filter[1:] if is_negative else status_filter
            criteria = [c.strip() for c in raw_value.split(',') if c.strip()]

            if criteria:
                if is_negative:
                    filtered_queryset = filtered_queryset.exclude(status__in=criteria)
                else:
                    filtered_queryset = filtered_queryset.filter(status__in=criteria)
        
        # Apply other standard filters (freelancer)
        # These are applied after the ordered filters
        for name, value in self.data.items():
            if name in ['freelancer'] and value:
                try:
                    if name == 'freelancer':
                        filtered_queryset = filtered_queryset.filter(freelancer_id=int(value))
                except (ValueError, TypeError):
                    pass
        
        return filtered_queryset

class ExpenseCardFilter(filters.FilterSet):
    status = CharInFilter(field_name="status", lookup_expr="in")
    created_at = filters.DateFromToRangeFilter(field_name='created_at', label='Created At Range')
    users = NumberInFilter(field_name='created_by', lookup_expr='in', label='Created By Users')
    projects = NumberInFilter(field_name='project', lookup_expr='in', label='Projects')
    expense_date = filters.DateFromToRangeFilter(field_name='expense_date', label='Date Range')  


    type = filters.CharFilter(method='filter_by_type', label='Filter Type (project/company)')
    expense_date = filters.DateFromToRangeFilter(field_name='expense_date', label='Expense date Range')

    class Meta:
        model = ExpenseCard
        fields = ['freelancer', 'company_profile', 'status', 'expense_date', 'users', 'projects']

    def filter_by_type(self, queryset, name, value):
        """
        This method handles the type parameter but doesn't modify the queryset directly.
        It's used as a flag in filter_queryset method.
        """
        return queryset
    
    def filter_queryset(self, queryset):
        """
        Apply filters in specific order: projects -> users -> created_at -> status
        Each filter narrows down the results from the previous filter
        """
        # Start with the base queryset
        filtered_queryset = queryset
        filter_type = self.data.get('type', '').lower()

        # Start with the base queryset
        filtered_queryset = queryset
        filter_type = self.data.get('type', '').lower()
        
        if filter_type:
            types = filter_type.split(',')
            type_q = Q()
            if 'project' in types:
                type_q |= Q(project_id__isnull=False)
            if 'company' in types:
                type_q |= Q(company_profile_id__isnull=False)
            if type_q:
                filtered_queryset = filtered_queryset.filter(type_q)
        
        else:
            # 1. Filter by projects first (if provided) or company profile
            projects_filter = self.data.get('projects')
            company_profile_filter = self.data.get('company_profile')
            if projects_filter:
                project_ids = projects_filter.split(',') if isinstance(projects_filter, str) else projects_filter

                try:
                    project_ids = [int(pid) for pid in project_ids]
                    filtered_queryset = filtered_queryset.filter(project_id__in=project_ids)
                except (ValueError, TypeError):
                    pass
            elif company_profile_filter:
                filtered_queryset = filtered_queryset.filter(company_profile_id=company_profile_filter)
        
        # 2. Filter by users from the filtered results (if provided)
        users_filter = self.data.get('users')
        if users_filter:
            user_ids = users_filter.split(',') if isinstance(users_filter, str) else users_filter
            try:
                user_ids = [int(uid) for uid in user_ids]
                filtered_queryset = filtered_queryset.filter(created_by_id__in=user_ids)
            except (ValueError, TypeError):
                pass
        
        
        date_after = self.data.get('date_after')
        date_before = self.data.get('date_before')

        if date_after:
            try:
                # This will include records with date >= date_after
                filtered_queryset = filtered_queryset.filter(expense_date__gte=date_after)

            except (ValueError, TypeError) as e:

                pass

        if date_before:
            try:
                # This will include records with date <= date_before
                filtered_queryset = filtered_queryset.filter(expense_date__lte=date_before)
            except (ValueError, TypeError) as e:
                pass
        
        # 4. Filter by status from the created_at-filtered results (if provided)
        status_filter = self.data.get('status')
        if status_filter:
            is_negative = status_filter.startswith('-')
            raw_value = status_filter[1:] if is_negative else status_filter
            criteria = [c.strip() for c in raw_value.split(',') if c.strip()]

            if criteria:
                if is_negative:
                    filtered_queryset = filtered_queryset.exclude(status__in=criteria)
                else:
                    filtered_queryset = filtered_queryset.filter(status__in=criteria)
        
        # Apply other standard filters (freelancer)
        # These are applied after the ordered filters
        for name, value in self.data.items():
            if name in ['freelancer'] and value:
                try:
                    if name == 'freelancer':
                        filtered_queryset = filtered_queryset.filter(freelancer_id=int(value))
                except (ValueError, TypeError):
                    pass
        
        return filtered_queryset
