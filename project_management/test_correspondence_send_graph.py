"""MAIL-1.2A: sending an OUT Correspondence via Microsoft Graph (mailbox_id given), as a
second provider alongside the existing SMTP path (test_correspondence_send.py, entirely
unmodified and unaffected -- see SmtpRegressionTests below for a cheap confirmation of that).

Every Microsoft Graph call is mocked -- never a real HTTP call, per the ticket's own explicit
instruction. No real email is ever sent (same locmem-backend guarantee the existing SMTP tests
already rely on).

A single `requests.post` is shared by the real token-refresh call
(userapis/microsoft_outlook.py) and the real Graph sendMail call (userapis/mailbox_send.py) --
both modules do a plain `import requests`, so they hold the SAME module object, and patching
it from either import path patches it for both (two separate `patch(".../requests.post")`
targets collide rather than coexisting). `_mock_requests_post` below is therefore ONE mock with
a `side_effect` that dispatches on the URL, exactly mirroring what two real, independent HTTP
calls to two different endpoints actually look like.
"""
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from ecpmpapp.constants import MS_OAUTH_TOKEN_URL
from models.models import UserOutlookCredentials
from project_management.models import CorrespondenceAttachment, CorrespondenceSendEvent
from project_management.services.correspondence_send import SendBlocked, send_correspondence
from project_management.services.correspondence_workflow import WorkflowType, perform_action, start_validation
from project_management.test_correspondence_send import SendTestBase
from userapis.mailbox_send import MAX_GRAPH_ATTACHMENTS_BYTES
from userapis.models import MailboxConnection, MailboxProjectAssignment, MailboxUserGrant

Result = CorrespondenceSendEvent.Result

_REFRESH_PAYLOAD = {
    "access_token": "fresh-mail-token",
    "refresh_token": "rotated-refresh-token",
    "expires_in": 3600,
}


def _mock_requests_post(
    *, refresh_ok=True, refresh_payload=None, refresh_raises=None,
    graph_status=202, graph_body=None, graph_raises=None,
):
    """One dispatching mock for the one shared `requests.post` -- routes to the token
    endpoint's canned response or the Graph sendMail endpoint's canned response by URL,
    exactly like two independent real calls would hit two independent real endpoints."""
    refresh_payload = _REFRESH_PAYLOAD if refresh_payload is None else refresh_payload

    def _dispatch(url, *args, **kwargs):
        if url == MS_OAUTH_TOKEN_URL:
            if refresh_raises is not None:
                raise refresh_raises
            response = Mock()
            response.ok = refresh_ok
            response.json.return_value = refresh_payload
            return response
        if graph_raises is not None:
            raise graph_raises
        response = Mock()
        response.status_code = graph_status
        response.json.return_value = graph_body or {}
        response.text = ""
        return response

    return _dispatch


def _mock_requests_get(*, identity_email, identity_status=200, identity_raises=None):
    """Mocks the separate `requests.get` call MAIL-1.2A-REVIEW's identity check makes
    (/me?$select=mail,userPrincipalName) -- a different HTTP method from the sendMail/refresh
    `requests.post` calls above, so this never collides with that dispatcher."""

    def _dispatch(url, *args, **kwargs):
        if identity_raises is not None:
            raise identity_raises
        response = Mock()
        response.status_code = identity_status
        response.json.return_value = {
            "mail": identity_email, "userPrincipalName": identity_email,
        }
        return response

    return _dispatch


class GraphSendTestBase(SendTestBase):
    def setUp(self):
        super().setUp()
        self.individual_mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="owner@links-local.invalid",
            company_profile=self.company,
            owner_user=self.owner,
            connected_by=self.owner,
            status=MailboxConnection.Status.ACTIVE,
        )
        self.delegate = self._company_member("delegate")
        self.shared_mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="shared@links-local.invalid",
            company_profile=self.company,
            connected_by=self.delegate,
            status=MailboxConnection.Status.ACTIVE,
        )
        MailboxUserGrant.objects.create(
            mailbox=self.shared_mailbox, user=self.owner, can_send=True, can_read=True,
        )
        self._give_credentials(self.owner)
        self._give_credentials(self.delegate)

    def _give_credentials(self, user):
        UserOutlookCredentials.objects.create(
            user=user,
            outlook_access_token="stale-token",
            outlook_refresh_token=f"refresh-{user.id}",
            outlook_token_expires=timezone.now() - timedelta(minutes=5),
        )

    def send_via_graph(
        self, corr, mailbox, *, mock_kwargs=None, identity_kwargs=None, **kwargs
    ):
        # Identity check only ever runs for an individual mailbox (section 2) -- defaults to
        # the mailbox's own address so every pre-existing success test needs no change; a
        # mismatch test overrides identity_kwargs explicitly.
        identity_kwargs = dict(identity_kwargs or {})
        identity_kwargs.setdefault("identity_email", mailbox.email_address)
        mock_post = _mock_requests_post(**(mock_kwargs or {}))
        mock_get = _mock_requests_get(**identity_kwargs)
        with override_settings(MS_CLIENT_ID="client-id", MS_CLIENT_SECRET="client-secret"):
            with patch("requests.post", side_effect=mock_post) as mock_post_obj:
                with patch("requests.get", side_effect=mock_get) as mock_get_obj:
                    event = send_correspondence(
                        corr, sender=self.owner, mailbox_id=mailbox.id, **kwargs
                    )
        return event, mock_post_obj, mock_get_obj

    def _graph_call(self, mock_post):
        """The one call in mock_post's history that targeted Graph's sendMail, not the token
        endpoint."""
        for call in mock_post.call_args_list:
            if call.args and call.args[0] != MS_OAUTH_TOKEN_URL:
                return call
        return None


class IndividualMailboxSendTests(GraphSendTestBase):
    def test_owner_can_send_from_their_own_individual_mailbox(self):
        corr = self.approved_out()
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        self.assertEqual(event.provider, "graph")
        self.assertEqual(event.mailbox_id, self.individual_mailbox.id)
        self.assertEqual(event.from_email, "owner@links-local.invalid")
        # Section 8: 202 Accepted carries no message id -- never invented.
        self.assertEqual(event.provider_message_id, "")
        corr.refresh_from_db()
        self.assertEqual(corr.status, "ISSUED")

    def test_another_user_cannot_send_from_someone_elses_individual_mailbox(self):
        corr = self.approved_out()
        with self.assertRaises(SendBlocked):
            with override_settings(MS_CLIENT_ID="client-id", MS_CLIENT_SECRET="client-secret"):
                send_correspondence(
                    corr, sender=self.approver, mailbox_id=self.individual_mailbox.id,
                )
        self.assertFalse(corr.send_events.exists())


class SharedMailboxSendTests(GraphSendTestBase):
    def test_user_with_can_send_grant_can_send_from_shared_mailbox(self):
        corr = self.approved_out()
        event, mock_post, _ = self.send_via_graph(corr, self.shared_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        self.assertEqual(event.from_email, "shared@links-local.invalid")
        # Section 5: the delegate's own token is used, not the requesting user's.
        graph_call = self._graph_call(mock_post)
        self.assertIn("shared%40links-local.invalid", graph_call.args[0])

    def test_user_without_any_grant_is_blocked(self):
        corr = self.approved_out()
        with self.assertRaises(SendBlocked):
            send_correspondence(
                corr, sender=self.approver, mailbox_id=self.shared_mailbox.id,
            )
        self.assertFalse(corr.send_events.exists())

    def test_user_with_read_only_grant_cannot_send(self):
        MailboxUserGrant.objects.create(
            mailbox=self.shared_mailbox, user=self.approver, can_send=False, can_read=True,
        )
        corr = self.approved_out()
        with self.assertRaises(SendBlocked):
            send_correspondence(
                corr, sender=self.approver, mailbox_id=self.shared_mailbox.id,
            )

    def test_revoked_mailbox_cannot_be_used(self):
        self.shared_mailbox.status = MailboxConnection.Status.REVOKED
        self.shared_mailbox.save(update_fields=["status"])
        corr = self.approved_out()
        with self.assertRaises(SendBlocked):
            send_correspondence(
                corr, sender=self.owner, mailbox_id=self.shared_mailbox.id,
            )

    def test_mailbox_from_another_company_is_rejected(self):
        from models.models import CompanyProfile

        other_company = CompanyProfile.objects.create(
            admin_user=self.approver, company_name="Other Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x",
            company_email_address="other@x.invalid", company_tax_reference="T",
            company_iban="I",
        )
        foreign_mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="foreign@links-local.invalid",
            company_profile=other_company,
            status=MailboxConnection.Status.ACTIVE,
        )
        corr = self.approved_out()
        with self.assertRaises(SendBlocked):
            send_correspondence(
                corr, sender=self.owner, mailbox_id=foreign_mailbox.id,
            )
        self.assertFalse(corr.send_events.exists())

    def test_shared_mailbox_not_assigned_to_the_correspondence_project_is_rejected(self):
        corr = self.approved_out(project=self.project.id)
        # Deliberately no MailboxProjectAssignment created for self.shared_mailbox.
        with self.assertRaises(SendBlocked):
            send_correspondence(
                corr, sender=self.owner, mailbox_id=self.shared_mailbox.id,
            )

    def test_shared_mailbox_assigned_to_the_project_can_be_used(self):
        MailboxProjectAssignment.objects.create(
            mailbox=self.shared_mailbox, project=self.project,
        )
        corr = self.approved_out(project=self.project.id)
        event, _, _ = self.send_via_graph(corr, self.shared_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)


class TokenAndGraphFailureTests(GraphSendTestBase):
    def test_no_credentials_at_all_is_a_failed_send_not_a_block(self):
        # Authorization passes (owner, active, individual) -- the only problem is the
        # Microsoft identity having no stored credentials at all. Section 10's own distinct
        # "absence de token valide" scenario -- this is a delivery failure (FAILED event,
        # auditable), never a pre-send SendBlocked.
        UserOutlookCredentials.objects.filter(user=self.owner).delete()
        corr = self.approved_out()
        with override_settings(MS_CLIENT_ID="client-id", MS_CLIENT_SECRET="client-secret"):
            event = send_correspondence(
                corr, sender=self.owner, mailbox_id=self.individual_mailbox.id,
            )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIn("No valid Microsoft 365 access token", event.error_message)
        corr.refresh_from_db()
        self.assertEqual(corr.status, "DRAFT")

    def test_refresh_token_rejected_by_microsoft_is_a_failed_send(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={"refresh_ok": False, "refresh_payload": {"error": "invalid_grant"}},
        )
        self.assertEqual(event.result, Result.FAILED)

    def test_graph_non_202_response_is_a_failed_send_with_the_real_error(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={
                "graph_status": 403,
                "graph_body": {
                    "error": {"code": "ErrorAccessDenied", "message": "Not authorized."}
                },
            },
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIn("ErrorAccessDenied", event.error_message)
        self.assertIn("Not authorized.", event.error_message)
        corr.refresh_from_db()
        self.assertEqual(corr.status, "DRAFT")

    def test_202_response_never_produces_a_fabricated_message_id(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        self.assertEqual(event.provider_message_id, "")


class IndividualSenderIdentityTests(GraphSendTestBase):
    """MAIL-1.2A-REVIEW section 2: an individual MailboxConnection's email_address must
    actually match the Microsoft identity behind the connected token -- never trusted as a
    bare LINKS-side claim."""

    def test_mismatched_connected_identity_blocks_the_send(self):
        corr = self.approved_out()
        event, mock_post, mock_get = self.send_via_graph(
            corr, self.individual_mailbox,
            identity_kwargs={"identity_email": "someone-else@attacker.invalid"},
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIn("does not match", event.error_message)
        # The real sendMail call must never be attempted once identity verification fails.
        self.assertIsNone(self._graph_call(mock_post))
        corr.refresh_from_db()
        self.assertEqual(corr.status, "DRAFT")

    def test_identity_check_failure_fails_closed_not_open(self):
        corr = self.approved_out()
        event, mock_post, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            identity_kwargs={"identity_status": 401},
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIsNone(self._graph_call(mock_post))

    def test_matching_identity_still_sends_normally(self):
        # Already exercised implicitly by every other SUCCESS test (send_via_graph defaults
        # identity_email to the mailbox's own address) -- this test names that behavior
        # explicitly as its own regression guard.
        corr = self.approved_out()
        event, _, mock_get = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        mock_get.assert_called_once()

    def test_shared_mailbox_never_triggers_the_individual_identity_check(self):
        # Section 2: "conserver le modèle de délégation existant" for shared mailboxes --
        # the connected delegate is never expected to BE the shared mailbox's own identity.
        corr = self.approved_out()
        event, _, mock_get = self.send_via_graph(corr, self.shared_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        mock_get.assert_not_called()


class UncertainDeliveryTests(GraphSendTestBase):
    """MAIL-1.2A-REVIEW section 3: a failure where Microsoft may have already accepted the
    send must be visibly distinct from an ordinary, provably-safe-to-retry failure -- never a
    stronger guarantee, never an automatic retry, only honest, auditable uncertainty."""

    def test_read_timeout_after_the_request_was_sent_is_marked_uncertain(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={"graph_raises": requests.exceptions.ReadTimeout("no response")},
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIn("UNCERTAIN DELIVERY", event.error_message)
        self.assertIn("no response", event.error_message)

    def test_connection_reset_mid_response_is_marked_uncertain(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={"graph_raises": requests.ConnectionError("connection reset")},
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIn("UNCERTAIN DELIVERY", event.error_message)
        self.assertIn("connection reset", event.error_message)

    def test_connect_timeout_before_anything_was_sent_is_not_marked_uncertain(self):
        # The TCP handshake itself never completed -- provably nothing reached Microsoft.
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={
                "graph_raises": requests.exceptions.ConnectTimeout("handshake timed out"),
            },
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertNotIn("UNCERTAIN DELIVERY", event.error_message)
        self.assertIn("handshake timed out", event.error_message)

    def test_a_clean_graph_rejection_is_not_marked_uncertain(self):
        # Graph DID answer (403) -- there is nothing ambiguous about this outcome.
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={
                "graph_status": 403,
                "graph_body": {"error": {"code": "Forbidden", "message": "no."}},
            },
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertNotIn("UNCERTAIN DELIVERY", event.error_message)


class UncertainSendRetryGuardTests(GraphSendTestBase):
    """MAIL-1.2A-FINAL section 1: once a correspondence's most recent send attempt is
    uncertain, every subsequent standard send attempt (any provider, any mailbox) is blocked
    -- never silently retried, never routed around."""

    def _make_uncertain(self, corr):
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={"graph_raises": requests.exceptions.ReadTimeout("no response")},
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIn("UNCERTAIN DELIVERY", event.error_message)
        return event

    def test_retry_after_an_uncertain_send_is_blocked(self):
        corr = self.approved_out()
        self._make_uncertain(corr)
        with self.assertRaises(SendBlocked) as ctx:
            self.send_via_graph(corr, self.individual_mailbox)
        self.assertIn("uncertain", str(ctx.exception).lower())
        self.assertIn("Sent Items", str(ctx.exception))
        # Still exactly one event -- the blocked attempt never claimed a new slot.
        self.assertEqual(corr.send_events.count(), 1)

    def test_no_new_graph_call_is_made_while_blocked(self):
        corr = self.approved_out()
        self._make_uncertain(corr)
        with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
            with self.assertRaises(SendBlocked):
                send_correspondence(
                    corr, sender=self.owner, mailbox_id=self.individual_mailbox.id,
                )
        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_the_block_also_prevents_a_subsequent_smtp_attempt(self):
        # Section 1: the risk is the correspondence's content possibly already reaching the
        # external recipient, independent of which internal provider is used for a retry.
        from django.core import mail

        corr = self.approved_out()
        self._make_uncertain(corr)
        mail.outbox = []
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)
        self.assertEqual(len(mail.outbox), 0)

    def test_retry_after_a_certain_failure_is_still_allowed(self):
        # Contrast case: an ordinary, unambiguous failure must keep permitting a normal retry,
        # exactly as it already did before this lot (CORR-DIST-2's own established rule).
        corr = self.approved_out()
        first_event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={
                "graph_status": 403,
                "graph_body": {"error": {"code": "Forbidden", "message": "no."}},
            },
        )
        self.assertEqual(first_event.result, Result.FAILED)
        second_event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(second_event.result, Result.SUCCESS)

    def test_a_successful_send_is_never_blocked(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        # A plain resend after success remains governed by can_resend, unaffected.
        corr.refresh_from_db()
        second_event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(second_event.result, Result.SUCCESS)


class AttachmentTests(GraphSendTestBase):
    def _approved_with_attachment(self, *, file_size):
        corr = self.create_out(recipient_contact=self.recipient_contact.id)
        attachment = CorrespondenceAttachment(
            correspondence=corr, file_name="letter.pdf", file_size=file_size,
            uploaded_by=self.owner,
        )
        attachment.file.save(
            "letter.pdf", SimpleUploadedFile("letter.pdf", b"content"), save=False,
        )
        attachment.save()
        wf = start_validation(
            corr, WorkflowType.ONE_STEP, self.author, None, self.approver,
            started_by=self.author,
        )
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr, attachment

    def test_attachments_are_included_as_base64_file_attachments(self):
        corr, attachment = self._approved_with_attachment(file_size=7)
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        graph_call = self._graph_call(mock_post)
        sent_attachments = graph_call.kwargs["json"]["message"]["attachments"]
        self.assertEqual(len(sent_attachments), 1)
        self.assertEqual(sent_attachments[0]["name"], "letter.pdf")
        self.assertEqual(
            sent_attachments[0]["@odata.type"], "#microsoft.graph.fileAttachment",
        )
        import base64

        self.assertEqual(
            base64.b64decode(sent_attachments[0]["contentBytes"]), b"content",
        )

    def test_oversized_attachments_are_blocked_before_any_graph_call(self):
        corr, attachment = self._approved_with_attachment(
            file_size=MAX_GRAPH_ATTACHMENTS_BYTES + 1,
        )
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.FAILED)
        self.assertIn("too large", event.error_message)
        self.assertIsNone(self._graph_call(mock_post))


class DuplicatePreventionTests(GraphSendTestBase):
    def test_a_graph_send_cannot_start_while_another_send_is_in_progress(self):
        corr = self.approved_out()
        CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, result=Result.IN_PROGRESS,
        )
        with self.assertRaises(SendBlocked):
            send_correspondence(
                corr, sender=self.owner, mailbox_id=self.individual_mailbox.id,
            )

    def test_a_resend_after_a_successful_graph_send_creates_independent_evidence(self):
        corr = self.approved_out()
        first_event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        corr.refresh_from_db()
        second_event, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertNotEqual(first_event.id, second_event.id)
        self.assertEqual(corr.send_events.count(), 2)


class SmtpRegressionTests(GraphSendTestBase):
    """Cheap, direct confirmation that mailbox_id=None (every pre-existing caller) still takes
    the exact original SMTP path -- the full pre-existing suite in test_correspondence_send.py
    is the real regression coverage and is run unmodified alongside this file."""

    def test_omitting_mailbox_id_still_sends_via_smtp(self):
        from django.core import mail

        corr = self.approved_out()
        mail.outbox = []
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.provider, "smtp")
        self.assertIsNone(event.mailbox_id)
        self.assertEqual(len(mail.outbox), 1)
