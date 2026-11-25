from project_management.test_document_metadata import MetadataBase


class DocumentClientIdentificationTests(MetadataBase):
    def test_creation_persists_customer_reference_and_planned_revision(self):
        document = self.create_fileless(client_reference='CLIENT-123', planned_client_revision='C1')
        self.assertEqual(document.client_reference, 'CLIENT-123')
        self.assertEqual(document.planned_client_revision, 'C1')
        self.assertEqual(document.history.first().planned_client_revision, 'C1')
