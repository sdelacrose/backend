from django.test import SimpleTestCase

from project_management.models.constants import (
    CUSTOMER_DEMAND_DOCUMENT_TYPE,
    GC_COMPANY_DOCUMENT_TYPES,
)


class CompanyCustomerDemandDocumentTypeTests(SimpleTestCase):
    def test_company_document_defaults_include_customer_demand_before_other(self):
        self.assertIn(CUSTOMER_DEMAND_DOCUMENT_TYPE, GC_COMPANY_DOCUMENT_TYPES)
        self.assertLess(
            GC_COMPANY_DOCUMENT_TYPES.index(CUSTOMER_DEMAND_DOCUMENT_TYPE),
            GC_COMPANY_DOCUMENT_TYPES.index("Other:OTH"),
        )
