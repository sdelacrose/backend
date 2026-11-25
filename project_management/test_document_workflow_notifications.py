from unittest.mock import patch
from django.db import transaction
from rest_framework.exceptions import PermissionDenied
from project_management.services.document_workflow import perform_action, change_actor
from project_management.test_document_workflow import WorkflowTestBase, TWO, THREE

TARGET = "project_management.services.document_workflow_notifications.create_notification_with_recipients"

class WorkflowNotificationTests(WorkflowTestBase):
    def check_notice(self, mock, user, words):
        mock.assert_called_once()
        args = mock.call_args.kwargs
        self.assertEqual(args["recipients"], [user])
        self.assertEqual(args["object_id"], self.document.pk)
        self.assertIn(words, args["message"])
        self.assertNotIn("sequence number", args["message"])
        self.assertIn(self.document.file_name, args["message"])

    @patch(TARGET)
    def test_start_notifies_author_after_commit_only(self, notice):
        with self.captureOnCommitCallbacks(execute=True):
            self.start(TWO)
            notice.assert_not_called()
        self.check_notice(notice, self.alice, "signature")

    @patch(TARGET)
    def test_two_step_sign_notifies_approver(self, notice):
        wf = self.start(TWO)
        with self.captureOnCommitCallbacks(execute=True):
            perform_action(wf, self.alice, "SIGN", "")
        self.check_notice(notice, self.carol, "approval")

    @patch(TARGET)
    def test_three_step_notifies_verifier_then_approver(self, notice):
        wf = self.start(THREE)
        with self.captureOnCommitCallbacks(execute=True):
            wf = perform_action(wf, self.alice, "SIGN", "")
        self.check_notice(notice, self.bob, "verification")
        notice.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            perform_action(wf, self.bob, "VALIDATE", "")
        self.check_notice(notice, self.carol, "approval")

    @patch(TARGET)
    def test_result_notifies_author_in_preferred_language(self, notice):
        self.alice.preferred_language = "French"
        self.alice.save(update_fields=["preferred_language"])
        wf = self.start(TWO)
        wf = perform_action(wf, self.alice, "SIGN", "")
        with self.captureOnCommitCallbacks(execute=True):
            perform_action(wf, self.carol, "REJECT", "correct section 2")
        self.check_notice(notice, self.alice, "Document rejeté")

    @patch(TARGET)
    def test_approval_notifies_author(self, notice):
        wf = self.start(TWO)
        wf = perform_action(wf, self.alice, "SIGN", "")
        with self.captureOnCommitCallbacks(execute=True):
            perform_action(wf, self.carol, "APPROVE", "")
        self.check_notice(notice, self.alice, "Internal approval completed")

    @patch(TARGET)
    def test_invalid_action_and_rollback_do_not_notify(self, notice):
        wf = self.start(TWO)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(PermissionDenied):
                perform_action(wf, self.bob, "SIGN", "")
            try:
                with transaction.atomic():
                    perform_action(wf, self.alice, "SIGN", "")
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
        notice.assert_not_called()
        wf.refresh_from_db()
        self.assertEqual(wf.status, "DRAFT")

    @patch(TARGET)
    def test_stale_tasks_do_not_notify_previous_actor(self, notice):
        wf = self.start(THREE)
        with self.captureOnCommitCallbacks(execute=True):
            wf = perform_action(wf, self.alice, "SIGN", "")
            change_actor(wf, self.alice, "verifier", self.dave, "absence")
        self.check_notice(notice, self.dave, "verification")
