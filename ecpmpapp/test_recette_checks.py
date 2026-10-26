"""LINKS-RECETTE-03: the start-up check of the recette settings (ecpmpapp.recette_checks). The
settings module refuses to start when this returns anything, so each unsafe condition below
must be reported -- and a compliant configuration must report nothing."""
from cryptography.fernet import Fernet
from django.test import SimpleTestCase

from ecpmpapp.recette_checks import collect_problems


def safe_conf(**overrides):
    conf = {
        "DEBUG": False,
        "ALLOWED_HOSTS": ["recette.example.invalid"],
        "CORS_ALLOW_ALL_ORIGINS": False,
        "CORS_ALLOWED_ORIGINS": ["https://recette.example.invalid"],
        "CSRF_TRUSTED_ORIGINS": ["https://recette.example.invalid"],
        "SECRET_KEY": "x" * 40,
        "EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend",
        "MATRIX_ENABLED": False,
        "CHAT_PUSH_NOTIFICATIONS_ENABLED": False,
        "CELERY_BEAT_SCHEDULE": {},
        "STORAGES": {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}},
        "MS_TOKEN_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
        "MS_TOKEN_ENCRYPTION_WRITE_ENABLED": True,
        "MAIL_SEND_PROOF_ARCHIVING_ENABLED": False,
        "MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE": None,
        "RECETTE_MAIL_SEND_ENABLED": False,
    }
    conf.update(overrides)
    return conf


def safe_environ(**overrides):
    environ = {
        "DJANGO_SETTINGS_MODULE": "ecpmpapp.settings.recette",
        "CELERY_ENV": "recette",
        "DB_NAME": "links_recette",
        "DB_HOST": "links-recette-db",
        "REDIS_HOST": "links-recette-redis",
        "BACKEND_URL": "https://recette.example.invalid/",
        "FRONTEND_URL": "https://recette.example.invalid",
        "AUTH0_DOMAIN": "links-recette.eu.auth0.com",
        "AUTH0_ISSUER_BASE_URL": "https://links-recette.eu.auth0.com",
        "MS_REDIRECT_URI": "api/project_management/azure/callback",
        "HEALTH_CHECK_API_KEY": "a-dedicated-recette-key",
        "HTTP_PROXY": "http://links-recette-egress:3128",
        "HTTPS_PROXY": "http://links-recette-egress:3128",
    }
    environ.update(overrides)
    return environ


def resolver(*addresses):
    """A fake DNS lookup answering the given addresses for any name (no real network)."""
    def resolve(host, port):
        return [(2, 1, 6, "", (address, 0)) for address in addresses]
    return resolve


def unresolvable(host, port):
    raise OSError("name not resolved")


class RecetteChecksTests(SimpleTestCase):
    def problems(self, conf=None, environ=None, resolve=None):
        return collect_problems(
            safe_conf(**(conf or {})), safe_environ(**(environ or {})),
            resolve=resolve or resolver("172.20.0.5"),
        )

    def assertFlags(self, text, conf=None, environ=None, resolve=None):
        found = self.problems(conf, environ, resolve)
        self.assertTrue(any(text in p for p in found), msg=f"{text!r} not reported in {found}")

    def test_a_clean_name_must_also_resolve_to_a_private_address(self):
        self.assertFlags("DB_HOST does not resolve to a private address", resolve=resolver("52.17.3.4"))
        self.assertFlags("REDIS_HOST does not resolve to a private address", resolve=resolver("172.20.0.5", "8.8.8.8"))
        self.assertFlags("DB_HOST cannot be resolved", resolve=unresolvable)
        self.assertEqual(self.problems(resolve=resolver("10.0.0.7", "172.20.0.5")), [])

    def test_outbound_traffic_must_go_through_the_recette_egress_proxy(self):
        for name in ("HTTP_PROXY", "HTTPS_PROXY"):
            self.assertFlags(name, environ={name: ""})
            self.assertFlags(name, environ={name: "http://proxy.corp.example.com:3128"})
            self.assertFlags(name, environ={name: "https://links-recette-egress:3128"})

    def test_a_compliant_configuration_reports_nothing(self):
        self.assertEqual(self.problems(), [])

    def test_debug_hosts_cors_and_secret(self):
        self.assertFlags("DEBUG", conf={"DEBUG": True})
        self.assertFlags("RECETTE_ALLOWED_HOSTS", conf={"ALLOWED_HOSTS": ["*"]})
        self.assertFlags("RECETTE_ALLOWED_HOSTS", conf={"ALLOWED_HOSTS": [".example.invalid"]})
        self.assertFlags("RECETTE_ALLOWED_HOSTS", conf={"ALLOWED_HOSTS": []})
        self.assertFlags("CORS_ALLOW_ALL_ORIGINS", conf={"CORS_ALLOW_ALL_ORIGINS": True})
        self.assertFlags("RECETTE_ALLOWED_ORIGINS", conf={"CORS_ALLOWED_ORIGINS": ["http://x.invalid"]})
        self.assertFlags("CSRF_TRUSTED_ORIGINS", conf={"CSRF_TRUSTED_ORIGINS": []})
        self.assertFlags("RECETTE_SECRET_KEY", conf={"SECRET_KEY": "django-insecure-" + "x" * 40})
        self.assertFlags("RECETTE_SECRET_KEY", conf={"SECRET_KEY": "short"})

    def test_mail_and_external_notifications_are_off(self):
        self.assertFlags("EMAIL_BACKEND", conf={"EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend"})
        self.assertFlags("MATRIX_ENABLED", conf={"MATRIX_ENABLED": True})
        self.assertFlags("CHAT_PUSH_NOTIFICATIONS_ENABLED", conf={"CHAT_PUSH_NOTIFICATIONS_ENABLED": True})
        self.assertFlags("CELERY_BEAT_SCHEDULE", conf={"CELERY_BEAT_SCHEDULE": {"x": {}}})

    def test_forbidden_external_wiring(self):
        for name in ("AWS_SQS_QUEUE_URL", "AWS_ACCESS_KEY_ID", "FIREBASE_PRIVATE_KEY",
                     "MATRIX_ACCESS_TOKEN", "EMAIL_HOST_PASSWORD"):
            self.assertFlags(name, environ={name: "set"})
        self.assertFlags("storage", conf={"STORAGES": {"default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"}}})

    def test_resources_must_be_dedicated(self):
        self.assertFlags("DB_NAME", environ={"DB_NAME": "ecpmp"})
        self.assertFlags("DB_HOST", environ={"DB_HOST": "db.abc.eu-west-3.rds.amazonaws.com"})
        self.assertFlags("REDIS_HOST", environ={"REDIS_HOST": ""})
        self.assertFlags("BACKEND_URL", environ={"BACKEND_URL": "https://api.sharemundolinks.com/"})
        self.assertFlags("FRONTEND_URL", environ={"FRONTEND_URL": "http://recette.example.invalid"})
        self.assertFlags("AUTH0_DOMAIN", environ={"AUTH0_DOMAIN": "tenant.sharemundolinks.com"})
        self.assertFlags("FRONTEND_URL_CPM", environ={"FRONTEND_URL_CPM": "https://cpm.sharemundolinks.com"})
        self.assertFlags("BACKEND_URL", environ={"RECETTE_FORBIDDEN_MARKERS": "example.invalid"})

    def test_settings_module_and_celery_env_are_coherent(self):
        self.assertFlags("DJANGO_SETTINGS_MODULE", environ={"DJANGO_SETTINGS_MODULE": "ecpmpapp.settings.staging"})
        self.assertFlags("CELERY_ENV", environ={"CELERY_ENV": "development"})

    def test_insecure_defaults_are_refused(self):
        self.assertFlags("HEALTH_CHECK_API_KEY", environ={"HEALTH_CHECK_API_KEY": "health_check_secret_key_2024"})
        self.assertFlags("HEALTH_CHECK_API_KEY", environ={"HEALTH_CHECK_API_KEY": ""})

    def test_oauth_token_keys(self):
        self.assertFlags("MS_TOKEN_ENCRYPTION_KEYS", conf={"MS_TOKEN_ENCRYPTION_KEYS": ""})
        self.assertFlags("valid Fernet", conf={"MS_TOKEN_ENCRYPTION_KEYS": "not-a-key"})
        self.assertFlags("MS_TOKEN_ENCRYPTION_WRITE_ENABLED", conf={"MS_TOKEN_ENCRYPTION_WRITE_ENABLED": False})

    def test_proof_archiving_needs_its_start_date(self):
        self.assertFlags("MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE", conf={"MAIL_SEND_PROOF_ARCHIVING_ENABLED": True})
        self.assertEqual(self.problems(conf={"MAIL_SEND_PROOF_ARCHIVING_ENABLED": True,
                                             "MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE": object()}), [])

    def test_enabling_sending_requires_tenant_and_full_address_lists(self):
        enabled = {"RECETTE_MAIL_SEND_ENABLED": True}
        self.assertFlags("RECETTE_M365_TENANT_ID", conf=enabled)
        good = {**enabled, "RECETTE_M365_TENANT_ID": "tenant", "RECETTE_ALLOWED_SENDERS": ["a@t.invalid"],
                "RECETTE_ALLOWED_RECIPIENTS": ["b@t.invalid"]}
        self.assertEqual(self.problems(conf=good), [])
        self.assertFlags("RECETTE_ALLOWED_RECIPIENTS", conf={**good, "RECETTE_ALLOWED_RECIPIENTS": ["@t.invalid"]})
        self.assertFlags("RECETTE_ALLOWED_RECIPIENTS", conf={**good, "RECETTE_ALLOWED_RECIPIENTS": ["*"]})
        self.assertFlags("RECETTE_ALLOWED_SENDERS", conf={**good, "RECETTE_ALLOWED_SENDERS": []})

    def test_messages_never_contain_a_value(self):
        secret = "SUPER-SECRET-VALUE-123"
        found = self.problems(environ={"AWS_SECRET_ACCESS_KEY": secret, "DB_HOST": secret + ".amazonaws.com"})
        self.assertTrue(found)
        self.assertFalse(any(secret in p for p in found))
