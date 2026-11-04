import hashlib
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from project_management.models import TransmittalSendProof, TransmittalSendProofFile
from project_management.test_transmittal import TransmittalTestBase, grant_transmittal_project_permissions
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_send import send_transmittal
from project_management.services.transmittal_send_proof import archive_graph_original, transmittal_send_marker, prepare_send_proof_safely
from project_management.views.transmittal_send_proof import TransmittalProofView
from userapis.mailbox_read import MailboxReadError


@override_settings(TRANSMITTAL_PROOF_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', DEFAULT_FROM_EMAIL='archive@test.invalid')
class TransmittalProofTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        license_patch=patch('ecpmpapp.permissions.LicensePermission.has_permission',return_value=True)
        license_patch.start(); self.addCleanup(license_patch.stop)
        self.revision = self.make_approved_internal_revision()
        with self.revision.file.open('rb') as stream:
            self.content = stream.read()
        self.transmittal = self.create_transmittal(subject='Archive isolated test')
        self.transmittal.external_contacts.add(self.client_contact)
        add_transmittal_item(self.transmittal, self.revision, 'FOR_INFORMATION', self.owner)
        self.transmittal = finalize_transmittal(self.transmittal, self.owner)

    def sent(self, graph=False):
        if graph:
            with patch('project_management.services.transmittal_send.MailboxConnection.objects.filter') as query, patch('project_management.services.transmittal_send.validate_mailbox_for_send'), patch('project_management.services.transmittal_send._resolve_mail_sender_user', return_value=self.owner), patch('project_management.services.transmittal_send._deliver_via_graph', return_value='accepted'):
                query.return_value.first.return_value = SimpleNamespace(email_address='sender@test.invalid')
                event = send_transmittal(self.transmittal, self.owner, mailbox_id=999)
        else:
            event = send_transmittal(self.transmittal, self.owner)
        self.assertEqual(event.event_type, 'SEND_SUCCEEDED')
        return TransmittalSendProof.objects.get(event=event)

    def mime(self, proof, *, marker=None, recipient=None, content=None):
        meta = proof.attempt.metadata
        message = EmailMessage()
        message['Subject'] = meta['subject']; message['From'] = meta['from']
        message['To'] = recipient or meta['to']
        message['x-links-send'] = marker or transmittal_send_marker(proof.attempt)
        message.set_content(meta['body'])
        message.add_attachment(self.content if content is None else content, maintype='application', subtype='octet-stream', filename=meta['attachments'][0]['name'])
        return message.as_bytes()

    def archive(self, proof, content=None, marker=None, count=1, next_link=None):
        marker = marker or transmittal_send_marker(proof.attempt)
        raw = content or self.mime(proof)
        def download(graph_id, stream, *args, **kwargs):
            stream.write(raw); return len(raw), hashlib.sha256(raw).hexdigest()
        reader = SimpleNamespace(
            list_messages=lambda *a, **k: SimpleNamespace(messages=[SimpleNamespace(graph_id=str(i)) for i in range(count)], next_link=next_link),
            get_reply_identification=lambda *a: SimpleNamespace(send_marker=marker),
            download_message_mime=download,
        )
        with patch('project_management.services.transmittal_send_proof.authorize_mailbox_read'):
            return archive_graph_original(proof.pk, actor=self.owner, reader=reader)

    def test_smtp_archives_prepared_message_and_exact_attachment(self):
        proof=self.sent(); self.assertEqual(proof.state,'ARCHIVED')
        self.assertEqual(set(proof.files.values_list('kind',flat=True)),{'prepared','attachment'})
        evidence=proof.files.get(kind='attachment')
        with evidence.blob.file.open('rb') as stream: self.assertEqual(stream.read(),self.content)
        self.assertEqual(len(mail.outbox),1)

    def test_graph_prepares_copy_without_reading_mailbox(self):
        with patch('project_management.services.transmittal_send_proof.MailboxGraphReader') as reader:
            proof=self.sent(True)
        reader.assert_not_called();self.assertEqual(proof.state,'PENDING')

    def test_verified_graph_original_is_archived_without_resend(self):
        proof=self.sent(True)
        with patch('project_management.services.transmittal_send._deliver_via_graph') as send:
            archived=self.archive(proof)
        send.assert_not_called();self.assertEqual(archived.state,'ARCHIVED')
        self.assertEqual(archived.files.count(),3)

    def test_same_subject_without_signed_marker_is_not_trusted(self):
        self.assertEqual(self.archive(self.sent(True),marker='forged').state,'PENDING')

    def test_mime_marker_must_also_match(self):
        proof=self.sent(True)
        self.assertEqual(self.archive(proof,content=self.mime(proof,marker='forged')).state,'FAILED')

    def test_wrong_attachment_is_rejected(self):
        proof=self.sent(True)
        self.assertEqual(self.archive(proof,content=self.mime(proof,content=b'wrong')).error_code,'attachment_mismatch')

    def test_wrong_recipient_is_rejected(self):
        proof=self.sent(True)
        self.assertEqual(self.archive(proof,content=self.mime(proof,recipient='wrong@test.invalid')).state,'FAILED')

    def test_ambiguous_original_is_rejected(self):
        self.assertEqual(self.archive(self.sent(True),count=2).error_code,'ambiguous_copy')

    def test_incomplete_search_stays_pending(self):
        self.assertEqual(self.archive(self.sent(True),next_link='more').state,'PENDING')

    def test_missing_original_stays_pending(self):
        self.assertEqual(self.archive(self.sent(True),count=0).state,'PENDING')

    def test_archived_proof_is_idempotent(self):
        proof=self.archive(self.sent(True))
        with patch('project_management.services.transmittal_send_proof.MailboxGraphReader') as reader:
            again=archive_graph_original(proof.pk,actor=self.owner)
        reader.assert_not_called();self.assertEqual(again.files.count(),3)

    def test_storage_failure_does_not_change_accepted_send_or_resend(self):
        with patch('project_management.services.transmittal_send_proof._store',side_effect=OSError('disk failed')):
            proof=self.sent()
        self.assertEqual(proof.state,'FAILED');self.assertEqual(len(mail.outbox),1)
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner)
        self.assertEqual(len(mail.outbox),1)

    def test_immutable_proof_and_file_and_bulk_writes(self):
        proof=self.sent();evidence=proof.files.first()
        with self.assertRaises(ValidationError):proof.save()
        with self.assertRaises(ValidationError):evidence.save()
        with self.assertRaises(ValidationError):proof.delete()
        with self.assertRaises(ValidationError):TransmittalSendProof.objects.filter(pk=proof.pk).update(state='FAILED')
        with self.assertRaises(ValidationError):proof.files.all().delete()
        with self.assertRaises(ValidationError):TransmittalSendProofFile.objects.bulk_create([])

    @override_settings(TRANSMITTAL_PROOF_ENABLED=False)
    def test_disabled_feature_does_not_create_proof(self):
        event=send_transmittal(self.transmittal,self.owner)
        self.assertEqual(event.event_type,'SEND_SUCCEEDED');self.assertFalse(TransmittalSendProof.objects.exists())

    def request(self, action, user=None, **kwargs):
        request=APIRequestFactory().post('/') if action=='archive' else APIRequestFactory().get('/')
        force_authenticate(request,user=user or self.owner)
        return TransmittalProofView.as_view({'post' if action=='archive' else 'get':action})(request,transmittal_id=self.transmittal.pk,**kwargs)

    def grant(self, actions=('view','issue')):
        grant_transmittal_project_permissions(self.owner,self.project,self.company,actions)

    def test_api_requires_project_permission(self):
        self.sent(); self.assertEqual(self.request('list',self.approver).status_code,403)

    def test_api_lists_no_storage_path(self):
        self.sent(); self.grant();response=self.request('list')
        self.assertEqual(response.status_code,200);self.assertTrue(response.data['enabled'])
        self.assertNotIn('blob',str(response.data));self.assertNotIn('send_evidence/',str(response.data))

    def test_api_download_is_private_attachment_and_validated(self):
        proof=self.sent();self.grant();file=proof.files.get(kind='attachment')
        response=self.request('download',proof_id=proof.pk,file_id=file.pk)
        self.assertEqual(response.status_code,200);self.assertEqual(b''.join(response.streaming_content),self.content)
        self.assertEqual(response['Cache-Control'],'private, no-store');self.assertIn('attachment',response['Content-Disposition'])

    def test_api_corruption_blocks_download(self):
        proof=self.sent();self.grant();file=proof.files.get(kind='attachment')
        with open(file.blob.file.path,'wb') as stream:stream.write(b'corrupt')
        self.assertEqual(self.request('download',proof_id=proof.pk,file_id=file.pk).status_code,409)

    def test_api_mismatched_parent_cannot_read_proof(self):
        proof=self.sent();self.grant()
        self.assertEqual(self.request('download',proof_id=proof.pk+100,file_id=proof.files.first().pk).status_code,404)

    def test_view_only_cannot_download_message_or_archive(self):
        proof=self.sent(True);self.grant(('view',))
        self.assertEqual(self.request('download',proof_id=proof.pk,file_id=proof.files.get(kind='prepared').pk).status_code,403)
        self.assertEqual(self.request('archive',proof_id=proof.pk).status_code,403)

    def test_view_only_can_download_document(self):
        proof=self.sent();self.grant(('view',))
        self.assertEqual(self.request('download',proof_id=proof.pk,file_id=proof.files.get(kind='attachment').pk).status_code,200)

    def test_missing_mailbox_read_permission_blocks_reader(self):
        proof=self.sent(True)
        with patch('project_management.services.transmittal_send_proof.MailboxGraphReader') as reader:
            with self.assertRaises(MailboxReadError):archive_graph_original(proof.pk,actor=self.owner)
        reader.assert_not_called()

    def test_failed_original_can_be_retried_without_new_send(self):
        proof=self.sent(True)
        self.assertEqual(self.archive(proof,content=self.mime(proof,content=b'wrong')).state,'FAILED')
        self.assertFalse(proof.files.filter(kind='original').exists())
        self.assertEqual(self.archive(proof).state,'ARCHIVED')
        self.assertEqual(self.transmittal.events.filter(event_type='SEND_STARTED').count(),1)

    def test_stale_pending_instance_cannot_extend_archived_proof(self):
        pending=self.sent(True); self.archive(pending)
        existing=pending.files.get(kind='attachment')
        with self.assertRaises(ValidationError):
            TransmittalSendProofFile.objects.create(proof=pending,kind='prepared',file_name='extra.eml',
                content_type=existing.content_type,file_size=existing.file_size,sha256=existing.sha256,blob=existing.blob)

    def test_archived_bytes_survive_source_file_change(self):
        proof=self.sent();self.grant();file=proof.files.get(kind='attachment')
        with open(self.revision.file.path,'wb') as stream:stream.write(b'changed source')
        response=self.request('download',proof_id=proof.pk,file_id=file.pk)
        self.assertEqual(b''.join(response.streaming_content),self.content)

    def test_api_other_company_is_refused(self):
        self.sent()
        from models.models import User
        stranger=User.objects.create_user(email='foreign-archive@test.invalid',first_name='Other',last_name='Tenant',title='Mr',user_type='company_user',company=self.other_company)
        grant_transmittal_project_permissions(stranger,self.other_tenant_project,self.other_company,('view','issue'))
        self.assertEqual(self.request('list',stranger).status_code,403)

    @override_settings(TRANSMITTAL_PROOF_ENABLED=False)
    def test_disabled_api_does_not_access_proof_table(self):
        self.grant()
        with patch('project_management.views.transmittal_send_proof.TransmittalSendProof.objects.filter',side_effect=AssertionError('must not query')):
            response=self.request('list')
        self.assertEqual(response.data,{'enabled':False,'proofs':[]})
