from datetime import date
from types import SimpleNamespace
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError
from project_management.serializers.purchase_order import PurchaseOrderSerializer


class PurchaseOrderEndDateTests(SimpleTestCase):
    def test_create_requires_end_date(self):
        with self.assertRaises(ValidationError) as error:
            PurchaseOrderSerializer().validate({'po_type': 'incoming', 'po_date': date(2026, 11, 24)})
        self.assertIn('end_date', error.exception.detail)

    def test_end_date_cannot_precede_start(self):
        with self.assertRaises(ValidationError) as error:
            PurchaseOrderSerializer().validate({'po_type': 'incoming', 'po_date': date(2026, 11, 24), 'end_date': date(2026, 11, 23)})
        self.assertIn('end_date', error.exception.detail)

    def test_edit_legacy_order_requires_missing_end_date(self):
        instance = SimpleNamespace(po_type='incoming', document_type='purchase_order', end_date=None, po_date=date(2026, 11, 24))
        with self.assertRaises(ValidationError) as error:
            PurchaseOrderSerializer(instance=instance, partial=True).validate({'status': 'Received'})
        self.assertIn('end_date', error.exception.detail)
