"""LOT 1 (migration 0257): model-level guarantees only -- no diffusion, validation or closure rule is tested here.

index_revision (technical version sharing an approved index), unique revision_label kept, follow-up document /
client_company derivation, targeted "one open follow-up per document and client", parallel clients, history kept."""
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from project_management.models import (
    ClientSurveillance, ClientSurveillanceFollowUp, ContactCompany, ContactUser, DocumentRevision,
)
from project_management.services.document_revision import add_document_revision, next_revision_label
from project_management.test_client_surveillance_followup import Category, FollowBase


class ModelBase(FollowBase):
    def tech(self, source=None, tag="bpe", content=None, **overrides):
        """The BPE technical version of an approved index, created the way the next lot's service will: one row."""
        source = source or self.revision
        latest = max(DocumentRevision.objects.filter(document=source.document).values_list("version_number", flat=True))
        values = dict(
            document=source.document, major_revision=source.major_revision, minor_revision=90 + latest,
            file_name=f"{tag}.txt", issue_state="BPE", index_revision=source, created_by=self.owner,
        )
        values.update(overrides)
        revision = DocumentRevision(**values)
        revision.file = SimpleUploadedFile(f"{tag}.txt", content if content is not None else f"content-{tag}".encode())
        revision.save()
        return revision

    def raw(self, sql, params):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)

    def other_client(self, name="Second Client"):
        company = ContactCompany.objects.create(
            company_profile=self.company, company_name=name, company_role="Client", contact_email=f"{name[:3].lower()}@x.invalid")
        contact = ContactUser.objects.create(
            company_profile=self.company, contact_company=company, name="Bob", surname="Roe", email=f"{name[:3].lower()}@second.invalid")
        return company, contact


class IndexRevisionTests(ModelBase):
    def test_a_prel_and_a_bpe_share_the_index_with_two_distinct_files_and_one_label(self):
        prel = self.revision
        before = DocumentRevision.objects.filter(document=self.document).count()
        prel_file, prel_hash, label = prel.file.name, prel.file_sha256, prel.revision_label
        bpe = self.tech(content=b"v1")  # byte-identical content is allowed: it is still a separate stored file
        prel.refresh_from_db()
        self.assertEqual((prel.revision_label, prel.file.name, prel.file_sha256, prel.index_revision_id), (label, prel_file, prel_hash, None))
        self.assertEqual((bpe.index_revision_id, bpe.issue_state, bpe.revision_label), (prel.pk, "BPE", None))
        self.assertNotEqual(bpe.file.name, prel.file.name)  # two physical files, neither overwritten
        self.assertEqual(list(prel.technical_versions.values_list("pk", flat=True)), [bpe.pk])
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), before + 1)  # history kept, one row added

    def test_revision_label_stays_unique_and_next_label_ignores_technical_versions(self):
        self.tech()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentRevision.objects.create(  # a second revision of the same document claiming label A
                document=self.document, version_number=50, major_revision="Z", minor_revision=0, revision_label=self.revision.revision_label,
                file="x.txt", file_name="x.txt")
        labels = DocumentRevision.objects.filter(document=self.document, revision_label__isnull=False).values_list("revision_label", flat=True)
        self.assertEqual(next_revision_label(labels), "B")
        nxt = self.approve(add_document_revision(self.document, self.file("next"), self.owner))
        self.assertEqual(nxt.revision_label, "B")  # the technical version consumed no index

    def test_the_model_refuses_incoherent_technical_versions(self):
        other = self.review()  # an approved revision of ANOTHER document
        with self.assertRaises(ValidationError):
            DocumentRevision(
                document=self.document, version_number=70, major_revision="Y", minor_revision=0, issue_state="BPE",
                index_revision=other, file_name="o.txt").save()
        unapproved = add_document_revision(self.document, self.file("draft"), self.owner)  # no label yet
        with self.assertRaises(ValidationError):
            self.tech(source=unapproved, tag="u")
        bpe = self.tech()
        with self.assertRaises(ValidationError):  # no chain of technical versions
            self.tech(source=bpe, tag="chain")

    def test_the_database_refuses_self_reference_own_label_and_non_bpe_state(self):
        bpe = self.tech()
        with self.assertRaises(IntegrityError):
            self.raw("UPDATE project_management_documentrevision SET index_revision_id = id WHERE id = %s", [bpe.pk])
        with self.assertRaises(IntegrityError):
            self.raw("UPDATE project_management_documentrevision SET revision_label = 'Q' WHERE id = %s", [bpe.pk])
        with self.assertRaises(IntegrityError):
            self.raw("UPDATE project_management_documentrevision SET issue_state = 'PREL' WHERE id = %s", [bpe.pk])
        with self.assertRaises(IntegrityError):  # the same rules apply to a row inserted without going through save()
            self.raw(
                "INSERT INTO project_management_documentrevision (document_id, version_number, major_revision, minor_revision, file, file_name, "
                "comment, created_at, index_revision_id, issue_state, revision_label) VALUES (%s, 91, 'A', 91, 'f.txt', 'f.txt', '', now(), %s, 'PREL', NULL)",
                [self.document.pk, self.revision.pk])

    def test_ordinary_versions_and_existing_rows_are_unaffected(self):
        self.assertIsNone(self.revision.index_revision_id)
        added = add_document_revision(self.document, self.file("plain"), self.owner)
        self.assertIsNone(added.index_revision_id)
        with self.assertRaises(ValidationError):  # stored versions are still immutable
            added.index_revision = self.revision
            added.save()


class FollowUpModelTests(ModelBase):
    def test_document_and_client_company_come_from_the_return_and_expected_index_is_optional(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        self.assertEqual((followup.document_id, followup.client_company_id), (self.document.pk, self.client_company.pk))
        self.assertEqual(followup.expected_index, self.revision.revision_label)  # LOT 2: set at confirmation (code A keeps the index)
        legacy = ClientSurveillanceFollowUp.objects.get(pk=followup.pk)
        ClientSurveillanceFollowUp.objects.filter(pk=legacy.pk).update(expected_index=None)  # the column stays optional (legacy rows)
        self.assertIsNone(ClientSurveillanceFollowUp.objects.get(pk=legacy.pk).expected_index)

    def test_a_follow_up_cannot_claim_another_document_or_client(self):
        record = self.ret(Category.ACCEPTED)
        followup = self.follow(record)
        followup.delete()
        other_company, _ = self.other_client()
        with self.assertRaises(ValidationError):
            ClientSurveillanceFollowUp.objects.create(
                client_return=record, surveillance=self.surveillance, expected_state="BPE", client_company=other_company)
        with self.assertRaises(ValidationError):
            ClientSurveillanceFollowUp.objects.create(
                client_return=record, surveillance=self.surveillance, expected_state="BPE", document=self.review().document)

    def test_the_derived_revision_must_belong_to_the_follow_up_document(self):
        followup = self.follow(self.ret(Category.ACCEPTED_WITH_COMMENTS))
        foreign = self.review()
        followup.derived_revision = foreign
        with self.assertRaises(ValidationError):
            followup.save(update_fields=["derived_revision"])

    def _second_surveillance_same_client(self):
        """Another approved revision of the SAME document sent to the SAME client: its own issue and surveillance."""
        revision = self.approve(add_document_revision(self.document, self.file("rev-b"), self.owner))
        self.issue_and_send([(revision, "FOR_REVIEW")])
        return ClientSurveillance.objects.get(client_issue__document_revision=revision)

    def test_one_open_follow_up_per_document_and_client_is_a_database_guarantee(self):
        first = self.follow(self.ret(Category.COMMENTED))
        second_surveillance = self._second_surveillance_same_client()
        second_return = self.record(second_surveillance.client_issue, category=Category.COMMENTED, comment="x")
        # the prototype's service swallows the conflict (Lot 2 turns it into an explicit review): no second open row exists
        self.assertFalse(ClientSurveillanceFollowUp.objects.filter(client_return=second_return).exists())
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientSurveillanceFollowUp.objects.create(client_return=second_return, surveillance=second_surveillance, expected_state="PREL")
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(document=self.document, closed_at__isnull=True).count(), 1)
        self.assertEqual(first.document_id, self.document.pk)

    def test_a_closed_follow_up_no_longer_blocks_the_next_one_and_history_is_kept(self):
        first = self.follow(self.ret(Category.ACCEPTED))
        bpe = self.tech()
        ClientSurveillanceFollowUp.objects.filter(pk=first.pk).update(
            derived_revision=bpe, closing_send_event=self.event, closed_at=timezone.now())
        second_surveillance = self._second_surveillance_same_client()
        second_return = self.record(second_surveillance.client_issue, category=Category.COMMENTED, comment="x")
        second = self.follow(second_return)  # now allowed: the first one is closed
        self.assertIsNone(second.closed_at)
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(document=self.document).count(), 2)
        self.assertEqual(ClientSurveillanceFollowUp.objects.get(pk=first.pk).derived_revision_id, bpe.pk)

    def test_several_clients_keep_parallel_open_follow_ups_on_the_same_document(self):
        first = self.follow(self.ret(Category.ACCEPTED))
        other_company, other_contact = self.other_client()
        self.issue_and_send([(self.revision, "FOR_REVIEW")], company=other_company, contacts=[other_contact])
        other_surveillance = ClientSurveillance.objects.get(client_issue__client_company=other_company)
        other_return = self.record(other_surveillance.client_issue, category=Category.COMMENTED, comment="x")
        second = self.follow(other_return)
        self.assertEqual((first.document_id, second.document_id), (self.document.pk, self.document.pk))
        self.assertNotEqual(first.client_company_id, second.client_company_id)
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(document=self.document, closed_at__isnull=True).count(), 2)
