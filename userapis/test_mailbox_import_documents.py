"""MAIL-1.3D-C2-B3: keeping the text, the original .eml and the selected attachments of an imported
Outlook message. Graph is a fake that really streams; storage is a temporary directory; every write
verb to Outlook is patched to raise; nothing may be sent."""

import hashlib
import os
import time
from unittest.mock import patch

from django.core import mail
from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework.test import APIClient

from project_management.models import Correspondence, CorrespondenceAttachment, Document, DocumentRevision
from userapis import mailbox_import
from userapis import mailbox_preview as limits
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_preview import build_attachment_reference
from userapis.mailbox_read import normalize_message
from userapis.models import MailboxImportedFile, MailboxMessage, MailboxProjectAssignment, MailboxUserGrant
from userapis.test_mailbox_import import ImportBase, default_eml
from userapis.test_mailbox_read import SENTINEL_BODY

PDF = b"%PDF-1.4 a small but real looking document"


class DocsBase(ImportBase):
    def msg(self, n=1, folder="inbox", **over):
        over.setdefault("body", {"contentType": "text", "content": "Hello team,\r\nplease see the minutes."})
        over.setdefault("subject", f"Minutes {n}")
        return self.graph.add(n, folder=folder, **over)

    def att(self, raw, n, content=PDF, kind="file", **over):
        attachment_id = f"ATT-{raw['id']}-{n}"
        entry = {
            "@odata.type": f"#microsoft.graph.{kind}Attachment", "id": attachment_id, "name": f"document-{n}.pdf",
            "contentType": "application/pdf", "size": len(content), "isInline": False,
        }
        entry.update(over)
        self.graph.attachments.setdefault(raw["id"], []).append(entry)
        self.graph.attachment_bytes[attachment_id] = content
        return attachment_id

    def aref(self, user, raw, attachment_id, mailbox=None, folder="inbox"):
        return build_attachment_reference(user, mailbox or self.individual_mailbox, raw["id"], attachment_id, folder)

    def imp(self, user, raw, attachment_ids=(), mailbox=None, folder="inbox", **body):
        mailbox = mailbox or self.individual_mailbox
        refs = [self.aref(user, raw, a, mailbox, folder) for a in attachment_ids]
        if refs:
            body["attachments"] = refs
        return self.do(user, self.reference(user, raw, mailbox, folder), **body)

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.media) for d, _, names in os.walk(self.media) for f in names)

    def corr_of(self, response):
        return Correspondence.objects.get(pk=response.data["correspondence"]["id"])

    def legacy_import(self, n=1, text="", mailbox=None):
        """A message imported the way C2-A did: correspondence and state, no text, no files."""
        raw = self.msg(n)
        corr = self.make_in(message=text)
        row, _ = store_message_metadata(mailbox or self.individual_mailbox, normalize_message(raw, "inbox"))
        MailboxMessage.objects.filter(pk=row.pk).update(state="imported", imported_correspondence=corr, imported_by=self.owner)
        return raw, corr, row

    def nothing_was_written(self):
        self.assertEqual((Correspondence.objects.count(), MailboxImportedFile.objects.count(), CorrespondenceAttachment.objects.count()), (0, 0, 0))
        self.assertEqual(MailboxMessage.objects.filter(state="imported").count(), 0)
        self.assertEqual(self.files(), [])


class CreateWithContentTests(DocsBase):
    def test_the_text_and_the_original_message_are_kept_and_traced(self):
        raw = self.msg()
        before = (Document.objects.count(), DocumentRevision.objects.count())
        response = self.imp(self.owner, raw)
        self.assertEqual(response.status_code, 201, response.data)
        corr = self.corr_of(response)
        self.assertEqual(corr.message, "Hello team,\nplease see the minutes.")            # plain text, line endings normalised
        kept = list(corr.attachments.all())
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].file_name.endswith(".eml"))
        content = default_eml(raw)
        self.assertEqual((kept[0].file_size, kept[0].uploaded_by_id), (len(content), self.owner.pk))
        with kept[0].file.open("rb") as handle:
            self.assertEqual(handle.read(), content)
        source = MailboxImportedFile.objects.get()
        self.assertEqual((source.kind, source.correspondence_id, source.correspondence_attachment_id), ("original_eml", corr.pk, kept[0].pk))
        self.assertEqual((source.sha256, source.file_size, source.content_type, source.graph_attachment_id),
                         (hashlib.sha256(content).hexdigest(), len(content), "message/rfc822", ""))
        self.assertEqual(source.mailbox_message.imported_correspondence_id, corr.pk)
        self.assertEqual(response.data["documents"], {"text_stored": True, "eml_stored": True, "attachments_stored": 0, "skipped": []})
        self.assertEqual((Document.objects.count(), DocumentRevision.objects.count()), before)   # never a controlled document
        self.assertEqual(mail.outbox, [])

    def test_the_old_answer_shape_is_kept(self):
        data = self.imp(self.owner, self.msg()).data
        for key in ("result", "correspondence", "message"):
            self.assertIn(key, data)
        self.assertEqual(set(data["correspondence"]), {"id", "direction", "reference", "project", "status"})

    def test_the_text_is_cut_at_100_kb_on_a_character_boundary_but_the_eml_stays_whole(self):
        raw = self.msg(body={"contentType": "text", "content": "é" * 70000})            # 140 000 bytes
        corr = self.corr_of(self.imp(self.owner, raw))
        self.assertLessEqual(len(corr.message.encode("utf-8")), 100 * 1024)
        self.assertEqual(corr.message, "é" * len(corr.message))
        self.assertEqual(corr.attachments.get().file_size, len(default_eml(raw)))

    def test_html_is_never_stored_and_nothing_external_is_loaded(self):
        raw = self.msg(body={"contentType": "html", "content": '<p>Hi</p><img src="https://t.example.com/p.gif"><script>x()</script>'})
        corr = self.corr_of(self.imp(self.owner, raw))
        self.assertEqual(corr.message, "Hi")
        self.assertTrue(all(c["url"].startswith("https://graph.microsoft.com") for c in self.graph.calls))

    def test_the_http_content_type_of_the_eml_is_not_trusted(self):
        for kind in ("text/plain", "application/octet-stream", "text/html", ""):
            self.graph.eml_content_type = kind
            self.assertEqual(self.imp(self.owner, self.msg(len(kind) + 1)).status_code, 201, kind)

    def test_an_invalid_or_empty_original_refuses_the_import(self):
        for n, stream in enumerate(({"data": b"this is not a message\x00\x01\x02"}, {"data": b"Subject: only\r\n\r\nno from"}, {"data": b""}), start=1):
            raw = self.msg(n)
            self.graph.stream_overrides[raw["id"]] = stream
            response = self.imp(self.owner, raw)
            self.assertEqual((response.status_code, response.data["code"]), (502, "invalid_eml"), stream)
        self.nothing_was_written()

    def test_an_original_over_the_limit_is_refused_not_truncated_and_the_stream_stops_early(self):
        raw = self.msg()
        self.graph.stream_overrides[raw["id"]] = {"size": 400000}
        with override_settings(MAIL_IMPORT_MAX_EML_BYTES=1000):
            response = self.imp(self.owner, raw)
        self.assertEqual((response.status_code, response.data["code"]), (413, "eml_too_large"))
        self.assertLess(self.graph.streams[-1].served, 400000)
        self.assertTrue(self.graph.streams[-1].closed)
        self.nothing_was_written()

    def test_no_switch_can_turn_the_original_off(self):
        corr = self.corr_of(self.imp(self.owner, self.msg(), keep_eml=False, include_eml=False))
        self.assertEqual(MailboxImportedFile.objects.filter(kind="original_eml", correspondence=corr).count(), 1)


class AttachmentTests(DocsBase):
    def test_several_selected_attachments_are_kept_with_their_real_size_hash_and_type(self):
        raw = self.msg()
        contents = {1: PDF, 2: b"\x89PNG\r\n\x1a\n image bytes", 3: b"PK\x03\x04 zip bytes"}
        ids = {n: self.att(raw, n, c, size=len(c) + 500) for n, c in contents.items()}      # Graph overstates the size
        response = self.imp(self.owner, raw, [ids[1], ids[2], ids[3]])
        self.assertEqual(response.status_code, 201, response.data)
        corr = self.corr_of(response)
        self.assertEqual(corr.attachments.count(), 4)                                      # 3 files + the original
        self.assertEqual(response.data["documents"]["attachments_stored"], 3)
        for n, content in contents.items():
            row = MailboxImportedFile.objects.get(graph_attachment_id=ids[n])
            self.assertEqual((row.kind, row.file_size, row.sha256, row.content_type), ("attachment", len(content), hashlib.sha256(content).hexdigest(), "application/pdf"))
            self.assertEqual(row.correspondence_attachment.file_size, len(content))        # the real size, not Graph's
            self.assertEqual(row.mailbox_message.graph_message_id, raw["id"])

    def test_only_the_selected_files_are_ever_downloaded(self):
        raw = self.msg()
        wanted, ignored = self.att(raw, 1), self.att(raw, 2)
        self.imp(self.owner, raw, [wanted])
        fetched = [c["url"] for c in self.graph.calls if c["stream"]]
        self.assertTrue(any(wanted in u for u in fetched))
        self.assertFalse(any(ignored in u for u in fetched))
        self.assertEqual(MailboxImportedFile.objects.filter(kind="attachment").count(), 1)

    def test_no_selection_means_no_attachment_but_still_the_original(self):
        raw = self.msg()
        self.att(raw, 1)
        corr = self.corr_of(self.imp(self.owner, raw))
        self.assertEqual([i.kind for i in MailboxImportedFile.objects.all()], ["original_eml"])
        self.assertEqual(corr.attachments.count(), 1)

    def test_attaching_to_an_in_or_out_keeps_the_files_and_never_overwrites_text(self):
        inbound = self.make_in(message="written by hand")
        raw = self.msg(1)
        a = self.att(raw, 1)
        self.assertEqual(self.imp(self.owner, raw, [a], mode="attach", target_correspondence=inbound.pk).status_code, 201)
        inbound.refresh_from_db()
        self.assertEqual(inbound.message, "written by hand")
        self.assertEqual(inbound.attachments.count(), 2)
        # an empty IN gets the text, an OUT never has its message touched
        empty_in = self.make_in(message="", external_reference="EXT-2")
        self.assertEqual(self.imp(self.owner, self.msg(2), mode="attach", target_correspondence=empty_in.pk).status_code, 201)
        empty_in.refresh_from_db()
        self.assertTrue(empty_in.message.startswith("Hello team"))
        out = self.make_out(message="")
        sent = self.msg(3, folder="sentitems")
        self.assertEqual(self.imp(self.owner, sent, folder="sentitems", mode="attach", target_correspondence=out.pk).status_code, 201)
        out.refresh_from_db()
        self.assertEqual((out.message, out.attachments.count()), ("", 1))

    def test_a_correspondence_locked_by_its_workflow_still_takes_the_source_files_of_a_sent_message(self):
        # MAIL-1.3D-SENT-ITEMS-ATTACH: the lock protects what was approved, not the provenance of a mail
        from project_management.models import CorrespondenceValidationWorkflow

        out = self.make_out()
        CorrespondenceValidationWorkflow.objects.create(
            correspondence=out, workflow_type="ONE_STEP", status="APPROVED",
            author=self.owner, approver=self.owner, started_by=self.owner,
        )
        sent = self.msg(1, folder="sentitems")
        response = self.imp(self.owner, sent, folder="sentitems", mode="attach", target_correspondence=out.pk)
        self.assertEqual((response.status_code, response.data["result"]), (201, "attached"))
        self.assertEqual(MailboxImportedFile.objects.filter(correspondence=out).count(), 1)     # the original .eml

    def test_references_that_are_forged_expired_foreign_or_crossed_are_refused_with_the_index(self):
        raw, other = self.msg(1), self.msg(2)
        good, elsewhere = self.att(raw, 1), self.att(other, 1)
        valid = self.aref(self.owner, raw, good)
        with patch("django.core.signing.time.time", return_value=time.time() - 3600):
            expired = self.aref(self.owner, raw, good)
        cases = {
            "altered": valid[:-4] + "AAAA", "expired": expired, "garbage": "garbage",
            "another user": self.aref(self.approver, raw, good),
            "another message": self.aref(self.owner, other, elsewhere),
            "another mailbox": self.aref(self.owner, raw, good, self.shared_mailbox),
            "another folder": self.aref(self.owner, raw, good, folder="sentitems"),
        }
        for label, token in cases.items():
            response = self.do(self.owner, self.reference(self.owner, raw), attachments=[valid, token])
            self.assertEqual((response.status_code, response.data["code"], response.data["index"]), (400, "invalid_attachment_reference", 1), label)
        self.nothing_was_written()

    def test_only_importable_file_attachments_are_accepted(self):
        for kind, extra, reason in (
            ("item", {}, "item_attachment_not_supported"), ("reference", {}, "reference_attachment_not_supported"),
            ("file", {"isInline": True}, "inline_image"), ("file", {"size": 0}, "empty_file"),
            ("file", {"@odata.type": "#microsoft.graph.weird"}, "unknown_attachment_type"),
        ):
            self.graph.items.clear()
            raw = self.msg()
            aid = self.att(raw, 1, kind=kind, **extra)
            response = self.imp(self.owner, raw, [aid])
            self.assertEqual((response.status_code, response.data["code"], response.data["reason"]), (400, "attachment_not_importable", reason), (kind, extra))
        self.nothing_was_written()

    def test_an_attachment_that_vanished_from_outlook_refuses_the_import(self):
        raw = self.msg()
        aid = self.att(raw, 1)
        ref = self.aref(self.owner, raw, aid)
        self.graph.attachments[raw["id"]] = []
        response = self.do(self.owner, self.reference(self.owner, raw), attachments=[ref])
        self.assertEqual((response.status_code, response.data["code"]), (404, "attachment_gone"))
        self.nothing_was_written()

    def test_a_message_deleted_or_moved_after_the_preview_is_gone(self):
        raw = self.msg()
        aid = self.att(raw, 1)
        self.graph.items[raw["id"]]["parentFolderId"] = "FOLDER-ARCHIVE"
        response = self.imp(self.owner, raw, [aid])
        self.assertEqual((response.status_code, response.data["code"]), (404, "message_gone"))
        self.nothing_was_written()


class LimitTests(DocsBase):
    def test_the_number_of_attachments_is_limited_before_anything_is_read(self):
        raw = self.msg()
        ids = [self.att(raw, n) for n in range(1, 4)]
        with patch.object(limits, "MAX_FILES", 2):
            response = self.imp(self.owner, raw, ids)
        self.assertEqual((response.status_code, response.data["code"]), (400, "too_many_attachments"))
        self.assertEqual(self.graph.calls, [])
        # and the API itself never accepts more than the limit
        many = [self.aref(self.owner, raw, ids[0])] * (limits.MAX_FILES + 1)
        self.assertEqual(self.do(self.owner, self.reference(self.owner, raw), attachments=many).status_code, 400)

    def test_the_declared_size_cap_has_no_tolerance(self):
        raw = self.msg()
        over = self.att(raw, 1, size=limits.MAX_FILE_BYTES + 1)
        exact = self.att(raw, 2, size=limits.MAX_FILE_BYTES)
        response = self.imp(self.owner, raw, [over])
        self.assertEqual((response.data["code"], response.data["reason"]), ("attachment_not_importable", "file_too_large"))
        with patch.object(limits, "MAX_FILE_BYTES", 1000):
            self.assertEqual(self.imp(self.owner, self.msg(5), []).status_code, 201)
        self.assertEqual(self.imp(self.owner, raw, [exact]).status_code, 201)             # declared exactly at the cap is fine

    def test_the_real_size_is_enforced_while_streaming_whatever_graph_announced(self):
        raw = self.msg()
        aid = self.att(raw, 1, b"small", size=500)                                         # announced small...
        self.graph.stream_overrides[aid] = {"size": 5_000_000}                              # ...but really huge
        with patch.object(limits, "MAX_FILE_BYTES", 1000):
            response = self.imp(self.owner, raw, [aid])
        self.assertEqual((response.status_code, response.data["code"]), (413, "file_too_large"))
        self.assertLess(self.graph.streams[-1].served, 5_000_000)
        self.assertTrue(self.graph.streams[-1].closed)
        self.nothing_was_written()

    def test_the_total_includes_the_original_message(self):
        raw = self.msg()
        ids = [self.att(raw, n, b"%PDF " + bytes([n]) * 200) for n in (1, 2)]
        eml_size = len(default_eml(raw))
        with patch.object(limits, "MAX_TOTAL_BYTES", eml_size + 300):                      # room for one file, not two
            response = self.imp(self.owner, raw, ids)
        self.assertEqual((response.status_code, response.data["code"]), (413, "import_too_large"))
        self.nothing_was_written()
        with patch.object(limits, "MAX_TOTAL_BYTES", eml_size + 500):
            self.assertEqual(self.imp(self.owner, raw, ids).status_code, 201)

    def test_a_real_size_smaller_or_bigger_than_announced_is_accepted_within_the_caps(self):
        raw = self.msg()
        smaller = self.att(raw, 1, b"%PDF-1 small", size=9000)
        bigger = self.att(raw, 2, b"%PDF-2 " + b"b" * 400, size=100)
        response = self.imp(self.owner, raw, [smaller, bigger])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(sorted(MailboxImportedFile.objects.filter(kind="attachment").values_list("file_size", flat=True)), [len(b"%PDF-1 small"), 407])


class SecurityTests(DocsBase):
    def refused(self, name, content=PDF, content_type="application/pdf"):
        self.graph.items.clear()
        raw = self.msg()
        aid = self.att(raw, 1, content, name=name, contentType=content_type)
        response = self.imp(self.owner, raw, [aid])
        return response.status_code, response.data.get("code"), response.data.get("reason")

    def test_dangerous_or_active_formats_are_refused(self):
        for name in ("setup.exe", "run.BAT", "script.js", "macro.docm", "sheet.xlsm", "page.html", "image.svg",
                     "invoice.pdf.exe", "evil.exe.", "invoice\u202egpj.exe", "tool.ps1", "shortcut.lnk", "installer.msi"):
            self.assertEqual(self.refused(name), (400, "attachment_not_allowed", "blocked_extension"), name)
        self.nothing_was_written()

    def test_paths_are_refused(self):
        for name in ("../escape.pdf", "a/b.pdf", "a\\b.pdf", "C:\\x.pdf", "/etc/passwd"):
            self.assertEqual(self.refused(name), (400, "attachment_not_allowed", "unsafe_file_name"), name)

    def test_the_declared_type_and_the_real_content_are_both_checked(self):
        self.assertEqual(self.refused("fine.pdf", content_type="application/x-msdownload"), (400, "attachment_not_allowed", "blocked_content_type"))
        self.assertEqual(self.refused("fine.pdf", content_type="text/html"), (400, "attachment_not_allowed", "blocked_content_type"))
        for program in (b"MZ\x90\x00 pe", b"\x7fELF elf", b"#!/bin/sh\nrm -rf", b"\xca\xfe\xba\xbe class"):
            self.assertEqual(self.refused("harmless.pdf", program), (400, "attachment_not_allowed", "blocked_content"), program)
        self.nothing_was_written()

    def test_an_empty_download_is_refused(self):
        self.graph.items.clear()
        raw = self.msg()
        aid = self.att(raw, 1, PDF)
        self.graph.stream_overrides[aid] = {"data": b""}
        response = self.imp(self.owner, raw, [aid])
        self.assertEqual((response.status_code, response.data["reason"]), (400, "empty_file"))

    def test_ordinary_files_pass_and_names_are_cleaned(self):
        raw = self.msg()
        ids = [self.att(raw, 1, name="a b.pdf"), self.att(raw, 2, b"\xff\xd8\xff photo", name="photo.JPG", contentType="image/jpeg"),
               self.att(raw, 3, b"PK\x03\x04 z", name="data.zip", contentType="application/zip"), self.att(raw, 4, b"plain", name="tab\there  .txt", contentType="text/plain"),
               self.att(raw, 5, b"no extension", name="README", contentType="text/plain")]
        response = self.imp(self.owner, raw, ids)
        self.assertEqual(response.status_code, 201, response.data)
        names = sorted(MailboxImportedFile.objects.filter(kind="attachment").values_list("file_name", flat=True))
        self.assertEqual(names, ["README", "a b.pdf", "data.zip", "photo.JPG", "tabhere .txt"])
        self.assertTrue(all(os.sep not in n and ".." not in n for n in names))

    def test_a_malicious_subject_makes_a_safe_eml_name(self):
        raw = self.msg(subject="../../etc/passwd: <x>|*?")
        corr = self.corr_of(self.imp(self.owner, raw))
        name = corr.attachments.get().file_name
        self.assertTrue(name.endswith(".eml"))
        self.assertFalse(any(c in name for c in '/\\:*?"<>|'))

    def test_files_are_only_ever_served_as_attachments(self):
        """The existing download method, called directly (its HTTP route also runs a licence check
        that needs tables this test database does not have)."""
        from rest_framework.exceptions import PermissionDenied
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory
        from project_management.views.correspondence import CorrespondenceAttachmentViewSet

        corr = self.corr_of(self.imp(self.owner, self.msg()))
        attachment = corr.attachments.get()

        def download(user):
            request = Request(APIRequestFactory().get("/"))
            request.user = user
            viewset = CorrespondenceAttachmentViewSet()
            viewset.request = request
            return viewset.download(request, correspondence_id=corr.pk, attachment_id=attachment.pk)

        response = download(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        response.file_to_stream.close()          # (response.close() would fire request_finished and drop the test's DB connection)
        with self.assertRaises(PermissionDenied):                                          # normal correspondence permissions apply
            download(self.approver)


class DuplicateTests(DocsBase):
    def test_identical_content_is_kept_once_and_reported(self):
        raw = self.msg()
        a, b = self.att(raw, 1, PDF), self.att(raw, 2, PDF, name="copy.pdf")
        response = self.imp(self.owner, raw, [a, b])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["documents"]["attachments_stored"], 1)
        self.assertEqual(response.data["documents"]["skipped"], [{"name": "copy.pdf", "reason": "duplicate_content"}])
        self.assertEqual(MailboxImportedFile.objects.filter(kind="attachment").count(), 1)

    def test_the_database_itself_refuses_the_three_kinds_of_duplicate(self):
        raw = self.msg()
        corr = self.corr_of(self.imp(self.owner, raw, [self.att(raw, 1)]))
        first = MailboxImportedFile.objects.get(kind="attachment")
        spare = CorrespondenceAttachment.objects.create(correspondence=corr, file_name="x", file_size=1, file="x")
        def clone(**over):
            fields = dict(mailbox_message=first.mailbox_message, correspondence=corr, correspondence_attachment=spare, kind="attachment",
                          graph_attachment_id="OTHER", file_name="x", file_size=1, sha256="f" * 64)
            fields.update(over)
            with self.assertRaises(IntegrityError), transaction.atomic():
                MailboxImportedFile.objects.create(**fields)
        clone(graph_attachment_id=first.graph_attachment_id)                 # same message + same Graph attachment
        clone(sha256=first.sha256)                                           # same content in the same correspondence
        clone(kind="original_eml", sha256="e" * 64)                          # a second original for the same message

    def test_a_retry_of_the_same_import_adds_nothing(self):
        raw = self.msg()
        aid = self.att(raw, 1)
        first = self.imp(self.owner, raw, [aid])
        streams = len(self.graph.streams)
        again = self.imp(self.owner, raw, [aid])
        self.assertEqual((again.status_code, again.data["result"]), (200, "already_imported"))
        self.assertEqual((len(self.graph.streams), MailboxImportedFile.objects.count(), CorrespondenceAttachment.objects.count()), (streams, 2, 2))
        self.assertEqual(again.data["correspondence"]["id"], first.data["correspondence"]["id"])

    def test_a_concurrent_identical_import_stores_the_files_once(self):
        raw = self.msg()
        aid = self.att(raw, 1)

        def competing_request():
            self._post_inner(raw, aid)

        self.graph.on_message_get = competing_request
        response = self.imp(self.owner, raw, [aid])
        self.assertEqual((response.status_code, response.data["result"]), (200, "already_imported"))
        self.assertEqual(Correspondence.objects.count(), 1)
        self.assertEqual(sorted(MailboxImportedFile.objects.values_list("kind", flat=True)), ["attachment", "original_eml"])
        self.assertEqual(len(self.files()), 2)

    def _post_inner(self, raw, aid):
        other = APIClient()
        other.force_authenticate(self.owner)
        response = other.post("/api/user/mailbox-import/", {
            "reference": self.reference(self.owner, raw), "project": self.project.pk, "mode": "create",
            "external_reference": "EXT-RACE", "attachments": [self.aref(self.owner, raw, aid)],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)


class CompletionTests(DocsBase):
    def complete(self, user, raw, attachment_ids=(), mailbox=None, **body):
        refs = [self.aref(user, raw, a, mailbox) for a in attachment_ids]
        self.client.force_authenticate(user)
        payload = {"reference": self.reference(user, raw, mailbox), "project": self.project.pk, "mode": "complete", **body}
        if refs:
            payload["attachments"] = refs
        return self.client.post("/api/user/mailbox-import/", payload, format="json")

    def test_an_earlier_import_gets_its_text_its_original_and_more_files(self):
        raw, corr, row = self.legacy_import()
        a, b = self.att(raw, 1), self.att(raw, 2, b"%PDF second")
        count = Correspondence.objects.count()
        response = self.complete(self.owner, raw, [a])
        self.assertEqual((response.status_code, response.data["result"]), (200, "completed"))
        corr.refresh_from_db()
        self.assertTrue(corr.message.startswith("Hello team"))
        self.assertEqual(sorted(MailboxImportedFile.objects.values_list("kind", flat=True)), ["attachment", "original_eml"])
        self.assertEqual(response.data["documents"], {"text_stored": True, "eml_stored": True, "attachments_stored": 1, "skipped": []})
        # a later completion adds a further file; the correspondence is never recreated
        again = self.complete(self.owner, raw, [a, b])
        self.assertEqual(again.data["documents"]["attachments_stored"], 1)
        self.assertEqual(again.data["documents"]["skipped"], [{"name": "document-1.pdf", "reason": "already_present"}])
        self.assertEqual((Correspondence.objects.count(), corr.attachments.count()), (count, 3))
        row.refresh_from_db()
        self.assertEqual((row.state, row.imported_correspondence_id), ("imported", corr.pk))

    def test_a_completion_is_idempotent_and_needs_no_graph_when_nothing_is_missing(self):
        raw, corr, _ = self.legacy_import()
        self.complete(self.owner, raw)
        calls = len(self.graph.calls)
        again = self.complete(self.owner, raw)
        self.assertEqual((again.status_code, again.data["result"]), (200, "already_complete"))
        self.assertEqual((len(self.graph.calls), corr.attachments.count()), (calls, 1))

    def test_existing_text_is_never_overwritten(self):
        raw, corr, _ = self.legacy_import(text="the user's own notes")
        self.assertEqual(self.complete(self.owner, raw).status_code, 200)
        corr.refresh_from_db()
        self.assertEqual(corr.message, "the user's own notes")
        self.assertEqual(corr.attachments.count(), 1)                                       # the original was still added

    def test_content_already_in_the_correspondence_is_not_added_again(self):
        raw, corr, _ = self.legacy_import()
        a = self.att(raw, 1, PDF)
        self.complete(self.owner, raw, [a])
        other = self.att(raw, 2, PDF, name="same-bytes.pdf")
        response = self.complete(self.owner, raw, [other])
        self.assertEqual(response.data["documents"]["skipped"], [{"name": "same-bytes.pdf", "reason": "duplicate_content"}])
        self.assertEqual(corr.attachments.count(), 2)

    def test_only_an_imported_message_can_be_completed_and_only_into_its_own_project(self):
        raw = self.msg()
        self.assertEqual((self.complete(self.owner, raw).status_code, self.complete(self.owner, raw).data["code"]), (409, "not_imported"))
        raw2, corr, _ = self.legacy_import(2)
        MailboxProjectAssignment.objects.create(mailbox=self.individual_mailbox, project=self.project_b)
        self.grant_project(self.owner, ("view", "create", "update"), self.project_b)
        self.client.force_authenticate(self.owner)
        response = self.client.post("/api/user/mailbox-import/", {
            "reference": self.reference(self.owner, raw2), "project": self.project_b.pk, "mode": "complete"}, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (400, "invalid_target"))
        # the target is never chosen by the client
        bad = self.complete(self.owner, raw2, target_correspondence=corr.pk)
        self.assertEqual(bad.status_code, 400)

    def test_permissions_are_enforced(self):
        raw, corr, _ = self.legacy_import(3, mailbox=self.shared_mailbox)
        self.grant_project(self.reader_only, ("view", "create"))                           # may read the box and create, not update
        response = self.complete(self.reader_only, raw, mailbox=self.shared_mailbox)
        self.assertEqual((response.status_code, response.data["code"]), (403, "correspondence_permission_denied"))
        self.assertEqual(self.complete(self.approver, raw, mailbox=self.shared_mailbox).status_code, 403)
        self.assertEqual(MailboxImportedFile.objects.count(), 0)

    def test_two_completions_at_once_keep_one_original(self):
        raw, corr, _ = self.legacy_import()

        def competing_request():
            other = APIClient()
            other.force_authenticate(self.owner)
            self.assertEqual(other.post("/api/user/mailbox-import/", {
                "reference": self.reference(self.owner, raw), "project": self.project.pk, "mode": "complete"}, format="json").status_code, 200)

        self.graph.on_message_get = competing_request
        self.assertEqual(self.complete(self.owner, raw).status_code, 200)
        self.assertEqual((MailboxImportedFile.objects.filter(kind="original_eml").count(), corr.attachments.count(), len(self.files())), (1, 1, 1))


class FailureTests(DocsBase):
    def test_a_failed_download_leaves_nothing_and_the_retry_works(self):
        raw = self.msg()
        a, b = self.att(raw, 1), self.att(raw, 2, b"%PDF second")
        self.graph.stream_overrides[b] = {"status": 500}
        response = self.imp(self.owner, raw, [a, b])
        self.assertEqual((response.status_code, response.data["code"]), (503, "temporary"))
        self.nothing_was_written()
        self.graph.stream_overrides.clear()
        retry = self.imp(self.owner, raw, [a, b])
        self.assertEqual(retry.status_code, 201)
        self.assertEqual((Correspondence.objects.count(), MailboxImportedFile.objects.count(), len(self.files())), (1, 3, 3))

    def test_a_storage_failure_rolls_back_and_removes_the_files_of_the_attempt(self):
        raw = self.msg()
        ids = [self.att(raw, 1), self.att(raw, 2, b"%PDF second")]
        original, calls = mailbox_import._store_file, {"n": 0}

        def failing(correspondence, row, user, item, stored):
            calls["n"] += 1
            original(correspondence, row, user, item, stored)         # the file IS written...
            if calls["n"] == 2:
                raise OSError("disk full")                            # ...then the second one fails

        with patch.object(mailbox_import, "_store_file", failing):
            with self.assertRaises(OSError):
                self.imp(self.owner, raw, ids)
        self.assertEqual(calls["n"], 2)
        self.nothing_was_written()                                    # database rolled back, both files deleted
        self.assertEqual(self.imp(self.owner, raw, ids).status_code, 201)       # and the retry is clean
        self.assertEqual(len(self.files()), 3)

    def test_an_error_after_the_files_are_written_also_cleans_them(self):
        raw = self.msg()
        with patch.object(mailbox_import.MailboxImportedFile.objects, "create", side_effect=IntegrityError("mailbox_imported_file x")):
            response = self.imp(self.owner, raw)
        self.assertEqual((response.status_code, response.data["code"]), (409, "duplicate_file"))
        self.nothing_was_written()

    def test_a_graph_rate_limit_stops_everything(self):
        raw = self.msg()
        self.graph.stream_overrides[raw["id"]] = {"status": 429, "headers": {"Retry-After": "5"}}
        response = self.imp(self.owner, raw)
        self.assertEqual((response.status_code, response.data["code"]), (429, "rate_limited"))
        self.nothing_was_written()


class AuthorizationTests(DocsBase):
    def test_no_correspondence_right_means_no_graph_read_at_all(self):
        raw = self.msg()
        aid = self.att(raw, 1)
        response = self.imp(self.reader_only, raw, [aid], mailbox=self.shared_mailbox)
        self.assertEqual((response.status_code, response.data["code"]), (403, "correspondence_permission_denied"))
        self.assertEqual(self.graph.calls, [])

    def test_a_revoked_mailbox_right_stops_the_import(self):
        raw = self.msg()
        aid = self.att(raw, 1)
        ref = self.reference(self.delegate, raw, self.shared_mailbox)
        attachment = self.aref(self.delegate, raw, aid, self.shared_mailbox)
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.delegate).update(can_read=False)
        response = self.do(self.delegate, ref, attachments=[attachment])
        self.assertEqual((response.status_code, response.data["code"]), (403, "not_authorized"))
        self.nothing_was_written()

    def test_a_personal_mailbox_stays_closed_to_others_even_with_valid_looking_references(self):
        raw = self.msg()
        aid = self.att(raw, 1)
        for user in (self.approver, self.delegate, self.reader_only, self.foreign):
            response = self.do(user, self.reference(user, raw), attachments=[self.aref(user, raw, aid)])
            self.assertEqual(response.status_code, 403, user.email)
        self.assertEqual(self.graph.calls, [])
        self.nothing_was_written()

    def test_nothing_is_written_to_outlook_and_nothing_is_sent(self):
        raw = self.msg()
        self.imp(self.owner, raw, [self.att(raw, 1)])
        # every write verb is patched to raise in the base class: reaching this line proves none was used
        self.assertEqual(mail.outbox, [])
        self.assertTrue(all(c["url"].startswith("https://graph.microsoft.com") for c in self.graph.calls))
        self.assertEqual(DocumentRevision.objects.count(), 0)


class SentItemsAttachToLockedOutTests(DocsBase):
    """MAIL-1.3D-SENT-ITEMS-ATTACH: keeping the copy of an e-mail LINKS did not send itself (or sent
    long ago) on an OUT, whatever its validation state. The files are provenance of that mail; the
    approved content, the workflow, the send history and the send proofs are never touched."""

    def locked_out(self, **over):
        from project_management.models import CorrespondenceValidationWorkflow

        out = self.make_out(**over)
        CorrespondenceValidationWorkflow.objects.create(
            correspondence=out, workflow_type="ONE_STEP", status="APPROVED",
            author=self.owner, approver=self.owner, started_by=self.owner,
        )
        return out

    def untouched(self, out):
        from project_management.models import (
            CorrespondenceSendEvent, CorrespondenceSendProof, CorrespondenceValidationWorkflow,
        )

        return (
            Correspondence.objects.filter(pk=out.pk).values().get(),
            list(CorrespondenceValidationWorkflow.objects.filter(correspondence=out).values()),
            CorrespondenceSendEvent.objects.count(), CorrespondenceSendProof.objects.count(),
            Document.objects.count(), DocumentRevision.objects.count(),
        )

    def sent_with_pdf(self, n=1):
        raw = self.msg(n, folder="sentitems")
        return raw, self.att(raw, 1, content=PDF)

    def test_the_eml_and_a_pdf_are_kept_on_an_approved_locked_out_and_nothing_of_it_changes(self):
        out = self.locked_out(message="Approved text")
        before = self.untouched(out)
        raw, attachment = self.sent_with_pdf()
        response = self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=out.pk)
        self.assertEqual((response.status_code, response.data["result"]), (201, "attached"))
        self.assertEqual((response.data["documents"]["eml_stored"], response.data["documents"]["attachments_stored"]), (True, 1))
        files = MailboxImportedFile.objects.filter(correspondence=out)
        self.assertEqual(sorted(f.kind for f in files), ["attachment", "original_eml"])
        self.assertIn(hashlib.sha256(PDF).hexdigest(), {f.sha256 for f in files})
        self.assertEqual(out.attachments.count(), 2)
        self.assertEqual(self.untouched(out), before)                                  # content, workflow, sends, proofs, documents
        self.assertEqual(mail.outbox, [])
        row = MailboxMessage.objects.get(graph_message_id=raw["id"])
        self.assertEqual((row.imported_correspondence_id, row.folder, row.state), (out.pk, "sentitems", "imported"))

    def test_an_unlocked_out_takes_the_same_files(self):
        out = self.make_out()
        raw, attachment = self.sent_with_pdf()
        response = self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=out.pk)
        self.assertEqual((response.status_code, response.data["documents"]["attachments_stored"]), (201, 1))
        self.assertEqual(out.attachments.count(), 2)

    def test_importing_the_same_message_again_creates_nothing_more(self):
        out = self.locked_out()
        raw, attachment = self.sent_with_pdf()
        self.assertEqual(self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=out.pk).status_code, 201)
        before = (self.untouched(out), CorrespondenceAttachment.objects.count(), MailboxImportedFile.objects.count())
        again = self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=out.pk)
        self.assertEqual((again.status_code, again.data["result"]), (200, "already_imported"))
        self.assertEqual((self.untouched(out), CorrespondenceAttachment.objects.count(), MailboxImportedFile.objects.count()), before)

    def test_another_project_another_user_or_no_permission_is_refused_and_stores_nothing(self):
        raw, attachment = self.sent_with_pdf()
        other_project_out = self.locked_out(project=self.project_b, reference="PRJ-OUT-B")
        refused = self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=other_project_out.pk)
        self.assertEqual((refused.status_code, refused.data["code"]), (400, "invalid_target"))
        out = self.locked_out(reference="PRJ-OUT-2")
        self.grant_project(self.reader_only, ("view", "create"))                       # may create, not modify
        shared_raw = self.msg(2, folder="sentitems")
        response = self.imp(self.reader_only, shared_raw, folder="sentitems", mode="attach", mailbox=self.shared_mailbox, target_correspondence=out.pk)
        self.assertEqual((response.status_code, response.data["code"]), (403, "correspondence_permission_denied"))
        # a reference issued to someone else is never usable by another person or company
        reference = self.reference(self.owner, raw, folder="sentitems")
        self.client.force_authenticate(self.foreign)
        foreign = self.client.post("/api/user/mailbox-import/", {"reference": reference, "project": self.project.pk, "mode": "attach", "target_correspondence": out.pk}, format="json")
        self.assertIn(foreign.status_code, (400, 403, 404))
        self.assertEqual(MailboxImportedFile.objects.count(), 0)
        self.assertEqual(CorrespondenceAttachment.objects.filter(correspondence__in=[out, other_project_out]).count(), 0)

    def test_an_inbox_message_still_cannot_attach_to_an_out_and_a_sent_one_never_creates(self):
        out = self.locked_out()
        inbox = self.msg(1)
        crossed = self.imp(self.owner, inbox, mode="attach", target_correspondence=out.pk)
        self.assertEqual((crossed.status_code, crossed.data["code"]), (400, "direction_mismatch"))
        sent = self.msg(2, folder="sentitems")
        created = self.imp(self.owner, sent, folder="sentitems", mode="create")
        self.assertEqual((created.status_code, created.data["code"]), (409, "out_creation_not_supported"))

    def test_inbox_still_creates_an_in_and_an_in_answering_a_locked_out(self):
        out = self.locked_out(response_required=True, response_status="AWAITING_REPLY")
        plain = self.imp(self.owner, self.msg(1), mode="create")
        self.assertEqual((plain.status_code, plain.data["result"]), (201, "created"))

        def fields():
            return {k: v for k, v in Correspondence.objects.filter(pk=out.pk).values().get().items() if k not in ("response_status", "updated_at")}

        before_out = fields()
        answer = self.imp(self.owner, self.msg(2), mode="create", parent_correspondence=out.pk)
        self.assertEqual(answer.status_code, 201, answer.data)
        self.assertEqual(Correspondence.objects.get(pk=answer.data["correspondence"]["id"]).parent_correspondence_id, out.pk)
        self.assertEqual(fields(), before_out)

    def test_no_send_event_and_no_send_proof_is_made_from_a_manual_import(self):
        from project_management.models import CorrespondenceSendEvent, CorrespondenceSendProof

        out = self.locked_out()
        raw, attachment = self.sent_with_pdf()
        with patch("requests.post", side_effect=AssertionError("nothing may be sent")):
            self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=out.pk)
        self.assertEqual((CorrespondenceSendEvent.objects.count(), CorrespondenceSendProof.objects.count()), (0, 0))

    def test_files_kept_from_a_mail_are_never_part_of_what_a_correspondence_sends(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from project_management.services.correspondence_send import SendBlocked, _resolve_attachments

        out = self.locked_out()
        own = CorrespondenceAttachment(correspondence=out, file_name="letter.pdf", file_size=3)
        own.file.save("letter.pdf", SimpleUploadedFile("letter.pdf", b"abc"), save=False)
        own.save()
        raw, attachment = self.sent_with_pdf()
        self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=out.pk)
        self.assertEqual(out.attachments.count(), 3)
        self.assertEqual([a.pk for a in _resolve_attachments(out, None)], [own.pk])          # by default: only its own file
        kept = out.attachments.exclude(pk=own.pk).first()
        with self.assertRaises(SendBlocked):                                                 # nor when named
            _resolve_attachments(out, [kept.pk])

    def test_the_attachment_list_tells_which_files_come_from_a_mail(self):
        out = self.locked_out()
        raw, attachment = self.sent_with_pdf()
        self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=out.pk)
        with patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True):
            self.client.force_authenticate(self.owner)
            listing = self.client.get(f"/api/project_management/correspondence/{out.pk}/attachments")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["is_mail_source"] for item in listing.json()["results"]], [True, True])


class SentItemsResendSafetyTests(DocsBase):
    """An approved OUT that holds its business attachments AND files kept from a Sent Items message
    (the .eml and a PDF): preparing and making a new Graph send through the real API sends only the
    business files, refuses the provenance ones whatever the selection, and keeps every usual check.
    Graph is a fake (sendMail and the token renewal are simulated), nothing real is sent."""

    BUSINESS = (("contract.pdf", b"business contract bytes"), ("annex.docx", b"business annex bytes"))

    def setUp(self):
        super().setUp()
        from ecpmpapp.permissions import LicensePermission

        self.grant_project(self.owner, ("view", "create", "update", "send"))
        self.viewer = self._company_member("viewer-resend")
        self.grant_project(self.viewer, ("view",))
        license_patch = patch.object(LicensePermission, "has_permission", return_value=True)
        license_patch.start()
        self.addCleanup(license_patch.stop)
        self.api = APIClient()

    def approved_out_with_files(self, approve=True):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from project_management.services.correspondence_workflow import WorkflowType, perform_action, start_validation

        corr = self.create_out_project(recipient_contact=self.recipient_contact.id)
        business = []
        for name, content in self.BUSINESS:
            attachment = CorrespondenceAttachment(correspondence=corr, file_name=name, file_size=len(content), uploaded_by=self.owner)
            attachment.file.save(name, SimpleUploadedFile(name, content), save=False)
            attachment.save()
            business.append(attachment)
        if approve:
            workflow = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
            perform_action(workflow, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr, business

    def keep_a_sent_copy_on(self, corr, n=1):
        raw = self.msg(n, folder="sentitems")
        attachment = self.att(raw, 1, content=PDF)
        response = self.imp(self.owner, raw, [attachment], folder="sentitems", mode="attach", target_correspondence=corr.pk)
        self.assertEqual(response.status_code, 201, response.data)
        return [a for a in corr.attachments.filter(mailbox_source__isnull=False)]

    def post_send(self, corr, user=None, **body):
        from project_management.test_correspondence_send_graph import _mock_requests_get, _mock_requests_post

        self.api.force_authenticate(user or self.owner)
        payload = {"mailbox_id": self.individual_mailbox.pk, **body}
        with override_settings(MS_CLIENT_ID="client-id", MS_CLIENT_SECRET="client-secret"), \
                patch("requests.post", side_effect=_mock_requests_post()) as post, \
                patch("requests.get", side_effect=_mock_requests_get(identity_email=self.individual_mailbox.email_address)):
            response = self.api.post(f"/api/project_management/correspondence/{corr.pk}/send", payload, format="json")
        return response, post

    def sent_names(self, post):
        message = self._graph_call(post).kwargs["json"]["message"]
        return sorted(a["name"] for a in message.get("attachments", []))

    def test_only_the_business_files_are_sent_and_the_snapshot_says_so(self):
        corr, business = self.approved_out_with_files()
        provenance = self.keep_a_sent_copy_on(corr)
        self.assertEqual((corr.attachments.count(), len(provenance)), (4, 2))
        response, post = self.post_send(corr)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.sent_names(post), ["annex.docx", "contract.pdf"])
        snapshot = response.json()["attachments_snapshot"]
        self.assertEqual(sorted(e["id"] for e in snapshot), sorted(a.pk for a in business))
        self.assertEqual({e["file_name"] for e in snapshot}, {"contract.pdf", "annex.docx"})
        self.assertEqual(
            {e["sha256"] for e in snapshot},
            {hashlib.sha256(content).hexdigest() for _, content in self.BUSINESS},
        )
        self.assertFalse({a.pk for a in provenance} & {e["id"] for e in snapshot})
        self.assertEqual(mail.outbox, [])

    def test_a_resend_prepared_the_same_way_still_sends_only_the_business_files(self):
        corr, business = self.approved_out_with_files()
        self.assertEqual(self.post_send(corr)[0].status_code, 201)               # a first send
        before = CorrespondenceAttachment.objects.filter(correspondence=corr).count()
        self.keep_a_sent_copy_on(corr)                                           # then the copy of that mail is kept on it
        self.assertEqual(CorrespondenceAttachment.objects.filter(correspondence=corr).count(), before + 2)
        response, post = self.post_send(corr)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.sent_names(post), ["annex.docx", "contract.pdf"])
        self.assertEqual(sorted(e["id"] for e in response.json()["attachments_snapshot"]), sorted(a.pk for a in business))

    def test_naming_a_provenance_file_is_refused_by_the_backend_and_nothing_is_sent(self):
        from project_management.models import CorrespondenceSendEvent

        corr, business = self.approved_out_with_files()
        provenance = self.keep_a_sent_copy_on(corr)
        for ids in ([provenance[0].pk], [p.pk for p in provenance], [business[0].pk, provenance[0].pk]):
            response, post = self.post_send(corr, attachment_ids=ids)
            self.assertEqual(response.status_code, 400, ids)
            self.assertIn("attachments", response.json())
            self.assertEqual(post.call_count, 0)                  # not even the token renewal: refused before Graph
        self.assertEqual(CorrespondenceSendEvent.objects.filter(correspondence=corr).count(), 0)

    def test_an_explicit_selection_of_business_files_sends_exactly_those(self):
        corr, business = self.approved_out_with_files()
        self.keep_a_sent_copy_on(corr)
        response, post = self.post_send(corr, attachment_ids=[business[0].pk])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.sent_names(post), [business[0].file_name])
        self.assertEqual([e["id"] for e in response.json()["attachments_snapshot"]], [business[0].pk])

    def test_the_usual_validation_and_permissions_still_apply(self):
        from project_management.models import CorrespondenceSendEvent

        draft, _ = self.approved_out_with_files(approve=False)
        self.keep_a_sent_copy_on(draft)
        response, post = self.post_send(draft)                     # never approved: not ready to send
        self.assertEqual((response.status_code, post.call_count), (400, 0))
        self.assertIn("ready_to_send", response.json())
        corr, business = self.approved_out_with_files()
        self.keep_a_sent_copy_on(corr, n=3)
        refused, post = self.post_send(corr, user=self.viewer)     # may see it, not send it
        self.assertEqual((refused.status_code, post.call_count), (403, 0))
        foreign, post = self.post_send(corr, user=self.foreign)    # another company
        self.assertIn(foreign.status_code, (403, 404))
        self.assertEqual(post.call_count, 0)
        self.assertEqual(CorrespondenceSendEvent.objects.count(), 0)
        self.api.force_authenticate(self.owner)
        no_mailbox = self.api.post(f"/api/project_management/correspondence/{corr.pk}/send", {"mailbox_id": 999999}, format="json")
        self.assertEqual(no_mailbox.status_code, 400)
        self.assertEqual(CorrespondenceSendEvent.objects.count(), 0)

    def test_nothing_real_is_called_during_the_whole_preparation_and_send(self):
        corr, business = self.approved_out_with_files()
        self.keep_a_sent_copy_on(corr)
        response, post = self.post_send(corr)
        self.assertEqual(response.status_code, 201, response.data)
        urls = [str(call.args[0]) for call in post.call_args_list]
        self.assertEqual(len([u for u in urls if "sendMail" in u]), 1)                     # one simulated send
        self.assertTrue(all("sendMail" in u or "oauth2" in u or "token" in u for u in urls), urls)
        self.assertEqual(mail.outbox, [])
