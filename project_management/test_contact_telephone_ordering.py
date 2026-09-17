from django.test import TestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from ecpmpapp.ordering import TableOrderingFilter
from project_management.models import ContactCompany, ContactUser
from project_management.views.contact import ContactCompanyViewSet, ContactUserViewSet


class ContactTelephoneOrderingTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def order_queryset(self, queryset, view_class, ordering):
        request = Request(self.factory.get("/", {"ordering": ordering}))
        return TableOrderingFilter().filter_queryset(
            request,
            queryset,
            view_class(),
        )

    def test_contact_users_can_be_ordered_by_telephone_number(self):
        contacts = ContactUser.objects.bulk_create(
            [
                ContactUser(
                    name="Twenty",
                    surname="Contact",
                    email="twenty@example.com",
                    telephone_no="20",
                ),
                ContactUser(
                    name="Three",
                    surname="Contact",
                    email="three@example.com",
                    telephone_no="3",
                ),
            ]
        )

        ordered_ids = list(
            self.order_queryset(
                ContactUser.objects.filter(id__in=[contact.id for contact in contacts]),
                ContactUserViewSet,
                "telephone_no",
            ).values_list("id", flat=True)
        )

        self.assertEqual(ordered_ids, [contacts[1].id, contacts[0].id])

    def test_contact_companies_can_be_ordered_by_telephone_number(self):
        companies = ContactCompany.objects.bulk_create(
            [
                ContactCompany(
                    company_name="Twenty Company",
                    contact_email="twenty-company@example.com",
                    company_tel_no="20",
                ),
                ContactCompany(
                    company_name="Three Company",
                    contact_email="three-company@example.com",
                    company_tel_no="3",
                ),
            ]
        )

        ordered_ids = list(
            self.order_queryset(
                ContactCompany.objects.filter(
                    id__in=[company.id for company in companies]
                ),
                ContactCompanyViewSet,
                "company_tel_no",
            ).values_list("id", flat=True)
        )

        self.assertEqual(ordered_ids, [companies[1].id, companies[0].id])
