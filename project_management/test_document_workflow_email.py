from unittest.mock import patch
from django.core import mail
from django.db import transaction
from django.test import override_settings
from rest_framework.exceptions import PermissionDenied
from models.models import Preference
from project_management.services.document_workflow import perform_action,change_actor,stop_workflow
from project_management.services.document_workflow_notifications import deliver_workflow_notification
from project_management.test_document_workflow import WorkflowTestBase,TWO,THREE

TARGET='project_management.services.document_workflow_notifications.create_notification_with_recipients'

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',FRONTEND_URL='http://localhost:3000',DEFAULT_FROM_EMAIL='LINKS <notifications@links-local.invalid>')
class WorkflowEmailTests(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        for user in [self.alice,self.bob,self.carol,self.dave]:
            Preference.objects.update_or_create(user=user,defaults={'email_check':True})
        mail.get_connection()
        mail.outbox.clear()

    def flush(self,operation):
        with self.captureOnCommitCallbacks(execute=True): result=operation()
        return result

    def test_start_notifies_author_only_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            wf=self.start(TWO)
            self.assertEqual(mail.outbox,[])
        self.assertEqual(len(mail.outbox),1)
        message=mail.outbox[0]
        self.assertEqual(message.to,[self.alice.email])
        self.assertIn('signature',message.subject)
        self.assertIn(self.document.file_name,message.body)
        self.assertIn('/company-documents/'+str(self.document.pk),message.body)
        self.assertEqual(message.attachments,[])
        self.assertEqual(message.alternatives[0][1],'text/html')

    def test_author_launch_and_sign_sends_only_next_actor_task(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                wf=self.start(THREE)
                perform_action(wf,self.alice,'SIGN','')
        self.assertEqual([m.to for m in mail.outbox],[[self.bob.email]])

    def test_three_step_then_result_have_only_expected_recipients(self):
        wf=self.start(THREE)
        self.flush(lambda:perform_action(wf,self.alice,'SIGN',''))
        self.flush(lambda:perform_action(wf,self.bob,'VALIDATE',''))
        self.flush(lambda:perform_action(wf,self.carol,'APPROVE',''))
        self.assertEqual([m.to for m in mail.outbox],[[self.bob.email],[self.carol.email],[self.alice.email]])
        self.assertIn('Internal approval completed',mail.outbox[-1].body)

    def test_two_step_skips_verifier(self):
        wf=self.start(TWO)
        self.flush(lambda:perform_action(wf,self.alice,'SIGN',''))
        self.assertEqual([m.to for m in mail.outbox],[[self.carol.email]])

    def test_rejection_email_contains_comment_in_author_language(self):
        self.alice.preferred_language='French';self.alice.save(update_fields=['preferred_language'])
        wf=self.start(TWO);perform_action(wf,self.alice,'SIGN','')
        self.flush(lambda:perform_action(wf,self.carol,'REJECT','Corriger la section 2.'))
        self.assertEqual(mail.outbox[0].to,[self.alice.email])
        self.assertIn('Document rejeté',mail.outbox[0].subject)
        self.assertIn('Commentaire: Corriger la section 2.',mail.outbox[0].body)

    def test_stopped_email_contains_reason(self):
        wf=self.start(TWO)
        with patch('project_management.services.document_cycle.can_stop_workflow',return_value=True):
            self.flush(lambda:stop_workflow(wf,self.alice,'Suspension de la revue.'))
        self.assertIn('Validation workflow stopped',mail.outbox[0].subject)
        self.assertIn('Suspension de la revue.',mail.outbox[0].body)

    def test_german_labels(self):
        self.alice.preferred_language='German';self.alice.save(update_fields=['preferred_language'])
        self.flush(lambda:self.start(TWO))
        self.assertIn('Ihre Unterschrift',mail.outbox[0].subject)
        self.assertIn('Arbeitsversion:',mail.outbox[0].body)

    @patch(TARGET)
    def test_email_opt_out_keeps_bell_notification(self,bell):
        Preference.objects.filter(user=self.alice).update(email_check=False)
        self.flush(lambda:self.start(TWO))
        self.assertEqual(mail.outbox,[]);bell.assert_called_once()

    def test_missing_preference_does_not_opt_user_in(self):
        Preference.objects.filter(user=self.alice).delete()
        self.flush(lambda:self.start(TWO))
        self.assertEqual(mail.outbox,[])

    def test_rollback_and_invalid_action_send_nothing(self):
        wf=self.start(TWO)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(PermissionDenied): perform_action(wf,self.bob,'SIGN','')
            try:
                with transaction.atomic():
                    perform_action(wf,self.alice,'SIGN','')
                    raise RuntimeError('rollback')
            except RuntimeError: pass
        self.assertEqual(mail.outbox,[])

    def test_old_assignee_is_not_notified_after_replacement(self):
        wf=self.start(THREE)
        with self.captureOnCommitCallbacks(execute=True):
            perform_action(wf,self.alice,'SIGN','')
            change_actor(wf,self.alice,'verifier',self.dave,'Absence')
        self.assertEqual([m.to for m in mail.outbox],[[self.dave.email]])

    def test_inactive_user_gets_no_email(self):
        wf=self.start(TWO);self.alice.is_active=False;self.alice.save(update_fields=['is_active'])
        deliver_workflow_notification(wf.pk,'DRAFT',self.alice.pk)
        self.assertEqual(mail.outbox,[])

    def test_html_and_headers_escape_document_content(self):
        self.document.file_name='<script>alert(1)</script>\nInjected: bad'
        self.document.save(update_fields=['file_name'])
        self.flush(lambda:self.start(TWO))
        message=mail.outbox[0]
        self.assertNotIn('\n',message.subject)
        self.assertNotIn('<script>',message.alternatives[0][0])
        self.assertIn('&lt;script&gt;',message.alternatives[0][0])

    @patch('project_management.services.document_workflow_email.EmailMultiAlternatives.send',side_effect=RuntimeError('SMTP error'))
    @patch(TARGET)
    def test_smtp_failure_does_not_undo_workflow_or_bell(self,bell,send):
        with self.assertLogs('project_management.services.document_workflow_email',level='ERROR'):
            wf=self.flush(lambda:self.start(TWO))
        wf.refresh_from_db();self.assertEqual(wf.status,'DRAFT')
        bell.assert_called_once();send.assert_called_once()

    @override_settings(FRONTEND_URL='https://user:password@example.com')
    def test_invalid_frontend_configuration_does_not_create_email(self):
        with self.assertLogs('project_management.services.document_workflow_email',level='WARNING'):
            self.flush(lambda:self.start(TWO))
        self.assertEqual(mail.outbox,[])

