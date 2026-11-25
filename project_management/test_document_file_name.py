from types import SimpleNamespace
from django.test import SimpleTestCase
from project_management.services.document_file_name import controlled_file_name, revision_file_name
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_transmittal import TransmittalTestBase
from project_management.test_document_metadata import MetadataBase


class ControlledFileNameTests(SimpleTestCase):
    def test_reference_revision_state_without_separator_keeps_extension(self):
        self.assertEqual(controlled_file_name('PRJ-001-DR-02', 'A', 'PREL', 'uploads/title.pdf'), 'PRJ-001-DR-02 A PREL.pdf')
        self.assertEqual(controlled_file_name('PRJ-001-DR-02', 'B', 'BPE', 'title.docx'), 'PRJ-001-DR-02 B BPE.docx')

    def test_legacy_execution_state_uses_the_bpe_code(self):
        self.assertEqual(controlled_file_name('REF', 'A', 'For construction (IFC)', 'title.pdf'), 'REF A BPE.pdf')

    def test_missing_reference_keeps_the_original_name_and_missing_index_is_not_invented(self):
        self.assertEqual(controlled_file_name(None, 'A', 'PREL', 'uploads/supplier.pdf'), 'supplier.pdf')
        self.assertEqual(controlled_file_name('REF', None, 'PREL', 'title.pdf'), 'REF PREL.pdf')

    def test_history_uses_its_approval_snapshot_instead_of_current_document_state(self):
        document = SimpleNamespace(full_document_number='REF', status='BPE')
        revision = SimpleNamespace(revision_label='A', approval_snapshot={'document': {'reference': 'REF', 'status': 'PREL'}}, file=SimpleNamespace(name='versions/original.pdf'), file_name='original.pdf')
        self.assertEqual(revision_file_name(document, revision), 'REF A PREL.pdf')

    def test_legacy_history_does_not_invent_a_missing_historical_state(self):
        document = SimpleNamespace(full_document_number='REF', status='BPE')
        revision = SimpleNamespace(revision_label='A', approval_snapshot=None, file=SimpleNamespace(name='original.pdf'), file_name='original.pdf')
        self.assertEqual(revision_file_name(document, revision), 'REF A.pdf')

    def test_filename_cannot_introduce_path_segments(self):
        self.assertEqual(controlled_file_name('../REF', 'A', 'PREL', 'x.pdf'), '.._REF A PREL.pdf')


class DistributionFileNameTests(TransmittalTestBase):
    def test_finalization_freezes_filename_before_later_metadata_changes(self):
        revision = self.make_approved_internal_revision()
        revision.document.full_document_number = 'PRJ-DOC-001'
        revision.document.status = 'PREL'
        revision.document.save(update_fields=['status', 'full_document_number'])
        transmittal = self.create_transmittal()
        add_transmittal_item(transmittal, revision, 'FOR_INFORMATION', self.owner)
        finalize_transmittal(transmittal, self.owner)
        frozen = transmittal.items.get().evidence_snapshot['revision']['download_name']
        self.assertIn(' PREL.', frozen)
        revision.document.status = 'BPE'
        revision.document.save(update_fields=['status'])
        self.assertEqual(transmittal.items.get().evidence_snapshot['revision']['download_name'], frozen)


class IssueStatePersistenceTests(MetadataBase):
    def test_issue_state_is_independent_from_internal_workflow(self):
        document = self.create_fileless(status='PREL')
        before = self.internal_status(document)
        updated = self.patch_document(document, status='BPE')
        self.assertEqual(updated.status, 'BPE')
        self.assertEqual(self.internal_status(updated), before)
