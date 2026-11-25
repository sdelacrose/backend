import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

import time
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.urls import path
from project_management.views.recette_document_file import RecetteDocumentFileView

GOOD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

class _SigningKey:
    def __init__(self, key):
        self.key = key.public_key()

def make_token(key=GOOD_KEY, **claims):
    payload = {"iss": settings.SIMPLE_JWT["ISSUER"], "aud": settings.SIMPLE_JWT["AUDIENCE"],
               "sub": "auth0|file-test", "iat": int(time.time()), "exp": int(time.time()) + 300}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test"})

# Exercise the real HTTP view independently of the process-level recette switch.
urlpatterns = [path("media/documents/<path:relative_path>", RecetteDocumentFileView.as_view())]
from jwt import PyJWKClient
from models.models import BlockedToken, CompanyProfile, User
from project_management.models import DocumentAttachment, DocumentRevision
from project_management.test_document_revision import NoNotificationsMixin, make_document
from project_management.test_documents_full_access import grant_company_permission


@override_settings(RECETTE_MODE=True, ROOT_URLCONF=__name__)
class RecetteDocumentFileTests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        override = override_settings(MEDIA_ROOT=self.storage.name)
        override.enable()
        self.addCleanup(override.disable)
        self.user, self.document = make_document()
        self.user.auth0_sub = 'auth0|file-test'
        self.user.company = self.document.folder.company_profile
        self.user.save(update_fields=['auth0_sub', 'company'])
        grant_company_permission(self.user, self.user.company, ['documents.view'], 'File reader')
        self.license = patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True)
        self.license.start()
        self.addCleanup(self.license.stop)
        signing_key = patch.object(PyJWKClient, 'get_signing_key_from_jwt', return_value=_SigningKey(GOOD_KEY))
        signing_key.start()
        self.addCleanup(signing_key.stop)
        self.client = APIClient()

    def login_cookie(self):
        token = make_token(sub=self.user.auth0_sub)
        self.client.cookies['auth-token'] = token
        return token

    def url(self, field=None):
        return '/media/' + (field or self.document.file).name

    def test_cookie_opens_authorized_file_without_basic_challenge(self):
        self.login_cookie()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'original')
        self.assertNotIn('WWW-Authenticate', response)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_anonymous_is_denied_without_a_basic_challenge(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('Basic', response.get('WWW-Authenticate', ''))

    def test_tampered_cookie_is_denied(self):
        self.client.cookies['auth-token'] = 'not-a-valid-jwt'
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_blocked_cookie_is_denied(self):
        token = self.login_cookie()
        BlockedToken.objects.create(token=token, user=self.user, is_blocked=True)
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_inactive_user_is_denied(self):
        self.login_cookie()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_bearer_header_remains_supported(self):
        token = make_token(sub=self.user.auth0_sub)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + token)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        b''.join(response.streaming_content)

    def test_cached_basic_header_does_not_replace_the_valid_links_cookie(self):
        self.login_cookie()
        self.client.credentials(HTTP_AUTHORIZATION='Basic b2xkOmNyZWRlbnRpYWxz')
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        b''.join(response.streaming_content)

    def test_foreign_company_with_document_rights_cannot_read_file(self):
        other = User.objects.create_user(email='foreign-file@example.invalid', user_type='company_user')
        company = CompanyProfile.objects.create(
            admin_user=other, company_name='Other', company_address='1', company_town='T',
            company_tell_no='1', company_mob_no='1', company_web_address='https://example.invalid',
            company_email_address='other@example.invalid', company_tax_reference='other', company_iban='x',
        )
        other.company = company
        other.save(update_fields=['company'])
        grant_company_permission(other, company, ['documents.full_access'], 'Other file reader')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_existing_but_unregistered_file_is_not_public(self):
        self.login_cookie()
        path = Path(self.storage.name) / 'documents' / 'unregistered.pdf'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'private')
        self.assertEqual(self.client.get('/media/documents/unregistered.pdf').status_code, 404)

    def test_revision_and_attachment_use_the_parent_document_rights(self):
        revision = DocumentRevision.objects.create(
            document=self.document, version_number=1, major_revision='A', minor_revision=0,
            file=ContentFile(b'revision', name='revision.pdf'), file_name='revision.pdf',
        )
        attachment = DocumentAttachment.objects.create(
            document=self.document, file=ContentFile(b'attachment', name='annex.docx'), file_name='annex.docx',
        )
        self.login_cookie()
        for record, expected in ((revision, b'revision'), (attachment, b'attachment')):
            response = self.client.get(self.url(record.file))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), expected)

    def test_active_content_is_downloaded_and_sandboxed(self):
        self.document.file_name = 'unsafe.html'
        self.document.save(update_fields=['file_name'])
        self.login_cookie()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Disposition'].startswith('attachment'))
        self.assertIn('sandbox', response['Content-Security-Policy'])
        b''.join(response.streaming_content)

    def test_disabled_outside_recette(self):
        self.login_cookie()
        with override_settings(RECETTE_MODE=False):
            self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_private_paths_and_write_methods_stay_closed(self):
        self.login_cookie()
        self.assertEqual(self.client.get('/media/private/secret.pdf').status_code, 404)
        self.assertEqual(self.client.post(self.url(), {}).status_code, 405)

    def test_expired_wrong_signature_issuer_and_audience_are_refused(self):
        cases = [make_token(exp=int(time.time()) - 600), make_token(key=OTHER_KEY),
                 make_token(iss="https://wrong.invalid/"), make_token(aud="wrong")]
        for token in cases:
            with self.subTest(token_type=cases.index(token)):
                self.client.cookies["auth-token"] = token
                response = self.client.get(self.url())
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("Basic", response.get("WWW-Authenticate", ""))

    def test_deleted_accounts_are_refused(self):
        self.login_cookie()
        for flag in ("is_deleted", "is_permanently_deleted"):
            with self.subTest(flag=flag):
                User.objects.filter(pk=self.user.pk).update(**{flag: True})
                self.assertEqual(self.client.get(self.url()).status_code, 401)
                User.objects.filter(pk=self.user.pk).update(**{flag: False})

    def test_missing_license_is_refused(self):
        self.login_cookie()
        with patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=False):
            response = self.client.get(self.url())
        self.assertEqual(response.status_code, 403)

    def test_same_company_without_document_rights_is_refused(self):
        other = User.objects.create_user(email="no-file-rights@example.invalid", user_type="company_user", company=self.user.company)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_head_reads_headers_without_sending_body(self):
        self.login_cookie()
        response = self.client.head(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], "8")
        self.assertEqual(b"".join(response.streaming_content), b"")

    def test_unsafe_and_unregistered_paths_are_refused(self):
        self.login_cookie()
        for path in ("../private/x", "folder/../../x", "folder//x", "folder\\secret", "folder/%00secret"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get("/media/documents/" + path).status_code, 404)

    def test_reading_withdrawn_document_and_its_revision_remains_allowed(self):
        from project_management.services.document_withdrawal import withdraw_document
        revision = DocumentRevision.objects.create(
            document=self.document, version_number=1, major_revision="A", minor_revision=0,
            file=ContentFile(b"evidence", name="evidence.pdf"), file_name="evidence.pdf")
        withdraw_document(self.document, self.user, "Local test withdrawal")
        self.login_cookie()
        for field, content in ((self.document.file, b"original"), (revision.file, b"evidence")):
            response = self.client.get(self.url(field))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), content)
        self.document.refresh_from_db()
        self.assertIsNotNone(self.document.withdrawn_at)

    def test_missing_stored_file_is_not_found(self):
        self.login_cookie()
        self.document.file.storage.delete(self.document.file.name)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_office_file_is_downloaded_without_external_redirect(self):
        self.document.file_name = "document.docx"
        self.document.save(update_fields=["file_name"])
        self.login_cookie()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertNotIn("Location", response)
        self.assertEqual(b"".join(response.streaming_content), b"original")

    def test_pdf_image_and_text_can_be_displayed_inline(self):
        self.login_cookie()
        for filename in ("document.pdf", "image.png", "text.txt"):
            with self.subTest(filename=filename):
                self.document.file_name = filename
                self.document.save(update_fields=["file_name"])
                response = self.client.get(self.url())
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response["Content-Disposition"].startswith("inline"))
                self.assertEqual(b"".join(response.streaming_content), b"original")

    def test_foreign_revision_and_attachment_are_not_accessible(self):
        revision = DocumentRevision.objects.create(
            document=self.document, version_number=1, major_revision="A", minor_revision=0,
            file=ContentFile(b"revision", name="revision.pdf"), file_name="revision.pdf")
        attachment = DocumentAttachment.objects.create(
            document=self.document, file=ContentFile(b"annex", name="annex.txt"), file_name="annex.txt")
        other = User.objects.create_user(email="unassigned-file@example.invalid", user_type="company_user", company=self.user.company)
        self.client.force_authenticate(other)
        for field in (self.document.file, revision.file, attachment.file):
            self.assertEqual(self.client.get(self.url(field)).status_code, 404)

    def test_project_document_follows_membership_and_project_rights(self):
        from datetime import date
        from project_management.models import Document, Folder, Project
        from project_management.test_documents_full_access import grant_project_permission
        project = Project.objects.create(project_name="File project", user=self.user, company_profile=self.user.company,
            solution="Energy Consultant Solution", contract_value=0,
            project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31))
        folder = Folder.objects.create(project=project, name="project files")
        document = Document.objects.create(folder=folder, file=ContentFile(b"project", name="plan.txt"), file_name="plan.txt", created_by=self.user)
        self.login_cookie()
        self.assertEqual(self.client.get(self.url(document.file)).status_code, 404)
        grant_project_permission(self.user, project, self.user.company, ["documents.view"], "Project file reader")
        response = self.client.get(self.url(document.file))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"project")

    def test_real_urlconf_registers_secure_view_only_in_recette(self):
        import importlib
        import ecpmpapp.urls as application_urls
        from django.urls import clear_url_caches, resolve
        original = application_urls.urlpatterns
        try:
            for enabled in (True, False):
                with override_settings(RECETTE_MODE=enabled, DEBUG=True, ROOT_URLCONF="ecpmpapp.urls"):
                    importlib.reload(application_urls)
                    clear_url_caches()
                    resolved = resolve(self.url())
                    self.assertEqual(getattr(resolved.func, "view_class", None) is RecetteDocumentFileView, enabled)
                    if not enabled:
                        response = self.client.get(self.url())
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(b"".join(response.streaming_content), b"original")
        finally:
            application_urls.urlpatterns = original
            clear_url_caches()
