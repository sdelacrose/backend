"""LINKS-RECETTE-03: the recette mail guard (userapis.recette_guard). Pure unit tests -- no
database, no HTTP. The guard must be inert outside recette and fail closed inside it."""
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from userapis.recette_guard import (
    MS_GRAPH_ORGANIZATION_URL,
    RecetteMailBlocked,
    block_in_recette,
    check_mailbox_address,
    check_send_allowed,
    confirm_tenant_with_microsoft,
    normalize_address,
)

SENDER = "sender@recette-tenant.invalid"
RECIPIENT = "tester.one@recette-tenant.invalid"
OTHER = "tester.two@recette-tenant.invalid"
TENANT = "11111111-2222-3333-4444-555555555555"

RECETTE = dict(
    RECETTE_MODE=True,
    RECETTE_MAIL_SEND_ENABLED=True,
    RECETTE_ALLOWED_SENDERS=[SENDER],
    RECETTE_ALLOWED_RECIPIENTS=[RECIPIENT],
    RECETTE_M365_TENANT_ID=TENANT,
)


def _organization(tenant=TENANT, status=200, payload=None):
    """What Microsoft Graph's GET /organization answers (mocked)."""
    response = Mock()
    response.status_code = status
    response.json.return_value = {"value": [{"id": tenant}]} if payload is None else payload
    return response


def _send(**overrides):
    kwargs = dict(sender_address=SENDER, to=RECIPIENT, cc=[], bcc=[], provider="graph")
    kwargs.update(overrides)
    check_send_allowed(**kwargs)


class OutsideRecetteTests(SimpleTestCase):
    def test_guard_is_inert_outside_recette(self):
        _send(sender_address="anyone@real-company.com", to="client@real-company.com", provider="smtp")
        check_mailbox_address("anyone@real-company.com")
        with patch("userapis.recette_guard.requests.get") as get:
            confirm_tenant_with_microsoft("not-a-token")
            block_in_recette("anything")
        get.assert_not_called()           # no Microsoft call outside recette


@override_settings(**RECETTE)
class RecetteSendTests(SimpleTestCase):
    def test_allow_listed_sender_and_recipient_pass(self):
        _send()
        _send(sender_address=f"  {SENDER.upper()} ", to=RECIPIENT.upper())

    def test_smtp_is_always_refused(self):
        with self.assertRaises(RecetteMailBlocked):
            _send(provider="smtp")

    def test_refused_while_sending_is_not_explicitly_enabled(self):
        with override_settings(RECETTE_MAIL_SEND_ENABLED=False), self.assertRaises(RecetteMailBlocked):
            _send()

    def test_empty_allow_lists_refuse_everything(self):
        for name in ("RECETTE_ALLOWED_SENDERS", "RECETTE_ALLOWED_RECIPIENTS"):
            with override_settings(**{name: []}), self.assertRaises(RecetteMailBlocked):
                _send()

    def test_sender_outside_the_allow_list_is_refused(self):
        with self.assertRaises(RecetteMailBlocked):
            _send(sender_address="owner@real-company.com")

    def test_every_recipient_is_checked_to_cc_and_bcc(self):
        for field in ("to", "cc", "bcc"):
            value = OTHER if field == "to" else [OTHER]
            with self.assertRaises(RecetteMailBlocked, msg=field):
                _send(**{field: value})

    def test_a_domain_is_not_enough_only_the_full_address_counts(self):
        with self.assertRaises(RecetteMailBlocked):
            _send(to=OTHER)  # same domain as the allowed recipient

    def test_empty_unknown_or_malformed_recipients_are_refused(self):
        for bad in ("", None, [], "a@b", "Name <tester.one@recette-tenant.invalid>",
                    f"{RECIPIENT}, {OTHER}", f"{RECIPIENT};{OTHER}", 42):
            with self.assertRaises(RecetteMailBlocked, msg=repr(bad)):
                _send(to=bad)
        with self.assertRaises(RecetteMailBlocked):
            _send(cc=[""])

    def test_a_list_given_as_to_is_checked_entirely(self):
        with self.assertRaises(RecetteMailBlocked):
            _send(to=[RECIPIENT, OTHER])
        _send(to=[RECIPIENT])


@override_settings(**RECETTE)
class RecetteMailboxAndTenantTests(SimpleTestCase):
    def test_mailbox_address_must_be_allow_listed(self):
        check_mailbox_address(SENDER)
        with self.assertRaises(RecetteMailBlocked):
            check_mailbox_address("owner@real-company.com")
        with self.assertRaises(RecetteMailBlocked):
            check_mailbox_address("")

    def test_the_tenant_is_confirmed_by_microsoft_graph_itself(self):
        with patch("userapis.recette_guard.requests.get", return_value=_organization(TENANT.upper())) as get:
            confirm_tenant_with_microsoft("opaque-graph-token")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], MS_GRAPH_ORGANIZATION_URL)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer opaque-graph-token")

    def test_a_token_claiming_the_right_tenant_proves_nothing_microsoft_decides(self):
        import base64, json  # a token whose own payload says "recette" ...
        part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        lying_token = f"{part({'alg': 'none'})}.{part({'tid': TENANT})}.sig"
        other = "99999999-0000-0000-0000-000000000000"
        # ... is refused when Microsoft says it belongs to another tenant.
        with patch("userapis.recette_guard.requests.get", return_value=_organization(other)):
            with self.assertRaises(RecetteMailBlocked):
                confirm_tenant_with_microsoft(lying_token)

    def test_another_tenant_or_any_unclear_answer_is_refused(self):
        answers = [
            _organization("99999999-0000-0000-0000-000000000000"),
            _organization(status=401), _organization(status=403), _organization(status=500),
            _organization(payload={}), _organization(payload={"value": []}),
            _organization(payload={"value": [{"id": TENANT}, {"id": TENANT}]}),
            _organization(payload={"value": [{}]}), _organization(payload=[]),
            _organization(payload={"value": None}),
        ]
        for answer in answers:
            with patch("userapis.recette_guard.requests.get", return_value=answer):
                with self.assertRaises(RecetteMailBlocked, msg=repr(answer.json.return_value)):
                    confirm_tenant_with_microsoft("token")

    def test_network_or_parsing_failures_refuse(self):
        broken = Mock()
        broken.status_code = 200
        broken.json.side_effect = ValueError("not json")
        for effect in ({"side_effect": requests.ConnectionError("down")},
                       {"side_effect": requests.Timeout("slow")}, {"return_value": broken}):
            with patch("userapis.recette_guard.requests.get", **effect):
                with self.assertRaises(RecetteMailBlocked):
                    confirm_tenant_with_microsoft("token")

    def test_no_token_or_no_configured_tenant_refuses_without_calling_microsoft(self):
        with patch("userapis.recette_guard.requests.get") as get:
            with self.assertRaises(RecetteMailBlocked):
                confirm_tenant_with_microsoft("")
            with self.assertRaises(RecetteMailBlocked):
                confirm_tenant_with_microsoft(None)
            with override_settings(RECETTE_M365_TENANT_ID=""), self.assertRaises(RecetteMailBlocked):
                confirm_tenant_with_microsoft("token")
        get.assert_not_called()

    def test_block_in_recette_disables_a_feature(self):
        with self.assertRaises(RecetteMailBlocked):
            block_in_recette("Something")

    def test_normalize_address_rejects_everything_but_a_bare_address(self):
        self.assertEqual(normalize_address(" A.B@C.D.invalid "), "a.b@c.d.invalid")
        for bad in (None, 1, "", "no-at", "a@b", "a b@c.invalid", "<a@c.invalid>", "a@c.invalid,b@c.invalid"):
            self.assertIsNone(normalize_address(bad), msg=repr(bad))


# LINKS-RECETTE-05H: first start without Microsoft app registration / Auth0 M2M credentials.
NO_APP = dict(RECETTE_MODE=True, MS_CLIENT_ID="", MS_CLIENT_SECRET="", MS_TENANT_ID="",
              AZURE_CLIENT_ID="", AZURE_CLIENT_SECRET="", AZURE_TENANT_ID="")
WITH_APP = {**NO_APP, **{k: "app-value" for k in NO_APP if k != "RECETTE_MODE"}}


class MicrosoftAppRegistrationTests(SimpleTestCase):
    @override_settings(**{**NO_APP, "RECETTE_MODE": False})
    def test_inert_outside_recette(self):
        from userapis.recette_guard import microsoft_app_configured, require_microsoft_app
        self.assertTrue(microsoft_app_configured())
        require_microsoft_app()

    @override_settings(**NO_APP)
    def test_recette_without_registration_refuses_every_microsoft_flow(self):
        from userapis.mailbox_oauth import MailOAuthError, build_mail_oauth_url
        from userapis.microsoft_outlook import refresh_outlook_access_token
        from userapis.recette_guard import microsoft_app_configured, require_microsoft_app
        from project_management.views.sharepoint import (
            AzureCallbackAPIView, AzureLoginAPIView, AzureTokenRefreshAPIView)
        self.assertFalse(microsoft_app_configured())
        with self.assertRaises(RecetteMailBlocked):
            require_microsoft_app()
        with self.assertRaises(MailOAuthError):
            build_mail_oauth_url(Mock(), Mock())
        user = Mock()
        user.outlook_credentials.outlook_refresh_token = "stored-refresh"
        with patch("userapis.microsoft_outlook.requests.post") as post, \
                patch("project_management.views.sharepoint.requests.post") as sp_post, \
                patch("project_management.views.sharepoint.requests.get") as sp_get:
            self.assertIsNone(refresh_outlook_access_token(user))
            for response in (AzureLoginAPIView().get(Mock()), AzureCallbackAPIView().get(Mock()),
                             AzureTokenRefreshAPIView().post(Mock())):
                self.assertEqual(response.status_code, 403)
                self.assertIn("not configured in recette", response.data["error"])
        for mocked in (post, sp_post, sp_get):
            mocked.assert_not_called()

    @override_settings(**WITH_APP)
    def test_a_complete_registration_is_accepted(self):
        from userapis.recette_guard import microsoft_app_configured
        self.assertTrue(microsoft_app_configured())

    def test_any_missing_value_counts_as_unconfigured(self):
        from userapis.recette_guard import microsoft_app_configured
        for name in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID",
                     "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"):
            with override_settings(**{**WITH_APP, name: "  "}):
                self.assertFalse(microsoft_app_configured(), msg=name)
