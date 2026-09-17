from django.db.models import Q
from django_filters import rest_framework as filters

from companyprofileapis.models import Opportunity


class OpportunityFilter(filters.FilterSet):
    status = filters.CharFilter(
        method="filter_status",
        label="Status (prefix with - for exclusion)",
    )
    title = filters.CharFilter(field_name="title", lookup_expr="icontains")
    search = filters.CharFilter(method="filter_search")
    competence = filters.CharFilter(method="filter_competence")
    country = filters.CharFilter(field_name="country", lookup_expr="icontains")
    city = filters.CharFilter(method="filter_city")
    responsible_salesperson = filters.NumberFilter(
        field_name="responsible_salesperson_id"
    )

    class Meta:
        model = Opportunity
        fields = [
            "freelancer",
            "company_profile",
            "status",
            "title",
            "search",
            "competence",
            "country",
            "city",
            "responsible_salesperson",
        ]

    def filter_status(self, queryset, name, value):
        if not value:
            return queryset

        is_negative = value.startswith('-')
        raw_value = value[1:] if is_negative else value
        criteria = [c.strip() for c in raw_value.split(',') if c.strip()]

        if not criteria:
            return queryset

        if is_negative:
            # Exclude opportunities whose status is in the provided list
            return queryset.exclude(status__in=criteria)

        # Include opportunities whose status is in the provided list
        return queryset.filter(status__in=criteria)

    def filter_search(self, queryset, name, value):
        if not value:
            return queryset

        return queryset.filter(
            Q(customer_company__company_name__icontains=value)
            | Q(customer_company__contact_name__icontains=value)
            | Q(customer_company__contact_surname__icontains=value)
            | Q(customer_user__name__icontains=value)
            | Q(customer_user__surname__icontains=value)
        ).distinct()

    def filter_competence(self, queryset, name, value):
        if not value:
            return queryset

        return queryset.filter(
            Q(customer_company__area_of_competence__icontains=value)
            | Q(customer_user__competencies__icontains=value)
        ).distinct()

    def filter_city(self, queryset, name, value):
        if not value:
            return queryset

        return queryset.filter(
            Q(customer_company__company_town__icontains=value)
            | Q(customer_user__city__icontains=value)
        ).distinct()

from django_filters import rest_framework as filters

from models.models import CompanyProfile, Role  
from ecpmpapp.filters import ArrayOverlapFilter


class CharInFilter(filters.BaseInFilter, filters.CharFilter):
    pass

class CompanySearchFilter(filters.FilterSet):
    company_country = filters.CharFilter(field_name="company_country", lookup_expr="icontains")
    company_town = filters.CharFilter(field_name="company_town", lookup_expr="icontains")
    expertise = filters.CharFilter(field_name="expertise", lookup_expr="icontains")
    products = filters.CharFilter(field_name="products", lookup_expr="icontains")
    services = filters.CharFilter(field_name="services", lookup_expr="icontains")
    main_area_of_expertise = CharInFilter(
        field_name="main_area_of_expertise",
        lookup_expr="in",
    )
    sub_area_of_expertise = ArrayOverlapFilter(
        field_name="sub_area_of_expertise",
    )

    class Meta:
        model = CompanyProfile
        fields = [
            'company_country', 'company_town', 'expertise', 'products', 'services',
            'main_area_of_expertise', 'sub_area_of_expertise',
        ]

class RoleFilter(filters.FilterSet):
    scope = filters.CharFilter(lookup_expr='iexact', field_name='scope')
    type = filters.CharFilter(lookup_expr='iexact', field_name='type')
    company = filters.CharFilter(lookup_expr='iexact', field_name='company')
    
    class Meta:
        model = Role
        fields = ['scope', 'type', 'company']
