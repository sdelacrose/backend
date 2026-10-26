"""LINKS-RECETTE-03: sending a Correspondence in the shared recette environment.

The recette guard (userapis.recette_guard) must refuse -- before any send event exists and before
any HTTP call -- everything that is not an allow-listed sender writing to allow-listed full
addresses (To, Cc and Bcc), through the view's one entry point (send_correspondence), for first
sends and resends alike. A second layer inside send_via_graph / the SMTP provider must hold on its
own, and the Microsoft tenant is confirmed by Microsoft Graph itself (GET /organization), never read
from the token. Every HTTP call is mocked; nothing real is ever sent.
"""
from unittest.mock import Mock, patch

from django.core import mail
from django.test import override_settings

from project_management.models import CorrespondenceSendEvent
from project_management.services.correspondence_send import SendBlocked, send_correspondence
from project_management.test_correspondence_send_graph import GraphSendTestBase, _mock_requests_post
from userapis.mailbox_send import GraphSendError, send_via_graph

Result = CorrespondenceSendEvent.Result

TENANT = "11111111-2222-3333-4444-555555555555"
RECIPIENT = "jane.doe@links-local.invalid"


def _graph_get(*, tenant=TENANT, identity_email, organization_status=200):
    """The two Graph reads a recette send makes: GET /organization (tenant) and GET /me (identity)."""
    def dispatch(url, *args, **kwargs):
        response = Mock()
        if "/organization" in url:
            response.status_code = organization_status
            response.json.return_value = {"value": [{"id": tenant}]}
        else:
            response.status_code = 200
            response.json.return_value = {"mail": identity_email, "userPrincipalName": identity_email}
        return response
    return dispatch


def recette(**overrides):
    settings_ = dict(
        RECETTE_MODE=True,
        RECETTE_MAIL_SEND_ENABLED=True,
        RECETTE_ALLOWED_SENDERS=["owner@links-local.invalid"],
        RECETTE_ALLOWED_RECIPIENTS=[RECIPIENT, "cc@links-local.invalid"],
        RECETTE_M365_TENANT_ID=TENANT,
    )
    settings_.update(overrides)
    return override_settings(**settings_)


class RecetteSendTests(GraphSendTestBase):
    def send_via_graph(self, corr, mailbox, *, tenant=TENANT, organization_status=200, **kwargs):
        """Like the base helper, with Graph answering the tenant question too."""
        mock_get = _graph_get(
            tenant=tenant, identity_email=mailbox.email_address, organization_status=organization_status,
        )
        with override_settings(MS_CLIENT_ID="client-id", MS_CLIENT_SECRET="client-secret"):
            with patch("requests.post", side_effect=_mock_requests_post()) as post:
                with patch("requests.get", side_effect=mock_get) as get:
                    event = send_correspondence(corr, sender=self.owner, mailbox_id=mailbox.id, **kwargs)
        return event, post, get

    def _send_expecting_block(self, corr, **kwargs):
        """The guard refuses before ANY event or HTTP call."""
        kwargs.setdefault("mailbox_id", self.individual_mailbox.id)
        with patch("requests.post") as post, patch("requests.get") as get:
            with self.assertRaises(SendBlocked) as raised:
                send_correspondence(corr, sender=self.owner, **kwargs)
        post.assert_not_called()
        get.assert_not_called()
        self.assertFalse(corr.send_events.exists())
        self.assertEqual(mail.outbox, [])
        return raised.exception

    def test_an_allow_listed_send_still_works(self):
        corr = self.approved_out()
        with recette():
            event, mock_post, mock_get = self.send_via_graph(
                corr, self.individual_mailbox, cc=["cc@links-local.invalid"],
            )
        self.assertEqual(event.result, Result.SUCCESS)
        self.assertIsNotNone(self._graph_call(mock_post))
        self.assertTrue(any("/organization" in c.args[0] for c in mock_get.call_args_list))

    def test_sending_is_refused_until_it_is_explicitly_enabled(self):
        corr = self.approved_out()
        with recette(RECETTE_MAIL_SEND_ENABLED=False):
            self._send_expecting_block(corr)

    def test_empty_allow_lists_refuse_everything(self):
        corr = self.approved_out()
        for name in ("RECETTE_ALLOWED_SENDERS", "RECETTE_ALLOWED_RECIPIENTS"):
            with recette(**{name: []}):
                self._send_expecting_block(corr)

    def test_a_recipient_outside_the_allow_list_is_refused_in_to_cc_and_bcc(self):
        corr = self.approved_out()
        with recette(RECETTE_ALLOWED_RECIPIENTS=["someone.else@links-local.invalid"]):
            self._send_expecting_block(corr)  # the To address
        with recette():
            self._send_expecting_block(corr, cc=["stranger@links-local.invalid"])
            self._send_expecting_block(corr, bcc=["stranger@links-local.invalid"])

    def test_a_sender_mailbox_outside_the_allow_list_is_refused(self):
        corr = self.approved_out()
        with recette():
            self._send_expecting_block(corr, mailbox_id=self.shared_mailbox.id)

    def test_the_smtp_path_is_refused_without_a_mailbox(self):
        corr = self.approved_out()
        with recette():
            self._send_expecting_block(corr, mailbox_id=None)

    def test_a_resend_goes_through_the_same_guard(self):
        corr = self.approved_out()
        with recette():
            first, _, _ = self.send_via_graph(corr, self.individual_mailbox)
            self.assertEqual(first.result, Result.SUCCESS)
            with patch("requests.post") as post, patch("requests.get") as get:
                with self.assertRaises(SendBlocked):
                    send_correspondence(
                        corr, sender=self.owner, mailbox_id=self.individual_mailbox.id,
                        cc=["stranger@links-local.invalid"],
                    )
            post.assert_not_called()
            get.assert_not_called()
        self.assertEqual(corr.send_events.count(), 1)

    def test_a_token_microsoft_places_in_another_tenant_never_reaches_sendmail(self):
        corr = self.approved_out()
        with recette():
            event, mock_post, _ = self.send_via_graph(
                corr, self.individual_mailbox, tenant="99999999-0000-0000-0000-000000000000",
            )
        self.assertEqual(event.result, Result.FAILED)
        self.assertIsNone(self._graph_call(mock_post))
        corr.refresh_from_db()
        self.assertNotEqual(corr.status, "ISSUED")

    def test_an_unconfirmed_tenant_never_reaches_sendmail(self):
        corr = self.approved_out()
        with recette():
            event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox, organization_status=403)
        self.assertEqual(event.result, Result.FAILED)
        self.assertIsNone(self._graph_call(mock_post))

    def test_send_via_graph_holds_on_its_own(self):
        with recette(), patch("requests.post") as post, patch("requests.get") as get:
            with self.assertRaises(GraphSendError):
                send_via_graph(
                    mailbox=self.individual_mailbox, sender_user=self.owner,
                    to="stranger@links-local.invalid", cc=[], bcc=[],
                    subject="s", body="b", attachments=[],
                )
        post.assert_not_called()
        get.assert_not_called()

    def test_outside_recette_nothing_changes(self):
        corr = self.approved_out()
        event, mock_post, mock_get = GraphSendTestBase.send_via_graph(
            self, corr, self.individual_mailbox, cc=["anyone@links-local.invalid"],
        )
        self.assertEqual(event.result, Result.SUCCESS)
        # only the pre-existing identity probe (/me); no tenant call outside recette
        self.assertFalse(any("/organization" in c.args[0] for c in mock_get.call_args_list))
