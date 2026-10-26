"""LINKS-RECETTE-03: connecting and registering Microsoft 365 accounts in the recette environment.

Only a mailbox of the explicit recette tenant (and on the allow-list, as a FULL address) may be
registered or connected. The tenant is confirmed by Microsoft Graph itself (GET /organization, never
decoded from the token) and the identity behind the token by GET /me; nothing is stored for anything
else -- for the Mail flow and for the Calendar/Contacts flow alike. Every Microsoft call is mocked;
outside recette nothing changes.
"""
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from userapis.mailbox_oauth import MailOAuthError, build_mail_oauth_url, handle_mail_oauth_callback
from userapis.microsoft_outlook import create_outlook_calendar_event
from userapis.models import MailboxConnection, MailOAuthState
from userapis.views.microsoft_auth import MicrosoftAuthViewSet
from userapis.test_mailbox import _grant_mail_admin, _make_company, _make_user
from userapis.test_mailbox_oauth import _start

TENANT = "11111111-2222-3333-4444-555555555555"
ADDRESS = "owner@recette-tenant.invalid"
TOKENS = {"access_token": "opaque-graph-token", "refresh_token": "ms-refresh", "expires_in": 3600}


def _graph_get(*, tenant=TENANT, identity=ADDRESS, organization_status=200, identity_status=200):
    """GET /organization (the tenant) and GET /me (the identity), as Microsoft Graph would answer."""
    def dispatch(url, *args, **kwargs):
        response = Mock()
        if "/organization" in url:
            response.status_code = organization_status
            response.json.return_value = {"value": [{"id": tenant}]}
        else:
            response.status_code = identity_status
            response.json.return_value = {"mail": identity, "userPrincipalName": identity}
        return response
    return dispatch


class RecetteMailboxTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = _make_company()
        self.admin = _make_user("admin@example.com", self.company)
        _grant_mail_admin(self.admin, self.company)
        self.owner = _make_user("owner@example.com", self.company)
        self.key = Fernet.generate_key().decode("ascii")
        self.settings = dict(
            RECETTE_MODE=True,
            RECETTE_ALLOWED_SENDERS=[ADDRESS],
            RECETTE_M365_TENANT_ID=TENANT,
            MS_TOKEN_ENCRYPTION_KEYS=self.key,
        )

    def individual(self, address=ADDRESS):
        return MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address=address, company_profile=self.company, owner_user=self.owner,
            status=MailboxConnection.Status.PENDING,
        )

    def callback(self, mailbox, tokens=TOKENS, **graph):
        token = _start(self.owner, mailbox)
        with patch("userapis.mailbox_oauth.requests.post") as post, \
                patch("requests.get", side_effect=_graph_get(**graph)) as get:
            post.return_value.json.return_value = tokens
            try:
                handle_mail_oauth_callback(token, "code")
                error = None
            except MailOAuthError as raised:
                error = raised
        return error, get

    def assertNothingConnected(self, mailbox):
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, MailboxConnection.Status.PENDING)
        self.assertFalse(hasattr(self.owner, "outlook_credentials"))


class RecetteOAuthTests(RecetteMailboxTestBase):
    def test_a_mailbox_of_the_recette_tenant_connects(self):
        mailbox = self.individual()
        with override_settings(**self.settings):
            error, get = self.callback(mailbox)
        self.assertIsNone(error)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, MailboxConnection.Status.ACTIVE)
        urls = [call.args[0] for call in get.call_args_list]
        self.assertTrue(any("/organization" in u for u in urls))   # tenant confirmed by Microsoft
        self.assertTrue(any("/me" in u for u in urls))             # identity confirmed by Microsoft

    def test_a_token_microsoft_places_in_another_tenant_is_refused_and_nothing_is_stored(self):
        mailbox = self.individual()
        with override_settings(**self.settings):
            error, get = self.callback(mailbox, tenant="99999999-0000-0000-0000-000000000000")
        self.assertIsNotNone(error)
        self.assertEqual(get.call_count, 1)  # only the tenant question; no identity probe afterwards
        self.assertNothingConnected(mailbox)

    def test_an_unconfirmed_tenant_is_refused(self):
        mailbox = self.individual()
        with override_settings(**self.settings):
            error, _ = self.callback(mailbox, organization_status=403)
        self.assertIsNotNone(error)
        self.assertNothingConnected(mailbox)

    def test_no_explicit_tenant_configuration_refuses_every_connection(self):
        mailbox = self.individual()
        with override_settings(**{**self.settings, "RECETTE_M365_TENANT_ID": ""}):
            error, get = self.callback(mailbox)
        self.assertIsNotNone(error)
        get.assert_not_called()
        self.assertNothingConnected(mailbox)

    def test_the_right_tenant_is_not_enough_the_identity_must_match_the_mailbox(self):
        mailbox = self.individual()
        with override_settings(**self.settings):
            error, _ = self.callback(mailbox, identity="someone.else@recette-tenant.invalid")
        self.assertIsNotNone(error)
        self.assertNothingConnected(mailbox)

    def test_a_failed_identity_probe_refuses(self):
        mailbox = self.individual()
        with override_settings(**self.settings):
            error, _ = self.callback(mailbox, identity_status=403)
        self.assertIsNotNone(error)
        self.assertNothingConnected(mailbox)

    def test_a_mailbox_outside_the_allow_list_cannot_start_or_finish_a_connection(self):
        mailbox = self.individual("owner@another-company.invalid")
        with override_settings(**self.settings):
            with self.assertRaises(MailOAuthError):
                build_mail_oauth_url(self.owner, mailbox)
        self.assertFalse(MailOAuthState.objects.filter(mailbox=mailbox).exists())
        # a connection started before the allow-list changed is still refused at the callback
        token = _start(self.owner, mailbox)
        with override_settings(**self.settings), patch("userapis.mailbox_oauth.requests.post") as post:
            with self.assertRaises(MailOAuthError):
                handle_mail_oauth_callback(token, "code")
        post.assert_not_called()          # no token exchange for a refused mailbox
        self.assertNothingConnected(mailbox)

    def test_outside_recette_the_flow_is_unchanged(self):
        mailbox = self.individual("owner@example.com")
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            token = _start(self.owner, mailbox)
            with patch("userapis.mailbox_oauth.requests.post") as post, \
                    patch("requests.get") as get:
                post.return_value.json.return_value = {
                    "access_token": "ms-access-token", "refresh_token": "r", "expires_in": 3600,
                }
                handle_mail_oauth_callback(token, "code")
        get.assert_not_called()           # no tenant or identity probe outside recette
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, MailboxConnection.Status.ACTIVE)


class RecetteCalendarOAuthTests(RecetteMailboxTestBase):
    """The legacy Calendar/Contacts connection trusts a user id carried in an unsigned, anonymous
    `state` (anyone can complete it with their own Microsoft account for another user's id), so it
    is disabled in recette, and so is creating Outlook events (which invite attendees by e-mail).
    Outside recette nothing changes."""

    CALLBACK = "/api/user/auth/outlook/callback"

    def callback(self, user_id=None):
        state = f"{user_id or self.owner.id}:Energy Consultant Solution"
        with patch("userapis.views.microsoft_auth.get_access_token", return_value=dict(TOKENS)) as exchange, \
                patch("userapis.views.microsoft_auth.sync_outlook_events",
                      return_value={"success": True}) as sync, \
                patch("requests.get") as get:
            response = self.client.get(self.CALLBACK, {"code": "code", "state": state})
        return response, exchange, sync, get

    def test_the_calendar_callback_is_disabled_in_recette_even_for_a_forged_state(self):
        victim = _make_user("victim@example.com", self.company)
        with override_settings(**self.settings):
            response, exchange, sync, get = self.callback(user_id=victim.id)
        self.assertEqual(response.status_code, 403)
        exchange.assert_not_called()      # the authorization code is never even exchanged
        sync.assert_not_called()
        get.assert_not_called()
        victim.refresh_from_db()
        self.assertFalse(hasattr(victim, "outlook_credentials"))

    def test_starting_the_calendar_connection_is_disabled_in_recette(self):
        # Called directly: the DRF default permission (license tables of another app) is not under test.
        with override_settings(**self.settings), patch("userapis.views.microsoft_auth.build_outlook_oauth_url") as build:
            response = MicrosoftAuthViewSet().auth(Mock())
        self.assertEqual(response.status_code, 403)
        build.assert_not_called()

    def test_outside_recette_the_calendar_flow_is_unchanged(self):
        response, exchange, sync, _ = self.callback()
        self.assertEqual(response.status_code, 302)
        exchange.assert_called_once()
        sync.assert_called_once()
        self.owner.refresh_from_db()
        self.assertTrue(hasattr(self.owner, "outlook_credentials"))

    def test_creating_outlook_events_is_disabled_in_recette(self):
        with override_settings(**self.settings), patch("requests.post") as post, patch("requests.get") as get:
            result = create_outlook_calendar_event(
                self.owner, Mock(), attendee_emails=["client@real-company.com"],
            )
        self.assertFalse(result["success"])
        self.assertIn("recette", result["error"])
        post.assert_not_called()
        get.assert_not_called()


class RecetteMailboxRegistrationTests(RecetteMailboxTestBase):
    URL = "/api/user/mailbox-connections/"

    def test_only_allow_listed_addresses_can_be_registered(self):
        self.client.force_authenticate(self.admin)
        with override_settings(**self.settings):
            refused = self.client.post(
                self.URL, {"provider": "microsoft", "email_address": "info@real-company.com"},
            )
            accepted = self.client.post(
                self.URL, {"provider": "microsoft", "email_address": ADDRESS.upper()},
            )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("email_address", refused.data)
        self.assertFalse(MailboxConnection.objects.filter(email_address="info@real-company.com").exists())
        self.assertEqual(accepted.status_code, 201, accepted.data)

    def test_changing_a_mailbox_to_a_foreign_address_is_refused(self):
        mailbox = self.individual()
        self.client.force_authenticate(self.admin)
        with override_settings(**self.settings):
            response = self.client.patch(
                f"{self.URL}{mailbox.id}/", {"email_address": "info@real-company.com"},
            )
        self.assertEqual(response.status_code, 400)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.email_address, ADDRESS)

    def test_outside_recette_any_address_can_be_registered(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.URL, {"provider": "microsoft", "email_address": "info@example.com"},
        )
        self.assertEqual(response.status_code, 201, response.data)
