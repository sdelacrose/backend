"""GED-L1.5: freeze issued transmittal evidence across ORM and cascade paths."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import Collector
from project_management.models import Transmittal, TransmittalEvent, TransmittalItem
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_transmittal import TransmittalTestBase


class IssuedEvidenceFreezeTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        self.revision = self.make_approved_internal_revision()
        self.transmittal = self.create_transmittal()
        self.item = add_transmittal_item(self.transmittal, self.revision, "FOR_INFORMATION", self.owner)
        self.transmittal = finalize_transmittal(self.transmittal, self.owner)
        self.item.refresh_from_db()

    def refused(self, action):
        with self.assertRaises(ValidationError), transaction.atomic():
            action()

    def test_header_snapshot_cannot_be_rewritten(self):
        original = self.transmittal.evidence_snapshot
        self.transmittal.evidence_snapshot = {"tampered": True}
        self.refused(self.transmittal.save)
        self.transmittal.refresh_from_db()
        self.assertEqual(self.transmittal.evidence_snapshot, original)

    def test_header_cannot_be_bulk_modified_or_deleted(self):
        qs = Transmittal.objects.filter(pk=self.transmittal.pk)
        self.refused(lambda: qs.update(subject="tampered"))
        self.refused(qs.delete)
        self.assertTrue(qs.exists())

    def test_item_cannot_be_saved_bulk_modified_or_deleted(self):
        original = self.item.evidence_snapshot
        self.item.evidence_snapshot = {"tampered": True}
        self.refused(self.item.save)
        qs = TransmittalItem.objects.filter(pk=self.item.pk)
        self.refused(lambda: qs.update(distribution_purpose="FOR_REVIEW"))
        self.refused(qs.delete)
        self.item.refresh_from_db()
        self.assertEqual(self.item.evidence_snapshot, original)

    def test_item_cannot_escape_by_changing_parent(self):
        draft = self.create_transmittal()
        self.item.transmittal = draft
        self.refused(self.item.save)
        self.item.refresh_from_db()
        self.assertEqual(self.item.transmittal_id, self.transmittal.pk)

    def test_events_cannot_be_bulk_modified_or_deleted(self):
        qs = TransmittalEvent.objects.filter(transmittal=self.transmittal)
        count = qs.count()
        self.refused(lambda: qs.update(metadata={"tampered": True}))
        self.refused(qs.delete)
        self.assertEqual(qs.count(), count)

    def test_collector_cannot_cascade_issued_evidence(self):
        def cascade():
            collector = Collector(using="default")
            collector.collect([self.transmittal])
            collector.delete()
        self.refused(cascade)
        self.assertTrue(TransmittalItem.objects.filter(pk=self.item.pk).exists())

    def test_live_metadata_does_not_change_item_snapshot(self):
        original = self.item.evidence_snapshot
        document = self.revision.document
        document.file_name = "later title.txt"
        document.save(update_fields=["file_name"])
        self.item.refresh_from_db()
        self.assertEqual(self.item.evidence_snapshot, original)

    def test_bulk_insert_cannot_add_an_item_to_an_issued_transmittal(self):
        later = self.add_revision(self.revision.document, content=b"later version")
        self.refused(lambda: TransmittalItem.objects.bulk_create([
            TransmittalItem(transmittal=self.transmittal, document_revision=later,
                            distribution_purpose="FOR_INFORMATION")
        ]))
        self.assertFalse(TransmittalItem.objects.filter(
            transmittal=self.transmittal, document_revision=later
        ).exists())

    def test_draft_edits_and_delete_remain_possible(self):
        draft = self.create_transmittal()
        qs = Transmittal.objects.filter(pk=draft.pk)
        self.assertEqual(qs.update(subject="allowed draft edit"), 1)
        item = add_transmittal_item(draft, self.revision, "FOR_INFORMATION", self.owner)
        item.distribution_purpose = "FOR_REVIEW"
        item.save()
        draft.delete()
        self.assertFalse(qs.exists())
