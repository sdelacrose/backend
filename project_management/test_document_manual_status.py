from unittest.mock import patch
from django.test import SimpleTestCase
from project_management.models import Document
from project_management.serializers.document import DocumentSerializer
from rest_framework.exceptions import ValidationError

class ManualDocumentStatusTests(SimpleTestCase):
    def validate(self,value,current=None):
        serializer=DocumentSerializer(Document(status=current) if current is not None else None)
        with patch.object(serializer,'_get_valid_choices_for_config_key',return_value=['Draft','On Hold','Approved','Issued','Sent']):
            return serializer.validate_status(value)

    def test_reserved_labels_rejected_at_creation_and_on_change(self):
        for value in ['Under review','Submitted','Approved','Sent','Issued','Revised',' approved ','UNDER_REVIEW']:
            for current in [None,'Draft']:
                with self.subTest(value=value,current=current),self.assertRaises(ValidationError): self.validate(value,current)

    def test_exact_existing_reserved_value_is_preserved(self):
        for value in ['Under review','Submitted','Approved','Sent','Issued','Revised']:
            self.assertEqual(self.validate(value,value),value)

    def test_normal_manual_values_remain_available(self):
        for value in ['Draft','On Hold','',None]: self.assertEqual(self.validate(value),value)

    def test_a_legacy_status_can_be_changed_to_normal_choice(self):
        self.assertEqual(self.validate('On Hold','Approved'),'On Hold')

    def test_unconfigured_new_value_is_still_refused(self):
        with self.assertRaises(ValidationError): self.validate('Unexpected new status','Draft')
