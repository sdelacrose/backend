# MAIL-1.4-A: the structures of the proof of an OUT send, their protection, the per-event Sent Items
# correlation and the SHA-256 of what was really sent. Microsoft Graph is always simulated; no file is
# ever downloaded or stored, no task is run, and the only sendMail calls are the ones the tests make
# on purpose. Every address, subject and id is fictitious.
import base64
import hashlib
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from userapis.test_mailbox import _make_company
from project_management.models import (
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceSendEvent,
    CorrespondenceSendProof,
    CorrespondenceSendProofFile,
    EvidenceBlob,
)
from project_management.services.correspondence_send import send_correspondence
from project_management.services.correspondence_send_proof import ensure_send_proof
from project_management.services.correspondence_workflow import (
    WorkflowType,
    perform_action,
    start_validation,
)
from project_management.test_correspondence_send_graph import GraphSendTestBase
from project_management.views.correspondence import CorrespondenceViewSet
from userapis.mailbox_correlation import identify_sent_message, record_sent_copy
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_read import MailboxReadError, ReplyIdentification, normalize_message
from userapis.mailbox_sync import identify_pending_sent_copies
from userapis.models import MailboxMessage
from userapis.test_mailbox_correlation import marker_of
from userapis.test_mailbox_read import FakeGraph, raw_message

Result = CorrespondenceSendEvent.Result
SHA_A = "a" * 64
SHA_B = "b" * 64


def new_blob(company, sha=SHA_A, size=3):
    # No file is written: the field only carries a storage NAME, as these tests never use storage.
    return EvidenceBlob.objects.create(
        company_profile=company, sha256=sha, file_size=size, file=f"send_evidence/test/{sha}",
    )


def blob(company, sha=SHA_A, size=3):
    """The blob of this content for this company, created once (what the archiving will do)."""
    existing = EvidenceBlob.objects.filter(company_profile=company, sha256=sha).first()
    return existing or new_blob(company, sha, size)


class ProofBase(GraphSendTestBase):
    def graph_send(self, corr=None):
        corr = corr or self.approved_out()
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        return corr, event, mock_post


class ProofCreationTests(ProofBase):
    def test_a_successful_graph_send_gets_exactly_one_pending_proof(self):
        corr, event, mock_post = self.graph_send()
        proof = CorrespondenceSendProof.objects.get(event=event)
        self.assertEqual(
            (proof.state, proof.attempts, proof.error_code, proof.mailbox_message_id, proof.archived_at),
            (CorrespondenceSendProof.State.PENDING, 0, "", None, None),
        )
        self.assertIsNone(proof.next_attempt_at)
        self.assertIsNone(proof.distribution_snapshot)
        self.assertEqual(CorrespondenceSendProof.objects.count(), 1)

    def test_the_creation_is_idempotent_and_a_second_proof_for_an_event_is_impossible(self):
        _, event, _ = self.graph_send()
        first = ensure_send_proof(event)
        self.assertEqual(ensure_send_proof(event).pk, first.pk)
        self.assertEqual(CorrespondenceSendProof.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceSendProof.objects.create(event=event)

    def test_a_resend_of_the_same_correspondence_has_its_own_distinct_proof(self):
        corr, first_event, _ = self.graph_send()
        corr.refresh_from_db()
        second_event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(second_event.result, Result.SUCCESS)
        proofs = CorrespondenceSendProof.objects.filter(event__correspondence=corr)
        self.assertEqual(proofs.count(), 2)
        self.assertEqual(
            {p.event_id for p in proofs}, {first_event.pk, second_event.pk},
        )

    def test_only_a_confirmed_graph_send_has_a_proof(self):
        corr = self.approved_out()
        failed, _, _ = self.send_via_graph(corr, self.individual_mailbox, mock_kwargs={"graph_status": 500})
        self.assertEqual(failed.result, Result.FAILED)
        self.assertEqual(CorrespondenceSendProof.objects.count(), 0)
        self.assertIsNone(ensure_send_proof(failed))
        smtp_corr = self.approved_out(subject="By SMTP")
        smtp_event = send_correspondence(smtp_corr, sender=self.owner)
        self.assertEqual((smtp_event.result, smtp_event.provider), (Result.SUCCESS, "smtp"))
        self.assertIsNone(ensure_send_proof(smtp_event))
        in_flight = CorrespondenceSendEvent.objects.create(
            correspondence=self.approved_out(subject="Busy"), sender=self.owner, provider="graph",
            mailbox=self.individual_mailbox, result=Result.IN_PROGRESS,
        )
        self.assertIsNone(ensure_send_proof(in_flight))
        self.assertEqual(CorrespondenceSendProof.objects.count(), 0)

    def test_a_send_recorded_before_proofs_existed_can_get_one_later(self):
        corr = self.approved_out()
        legacy = CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(),
            attachments_snapshot=[{"id": 1, "file_name": "old.pdf", "file_size": 5}],
        )
        proof = ensure_send_proof(legacy)
        self.assertEqual(proof.state, CorrespondenceSendProof.State.PENDING)
        legacy.refresh_from_db()
        self.assertEqual(legacy.attachments_snapshot, [{"id": 1, "file_name": "old.pdf", "file_size": 5}])

    def test_a_failure_while_preparing_the_proof_never_turns_the_send_into_a_failure(self):
        corr = self.approved_out()
        with patch(
            "project_management.services.correspondence_send_proof.ensure_send_proof",
            side_effect=RuntimeError("boom"),
        ):
            event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)
        self.assertEqual(CorrespondenceSendProof.objects.count(), 0)
        self.assertEqual(ensure_send_proof(event).state, CorrespondenceSendProof.State.PENDING)

    def test_even_a_database_error_while_preparing_the_proof_leaves_the_send_recorded(self):
        from django.db import connection

        def broken_table(_event):
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM a_table_that_does_not_exist")

        corr = self.approved_out()
        with patch(
            "project_management.services.correspondence_send_proof.ensure_send_proof",
            side_effect=broken_table,
        ):
            event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        stored = CorrespondenceSendEvent.objects.get(pk=event.pk)
        self.assertEqual((stored.result, bool(stored.sent_at)), (Result.SUCCESS, True))
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)

    def test_nothing_is_downloaded_stored_or_sent_by_preparing_a_proof(self):
        corr, event, mock_post = self.graph_send()
        self.assertEqual(sum(1 for c in mock_post.call_args_list if "sendMail" in str(c)), 1)
        self.assertEqual(EvidenceBlob.objects.count(), 0)
        self.assertEqual(CorrespondenceSendProofFile.objects.count(), 0)
        with patch("requests.post", side_effect=AssertionError("no call expected")), \
                patch("requests.get", side_effect=AssertionError("no call expected")):
            ensure_send_proof(event)
        self.assertEqual(CorrespondenceSendProof.objects.count(), 1)


class ProofRulesTests(ProofBase):
    def setUp(self):
        super().setUp()
        _, self.event, _ = self.graph_send()
        self.proof = CorrespondenceSendProof.objects.get(event=self.event)

    def proof_file(self, proof=None, *, kind="eml", sha=SHA_A, size=3, graph_id="", name="m.eml"):
        return CorrespondenceSendProofFile.objects.create(
            proof=proof or self.proof, kind=kind, file_name=name, file_size=size, sha256=sha,
            graph_attachment_id=graph_id, blob=blob(self.company, sha, size),
        )

    def test_a_proof_starts_pending_and_is_archived_only_with_its_message_and_a_date(self):
        with self.assertRaises(ValidationError):
            CorrespondenceSendProof(
                event=self.approved_event(), state=CorrespondenceSendProof.State.ARCHIVED,
                archived_at=timezone.now(),
            ).save()
        self.proof.state = CorrespondenceSendProof.State.ARCHIVED
        self.proof.archived_at = timezone.now()
        with self.assertRaises(ValidationError):          # no .eml kept yet
            self.proof.save()
        self.proof.refresh_from_db()
        self.proof_file()
        self.proof.state = CorrespondenceSendProof.State.ARCHIVED
        with self.assertRaises(IntegrityError), transaction.atomic():   # DB: archived needs its date
            self.proof.save()
        self.proof.archived_at = timezone.now()
        self.proof.save()
        self.assertEqual(CorrespondenceSendProof.objects.get(pk=self.proof.pk).state, "ARCHIVED")

    def approved_event(self):
        corr = self.approved_out(subject="Other")
        return CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(),
        )

    def test_an_archived_proof_cannot_be_changed_extended_or_deleted(self):
        self.proof_file()
        self.proof.state = CorrespondenceSendProof.State.ARCHIVED
        self.proof.archived_at = timezone.now()
        self.proof.save()
        self.proof.error_code = "x"
        with self.assertRaises(ValidationError):
            self.proof.save()
        with self.assertRaises(ValidationError):
            self.proof_file(kind="attachment", sha=SHA_B, graph_id="g1", name="a.pdf")
        with self.assertRaises(ValidationError):
            self.proof.delete()

    def test_one_eml_per_proof_and_one_row_per_graph_attachment(self):
        self.proof_file()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.proof_file(sha=SHA_B, name="again.eml")
        self.proof_file(kind="attachment", sha=SHA_B, graph_id="g1", name="a.pdf")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.proof_file(kind="attachment", sha="c" * 64, graph_id="g1", name="b.pdf")
        self.proof_file(kind="attachment", sha="c" * 64, graph_id="g2", name="b.pdf")
        self.assertEqual(self.proof.files.count(), 3)

    def test_the_same_content_in_two_different_sends_is_two_histories_over_one_blob(self):
        other_event = self.approved_event()
        other = ensure_send_proof(other_event)
        first = self.proof_file(kind="attachment", sha=SHA_B, graph_id="g1", name="a.pdf")
        second = self.proof_file(other, kind="attachment", sha=SHA_B, graph_id="g1", name="a.pdf")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.blob_id, second.blob_id)
        self.assertEqual(EvidenceBlob.objects.filter(sha256=SHA_B).count(), 1)

    def test_blobs_are_unique_per_company_and_sha256_and_never_modified_or_deleted(self):
        one = new_blob(self.company, SHA_A)
        with self.assertRaises(IntegrityError), transaction.atomic():
            new_blob(self.company, SHA_A)
        foreign = _make_company("Other Co")
        self.assertNotEqual(new_blob(foreign, SHA_A).pk, one.pk)
        one.file_size = 99
        with self.assertRaises(ValidationError):
            one.save()
        with self.assertRaises(ValidationError):
            one.delete()
        self.assertIn(
            "send_evidence/", EvidenceBlob.objects.get(pk=one.pk).file.name,
        )

    def test_a_proof_file_must_match_its_blob_and_is_never_modified_or_deleted(self):
        with self.assertRaises(ValidationError):
            CorrespondenceSendProofFile.objects.create(
                proof=self.proof, kind="eml", file_name="m.eml", file_size=4, sha256=SHA_A,
                blob=blob(self.company, SHA_A, 3),
            )
        row = self.proof_file()
        row.file_name = "renamed.eml"
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            row.delete()

    def test_the_origin_attachment_is_information_only(self):
        corr = self.approved_out(subject="With file")
        attachment = CorrespondenceAttachment(correspondence=corr, file_name="a.pdf", file_size=3)
        attachment.file.save("a.pdf", SimpleUploadedFile("a.pdf", b"abc"), save=False)
        attachment.save()
        event = CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(),
        )
        proof = ensure_send_proof(event)
        row = CorrespondenceSendProofFile.objects.create(
            proof=proof, kind="attachment", file_name="a.pdf", file_size=3, sha256=SHA_A,
            graph_attachment_id="g1", source_attachment=attachment, blob=blob(self.company, SHA_A),
        )
        stored = attachment.file
        attachment.delete()
        stored.storage.delete(stored.name)
        row.refresh_from_db()
        self.assertIsNone(row.source_attachment_id)
        self.assertEqual(EvidenceBlob.objects.filter(pk=row.blob_id).count(), 1)


class DeleteProtectionTests(ProofBase):
    def test_a_correspondence_that_was_sent_cannot_be_deleted_by_any_path(self):
        corr, event, _ = self.graph_send()
        with self.assertRaises(ValidationError):
            corr.delete()
        with self.assertRaises(ProtectedError):           # a cascade (queryset, project...) is stopped by the proof
            Correspondence.objects.filter(pk=corr.pk).delete()
        with self.assertRaises(ProtectedError):
            CorrespondenceSendEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            CorrespondenceSendProof.objects.get(event=event).delete()
        self.assertTrue(Correspondence.objects.filter(pk=corr.pk).exists())
        self.assertEqual(CorrespondenceSendProof.objects.count(), 1)

    def test_the_delete_endpoint_refuses_cleanly(self):
        from rest_framework.exceptions import ValidationError as DrfValidationError

        corr, _, _ = self.graph_send()
        with self.assertRaises(DrfValidationError):
            CorrespondenceViewSet().perform_destroy(corr)
        self.assertTrue(Correspondence.objects.filter(pk=corr.pk).exists())

    def test_a_correspondence_never_sent_is_deleted_exactly_as_before(self):
        draft = self.approved_out(subject="Never sent")
        draft.delete()
        self.assertFalse(Correspondence.objects.filter(pk=draft.pk).exists())
        failed = self.approved_out(subject="Failed only")
        self.send_via_graph(failed, self.individual_mailbox, mock_kwargs={"graph_status": 500})
        self.assertEqual(failed.send_events.get().result, Result.FAILED)
        failed.delete()
        self.assertFalse(Correspondence.objects.filter(pk=failed.pk).exists())
        incoming = self.create_in() if hasattr(self, "create_in") else None
        if incoming is not None:
            incoming.delete()


class AttachmentHashTests(ProofBase):
    def with_attachment(self, content=b"letter content"):
        corr = self.create_out(recipient_contact=self.recipient_contact.id)
        attachment = CorrespondenceAttachment(
            correspondence=corr, file_name="letter.pdf", file_size=len(content), uploaded_by=self.owner,
        )
        attachment.file.save("letter.pdf", SimpleUploadedFile("letter.pdf", content), save=False)
        attachment.save()
        wf = start_validation(
            corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author,
        )
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr, attachment

    def test_the_snapshot_carries_the_sha256_of_the_bytes_actually_sent(self):
        corr, attachment = self.with_attachment()
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        sent = self._graph_call(mock_post).kwargs["json"]["message"]["attachments"][0]
        sent_bytes = base64.b64decode(sent["contentBytes"])
        event = CorrespondenceSendEvent.objects.get(pk=event.pk)
        self.assertEqual(event.attachments_snapshot, [{
            "id": attachment.id, "file_name": "letter.pdf", "file_size": 14,
            "sha256": hashlib.sha256(sent_bytes).hexdigest(),
        }])

    def test_changing_the_file_afterwards_never_changes_what_the_event_recorded(self):
        corr, attachment = self.with_attachment()
        event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        recorded = CorrespondenceSendEvent.objects.get(pk=event.pk).attachments_snapshot[0]["sha256"]
        with attachment.file.storage.open(attachment.file.name, "wb") as handle:
            handle.write(b"tampered afterwards")
        self.assertEqual(
            CorrespondenceSendEvent.objects.get(pk=event.pk).attachments_snapshot[0]["sha256"], recorded,
        )
        self.assertEqual(recorded, hashlib.sha256(b"letter content").hexdigest())

    def test_the_file_is_read_once_for_the_send_and_the_hash(self):
        corr, attachment = self.with_attachment()
        from django.db.models.fields.files import FieldFile

        opened = []
        original = FieldFile.open

        def counting_open(field_file, *args, **kwargs):
            opened.append(field_file.name)
            return original(field_file, *args, **kwargs)

        with patch.object(FieldFile, "open", counting_open):
            self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(opened.count(attachment.file.name), 1)

    def test_a_failed_send_that_had_read_the_file_keeps_its_hash_and_one_that_had_not_does_not(self):
        corr, attachment = self.with_attachment()
        failed, _, _ = self.send_via_graph(corr, self.individual_mailbox, mock_kwargs={"graph_status": 500})
        self.assertEqual(failed.result, Result.FAILED)
        self.assertEqual(
            CorrespondenceSendEvent.objects.get(pk=failed.pk).attachments_snapshot[0]["sha256"],
            hashlib.sha256(b"letter content").hexdigest(),
        )
        corr2, _ = self.with_attachment()
        with patch("userapis.mailbox_send.get_mail_access_token_for_sending", return_value=None):
            early, _, _ = self.send_via_graph(corr2, self.individual_mailbox)
        self.assertEqual(early.result, Result.FAILED)
        self.assertNotIn("sha256", CorrespondenceSendEvent.objects.get(pk=early.pk).attachments_snapshot[0])

    def test_events_recorded_before_this_lot_keep_their_snapshot_untouched(self):
        corr = self.approved_out()
        old = CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(),
            attachments_snapshot=[{"id": 7, "file_name": "x.pdf", "file_size": 3}],
        )
        ensure_send_proof(old)
        self.assertEqual(
            CorrespondenceSendEvent.objects.get(pk=old.pk).attachments_snapshot,
            [{"id": 7, "file_name": "x.pdf", "file_size": 3}],
        )

    def test_an_smtp_send_records_the_hash_too(self):
        corr, attachment = self.with_attachment(b"smtp content")
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(
            CorrespondenceSendEvent.objects.get(pk=event.pk).attachments_snapshot[0]["sha256"],
            hashlib.sha256(b"smtp content").hexdigest(),
        )


class StubReader:
    """Answers the two reads the identification makes, from what a test scripts; records every call
    and can write nothing."""

    def __init__(self):
        self.calls = []
        self.sent_items = []
        self.idents = {}

    def list_messages(self, folder, **kwargs):
        self.calls.append(("list", folder))
        from types import SimpleNamespace

        return SimpleNamespace(messages=list(self.sent_items))

    def get_reply_identification(self, graph_id):
        self.calls.append(("ident", graph_id))
        return self.idents[graph_id]


class SentCopyPerEventTests(ProofBase):
    def setUp(self):
        super().setUp()
        self.reader = StubReader()
        self.now = timezone.now()

    def copy_for(self, event, marker, n, *, internet_id=None):
        meta = normalize_message(raw_message(
            n, id=f"AAMk-sent-{n}", subject=event.subject, internetMessageId=internet_id or f"<sent{n}@links-local.invalid>",
            conversationId="conv-1",
            toRecipients=[{"emailAddress": {"address": "jane.doe@links-local.invalid"}}],
            receivedDateTime=(event.sent_at - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ), "sentitems")
        self.reader.idents[meta.graph_id] = ReplyIdentification(
            internet_message_id=meta.internet_message_id, conversation_id="conv-1",
            send_marker=marker,
        )
        return meta

    def sent(self):
        corr = self.approved_out()
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        return corr, event, marker_of(mock_post, self)

    def test_a_valid_marker_links_the_copy_to_this_event_proof(self):
        corr, event, marker = self.sent()
        self.reader.sent_items = [self.copy_for(event, marker, 1)]
        row = identify_sent_message(event, actor=self.owner, reader=self.reader)
        proof = CorrespondenceSendProof.objects.get(event=event)
        self.assertEqual(proof.mailbox_message_id, row.pk)
        self.assertEqual(proof.state, CorrespondenceSendProof.State.PENDING)   # nothing archived yet
        self.assertEqual(row.correspondence_id, corr.pk)

    def test_a_copy_with_an_invalid_marker_or_another_events_marker_is_never_accepted(self):
        corr, event, marker = self.sent()
        other_corr, other_event, other_marker = self.sent()
        for forged in ("", "999.deadbeefdeadbeefdead", other_marker, marker + "0"):
            self.reader.sent_items = [self.copy_for(event, forged, 1)]
            self.assertIsNone(identify_sent_message(event, actor=self.owner, reader=self.reader))
        self.assertEqual(MailboxMessage.objects.count(), 0)
        self.assertIsNone(CorrespondenceSendProof.objects.get(event=event).mailbox_message_id)
        # same subject and same recipient is still not enough
        self.assertEqual(self.reader.sent_items[0].subject, event.subject)

    def test_a_resend_finds_its_own_new_copy_and_leaves_the_first_one_alone(self):
        corr, first, first_marker = self.sent()
        self.reader.sent_items = [self.copy_for(first, first_marker, 1)]
        first_row = identify_sent_message(first, actor=self.owner, reader=self.reader)
        corr.refresh_from_db()
        second, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        second_marker = marker_of(mock_post, self)
        self.assertNotEqual(first_marker, second_marker)
        # the first copy is still in Sent Items: it must not be taken for the resend's copy
        self.reader.sent_items = [
            self.copy_for(first, first_marker, 1), self.copy_for(second, second_marker, 2),
        ]
        second_row = identify_sent_message(second, actor=self.owner, reader=self.reader)
        self.assertNotEqual(second_row.pk, first_row.pk)
        self.assertEqual(
            CorrespondenceSendProof.objects.get(event=first).mailbox_message_id, first_row.pk,
        )
        self.assertEqual(
            CorrespondenceSendProof.objects.get(event=second).mailbox_message_id, second_row.pk,
        )
        self.assertEqual(MailboxMessage.objects.count(), 2)

    def test_the_pending_search_now_covers_every_send_of_a_correspondence(self):
        corr, first, first_marker = self.sent()
        self.reader.sent_items = [self.copy_for(first, first_marker, 1)]
        self.assertEqual(
            identify_pending_sent_copies(self.individual_mailbox, actor=self.owner, reader=self.reader), 1,
        )
        corr.refresh_from_db()
        second, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        second_marker = marker_of(mock_post, self)
        self.reader.calls.clear()
        self.reader.sent_items = [
            self.copy_for(first, first_marker, 1), self.copy_for(second, second_marker, 2),
        ]
        self.assertEqual(
            identify_pending_sent_copies(self.individual_mailbox, actor=self.owner, reader=self.reader), 1,
        )
        self.assertIsNotNone(CorrespondenceSendProof.objects.get(event=second).mailbox_message_id)
        # nothing left to look for: no further read at all
        self.reader.calls.clear()
        self.assertEqual(
            identify_pending_sent_copies(self.individual_mailbox, actor=self.owner, reader=self.reader), 0,
        )
        self.assertEqual(self.reader.calls, [])

    def test_a_copy_already_known_for_the_event_is_not_asked_of_graph_again(self):
        _, event, marker = self.sent()
        self.reader.sent_items = [self.copy_for(event, marker, 1)]
        row = identify_sent_message(event, actor=self.owner, reader=self.reader)
        self.reader.calls.clear()
        self.assertEqual(identify_sent_message(event, actor=self.owner, reader=self.reader).pk, row.pk)
        self.assertEqual(self.reader.calls, [])

    def test_only_someone_who_can_read_the_mailbox_can_identify_even_a_known_copy(self):
        _, event, marker = self.sent()
        self.reader.sent_items = [self.copy_for(event, marker, 1)]
        identify_sent_message(event, actor=self.owner, reader=self.reader)
        with self.assertRaises(MailboxReadError):
            identify_sent_message(event, actor=self.delegate, reader=self.reader)

    def test_a_copy_recorded_before_proofs_existed_gets_its_proof_when_found_again(self):
        corr = self.approved_out()
        event = CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(), subject=corr.subject or "",
            to_recipients=[{"email": "jane.doe@links-local.invalid"}],
        )
        marker = __import__("userapis.mailbox_correlation", fromlist=["x"]).build_send_marker(event.pk, corr.pk)
        meta = self.copy_for(event, marker, 1)
        existing, _ = store_message_metadata(self.individual_mailbox, meta)
        MailboxMessage.objects.filter(pk=existing.pk).update(correspondence=corr)
        self.reader.sent_items = [meta]
        row = identify_sent_message(event, actor=self.owner, reader=self.reader)
        self.assertEqual(row.pk, existing.pk)
        self.assertEqual(CorrespondenceSendProof.objects.get(event=event).mailbox_message_id, existing.pk)
        self.assertEqual(MailboxMessage.objects.count(), 1)

    def test_identifying_never_sends_creates_files_or_changes_the_correspondence(self):
        corr, event, marker = self.sent()
        before = Correspondence.objects.get(pk=corr.pk)
        self.reader.sent_items = [self.copy_for(event, marker, 1)]
        with patch("requests.post", side_effect=AssertionError("no sendMail")):
            identify_sent_message(event, actor=self.owner, reader=self.reader)
        after = Correspondence.objects.get(pk=corr.pk)
        self.assertEqual((before.status, before.updated_at), (after.status, after.updated_at))
        self.assertEqual(EvidenceBlob.objects.count() + CorrespondenceSendProofFile.objects.count(), 0)
        self.assertEqual({c[0] for c in self.reader.calls}, {"list", "ident"})
