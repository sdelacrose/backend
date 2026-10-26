# MAIL-1.4-B: keeping the proof of an OUT send (.eml + sent attachments) -- the service, its state rules,
# idempotence and resumption, blob sharing, historical sends and the delete protection. Microsoft Graph
# is always simulated (a scripted reader); files go to a temporary folder; no sendMail is ever made by the
# archiving (requests.post raises if it is tried). Every address, subject and id is fictitious.
import base64
import hashlib
import logging
import os
import shutil
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import ProtectedError
from django.test import override_settings
from django.utils import timezone

from project_management.models import (
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceSendEvent,
    CorrespondenceSendProof,
    CorrespondenceSendProofFile,
    EvidenceBlob,
)
from project_management.services.correspondence_send import send_correspondence
from project_management.services.correspondence_send_proof import ensure_missing_send_proofs, ensure_send_proof
from project_management.services.correspondence_send_proof_archive import (
    ADVISORY_LOCK_NAMESPACE,
    Fact,
    archive_send_proof,
    match_attachments,
)
from project_management.services.correspondence_workflow import WorkflowType, perform_action, start_validation
from project_management.test_correspondence_send_graph import GraphSendTestBase
from django.core.files.uploadedfile import SimpleUploadedFile
from userapis.mailbox_read import (
    AttachmentListing,
    GraphAttachmentMeta,
    MailboxReadError,
    ReplyIdentification,
    normalize_message,
)
from userapis.models import MailboxMessage
from userapis.test_mailbox_correlation import marker_of
from userapis.test_mailbox_read import raw_message

Result = CorrespondenceSendEvent.Result
State = CorrespondenceSendProof.State
SUBJECT_SENTINEL = "Confidential-subject-sentinel"
GRAPH_ID_SENTINEL = "AAMk-graph-id-sentinel"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def eml_bytes(tag="1"):
    return (
        f"From: owner@links-local.invalid\r\nTo: jane.doe@links-local.invalid\r\n"
        f"Date: Mon, 12 Oct 2026 10:00:00 +0000\r\nMessage-ID: <m{tag}@links-local.invalid>\r\n"
        f"Subject: s\r\n\r\nHello\r\n"
    ).encode()


class ScriptedSentItems:
    """Stands in for MailboxGraphReader: the Sent Items copy of one or more sends, answered from what a
    test scripts. It can read only: it has no way to send or write, and records every call."""

    def __init__(self):
        self.calls = []
        self.sent_items = []
        self.idents = {}
        self.eml = {}                  # graph id -> bytes (or an exception to raise)
        self.attachments = {}          # graph id -> [dict(graph_id, name, content, size, kind, is_inline, content_type)]
        self.truncated = False
        self.fail_download = {}        # (graph id, attachment id) -> exception

    def of(self, name):
        return [c for c in self.calls if c[0] == name]

    def list_messages(self, folder, **kwargs):
        self.calls.append(("list", folder))
        return SimpleNamespace(messages=list(self.sent_items))

    def get_reply_identification(self, graph_id):
        self.calls.append(("ident", graph_id))
        return self.idents[graph_id]

    def list_attachments(self, graph_id):
        self.calls.append(("list_attachments", graph_id))
        metas = []
        for a in self.attachments.get(graph_id, []):
            metas.append(GraphAttachmentMeta(
                graph_id=a["graph_id"], name=a["name"], content_type=a.get("content_type", "application/pdf"),
                size=a.get("size", len(a["content"]) + 40), kind=a.get("kind", "file"),
                is_inline=a.get("is_inline", False),
            ))
        return AttachmentListing(attachments=metas, truncated=self.truncated)

    def _write(self, data, fh, max_bytes):
        if len(data) > max_bytes:
            raise MailboxReadError("content_too_large")
        fh.write(data)
        return len(data), sha(data)

    def download_message_mime(self, graph_id, fh, max_bytes):
        self.calls.append(("eml", graph_id))
        data = self.eml[graph_id]
        if isinstance(data, Exception):
            raise data
        return self._write(data, fh, max_bytes)

    def download_attachment(self, graph_id, attachment_id, fh, max_bytes):
        self.calls.append(("attachment", graph_id, attachment_id))
        failure = self.fail_download.get((graph_id, attachment_id))
        if failure is not None:
            raise failure
        for a in self.attachments[graph_id]:
            if a["graph_id"] == attachment_id:
                return self._write(a["content"], fh, max_bytes)
        raise MailboxReadError("not_found")


class ArchiveBase(GraphSendTestBase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp(prefix="send-proof-test-")
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.reader = ScriptedSentItems()
        self.counter = 0
        self.other_sessions = []
        self.addCleanup(self._close_sessions)

    def _close_sessions(self):
        for conn in self.other_sessions:
            try:
                conn.close()
            except Exception:
                pass

    # -- sending (Graph mocked) ---------------------------------------------------------------
    def out_with(self, files=()):
        corr = self.create_out(recipient_contact=self.recipient_contact.id)
        for name, content in files:
            attachment = CorrespondenceAttachment(
                correspondence=corr, file_name=name, file_size=len(content), uploaded_by=self.owner,
            )
            attachment.file.save(name, SimpleUploadedFile(name, content), save=False)
            attachment.save()
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr

    def send(self, corr):
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        return event, marker_of(mock_post, self), self._graph_call(mock_post).kwargs["json"]["message"].get("attachments", [])

    def copy(self, event, marker, sent_attachments, *, eml=None, graph_id=None):
        """Put THIS send's copy in the scripted Sent Items, holding exactly what was sent."""
        self.counter += 1
        graph_id = graph_id or f"AAMk-copy-{self.counter}"
        meta = normalize_message(raw_message(
            self.counter, id=graph_id, subject=event.subject, internetMessageId=f"<sent{self.counter}@links-local.invalid>",
            conversationId=f"conv-{self.counter}",
            toRecipients=[{"emailAddress": {"address": "jane.doe@links-local.invalid"}}],
            receivedDateTime=(event.sent_at - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ), "sentitems")
        self.reader.sent_items = [m for m in self.reader.sent_items if m.graph_id != graph_id] + [meta]
        self.reader.idents[meta.graph_id] = ReplyIdentification(
            internet_message_id=meta.internet_message_id, conversation_id=f"conv-{self.counter}", send_marker=marker,
        )
        self.reader.eml[meta.graph_id] = eml if eml is not None else eml_bytes(str(self.counter))
        self.reader.attachments[meta.graph_id] = [
            {"graph_id": f"{graph_id}-att-{i}", "name": a["name"], "content": base64.b64decode(a["contentBytes"])}
            for i, a in enumerate(sent_attachments)
        ]
        return meta.graph_id

    def sent_and_copied(self, files=(), **kwargs):
        corr = self.out_with(files)
        event, marker, sent = self.send(corr)
        graph_id = self.copy(event, marker, sent, **kwargs)
        return corr, event, graph_id

    def archive(self, event, **kwargs):
        proof = CorrespondenceSendProof.objects.get(event=event)
        with patch("requests.post", side_effect=AssertionError("sendMail must never be called")):
            return proof, archive_send_proof(proof.pk, actor=self.owner, reader=self.reader, **kwargs)

    def on_disk(self):
        return sorted(
            os.path.relpath(os.path.join(d, f), self.media)
            for d, _, names in os.walk(self.media) for f in names if d.startswith(os.path.join(self.media, "send_evidence"))
        )


class ArchiveSuccessTests(ArchiveBase):
    def test_a_send_without_attachment_keeps_its_eml(self):
        corr, event, graph_id = self.sent_and_copied()
        before = (event.result, event.sent_at, Correspondence.objects.get(pk=corr.pk).status)
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code, result.verification), ("archived", State.ARCHIVED, "", "full"))
        self.assertIsNotNone(proof.archived_at)
        files = list(proof.files.all())
        self.assertEqual([(f.kind, f.sha256) for f in files], [("eml", sha(self.reader.eml[graph_id]))])
        self.assertEqual(files[0].file_size, len(self.reader.eml[graph_id]))
        self.assertTrue(files[0].file_name.endswith(".eml"))
        # the bytes on disk are what the hash says, in a private path keyed by the hash
        with files[0].blob.file.open("rb") as handle:
            self.assertEqual(sha(handle.read()), files[0].sha256)
        self.assertIn(files[0].sha256, files[0].blob.file.name)
        # the send itself is untouched
        event.refresh_from_db()
        self.assertEqual((event.result, event.sent_at, Correspondence.objects.get(pk=corr.pk).status), before)

    def test_a_send_with_one_attachment_keeps_it_apart_from_the_eml(self):
        corr, event, graph_id = self.sent_and_copied([("letter.pdf", b"letter content")])
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, result.verification), (State.ARCHIVED, "full"))
        by_kind = {f.kind: f for f in proof.files.all()}
        self.assertEqual(set(by_kind), {"eml", "attachment"})
        attachment = by_kind["attachment"]
        self.assertEqual((attachment.file_name, attachment.sha256, attachment.file_size),
                         ("letter.pdf", sha(b"letter content"), 14))
        self.assertEqual(attachment.graph_attachment_id, f"{graph_id}-att-0")
        self.assertEqual(attachment.source_attachment.file_name, "letter.pdf")
        # the event's own hash and the kept file's hash are the same bytes
        event.refresh_from_db()
        self.assertEqual(event.attachments_snapshot[0]["sha256"], attachment.sha256)
        self.assertNotEqual(by_kind["eml"].blob_id, attachment.blob_id)

    def test_several_attachments_are_all_kept_and_matched(self):
        files = [("a.pdf", b"aaa"), ("b.pdf", b"bbbb"), ("c.docx", b"ccccc")]
        _, event, _ = self.sent_and_copied(files)
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual(proof.state, State.ARCHIVED)
        self.assertEqual(proof.files.filter(kind="attachment").count(), 3)
        self.assertEqual(
            {f.sha256 for f in proof.files.filter(kind="attachment")}, {sha(c) for _, c in files},
        )

    def test_two_attachments_with_the_same_name_and_different_contents_are_told_apart(self):
        _, event, _ = self.sent_and_copied([("same.pdf", b"first content"), ("same.pdf", b"second content")])
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual(proof.state, State.ARCHIVED)
        kept = list(proof.files.filter(kind="attachment"))
        self.assertEqual(sorted(f.sha256 for f in kept), sorted([sha(b"first content"), sha(b"second content")]))
        self.assertEqual({f.file_name for f in kept}, {"same.pdf"})
        self.assertEqual(len({f.graph_attachment_id for f in kept}), 2)
        self.assertEqual(len({f.source_attachment_id for f in kept}), 2)

    def test_the_proof_survives_a_change_or_deletion_of_the_current_attachment(self):
        corr, event, _ = self.sent_and_copied([("letter.pdf", b"letter content")])
        proof, _ = self.archive(event)
        attachment = CorrespondenceAttachment.objects.get(correspondence=corr)
        stored = attachment.file
        attachment.delete()
        stored.storage.delete(stored.name)
        proof.refresh_from_db()
        kept = proof.files.get(kind="attachment")
        self.assertIsNone(kept.source_attachment_id)
        with kept.blob.file.open("rb") as handle:
            self.assertEqual(handle.read(), b"letter content")
        self.assertEqual(proof.state, State.ARCHIVED)

    def test_an_archived_proof_is_not_touched_by_a_second_run(self):
        _, event, _ = self.sent_and_copied([("letter.pdf", b"letter content")])
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        stamp, calls = proof.updated_at, len(self.reader.calls)
        again = archive_send_proof(proof.pk, actor=self.owner, reader=self.reader)
        proof.refresh_from_db()
        self.assertEqual((again.outcome, proof.updated_at, len(self.reader.calls)), ("already_archived", stamp, calls))
        self.assertEqual(proof.files.count(), 2)


class SharedHistoriesTests(ArchiveBase):
    def test_a_resend_has_its_own_proof_and_shares_the_blobs_of_identical_files(self):
        corr = self.out_with([("letter.pdf", b"letter content")])
        first, first_marker, first_sent = self.send(corr)
        self.copy(first, first_marker, first_sent, eml=eml_bytes("same"))
        first_proof, _ = self.archive(first)
        corr.refresh_from_db()
        second, second_marker, second_sent = self.send(corr)
        self.copy(second, second_marker, second_sent, eml=eml_bytes("same"))
        second_proof, result = self.archive(second)
        first_proof.refresh_from_db(), second_proof.refresh_from_db()
        self.assertEqual((first_proof.state, second_proof.state), (State.ARCHIVED, State.ARCHIVED))
        self.assertNotEqual(first_proof.pk, second_proof.pk)
        self.assertEqual(result.blobs_reused, 2)
        self.assertEqual(EvidenceBlob.objects.count(), 2)                     # one physical file per content
        self.assertEqual(len(self.on_disk()), 2)
        self.assertEqual(CorrespondenceSendProofFile.objects.count(), 4)      # two distinct histories
        self.assertEqual(
            set(first_proof.files.values_list("blob_id", flat=True)),
            set(second_proof.files.values_list("blob_id", flat=True)),
        )
        self.assertEqual(first_proof.event.correspondence_id, second_proof.event.correspondence_id)

    def test_the_other_companys_content_is_never_shared(self):
        _, event, _ = self.sent_and_copied([("letter.pdf", b"letter content")])
        self.archive(event)
        foreign = CorrespondenceSendProofFile.objects.filter(kind="attachment").first().blob
        self.assertEqual(foreign.company_profile_id, self.company.pk)
        self.assertIn(f"company_{self.company.pk}", foreign.file.name)


class ArchiveIncompleteTests(ArchiveBase):
    def test_a_missing_eml_keeps_the_proof_pending_and_stores_nothing(self):
        _, event, graph_id = self.sent_and_copied([("letter.pdf", b"x")])
        self.reader.eml[graph_id] = MailboxReadError("not_found")
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("pending", State.PENDING, "copy_gone"))
        self.assertEqual(CorrespondenceSendProofFile.objects.count() + EvidenceBlob.objects.count(), 0)

    def test_an_invalid_eml_fails_and_is_not_kept(self):
        _, event, graph_id = self.sent_and_copied()
        self.reader.eml[graph_id] = b"\x00\x01 binary junk"
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.FAILED, "invalid_eml"))
        self.assertEqual(CorrespondenceSendProofFile.objects.count(), 0)
        self.assertEqual(self.on_disk(), [])

    def test_a_copy_not_in_sent_items_yet_stays_pending(self):
        _, event, _ = self.sent_and_copied()
        self.reader.sent_items = []
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code, proof.attempts), ("pending", State.PENDING, "copy_not_found", 1))
        self.assertEqual(self.reader.of("eml"), [])

    def test_a_missing_attachment_makes_the_proof_partial_and_a_later_run_completes_it(self):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa"), ("b.pdf", b"bbb")])
        full = list(self.reader.attachments[graph_id])
        self.reader.attachments[graph_id] = full[:1]
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("partial", State.PARTIAL, "attachment_missing"))
        self.assertIsNone(proof.archived_at)
        self.assertEqual(proof.files.count(), 2)                              # the .eml and the one present
        # the copy now lists both: only the missing one is downloaded, the rest is not redone
        self.reader.attachments[graph_id] = full
        self.reader.calls.clear()
        _, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code, proof.files.count()), ("archived", State.ARCHIVED, "", 3))
        self.assertEqual(self.reader.of("eml"), [])
        self.assertEqual([c[2] for c in self.reader.of("attachment")], [full[1]["graph_id"]])

    def test_a_different_content_under_the_same_name_is_a_divergence_not_a_proof(self):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"what was sent")])
        self.reader.attachments[graph_id][0]["content"] = b"what Sent Items holds"
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code, result.verification), (State.PARTIAL, "attachment_hash_mismatch", "none"))
        kept = proof.files.get(kind="attachment")
        self.assertEqual(kept.sha256, sha(b"what Sent Items holds"))          # kept as information
        self.assertNotEqual(kept.sha256, CorrespondenceSendEvent.objects.get(pk=event.pk).attachments_snapshot[0]["sha256"])

    def test_an_attachment_nobody_sent_is_reported(self):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa")])
        self.reader.attachments[graph_id].append({"graph_id": "extra", "name": "extra.pdf", "content": b"extra"})
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.PARTIAL, "unexpected_attachment"))

    def test_a_truncated_attachment_list_is_never_a_complete_proof(self):
        _, event, _ = self.sent_and_copied([("a.pdf", b"aaa")])
        self.reader.truncated = True
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.PARTIAL, "attachments_truncated"))


class SentCopyAuthenticationTests(ArchiveBase):
    def test_a_copy_without_the_right_marker_is_never_archived(self):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa")])
        self.reader.idents[graph_id] = ReplyIdentification(
            internet_message_id="<x@x>", conversation_id="c", send_marker="999.deadbeefdeadbeefdead",
        )
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("pending", State.PENDING, "copy_not_found"))
        self.assertEqual(self.reader.of("eml") + self.reader.of("attachment"), [])
        self.assertEqual(MailboxMessage.objects.count(), 0)

    def test_a_linked_copy_whose_marker_no_longer_matches_fails_and_downloads_nothing(self):
        from userapis.mailbox_correlation import identify_sent_message

        _, event, graph_id = self.sent_and_copied([("b.pdf", b"bbb")])
        identify_sent_message(event, actor=self.owner, reader=self.reader)       # links the copy to the proof
        self.reader.idents[graph_id] = ReplyIdentification(
            internet_message_id="<x@x>", conversation_id="c", send_marker="1.0000000000000000000",
        )
        self.reader.calls.clear()
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("failed", State.FAILED, "marker_mismatch"))
        self.assertEqual(self.reader.of("eml") + self.reader.of("attachment"), [])

    def test_without_the_right_to_read_the_mailbox_nothing_is_read(self):
        _, event, _ = self.sent_and_copied()
        proof = CorrespondenceSendProof.objects.get(event=event)
        with patch("requests.post", side_effect=AssertionError("no sendMail")), \
                patch("requests.get", side_effect=AssertionError("no Graph call")):
            result = archive_send_proof(proof.pk, actor=self.delegate)    # may not read the owner's personal mailbox
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("failed", State.FAILED, "not_authorized"))


class HistoricalEventTests(ArchiveBase):
    def strip_hashes(self, event):
        snapshot = [{k: v for k, v in e.items() if k != "sha256"} for e in event.attachments_snapshot]
        CorrespondenceSendEvent.objects.filter(pk=event.pk).update(attachments_snapshot=snapshot)

    def test_an_event_without_sha256_is_matched_by_name_and_size_at_a_reduced_level(self):
        _, event, _ = self.sent_and_copied([("letter.pdf", b"letter content")])
        self.strip_hashes(event)
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code, result.verification), (State.ARCHIVED, "reduced_verification", "reduced"))
        stored = CorrespondenceSendEvent.objects.get(pk=event.pk).attachments_snapshot
        self.assertNotIn("sha256", stored[0])           # no hash is ever invented for the historical event

    def test_an_historical_event_with_two_identical_looking_entries_is_ambiguous_never_verified(self):
        _, event, _ = self.sent_and_copied([("same.pdf", b"AAAA"), ("same.pdf", b"BBBB")])
        self.strip_hashes(event)
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code, result.verification), (State.PARTIAL, "attachment_ambiguous", "none"))

    def test_an_historical_event_whose_size_differs_is_a_divergence(self):
        _, event, graph_id = self.sent_and_copied([("letter.pdf", b"letter content")])
        self.strip_hashes(event)
        self.reader.attachments[graph_id][0]["content"] = b"a different length content"
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.PARTIAL, "attachment_hash_mismatch"))

    def test_a_name_alone_proves_nothing(self):
        expected = [{"id": 1, "file_name": "a.pdf", "file_size": 3, "sha256": sha(b"aaa")}]
        facts = [Fact("g1", "a.pdf", 3, sha(b"zzz"), True)]
        pairs, missing, unmatched, problems, _ = match_attachments(expected, facts)
        self.assertEqual(pairs, [])
        self.assertIn("attachment_hash_mismatch", problems)

    def test_missing_proofs_of_older_graph_sends_are_prepared_without_touching_anything(self):
        corr = self.out_with([("a.pdf", b"aaa")])
        first = CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now() - timedelta(days=3), subject="s",
            attachments_snapshot=[{"id": 1, "file_name": "a.pdf", "file_size": 3}],
        )
        second = CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now() - timedelta(days=1), subject="s",
        )
        alone = CorrespondenceSendEvent.objects.create(
            correspondence=self.out_with(), sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(),
        )
        smtp = CorrespondenceSendEvent.objects.create(
            correspondence=self.out_with(), sender=self.owner, provider="smtp", result=Result.SUCCESS, sent_at=timezone.now(),
        )
        # a single recorded copy for a correspondence that was sent twice
        meta = normalize_message(raw_message(1, id="AAMk-old", folder="sentitems"), "sentitems")
        from userapis.mailbox_messages import store_message_metadata

        row, _ = store_message_metadata(self.individual_mailbox, meta)
        MailboxMessage.objects.filter(pk=row.pk).update(correspondence=corr)
        snapshots = {e.pk: list(e.attachments_snapshot) for e in (first, second, alone, smtp)}
        with patch("requests.post", side_effect=AssertionError("no call")), patch("requests.get", side_effect=AssertionError("no call")):
            report = ensure_missing_send_proofs(limit=100, dry_run=False)
            again = ensure_missing_send_proofs(limit=100, dry_run=False)
        self.assertEqual(sorted(report.created), sorted([first.pk, second.pk, alone.pk]))
        self.assertEqual(sorted(report.uncertain_copy), sorted([first.pk, second.pk]))
        self.assertEqual(report.no_recorded_copy, [alone.pk])
        self.assertEqual((again.created, again.examined), ([], 0))
        self.assertFalse(CorrespondenceSendProof.objects.filter(event=smtp).exists())      # SMTP: no Sent Items copy
        for pk, snapshot in snapshots.items():
            self.assertEqual(CorrespondenceSendEvent.objects.get(pk=pk).attachments_snapshot, snapshot)
        self.assertEqual(CorrespondenceSendProofFile.objects.count() + EvidenceBlob.objects.count(), 0)

    def test_a_historical_graph_send_without_proof_cannot_be_deleted(self):
        corr = self.out_with()
        CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(),
        )
        self.assertFalse(CorrespondenceSendProof.objects.exists())
        with self.assertRaises(ValidationError):
            corr.delete()
        with self.assertRaises(ProtectedError), transaction.atomic():   # raised mid-cascade: a savepoint keeps the test usable
            Correspondence.objects.filter(pk=corr.pk).delete()
        self.assertTrue(Correspondence.objects.filter(pk=corr.pk).exists())

    def test_an_smtp_send_keeps_its_previous_behavior_for_a_cascading_delete(self):
        corr = self.out_with()
        CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="smtp", result=Result.SUCCESS, sent_at=timezone.now(),
        )
        Correspondence.objects.filter(pk=corr.pk).delete()            # reported as a limitation, not changed here
        self.assertFalse(Correspondence.objects.filter(pk=corr.pk).exists())


class ArchiveLimitsAndSafetyTests(ArchiveBase):
    def test_an_eml_over_the_limit_fails_without_keeping_anything(self):
        _, event, _ = self.sent_and_copied()
        with override_settings(MAIL_IMPORT_MAX_EML_BYTES=10):
            proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.FAILED, "eml_too_large"))
        self.assertEqual((CorrespondenceSendProofFile.objects.count(), self.on_disk()), (0, []))

    def test_an_attachment_over_the_size_limit_is_not_kept_and_the_proof_is_partial(self):
        _, event, graph_id = self.sent_and_copied([("big.pdf", b"x" * 100)])
        with patch("userapis.mailbox_preview.MAX_FILE_BYTES", 50):
            proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.PARTIAL, "file_too_large"))
        self.assertEqual(proof.files.filter(kind="attachment").count(), 0)
        self.assertEqual(self.reader.of("attachment"), [])          # declared too large: never downloaded

    def test_a_download_that_exceeds_the_cap_is_refused_whatever_was_declared(self):
        _, event, graph_id = self.sent_and_copied([("big.pdf", b"x" * 100)])
        self.reader.attachments[graph_id][0]["size"] = 10            # a lie: the real size is checked while streaming
        with patch("userapis.mailbox_preview.MAX_FILE_BYTES", 50):
            proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.PARTIAL, "file_too_large"))
        self.assertEqual(proof.files.filter(kind="attachment").count(), 0)

    def test_a_dangerous_attachment_is_never_kept(self):
        _, event, graph_id = self.sent_and_copied([("tool.exe", b"MZ\x90\x00 program")])
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.PARTIAL, "attachment_not_allowed"))
        self.assertEqual(proof.files.filter(kind="attachment").count(), 0)
        self.assertEqual(self.reader.of("attachment"), [])           # blocked by name before any download

    def test_a_corrupt_existing_blob_is_never_overwritten_and_the_proof_fails(self):
        corr = self.out_with([("letter.pdf", b"letter content")])
        first, m1, s1 = self.send(corr)
        self.copy(first, m1, s1)
        self.archive(first)
        blob = EvidenceBlob.objects.get(sha256=sha(b"letter content"))
        with open(blob.file.path, "wb") as handle:
            handle.write(b"tampered")
        corr.refresh_from_db()
        second, m2, s2 = self.send(corr)
        self.copy(second, m2, s2)
        proof, result = self.archive(second)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("failed", State.FAILED, "blob_corrupt"))
        with open(blob.file.path, "rb") as handle:
            self.assertEqual(handle.read(), b"tampered")             # left exactly as found
        self.assertEqual(EvidenceBlob.objects.filter(sha256=sha(b"letter content")).count(), 1)
        self.assertFalse(proof.files.filter(kind="attachment").exists())

    def test_a_missing_blob_file_is_also_corrupt_not_recreated_silently(self):
        corr = self.out_with([("letter.pdf", b"letter content")])
        first, m1, s1 = self.send(corr)
        self.copy(first, m1, s1)
        self.archive(first)
        blob = EvidenceBlob.objects.get(sha256=sha(b"letter content"))
        os.remove(blob.file.path)
        corr.refresh_from_db()
        second, m2, s2 = self.send(corr)
        self.copy(second, m2, s2)
        proof, _ = self.archive(second)
        proof.refresh_from_db()
        self.assertEqual((proof.state, proof.error_code), (State.FAILED, "blob_corrupt"))

    def test_an_interrupted_run_is_resumed_without_redoing_what_was_kept(self):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa"), ("b.pdf", b"bbb")])
        second_id = self.reader.attachments[graph_id][1]["graph_id"]
        self.reader.fail_download[(graph_id, second_id)] = MailboxReadError("temporary")
        proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("failed", State.FAILED, "temporary"))
        self.assertEqual(proof.files.count(), 2)                                # .eml and the first file stay
        self.reader.fail_download.clear()
        self.reader.calls.clear()
        _, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.files.count(), proof.attempts), ("archived", State.ARCHIVED, 3, 2))
        self.assertEqual(self.reader.of("eml"), [])
        self.assertEqual([c[2] for c in self.reader.of("attachment")], [second_id])
        self.assertEqual(len(self.on_disk()), 3)

    def test_a_storage_or_database_failure_removes_the_file_that_step_wrote_and_keeps_the_rest(self):
        _, event, _ = self.sent_and_copied([("a.pdf", b"aaa"), ("b.pdf", b"bbb")])
        real_create = CorrespondenceSendProofFile.objects.create
        calls = []

        def failing_create(**kwargs):
            calls.append(kwargs["kind"])
            if len(calls) == 3:
                raise RuntimeError("database went away")
            return real_create(**kwargs)

        with patch.object(CorrespondenceSendProofFile.objects, "create", side_effect=failing_create):
            proof, result = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual((result.outcome, proof.state, proof.error_code), ("failed", State.FAILED, "unexpected_error"))
        self.assertEqual(proof.files.count(), 2)
        # no orphan: every file on disk belongs to a blob row, and the failed step's file is gone
        self.assertEqual(sorted(b.file.name for b in EvidenceBlob.objects.all()),
                         sorted(f.replace(os.sep, "/") for f in self.on_disk()))
        self.assertEqual(EvidenceBlob.objects.count(), 2)

    def test_two_concurrent_runs_do_not_both_work(self):
        _, event, _ = self.sent_and_copied([("a.pdf", b"aaa")])
        proof = CorrespondenceSendProof.objects.get(event=event)
        other = connection.copy()
        self.other_sessions.append(other)
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, proof.pk])
            self.assertTrue(cursor.fetchone()[0])
        busy = archive_send_proof(proof.pk, actor=self.owner, reader=self.reader)
        self.assertEqual(busy.outcome, "busy")
        self.assertEqual(self.reader.calls, [])
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, proof.pk])
        _, done = self.archive(event)
        self.assertEqual(done.outcome, "archived")
        self.assertEqual(CorrespondenceSendProofFile.objects.filter(proof=proof).count(), 2)   # never twice

    def test_the_run_never_leaks_content_into_the_logs(self):
        corr = self.out_with([("secret-name.pdf", b"aaa")])
        corr.subject = SUBJECT_SENTINEL
        event, marker, sent = self.send(corr)
        graph_id = self.copy(event, marker, sent, graph_id=GRAPH_ID_SENTINEL)
        self.reader.eml[graph_id] = b"\x00junk"                       # a failing path, the one that logs
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("probe").debug("start")
            self.archive(event)
        text = "\n".join(captured.output)
        for sentinel in (SUBJECT_SENTINEL, GRAPH_ID_SENTINEL, "secret-name", "jane.doe@links-local.invalid", marker):
            self.assertNotIn(sentinel, text)

    def test_the_state_rule_is_stricter_than_the_models_own_check(self):
        # the model would accept ARCHIVED as soon as an .eml exists and a date is set;
        # the service never archives a proof whose attachments are not all kept and verified
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa"), ("b.pdf", b"bbb")])
        self.reader.attachments[graph_id] = self.reader.attachments[graph_id][:1]
        proof, _ = self.archive(event)
        proof.refresh_from_db()
        self.assertEqual(proof.files.filter(kind="eml").count(), 1)
        self.assertNotEqual(proof.state, State.ARCHIVED)
        self.assertIsNone(proof.archived_at)
