# MAIL-1.1C2 / MAIL-1.1C2-S: Microsoft 365 Mail OAuth for MailboxConnection. Every Microsoft
# Graph call is mocked (requests.post/requests.get patched within userapis.mailbox_oauth's own
# namespace) -- never a real call, per the ticket's own explicit instruction.
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from userapis.mailbox_access import can_use_mailbox
from userapis.mailbox_oauth import (
    MailOAuthError,
    build_mail_oauth_url,
    handle_mail_oauth_callback,
)
from userapis.models import (
    MailboxConnection,
    MailboxProjectAssignment,
    MailboxUserGrant,
    MailOAuthState,
)
from userapis.test_mailbox import _grant_mail_admin, _make_company, _make_project, _make_user


def _tokens(**over):
    data = {"access_token": "ms-access-token", "refresh_token": "ms-refresh-token", "expires_in": 3600}
    data.update(over)
    return data


def _start(user, mailbox):
    """Mirrors what a real POST .../connect-oauth/ hands back: the opaque state_token inside
    the generated oauth_url's own `state` query param -- never raw ids anymore
    (MAIL-1.1C2-S)."""
    url = build_mail_oauth_url(user, mailbox)
    state = parse_qs(urlparse(url).query)["state"][0]
    prefix, token = state.split(":")
    assert prefix == "mail"
    return token


class MailOAuthStartTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = _make_company()
        self.admin = _make_user("admin@example.com", self.company)
        _grant_mail_admin(self.admin, self.company)
        self.owner = _make_user("owner@example.com", self.company)
        self.plain_user = _make_user("plain@example.com", self.company)

    def test_individual_owner_can_start(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="owner@example.com",
            company_profile=self.company,
            owner_user=self.owner,
        )
        self.client.force_authenticate(self.owner)
        response = self.client.post(f"/api/user/mailbox-connections/{mailbox.id}/connect-oauth/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("oauth_url", response.data)

        query = parse_qs(urlparse(response.data["oauth_url"]).query)
        state = query["state"][0]
        self.assertTrue(state.startswith("mail:"))
        # MAIL-1.1C2-S: state now carries exactly one opaque uuid4 token, never the old
        # "mail:<user_id>:<mailbox_id>" 3-part, predictable/non-expiring shape this checkpoint
        # replaced. (A raw id substring check against the token is unreliable -- a uuid4's hex
        # digits can coincidentally contain a small id's decimal digits by chance; the shape
        # check below plus a real uuid4 parse is the meaningful assertion.)
        parts = state.split(":")
        self.assertEqual(len(parts), 2)
        token = parts[1]
        UUID(token)  # raises ValueError if this weren't a real uuid4 token
        state_row = MailOAuthState.objects.get(token=token)
        self.assertEqual(state_row.user_id, self.owner.id)
        self.assertEqual(state_row.mailbox_id, mailbox.id)
        self.assertIsNone(state_row.consumed_at)
        self.assertIn("Mail.Send", query["scope"][0])
        self.assertIn("Calendars.ReadWrite", query["scope"][0])  # union, never a narrower token

    def test_admin_can_start_for_shared_mailbox(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(f"/api/user/mailbox-connections/{mailbox.id}/connect-oauth/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_refused_without_permission_or_ownership(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="owner@example.com",
            company_profile=self.company,
            owner_user=self.owner,
        )
        self.client.force_authenticate(self.plain_user)
        response = self.client.post(f"/api/user/mailbox-connections/{mailbox.id}/connect-oauth/")
        self.assertEqual(response.status_code, 403)

    def test_cross_company_mailbox_is_not_reachable(self):
        other_company = _make_company("Other")
        foreign_mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="foreign@example.com",
            company_profile=other_company,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/user/mailbox-connections/{foreign_mailbox.id}/connect-oauth/"
        )
        self.assertIn(response.status_code, (403, 404))


class MailOAuthStateSecurityTests(TestCase):
    """MAIL-1.1C2-S section 2: the state token itself -- unpredictable, single-use,
    expiring, never a self-asserted proof of authorization on its own."""

    def setUp(self):
        self.company = _make_company()
        self.admin = _make_user("admin@example.com", self.company)
        _grant_mail_admin(self.admin, self.company)
        self.owner = _make_user("owner@example.com", self.company)
        self.key = Fernet.generate_key().decode("ascii")
        self.mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="owner@example.com",
            company_profile=self.company,
            owner_user=self.owner,
            status=MailboxConnection.Status.PENDING,
        )

    def test_malformed_state_token_is_rejected(self):
        with self.assertRaises(MailOAuthError):
            handle_mail_oauth_callback("not-a-uuid", "code")

    def test_unknown_forged_state_token_is_rejected(self):
        # A syntactically valid UUID that was never actually issued by build_mail_oauth_url.
        with self.assertRaises(MailOAuthError):
            handle_mail_oauth_callback(str(uuid4()), "code")

    def test_expired_state_is_rejected(self):
        token = _start(self.owner, self.mailbox)
        state_row = MailOAuthState.objects.get(token=token)
        state_row.created_at = timezone.now() - timedelta(
            minutes=MailOAuthState.EXPIRY_MINUTES + 1
        )
        state_row.save(update_fields=["created_at"])
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            with patch("userapis.mailbox_oauth.requests.post") as post:
                post.return_value.json.return_value = _tokens()
                with self.assertRaises(MailOAuthError):
                    handle_mail_oauth_callback(token, "code")

    def test_state_cannot_be_replayed_after_a_successful_use(self):
        token = _start(self.owner, self.mailbox)
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            with patch("userapis.mailbox_oauth.requests.post") as post:
                post.return_value.json.return_value = _tokens()
                handle_mail_oauth_callback(token, "code")
                # Second attempt, same token -- must be refused even though the first
                # succeeded cleanly.
                with self.assertRaises(MailOAuthError):
                    handle_mail_oauth_callback(token, "code")

    def test_state_is_marked_consumed_even_when_a_later_step_fails(self):
        # No encryption key configured -> store_mail_tokens fails AFTER state consumption --
        # the token must still never be reusable (section 2's "usage unique", independent of
        # whether the overall attempt succeeded).
        token = _start(self.owner, self.mailbox)
        # Hermetic: a developer's local .env.local may now carry a real key (MAIL-1.2E).
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=""), patch(
            "userapis.mailbox_oauth.requests.post"
        ) as post:
            post.return_value.json.return_value = _tokens()
            with self.assertRaises(MailOAuthError):
                handle_mail_oauth_callback(token, "code")
        self.assertIsNotNone(MailOAuthState.objects.get(token=token).consumed_at)
        with self.assertRaises(MailOAuthError):
            handle_mail_oauth_callback(token, "code")

    def test_mailbox_substituted_after_start_is_still_caught_at_callback(self):
        # Simulates the mailbox having moved to a different company between start and
        # completion (direct model construction -- the normal flow could never produce this
        # combination, since connect-oauth's own can_start_mail_oauth gate already prevents
        # it; this proves the callback's OWN re-check is real, not merely decorative).
        other_company = _make_company("Other")
        foreign_mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="foreign@example.com",
            company_profile=other_company,
        )
        state_row = MailOAuthState.objects.create(user=self.admin, mailbox=foreign_mailbox)
        with self.assertRaises(MailOAuthError):
            handle_mail_oauth_callback(str(state_row.token), "code")

    def test_user_substituted_after_start_is_still_caught_at_callback(self):
        outsider = _make_user("outsider@example.com", self.company)
        state_row = MailOAuthState.objects.create(user=outsider, mailbox=self.mailbox)
        with self.assertRaises(MailOAuthError):
            handle_mail_oauth_callback(str(state_row.token), "code")


class MailOAuthCallbackTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = _make_company()
        self.admin = _make_user("admin@example.com", self.company)
        _grant_mail_admin(self.admin, self.company)
        self.owner = _make_user("owner@example.com", self.company)
        self.key = Fernet.generate_key().decode("ascii")

    def _individual_mailbox(self):
        # status=PENDING: matches the real creation flow (MailboxConnectionCreateSerializer
        # always forces "pending", MAIL-1.1C1) -- the model's own default stays "active" only
        # for MAIL-1.1B's own pre-existing tests, not a realistic starting point here.
        return MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="owner@example.com",
            company_profile=self.company,
            owner_user=self.owner,
            status=MailboxConnection.Status.PENDING,
        )

    def _shared_mailbox(self):
        return MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="info@example.com",
            company_profile=self.company,
            status=MailboxConnection.Status.PENDING,
        )

    def test_consent_refused_via_the_http_view_never_touches_the_mailbox(self):
        mailbox = self._individual_mailbox()
        state_row = MailOAuthState.objects.create(user=self.owner, mailbox=mailbox)
        response = self.client.get(
            "/api/user/auth/outlook/callback",
            {"error": "access_denied", "state": f"mail:{state_row.token}"},
        )
        self.assertEqual(response.status_code, 400)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, MailboxConnection.Status.PENDING)
        # Consent refusal never even reaches _consume_state (no code to exchange) -- the
        # token is left usable for a genuine retry.
        self.assertIsNone(MailOAuthState.objects.get(pk=state_row.pk).consumed_at)

    # --- happy paths -------------------------------------------------------------------------

    @override_settings(MS_TOKEN_ENCRYPTION_KEYS="")
    def test_individual_connection_without_a_key_is_blocked_not_plaintext(self):
        # Section 3's blocking condition: no key configured -> MailOAuthError, never a
        # plaintext token written.
        mailbox = self._individual_mailbox()
        token = _start(self.owner, mailbox)
        with patch("userapis.mailbox_oauth.requests.post") as post:
            post.return_value.json.return_value = _tokens()
            with self.assertRaises(MailOAuthError):
                handle_mail_oauth_callback(token, "code")
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, MailboxConnection.Status.PENDING)
        self.assertFalse(hasattr(self.owner, "outlook_credentials"))

    def test_individual_connection_success_activates_and_encrypts(self):
        mailbox = self._individual_mailbox()
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            token = _start(self.owner, mailbox)
            with patch("userapis.mailbox_oauth.requests.post") as post:
                post.return_value.json.return_value = _tokens()
                result = handle_mail_oauth_callback(token, "code")

            self.assertEqual(result.status, MailboxConnection.Status.ACTIVE)
            self.assertEqual(result.connected_by_id, self.owner.id)

            from models.models import UserOutlookCredentials

            raw_access = (
                UserOutlookCredentials.objects.filter(user=self.owner)
                .values_list("outlook_access_token", flat=True)
                .first()
            )
            self.assertTrue(raw_access.startswith("encv1:"))
            # Never exposed in the (simulated) response the HTTP callback would return.
            credentials = self.owner.outlook_credentials
            self.assertEqual(credentials.outlook_access_token, "ms-access-token")  # decrypted on read

    def test_shared_connection_with_verified_exchange_access_activates(self):
        mailbox = self._shared_mailbox()
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            token = _start(self.admin, mailbox)
            with patch("userapis.mailbox_oauth.requests.post") as post, patch(
                "userapis.mailbox_oauth.requests.get"
            ) as get:
                post.return_value.json.return_value = _tokens()
                get.return_value.status_code = 200
                result = handle_mail_oauth_callback(token, "code")
        self.assertEqual(result.status, MailboxConnection.Status.ACTIVE)
        self.assertEqual(result.connected_by_id, self.admin.id)

    def test_shared_connection_with_unverified_exchange_access_does_not_activate(self):
        # Section 5: OAuth success alone is never sufficient for a shared mailbox.
        mailbox = self._shared_mailbox()
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            token = _start(self.admin, mailbox)
            with patch("userapis.mailbox_oauth.requests.post") as post, patch(
                "userapis.mailbox_oauth.requests.get"
            ) as get:
                post.return_value.json.return_value = _tokens()
                get.return_value.status_code = 403  # ErrorAccessDenied
                result = handle_mail_oauth_callback(token, "code")
        self.assertEqual(result.status, MailboxConnection.Status.NEEDS_RECONSENT)
        # The delegate identity is still recorded -- only the "active" claim is withheld.
        self.assertEqual(result.connected_by_id, self.admin.id)

    def test_active_status_never_implies_a_verified_send_right(self):
        # MAIL-1.1C2-S section 4: "active" here means (1) OAuth succeeded and (2) the inbox
        # was read-verified -- it must never be read anywhere as (3) a verified Send As/Send
        # on Behalf right. Confirmed by construction: MailboxConnection carries no field that
        # could be mistaken for a send-verified claim, and reaching "active" only ever
        # required a read probe (get, never post/patch/delete) against Graph.
        mailbox = self._shared_mailbox()
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            token = _start(self.admin, mailbox)
            with patch("userapis.mailbox_oauth.requests.post") as post, patch(
                "userapis.mailbox_oauth.requests.get"
            ) as get:
                post.return_value.json.return_value = _tokens()
                get.return_value.status_code = 200
                result = handle_mail_oauth_callback(token, "code")
        self.assertEqual(result.status, MailboxConnection.Status.ACTIVE)
        field_names = {f.name for f in MailboxConnection._meta.get_fields()}
        self.assertNotIn("send_verified", field_names)
        self.assertNotIn("can_send", field_names)
        # The verification call itself was a GET (read), never a mutating method.
        get.assert_called_once()
        post_calls_to_graph = [
            c for c in post.call_args_list if "graph.microsoft.com" in str(c)
        ]
        self.assertEqual(post_calls_to_graph, [])

    def test_existing_assignments_and_grants_survive_a_shared_connection(self):
        mailbox = self._shared_mailbox()
        project = _make_project(self.company)
        grantee = _make_user("grantee@example.com", self.company)
        assignment = MailboxProjectAssignment.objects.create(mailbox=mailbox, project=project)
        grant = MailboxUserGrant.objects.create(
            mailbox=mailbox, user=grantee, can_send=True, can_read=True
        )
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            token = _start(self.admin, mailbox)
            with patch("userapis.mailbox_oauth.requests.post") as post, patch(
                "userapis.mailbox_oauth.requests.get"
            ) as get:
                post.return_value.json.return_value = _tokens()
                get.return_value.status_code = 200
                handle_mail_oauth_callback(token, "code")
        self.assertTrue(
            MailboxProjectAssignment.objects.filter(pk=assignment.pk, mailbox=mailbox).exists()
        )
        grant.refresh_from_db()
        self.assertTrue(grant.can_send)
        self.assertTrue(grant.can_read)
        # handle_mail_oauth_callback operated on its own freshly-fetched MailboxConnection
        # instance (by design -- it never trusts an in-memory object from the caller); this
        # local `mailbox` variable must be refreshed to see the resulting "active" status.
        mailbox.refresh_from_db()
        # And the grant still works exactly as before via the unchanged access-control service.
        self.assertTrue(can_use_mailbox(grantee, mailbox, for_sending=True))

    def test_token_exchange_failure_is_rejected_cleanly(self):
        mailbox = self._individual_mailbox()
        token = _start(self.owner, mailbox)
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            with patch("userapis.mailbox_oauth.requests.post") as post:
                post.return_value.json.return_value = {"error": "invalid_grant"}
                with self.assertRaises(MailOAuthError):
                    handle_mail_oauth_callback(token, "code")
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.status, MailboxConnection.Status.PENDING)

    def test_mail_token_refresh_keeps_encryption(self):
        # Section 3: "le renouvellement des tokens conserve une protection équivalente" --
        # exercises the REAL, pre-existing refresh_outlook_access_token (SECURITY-MAIL-0),
        # which MAIL-1.1C2's own store_mail_tokens does not replace or duplicate -- a Mail
        # connection's token is refreshed through the exact same, already-tested mechanism
        # every Calendar/Contacts connection already uses.
        mailbox = self._individual_mailbox()
        with override_settings(MS_TOKEN_ENCRYPTION_KEYS=self.key):
            token = _start(self.owner, mailbox)
            with patch("userapis.mailbox_oauth.requests.post") as post:
                post.return_value.json.return_value = _tokens()
                handle_mail_oauth_callback(token, "code")

            from userapis.microsoft_outlook import refresh_outlook_access_token

            self.owner.outlook_credentials.outlook_token_expires = timezone.now() - timedelta(
                minutes=5
            )
            self.owner.outlook_credentials.save(update_fields=["outlook_token_expires"])
            with override_settings(MS_CLIENT_ID="client-id", MS_CLIENT_SECRET="client-secret"):
                with patch("userapis.microsoft_outlook.requests.post") as refresh_post:
                    refresh_post.return_value.ok = True
                    refresh_post.return_value.json.return_value = {
                        "access_token": "refreshed-mail-token",
                        "refresh_token": "rotated-mail-refresh",
                        "expires_in": 3600,
                    }
                    result = refresh_outlook_access_token(self.owner)
            self.assertEqual(result, "refreshed-mail-token")

            from models.models import UserOutlookCredentials

            raw = (
                UserOutlookCredentials.objects.filter(user=self.owner)
                .values_list("outlook_access_token", flat=True)
                .first()
            )
            self.assertTrue(raw.startswith("encv1:"))


class CalendarContactsCompatibilityTests(TestCase):
    """Section 2/6: the pre-existing Calendar/Contacts flow must be byte-for-byte unaffected --
    confirmed by driving the SAME callback view with a non-"mail:" state and the pre-existing
    mocked Microsoft responses, exactly as userapis.tests.MicrosoftOutlookAuthCallbackTests and
    OutlookEventSyncTests already do (re-run as part of this lot's own targeted suite, not
    duplicated here)."""

    def test_non_mail_state_still_takes_the_original_code_path(self):
        from models.models import User, UserOutlookCredentials

        user = User.objects.create(
            email="calendar-user@example.com", last_name="Cal", user_type="company_user",
        )
        with patch("userapis.views.microsoft_auth.get_access_token") as get_access_token, patch(
            "userapis.views.microsoft_auth.sync_outlook_events"
        ) as sync:
            get_access_token.return_value = _tokens()
            sync.return_value = {"success": True, "message": "ok"}
            client = APIClient()
            response = client.get(
                "/api/user/auth/outlook/callback",
                {"code": "code", "state": f"{user.id}:Energy Consultant Solution"},
            )
        self.assertNotEqual(response.status_code, 400)
        # Untouched legacy behaviour: plaintext write path (SECURITY-MAIL-0's write-encryption
        # flag is off by default, exactly as every prior lot's report already established).
        credentials = UserOutlookCredentials.objects.get(user=user)
        self.assertEqual(credentials.outlook_access_token, "ms-access-token")

    def test_mail_scope_extension_never_narrows_the_calendar_only_scope_string(self):
        # Section 5: "l'absence d'élargissement des scopes dans le flux Calendar standard" --
        # the pre-existing build_outlook_oauth_url must still request exactly its own,
        # unchanged scope string, never MS_MAIL_SCOPES.
        from ecpmpapp.constants import MS_MAIL_SCOPES, MS_OUTLOOK_CALENDAR_SCOPES
        from userapis.views.microsoft_auth import build_outlook_oauth_url

        user = type("U", (), {"id": 1})()
        with patch("userapis.views.microsoft_auth.get_user_active_solution", return_value="s"):
            url = build_outlook_oauth_url(user, "s")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["scope"], [MS_OUTLOOK_CALENDAR_SCOPES])
        self.assertNotIn("Mail.Send", query["scope"][0])
        self.assertNotEqual(query["scope"][0], MS_MAIL_SCOPES)
