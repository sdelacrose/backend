import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from ecpmpapp.test_websocket_jwt import GOOD_KEY, _SigningKey, make_token
from jwt import PyJWKClient
from models.models import BlockedToken, CompanyProfile, User
from project_management.models import DocumentAttachment, DocumentRevision
from project_management.test_document_revision import NoNotificationsMixin, make_document
from project_management.test_documents_full_access import grant_company_permission


@override_settings(RECETTE_MODE=True)
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
