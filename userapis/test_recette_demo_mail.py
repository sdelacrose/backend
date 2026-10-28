"""Demo opt-in keeps the sender/tenant protections and never sends mail here."""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from ecpmpapp.recette_checks import collect_problems
from ecpmpapp.test_recette_checks import resolver, safe_conf, safe_environ
from userapis.recette_guard import RecetteMailBlocked, check_mailbox_address, check_send_allowed


SENDER = 'demo@example.invalid'


@override_settings(RECETTE_MODE=True, RECETTE_MAIL_SEND_ENABLED=True,
                   RECETTE_ALLOWED_SENDERS=[SENDER], RECETTE_ALLOWED_RECIPIENTS=[],
                   RECETTE_MAIL_ALLOW_ANY_RECIPIENT=True)
class RecetteDemoMailTests(SimpleTestCase):
    def check(self, **overrides):
        arguments = dict(sender_address=SENDER, to='internal@example.invalid',
                         cc=['external@other.invalid'], bcc=[], provider='graph')
        arguments.update(overrides)
        return check_send_allowed(**arguments)

    def test_explicit_opt_in_accepts_internal_and_external_without_network(self):
        with patch('requests.get') as get, patch('requests.post') as post:
            self.check(bcc=['external-bcc@other.invalid'])
        get.assert_not_called()
        post.assert_not_called()

    def test_default_keeps_the_allow_list_restriction(self):
        with override_settings(RECETTE_MAIL_ALLOW_ANY_RECIPIENT=False):
            with self.assertRaises(RecetteMailBlocked):
                self.check()

    def test_sender_restriction_is_not_relaxed(self):
        with self.assertRaises(RecetteMailBlocked):
            self.check(sender_address='other@example.invalid')
        with self.assertRaises(RecetteMailBlocked):
            check_mailbox_address('other@example.invalid')

    def test_smtp_and_disabled_sending_stay_blocked(self):
        with self.assertRaises(RecetteMailBlocked):
            self.check(provider='smtp')
        with override_settings(RECETTE_MAIL_SEND_ENABLED=False):
            with self.assertRaises(RecetteMailBlocked):
                self.check()

    def test_malformed_recipients_stay_blocked_in_all_fields(self):
        for name in ('to', 'cc', 'bcc'):
            with self.subTest(name=name), self.assertRaises(RecetteMailBlocked):
                self.check(**{name: ['not-an-address']})

    def problems(self, **overrides):
        conf = safe_conf(RECETTE_MAIL_SEND_ENABLED=True,
                         RECETTE_MAIL_ALLOW_ANY_RECIPIENT=True,
                         RECETTE_ALLOWED_SENDERS=[SENDER], RECETTE_ALLOWED_RECIPIENTS=[],
                         RECETTE_M365_TENANT_ID='test-tenant')
        conf.update(overrides)
        return collect_problems(conf, safe_environ(), resolve=resolver('172.20.0.4'))

    def test_startup_accepts_explicit_opt_in_with_a_sender_and_tenant(self):
        self.assertEqual(self.problems(), [])

    def test_startup_still_requires_tenant_and_sender(self):
        for missing in ('RECETTE_M365_TENANT_ID', 'RECETTE_ALLOWED_SENDERS'):
            with self.subTest(missing=missing):
                self.assertTrue(self.problems(**{missing: ''}))

    def test_startup_does_not_accept_a_truthy_string_as_opt_in(self):
        self.assertTrue(self.problems(RECETTE_MAIL_ALLOW_ANY_RECIPIENT='true'))

    def test_without_opt_in_an_empty_recipient_list_is_rejected(self):
        self.assertTrue(self.problems(RECETTE_MAIL_ALLOW_ANY_RECIPIENT=False))
