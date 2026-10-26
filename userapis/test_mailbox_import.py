"""MAIL-1.3D-C2-A: importing a found Outlook message into LINKS. Graph is a fake (patched GET);
every write verb, and any mail, would raise or show up in the outbox -- none does."""

import re
import shutil
import tempfile
import time
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import unquote

from django.core import mail
from django.test import override_settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from models.models import Permission, Role, RolePermission, UserOutlookCredentials
from project_management.models import Correspondence, ProjectTeamMembers
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_read import normalize_message
from userapis.mailbox_search import build_search_reference
from userapis.models import MailboxConnection, MailboxMessage, MailboxProjectAssignment, MailboxUserGrant
from userapis.test_mailbox import _make_company, _make_project, _make_user
from userapis.test_mailbox_read import BASE, SENTINEL_BODY, FakeResponse, raw_message
from userapis.test_mailbox_sync import SyncBase, iso

URL = "/api/user/mailbox-import/"


class FakeStream:
    """A streamed Graph answer: real bytes, or `size` synthetic ones produced lazily (so a huge
    answer never exists in memory). `served` says how much the reader really pulled."""

    def __init__(self, status=200, data=b"", size=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._data, self._size = data, size
        self.served = 0
        self.closed = False

    def iter_content(self, chunk_size=65536):
        total = self._size if self._size is not None else len(self._data)
        sent = 0
        while sent < total:
            n = min(chunk_size, total - sent)
            piece = self._data[sent:sent + n] if self._size is None else b"x" * n
            sent += n
            self.served += n
            yield piece

    def close(self):
        self.closed = True


def default_eml(raw):
    return (
        f"From: Alice <alice@example.com>\r\nTo: owner@example.com\r\nSubject: {raw.get('subject', 's')}\r\n"
        f"Date: Tue, 13 Oct 2026 10:00:00 +0000\r\nMessage-ID: {raw.get('internetMessageId', '<m@x>')}\r\n"
        "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nORIGINAL-EML-BODY\r\n"
    ).encode()


class ImportGraph:
    """Answers the import's GETs: identity, a folder's id, one message by id, its attachments
    listing, and the streamed `$value` of the message (.eml) and of an attachment."""

    def __init__(self, me_address):
        self.me_address = me_address
        self.calls = []
        self.folders = {"inbox": "FOLDER-INBOX", "sentitems": "FOLDER-SENT"}
        self.items = {}
        self.forced = None
        self.on_message_get = None
        self.attachments = {}          # message graph id -> [raw attachment dicts] (the listing)
        self.attachment_bytes = {}     # attachment id -> bytes of its content
        self.stream_overrides = {}     # message or attachment id -> FakeStream kwargs
        self.streams = []              # every FakeStream handed out, in order
        self.eml_content_type = "text/plain"      # what Graph really answers for a message's $value

    def add(self, n, folder="inbox", **over):
        fields = dict(
            id=f"AAMk-imp-{n}", internetMessageId=f"<imp-{n}@example.com>", conversationId=f"conv-imp-{n}",
            parentFolderId=self.folders[folder],
        )
        fields.update(over)
        raw = raw_message(n, **fields)
        self.items[raw["id"]] = raw
        return raw

    def __call__(self, url, params=None, headers=None, timeout=None, **kwargs):
        if url.startswith(f"{BASE}/me?"):
            return FakeResponse(200, {"mail": self.me_address, "userPrincipalName": self.me_address})
        self.calls.append({"url": url, "params": dict(params or {}), "stream": bool(kwargs.get("stream"))})
        if self.forced is not None:
            return self.forced
        path = url.split("?")[0]
        stream = re.search(r"/messages/([^/]+)/(?:attachments/([^/]+)/)?\$value$", path)
        if stream:
            message_id, attachment_id = unquote(stream.group(1)), stream.group(2) and unquote(stream.group(2))
            key = attachment_id or message_id
            if attachment_id:
                options = {"data": self.attachment_bytes.get(attachment_id, b"%PDF-1.4 fake attachment"),
                           "headers": {"Content-Type": "application/octet-stream"}}
            else:
                options = {"data": default_eml(self.items.get(message_id, {})),
                           "headers": {"Content-Type": self.eml_content_type}}
            options.update(self.stream_overrides.get(key, {}))
            handed = FakeStream(**options)
            self.streams.append(handed)
            return handed
        listing = re.search(r"/messages/([^/]+)/attachments$", path)
        if listing:
            return FakeResponse(200, {"value": self.attachments.get(unquote(listing.group(1)), [])})
        folder = re.search(r"/mailFolders/(inbox|sentitems)$", path)
        if folder:
            return FakeResponse(200, {"id": self.folders[folder.group(1)]})
        message = re.search(r"/messages/([^/]+)$", path)
        if message:
            if self.on_message_get:
                hook, self.on_message_get = self.on_message_get, None
                hook()
            item = self.items.get(unquote(message.group(1)))
            return FakeResponse(200, item) if item else FakeResponse(404, {"error": {"code": "ErrorItemNotFound"}})
        return FakeResponse(404, {})


class ImportBase(SyncBase):
    def setUp(self):
        super().setUp()
        self.graph = ImportGraph(self.individual_mailbox.email_address)
        self.media = tempfile.mkdtemp(prefix="links-test-media-")
        media = override_settings(MEDIA_ROOT=self.media)
        media.enable()
        self.addCleanup(media.disable)
        self.addCleanup(shutil.rmtree, self.media, True)
        patches = [
            ("userapis.mailbox_read.requests.get", {"side_effect": self.graph}),
            ("userapis.mailbox_read.refresh_outlook_access_token", {"return_value": "REFRESHED-TOKEN"}),
        ] + [(f"requests.{verb}", {"side_effect": AssertionError("nothing may be written to Outlook")})
             for verb in ("post", "put", "patch", "delete")]
        for target, kwargs in patches:
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = APIClient()
        for mailbox in (self.individual_mailbox, self.shared_mailbox):
            MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        self.project_b = _make_project(self.company)
        self.grant_project(self.owner, ("view", "create", "update"))
        self.grant_project(self.delegate, ("view", "create", "update"))
        self.reader_only = self._company_member("readeronly")                # may read the shared box, no correspondence rights
        MailboxUserGrant.objects.create(mailbox=self.shared_mailbox, user=self.reader_only, can_read=True)
        self.foreign = _make_user("foreign@other.test", _make_company("Other"))
        mail.outbox.clear()

    # -- helpers
    def grant_project(self, user, actions, project=None):
        """The correspondence rights a project-scoped action checks: a project role held through
        the project team (a company-wide role is not what a project-scoped check looks at)."""
        role = Role.objects.create(
            name=f"Import test {user.pk}-{'-'.join(actions)}-{(project or self.project).pk}", company=self.company, type="solution",
            scope="project", is_active=True, created_by=user,
        )
        for action in actions:
            key = f"documents.correspondence_{action}"
            perm, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": "documents", "description": "test"},
            )
            RolePermission.objects.get_or_create(role=role, permission=perm)
        ProjectTeamMembers.objects.update_or_create(
            project=project or self.project, team_member=user,
            defaults={"role": "Editor", "project_access": "RW", "project_role": role},
        )

    def reference(self, user, raw, mailbox=None, folder="inbox"):
        meta = normalize_message(raw, folder)
        return build_search_reference(user, mailbox or self.individual_mailbox, meta)

    def do(self, user, reference, **body):
        body.setdefault("project", self.project.pk)
        body.setdefault("mode", "create")
        if body["mode"] == "create":
            body.setdefault("external_reference", "EXT-001")
        self.client.force_authenticate(user)
        return self.client.post(URL, {"reference": reference, **body}, format="json")

    def make_in(self, **over):
        return Correspondence.objects.create(**{
            "direction": "IN", "project": self.project, "subject": "Existing IN", "external_reference": "EXT-OLD",
            "status": "ISSUED", "response_status": "NOT_REQUIRED", **over,
        })

    def make_out(self, **over):
        return Correspondence.objects.create(**{
            "direction": "OUT", "project": self.project, "subject": "Existing OUT", "reference": "PRJ-OUT-1",
            "status": "ISSUED", "response_status": "NOT_REQUIRED", **over,
        })

    @property
    def message_gets(self):
        return [c for c in self.graph.calls if re.search(r"/messages/[^/]+$", c["url"].split("?")[0])]

    def counts(self):
        return (Correspondence.objects.count(), MailboxMessage.objects.count())


class CreateInTests(ImportBase):
    def test_an_authorized_user_imports_an_inbox_message_as_a_new_in(self):
        raw = self.graph.add(1, subject="Site visit minutes", receivedDateTime=iso(self.now - timedelta(days=2)))
        # a Network contact with the sender's very address exists: it must NOT be picked silently
        from project_management.models import ContactCompany, ContactUser
        company = ContactCompany.objects.create(company_profile=self.company, company_name="Alice Co", company_role="Client", contact_email="a@x.invalid")
        ContactUser.objects.create(company_profile=self.company, contact_company=company, name="Alice", surname="Test", email="alice@example.com")
        response = self.do(self.owner, self.reference(self.owner, raw))
        self.assertEqual(response.status_code, 201, response.data)
        corr = Correspondence.objects.get(pk=response.data["correspondence"]["id"])
        self.assertEqual((corr.direction, corr.project_id, corr.status), ("IN", self.project.pk, "ISSUED"))
        self.assertEqual((corr.subject, corr.external_reference, corr.reference), ("Site visit minutes", "EXT-001", ""))
        self.assertEqual(corr.received_date, (self.now - timedelta(days=2)).astimezone().date())
        self.assertEqual((corr.response_status, corr.created_by_id), ("NOT_REQUIRED", self.owner.pk))
        self.assertEqual(corr.message, SENTINEL_BODY)                                # the plain text is kept (B3)
        self.assertEqual((corr.sender_company_id, corr.sender_contact_id), (None, None))   # never guessed from the address
        row = MailboxMessage.objects.get(graph_message_id=raw["id"])
        self.assertEqual((row.state, row.imported_correspondence_id, row.imported_by_id, row.correspondence_id),
                         ("imported", corr.pk, self.owner.pk, None))
        self.assertIsNotNone(row.imported_at)
        self.assertEqual(response.data["result"], "created")

    def test_a_refused_import_leaves_nothing_behind(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        before = self.counts()
        self.assertEqual(self.do(self.owner, ref, correspondence_type="No such type").status_code, 400)
        self.assertEqual(self.counts(), before)                                       # nothing half-done
        self.assertEqual(MailboxMessage.objects.filter(state="imported").count(), 0)

    def test_explicit_parties_and_response_tracking_are_taken_as_sent(self):
        from project_management.models import ContactCompany, ContactUser
        company = ContactCompany.objects.create(company_profile=self.company, company_name="Sender Co", company_role="Client", contact_email="s@x.invalid")
        contact = ContactUser.objects.create(company_profile=self.company, contact_company=company, name="S", surname="C", email="sc@x.invalid")
        raw = self.graph.add(1)
        response = self.do(self.owner, self.reference(self.owner, raw), sender_company=company.pk, sender_contact=contact.pk, response_required=True)
        self.assertEqual(response.status_code, 201, response.data)
        corr = Correspondence.objects.get(pk=response.data["correspondence"]["id"])
        self.assertEqual((corr.sender_company_id, corr.sender_contact_id, corr.response_status), (company.pk, contact.pk, "TO_ANSWER"))

    def test_an_in_created_as_the_reply_to_an_out_follows_the_existing_response_rules(self):
        out = self.make_out(response_required=True, response_status="AWAITING_REPLY")
        raw = self.graph.add(1)
        response = self.do(self.owner, self.reference(self.owner, raw), parent_correspondence=out.pk)
        self.assertEqual(response.status_code, 201, response.data)
        child = Correspondence.objects.get(pk=response.data["correspondence"]["id"])
        out.refresh_from_db()
        self.assertEqual((child.parent_correspondence_id, out.response_status), (out.pk, "ANSWERED"))
        # a parent of the same direction, of another project, or invisible is refused
        another = self.graph.add(2)
        self.assertEqual(self.do(self.owner, self.reference(self.owner, another), parent_correspondence=self.make_in().pk).status_code, 400)
        self.assertEqual(self.do(self.owner, self.reference(self.owner, another), parent_correspondence=999999).status_code, 400)

    def test_an_in_can_answer_a_sent_locked_out_whose_proof_is_still_pending_and_nothing_of_that_out_changes(self):
        """MAIL-1.3D-REPLY-LINK: the OUT a person replies to has gone through an approved validation (so
        it is locked) and was sent through Microsoft 365 (so its send proof exists, PENDING). Neither
        blocks answering it: the new IN gets its own LINKS reference, its .eml and the link to the OUT,
        and the OUT keeps everything but the response tracking the existing rules move."""
        from project_management.models import (
            CorrespondenceSendEvent, CorrespondenceSendProof, CorrespondenceValidationWorkflow,
        )
        from project_management.services.correspondence_send_proof import ensure_send_proof

        out = self.make_out(response_required=True, response_status="AWAITING_REPLY")
        CorrespondenceValidationWorkflow.objects.create(
            correspondence=out, workflow_type="ONE_STEP", status="APPROVED",
            author=self.owner, approver=self.owner, started_by=self.owner,
        )
        event = CorrespondenceSendEvent.objects.create(
            correspondence=out, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
            result="SUCCESS", sent_at=timezone.now(), subject="Existing OUT",
        )
        proof = ensure_send_proof(event)
        self.assertEqual(proof.state, "PENDING")

        def keep():
            fields = {
                k: v for k, v in Correspondence.objects.filter(pk=out.pk).values().get().items()
                if k not in ("response_status", "updated_at")
            }
            return (
                fields,
                CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get(),
                CorrespondenceSendProof.objects.filter(pk=proof.pk).values().get(),
            )

        before = keep()
        raw = self.graph.add(1)
        response = self.do(self.owner, self.reference(self.owner, raw), external_reference="", parent_correspondence=out.pk)
        self.assertEqual(response.status_code, 201, response.data)
        child = Correspondence.objects.get(pk=response.data["correspondence"]["id"])
        self.assertEqual((child.direction, child.parent_correspondence_id, child.status), ("IN", out.pk, "ISSUED"))
        self.assertTrue(child.reference)                                   # its own LINKS reference (no external one was given)
        self.assertNotEqual(child.reference, out.reference)
        self.assertTrue(response.data["documents"]["eml_stored"])
        out.refresh_from_db()
        self.assertEqual(out.response_status, "ANSWERED")                  # the existing response rule, nothing else
        self.assertEqual(keep(), before)
        # the same message again: the existing result, no second IN and no second link
        again = self.do(self.owner, self.reference(self.owner, raw), external_reference="", parent_correspondence=out.pk)
        self.assertEqual((again.status_code, again.data["result"]), (200, "already_imported"))
        self.assertEqual(Correspondence.objects.filter(parent_correspondence=out).count(), 1)
        # a second, different message may answer the same OUT (a new IN each)
        second = self.do(self.owner, self.reference(self.owner, self.graph.add(2)), external_reference="", parent_correspondence=out.pk)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(Correspondence.objects.filter(parent_correspondence=out).count(), 2)
        self.assertEqual(keep()[1:], before[1:])

    def test_answering_an_out_needs_the_right_to_create_and_creates_nothing_otherwise(self):
        out = self.make_out(response_required=True, response_status="AWAITING_REPLY")
        raw = self.graph.add(1)
        count = Correspondence.objects.count()
        response = self.do(self.reader_only, self.reference(self.reader_only, raw, mailbox=self.shared_mailbox), parent_correspondence=out.pk)
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(Correspondence.objects.count(), count)
        out.refresh_from_db()
        self.assertEqual(out.response_status, "AWAITING_REPLY")
        # an OUT of another project is never a parent
        other_out = self.make_out(project=self.project_b, reference="PRJ-OUT-B")
        self.assertEqual(self.do(self.owner, self.reference(self.owner, self.graph.add(3)), parent_correspondence=other_out.pk).status_code, 400)

    def test_the_out_link_of_a_detected_reply_is_preserved(self):
        out = self.make_out()
        raw = self.graph.add(1)
        row, _ = store_message_metadata(self.individual_mailbox, normalize_message(raw, "inbox"))
        MailboxMessage.objects.filter(pk=row.pk).update(correspondence=out)         # as the sync's correlation did
        response = self.do(self.owner, self.reference(self.owner, raw))
        self.assertEqual(response.status_code, 201, response.data)
        row.refresh_from_db()
        self.assertEqual((row.correspondence_id, row.imported_correspondence_id, row.state),
                         (out.pk, response.data["correspondence"]["id"], "imported"))
        self.assertNotEqual(row.correspondence_id, row.imported_correspondence_id)

    def test_a_shared_mailbox_message_is_imported_by_a_reader_with_correspondence_rights(self):
        raw = self.graph.add(1, subject="Shared inbox mail")
        response = self.do(self.delegate, self.reference(self.delegate, raw, self.shared_mailbox))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("/users/", self.graph.calls[0]["url"])
        self.assertEqual(MailboxMessage.objects.get(graph_message_id=raw["id"]).mailbox_id, self.shared_mailbox.pk)


class SentItemsTests(ImportBase):
    def test_a_sent_message_never_becomes_an_in_or_any_new_correspondence(self):
        raw = self.graph.add(1, folder="sentitems")
        before = self.counts()
        response = self.do(self.owner, self.reference(self.owner, raw, folder="sentitems"))
        self.assertEqual((response.status_code, response.data["code"]), (409, "out_creation_not_supported"))
        self.assertEqual(self.counts(), before)
        self.assertEqual(Correspondence.objects.filter(direction="IN").count(), 0)
        self.assertEqual(self.graph.calls, [])                                        # refused before Graph

    def test_a_sent_message_can_be_attached_to_an_existing_out_without_changing_it(self):
        out = self.make_out(response_required=True, response_status="AWAITING_REPLY")
        snapshot = list(Correspondence.objects.filter(pk=out.pk).values())
        raw = self.graph.add(1, folder="sentitems")
        response = self.do(self.owner, self.reference(self.owner, raw, folder="sentitems"), mode="attach", target_correspondence=out.pk)
        self.assertEqual((response.status_code, response.data["result"]), (201, "attached"))
        self.assertEqual(list(Correspondence.objects.filter(pk=out.pk).values()), snapshot)
        row = MailboxMessage.objects.get(graph_message_id=raw["id"])
        # recorded as a source, NOT as a LINKS-sent copy (that link needs the send marker's proof)
        self.assertEqual((row.imported_correspondence_id, row.correspondence_id, row.state), (out.pk, None, "imported"))

    def test_the_direction_must_match_the_folder(self):
        sent = self.graph.add(1, folder="sentitems")
        received = self.graph.add(2)
        self.assertEqual(self.do(self.owner, self.reference(self.owner, sent, folder="sentitems"), mode="attach", target_correspondence=self.make_in().pk).data["code"], "direction_mismatch")
        self.assertEqual(self.do(self.owner, self.reference(self.owner, received), mode="attach", target_correspondence=self.make_out().pk).data["code"], "direction_mismatch")
        self.assertEqual(MailboxMessage.objects.filter(state="imported").count(), 0)


class AttachTests(ImportBase):
    def test_a_message_attaches_to_an_existing_in_which_stays_untouched(self):
        existing = self.make_in()
        fields = lambda: {k: v for k, v in Correspondence.objects.filter(pk=existing.pk).values().get().items() if k not in ("message", "updated_at")}
        snapshot = fields()
        raw = self.graph.add(1)
        response = self.do(self.owner, self.reference(self.owner, raw), mode="attach", target_correspondence=existing.pk)
        self.assertEqual((response.status_code, response.data["result"]), (201, "attached"))
        self.assertEqual(fields(), snapshot)                                         # nothing but the empty text was filled (B3)
        self.assertEqual(Correspondence.objects.get(pk=existing.pk).message, SENTINEL_BODY)
        self.assertEqual(Correspondence.objects.count(), 1)
        self.assertEqual(MailboxMessage.objects.get(graph_message_id=raw["id"]).imported_correspondence_id, existing.pk)

    def test_an_existing_link_is_never_replaced(self):
        existing = self.make_in()
        first, second = self.graph.add(1), self.graph.add(2)
        self.assertEqual(self.do(self.owner, self.reference(self.owner, first), mode="attach", target_correspondence=existing.pk).status_code, 201)
        response = self.do(self.owner, self.reference(self.owner, second), mode="attach", target_correspondence=existing.pk)
        self.assertEqual((response.status_code, response.data["code"]), (409, "target_already_linked"))
        self.assertEqual(MailboxMessage.objects.get(graph_message_id=first["id"]).imported_correspondence_id, existing.pk)
        self.assertFalse(MailboxMessage.objects.filter(graph_message_id=second["id"]).exists())   # nothing left behind

    def test_the_target_must_be_in_the_chosen_project_and_visible(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        elsewhere = self.make_in(project=self.project_b)
        for target in (elsewhere.pk, 999999):
            self.assertEqual(self.do(self.owner, ref, mode="attach", target_correspondence=target).data["code"], "invalid_target")
        self.assertEqual(self.do(self.owner, ref, mode="attach").status_code, 400)       # target required

    def test_attaching_needs_the_update_right_not_just_read(self):
        self.grant_project(self.reader_only, ("view", "create"))
        raw = self.graph.add(1)
        ref = self.reference(self.reader_only, raw, self.shared_mailbox)
        response = self.do(self.reader_only, ref, mode="attach", target_correspondence=self.make_in().pk)
        self.assertEqual((response.status_code, response.data["code"]), (403, "correspondence_permission_denied"))


class AuthorizationTests(ImportBase):
    def test_a_reference_issued_to_someone_else_is_useless(self):
        raw = self.graph.add(1)
        response = self.do(self.approver, self.reference(self.owner, raw))
        self.assertEqual((response.status_code, response.data["code"]), (400, "invalid_reference"))
        self.assertEqual((self.graph.calls, self.counts()), ([], (0, 0)))

    def test_nobody_but_the_owner_can_import_from_a_personal_mailbox(self):
        raw = self.graph.add(1)
        for user in (self.approver, self.delegate, self.reader_only, self.foreign):
            response = self.do(user, self.reference(user, raw))                       # even a reference of their own
            self.assertEqual((response.status_code, response.data["code"]), (403, "not_authorized"), user.email)
        self.assertEqual(self.graph.calls, [])

    def test_reading_the_mailbox_is_not_enough_correspondence_rights_are_needed_too(self):
        raw = self.graph.add(1)
        response = self.do(self.reader_only, self.reference(self.reader_only, raw, self.shared_mailbox))
        self.assertEqual((response.status_code, response.data["code"]), (403, "correspondence_permission_denied"))
        self.assertEqual((self.graph.calls, Correspondence.objects.count()), ([], 0))

    def test_correspondence_rights_are_not_enough_the_mailbox_must_be_readable(self):
        # the approver validated correspondences, the delegate may create them: neither reads the personal box
        self.grant_project(self.approver, ("view", "create", "update"))
        raw = self.graph.add(1)
        self.assertEqual(self.do(self.approver, self.reference(self.approver, raw)).status_code, 403)

    def test_the_mailbox_must_be_assigned_to_the_project_and_the_project_must_be_the_companys(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        self.assertEqual(self.do(self.owner, ref, project=self.project_b.pk).status_code, 403)       # not assigned
        foreign_project = _make_project(self.foreign.company)
        self.assertEqual(self.do(self.owner, ref, project=foreign_project.pk).status_code, 403)
        self.assertEqual(self.do(self.owner, ref, project=999999).status_code, 403)
        self.assertEqual((self.graph.calls, Correspondence.objects.count()), ([], 0))

    def test_a_revoked_mailbox_or_missing_connection_stops_the_import(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        UserOutlookCredentials.objects.filter(user=self.owner).delete()
        self.assertEqual(self.do(self.owner, ref).data["code"], "token_unavailable")
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="revoked")
        self.assertEqual(self.do(self.owner, ref).data["code"], "mailbox_inactive")
        self.assertEqual(Correspondence.objects.count(), 0)

    def test_a_forged_altered_or_expired_reference_is_refused_before_any_read(self):
        raw = self.graph.add(1)
        token = self.reference(self.owner, raw)
        for bad in (token[:-4] + "AAAA", "garbage", token.upper()):
            self.assertEqual(self.do(self.owner, bad).data["code"], "invalid_reference")
        with patch("django.core.signing.time.time", return_value=time.time() + 31 * 60):
            self.assertEqual(self.do(self.owner, token).data["code"], "invalid_reference")
        self.assertEqual((self.graph.calls, Correspondence.objects.count()), ([], 0))

    def test_authentication_method_and_payload_shape(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(URL, {}, format="json").status_code, (401, 403))
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(URL).status_code, 405)
        ref = self.reference(self.owner, self.graph.add(1))
        for bad in ({"mode": "merge"}, {"project": "x"}, {"mode": "attach", "target_correspondence": 5, "external_reference": "X"},
                    {"mode": "create", "target_correspondence": 5}):
            self.assertEqual(self.do(self.owner, ref, **bad).status_code, 400, bad)


class MessageStateTests(ImportBase):
    def test_a_deleted_or_moved_message_cannot_be_imported(self):
        gone = self.graph.add(1)
        moved = self.graph.add(2)
        self.graph.items[moved["id"]]["parentFolderId"] = "FOLDER-ARCHIVE"
        del self.graph.items[gone["id"]]
        for raw in (gone, moved):
            response = self.do(self.owner, self.reference(self.owner, raw))
            self.assertEqual((response.status_code, response.data["code"]), (404, "message_gone"))
        self.assertEqual(self.counts(), (0, 0))

    def test_graph_failures_leave_nothing_behind(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        for status, code in ((429, "rate_limited"), (500, "temporary"), (403, "access_denied")):
            self.graph.forced = FakeResponse(status, {}, headers={"Retry-After": "9"})
            response = self.do(self.owner, ref)
            self.assertEqual(response.data["code"], code)
            self.assertNotIn("TOKEN", str(response.data))
        self.assertEqual(self.counts(), (0, 0))

    def test_the_review_state_changes_only_after_a_complete_success(self):
        raw = self.graph.add(1)
        row, _ = store_message_metadata(self.shared_mailbox, normalize_message(raw, "inbox"))   # already in the review queue
        ref = self.reference(self.delegate, raw, self.shared_mailbox)
        self.assertEqual(self.do(self.delegate, ref, correspondence_type="Nonsense").status_code, 400)
        row.refresh_from_db()
        self.assertEqual((row.state, row.imported_correspondence_id), ("new", None))               # untouched by the failure
        self.assertEqual(self.do(self.delegate, ref).status_code, 201)
        row.refresh_from_db()
        self.assertEqual(row.state, "imported")
        # the review API now refuses to ignore or restore it
        self.client.force_authenticate(self.delegate)
        self.assertEqual(self.client.post(f"/api/user/mailbox-messages/{row.pk}/ignore/").status_code, 409)
        self.assertEqual(self.client.post(f"/api/user/mailbox-messages/{row.pk}/restore/").status_code, 409)

    def test_only_the_chosen_message_is_stored_and_a_personal_import_stays_out_of_the_review_queue(self):
        chosen, other = self.graph.add(1), self.graph.add(2)
        self.assertEqual(self.do(self.owner, self.reference(self.owner, chosen)).status_code, 201)
        self.assertEqual(list(MailboxMessage.objects.values_list("graph_message_id", flat=True).filter(folder="inbox")), [chosen["id"]])
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get("/api/user/mailbox-messages/", {"mailbox": self.individual_mailbox.pk}).data["count"], 0)


class DuplicateTests(ImportBase):
    def test_a_retry_returns_the_existing_result_without_a_second_correspondence_or_graph_call(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        first = self.do(self.owner, ref)
        gets = len(self.graph.calls)
        again = self.do(self.owner, ref)
        self.assertEqual((again.status_code, again.data["result"]), (200, "already_imported"))
        self.assertEqual(again.data["correspondence"]["id"], first.data["correspondence"]["id"])
        self.assertEqual(Correspondence.objects.count(), 1)
        self.assertEqual(len(self.graph.calls), gets)

    def test_the_same_message_cannot_be_imported_into_another_project(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        self.do(self.owner, ref)
        MailboxProjectAssignment.objects.create(mailbox=self.individual_mailbox, project=self.project_b)
        self.grant_project(self.owner, ("view", "create", "update"), self.project_b)
        response = self.do(self.owner, ref, project=self.project_b.pk)
        self.assertEqual((response.status_code, response.data["code"]), (409, "already_imported"))
        self.assertEqual(Correspondence.objects.count(), 1)

    def test_the_same_internet_message_from_a_second_mailbox_is_a_duplicate_in_the_same_project(self):
        raw = self.graph.add(1)
        first = self.do(self.owner, self.reference(self.owner, raw))
        copy = self.graph.add(5, internetMessageId=raw["internetMessageId"])         # the same mail, as seen in the shared box
        response = self.do(self.delegate, self.reference(self.delegate, copy, self.shared_mailbox))
        self.assertEqual((response.status_code, response.data["code"]), (409, "already_imported"))
        self.assertEqual(response.data["correspondence_id"], first.data["correspondence"]["id"])
        self.assertEqual(Correspondence.objects.count(), 1)
        self.assertFalse(MailboxMessage.objects.filter(mailbox=self.shared_mailbox).exists())      # rolled back entirely

    def test_a_concurrent_identical_import_cannot_create_a_second_correspondence(self):
        raw = self.graph.add(1)
        ref = self.reference(self.owner, raw)
        winners = []

        def competing_request():
            other = APIClient()
            other.force_authenticate(self.owner)
            winners.append(other.post(URL, {"reference": ref, "project": self.project.pk, "mode": "create", "external_reference": "EXT-RACE"}, format="json"))

        self.graph.on_message_get = competing_request          # fires while the first request is between its checks and its write
        response = self.do(self.owner, ref)
        self.assertEqual(winners[0].status_code, 201)
        self.assertEqual((response.status_code, response.data["result"]), (200, "already_imported"))
        self.assertEqual(Correspondence.objects.filter(direction="IN").count(), 1)
        self.assertEqual(MailboxMessage.objects.filter(state="imported").count(), 1)

    def test_a_concurrent_attach_to_the_same_target_leaves_one_source(self):
        target = self.make_in()
        first, second = self.graph.add(1), self.graph.add(2)
        ref_second = self.reference(self.owner, second)

        def competing_request():
            other = APIClient()
            other.force_authenticate(self.owner)
            winners = other.post(URL, {"reference": self.reference(self.owner, first), "project": self.project.pk, "mode": "attach", "target_correspondence": target.pk}, format="json")
            self.assertEqual(winners.status_code, 201)

        self.graph.on_message_get = competing_request
        response = self.do(self.owner, ref_second, mode="attach", target_correspondence=target.pk)
        self.assertEqual((response.status_code, response.data["code"]), (409, "target_already_linked"))
        self.assertEqual(list(target.imported_from_messages.values_list("graph_message_id", flat=True)), [first["id"]])

    def test_the_database_itself_refuses_two_sources_for_one_correspondence(self):
        target = self.make_in()
        a, _ = store_message_metadata(self.individual_mailbox, normalize_message(self.graph.add(1), "inbox"))
        b, _ = store_message_metadata(self.individual_mailbox, normalize_message(self.graph.add(2), "inbox"))
        MailboxMessage.objects.filter(pk=a.pk).update(imported_correspondence=target)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MailboxMessage.objects.filter(pk=b.pk).update(imported_correspondence=target)


class NothingElseHappensTests(ImportBase):
    def test_the_outlook_side_is_only_read_and_no_mail_leaves(self):
        raw = self.graph.add(1)
        self.assertEqual(self.do(self.owner, self.reference(self.owner, raw)).status_code, 201)
        # write verbs were patched to raise: reaching here proves none was used
        self.assertEqual(mail.outbox, [])
        self.assertTrue(all(c["stream"] or "$select" in c["params"] or c["url"].endswith(("/inbox", "/sentitems")) for c in self.graph.calls))
        for call in self.message_gets:
            select = call["params"]["$select"].lower().replace("hasattachments", "")
            for word in ("attachments", "preview", "uniquebody"):
                self.assertNotIn(word, select)
        self.assertEqual(len(self.message_gets), 1)                                  # exactly the one chosen message
        self.assertEqual(len([c for c in self.graph.calls if c["stream"]]), 1)       # and its original, nothing else

    def test_only_the_plain_text_and_the_original_message_are_kept_no_html_no_unselected_file(self):
        raw = self.graph.add(1, body={"contentType": "html", "content": "<p>Hello <b>team</b></p><script>x()</script>"})
        self.graph.attachments[raw["id"]] = [{
            "@odata.type": "#microsoft.graph.fileAttachment", "id": "ATT-NOT-SELECTED", "name": "other.pdf",
            "contentType": "application/pdf", "size": 10, "isInline": False,
        }]
        response = self.do(self.owner, self.reference(self.owner, raw))
        corr = Correspondence.objects.get(pk=response.data["correspondence"]["id"])
        self.assertEqual(corr.message, "Hello team")
        self.assertEqual([a.file_name.endswith(".eml") for a in corr.attachments.all()], [True])   # nothing else
        self.assertFalse(any("ATT-NOT-SELECTED" in c["url"] for c in self.graph.calls if c["stream"]))
