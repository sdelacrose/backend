from datetime import date
from django.http import QueryDict
from project_management.serializers.document import DocumentSerializer
from project_management.test_document_metadata import MetadataBase

class DocumentDueDateTests(MetadataBase):
    def test_optional_due_date_can_be_set_and_cleared_without_changing_versions(self):
        document = self.create_fileless()
        before = self.snapshot(document)
        self.assertIsNone(document.due_date)
        updated = self.patch_document(document, due_date='2026-12-01')
        self.assertEqual(updated.due_date, date(2026, 12, 1))
        self.assertEqual(updated.history.first().due_date, date(2026, 12, 1))
        self.assertEqual(self.snapshot(updated), before)
        cleared = self.patch_document(updated, due_date=None)
        self.assertIsNone(cleared.due_date)
        self.assertEqual(self.snapshot(cleared), before)

    def test_blank_multipart_due_date_clears_the_plan(self):
        document = self.create_fileless(due_date='2026-12-01')
        data = QueryDict('', mutable=True)
        data['due_date'] = ''
        serializer = DocumentSerializer(document, data=data, partial=True, context={'user': self.creator})
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertIsNone(serializer.save().due_date)

    def test_invalid_date_is_rejected(self):
        document = self.create_fileless()
        serializer = DocumentSerializer(document, data={'due_date':'2026-02-30'}, partial=True, context={'user':self.creator})
        with self.relaxed():
            self.assertFalse(serializer.is_valid())
        self.assertIn('due_date', serializer.errors)
