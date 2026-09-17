from django_filters import rest_framework as filters
from django.db.models import Q, CharField, Value
from django.db.models.functions import Cast, Concat
from models.models import User, Feedback, UserRole
from ecpmpapp.filters import ArrayOverlapFilter


class CharInFilter(filters.BaseInFilter, filters.CharFilter):
    pass

class UserSearchFilter(filters.FilterSet):
    name = filters.CharFilter(method="filter_by_name")
    last_name = filters.CharFilter(field_name="last_name", lookup_expr="icontains")
    search = filters.CharFilter(method="filter_by_global_search")
    global_search = filters.CharFilter(method="filter_by_global_search")
    city = filters.CharFilter(field_name="city", lookup_expr="icontains")
    country = filters.CharFilter(field_name="country", lookup_expr="icontains")
    company_name = filters.CharFilter(field_name="company__company_name", lookup_expr="icontains")
    sharemundo_links_id = filters.CharFilter(field_name="sharemundo_links_id", lookup_expr="icontains")
    company_sharemundo_links_id = filters.CharFilter(field_name="company__sharemundo_links_id", lookup_expr="icontains")
    qualifications = filters.CharFilter(field_name="qualifications", lookup_expr="icontains")
    keywords = filters.CharFilter(field_name="keywords", lookup_expr="icontains")
    professional_headline = filters.CharFilter(field_name="professional_headline", lookup_expr="icontains")
    current_position = filters.CharFilter(field_name="current_position", lookup_expr="icontains")
    availability_location = filters.CharFilter(field_name="availability_location", lookup_expr="icontains")
    spoken_languages = filters.CharFilter(method="filter_by_spoken_languages")
    availability_from = filters.DateFilter(method="filter_by_availability_from")
    availability_until = filters.DateFilter(method="filter_by_availability_until")
    open_to_new_challenges = filters.BooleanFilter(field_name="open_to_new_challenges")
    main_area_of_expertise = CharInFilter(
        field_name="main_area_of_expertise",
        lookup_expr="in",
    )
    sub_area_of_expertise = ArrayOverlapFilter(
        field_name="sub_area_of_expertise",
    )

    def filter_by_name(self, queryset, name, value):
        normalized_value = str(value or "").strip()
        if not normalized_value:
            return queryset

        return queryset.annotate(
            search_full_name=Concat("first_name", Value(" "), "last_name", output_field=CharField())
        ).filter(
            Q(first_name__icontains=normalized_value) |
            Q(last_name__icontains=normalized_value) |
            Q(search_full_name__icontains=normalized_value)
        )

    def filter_by_global_search(self, queryset, name, value):
        normalized_value = str(value or "").strip()
        if not normalized_value:
            return queryset

        title_cased_value = normalized_value.title()

        return queryset.annotate(
            search_full_name=Concat("first_name", Value(" "), "last_name", output_field=CharField()),
            spoken_languages_text=Cast("spoken_languages", output_field=CharField()),
        ).filter(
            Q(first_name__icontains=normalized_value) |
            Q(last_name__icontains=normalized_value) |
            Q(search_full_name__icontains=normalized_value) |
            Q(email__icontains=normalized_value) |
            Q(username__icontains=normalized_value) |
            Q(sharemundo_links_id__icontains=normalized_value) |
            Q(company__sharemundo_links_id__icontains=normalized_value) |
            Q(company__company_name__icontains=normalized_value) |
            Q(title__icontains=normalized_value) |
            Q(job_title__icontains=normalized_value) |
            Q(professional_headline__icontains=normalized_value) |
            Q(industry__icontains=normalized_value) |
            Q(current_position__icontains=normalized_value) |
            Q(city__icontains=normalized_value) |
            Q(state__icontains=normalized_value) |
            Q(country__icontains=normalized_value) |
            Q(qualifications__icontains=normalized_value) |
            Q(keywords__icontains=normalized_value) |
            Q(availability_location__icontains=normalized_value) |
            Q(spoken_languages_text__icontains=normalized_value) |
            Q(spoken_languages__contains=[normalized_value]) |
            Q(spoken_languages__contains=[title_cased_value])
        )

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

    def filter_by_availability_from(self, queryset, name, value):
        if self.data.get("availability_until"):
            return queryset.filter(availability_from__lte=value)
        return queryset.filter(
            availability_from__lte=value,
            availability_until__gte=value,
        )

    def filter_by_availability_until(self, queryset, name, value):
        if self.data.get("availability_from"):
            return queryset.filter(availability_until__gte=value)
        return queryset.filter(
            availability_from__lte=value,
            availability_until__gte=value,
        )

    class Meta:
        model = User
        fields = [
            'name',
            'last_name',
            'search',
            'global_search',
            'city',
            'country',
            'company_name',
            'sharemundo_links_id',
            'company_sharemundo_links_id',
            'qualifications',
            'keywords',
            'professional_headline',
            'current_position',
            'availability_location',
            'spoken_languages',
            'availability_from',
            'availability_until',
            'open_to_new_challenges',
            'main_area_of_expertise',
            'sub_area_of_expertise',
        ]


class FeedbackFilter(filters.FilterSet):
    status = filters.CharFilter(field_name="status", lookup_expr="icontains")
    feedback_type = filters.CharFilter(field_name="feedback_type", lookup_expr="icontains")
    date_range = filters.DateFromToRangeFilter(field_name="created_at")
    class Meta:
        model = Feedback
        fields = ['status', 'feedback_type', 'date_range']

def has_no_active_projects(queryset):
    """Helper: IDs of users with zero active projects."""
    return queryset.exclude(project_teams__project__project_status='In progress')

def has_active_projects(queryset):
    """Helper: IDs of users with >=1 active projects."""
    return queryset.filter(project_teams__project__project_status='In progress')

class AssociatedUsersFilter(filters.FilterSet):
    email = filters.CharFilter(field_name='email', lookup_expr='icontains')
    name = filters.CharFilter(method='filter_by_name')
    usertype = filters.ChoiceFilter(method='filter_by_usertype', choices=[
        ('freelancer', 'Freelancer'),
        ('company', 'Company'),
        ('internal_company_users', 'Internal Company Users'),
    ])
    availability = filters.BooleanFilter(method='filter_by_availability')

    class Meta:
        model = User
        fields = ['email', 'name', 'usertype', 'availability']

    def filter_by_name(self, queryset, name, value):
        return queryset.filter(
            Q(first_name__icontains=value) |
            Q(last_name__icontains=value) |
            Q(username__icontains=value)
        )

    def filter_by_usertype(self, queryset, name, value):
        if value == 'freelancer':
            return queryset.filter(company__isnull=True)
        if value == 'company':
            return queryset.filter(company__isnull=False)
        if value == 'internal_company_users':
            user = self.request.user
            if user.is_freelancer:
                return queryset.none()
            elif user.company:
                return queryset.filter(company=user.company)
        return queryset

    def filter_by_availability(self, queryset, name, value):
        if value:
            return has_no_active_projects(queryset)
        else:
            return has_active_projects(queryset)
        
class UserRoleFilter(filters.FilterSet):
    role = filters.CharFilter(method='filter_role')
    scope = filters.CharFilter(method='filter_scope')
    type = filters.CharFilter(method='filter_type')

    def filter_role(self, queryset, name, value):
        return queryset.filter(role__id=value)

    def filter_scope(self, queryset, name, value):
        if value == 'company':
            return queryset.filter(role__scope='company')
        elif value == 'project':
            return queryset.filter(role__scope='project')
        return queryset

    def filter_type(self, queryset, name, value):
        if value == 'admin_server':
            return queryset.filter(role__type='admin_server')
        elif value == 'solution':
            return queryset.filter(role__type='solution')
        return queryset
    
    class Meta:
        model = UserRole
        fields = ['role', 'type', 'scope']
