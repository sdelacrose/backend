from unittest.mock import patch
from django.core.exceptions import ValidationError
from project_management.models import DocumentWorkflow, DocumentWorkflowEvent
from project_management.test_document_workflow import WorkflowApiTests, TWO


class LaunchAndSignatureTests(WorkflowApiTests):
    def test_author_launch_signs_once_and_waits_for_verifier(self):
        self.everything_allowed()
        response = self.call("post", self.alice, self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "VERIFY")
        self.assertEqual([(e["action"], e["actor"]["id"]) for e in response.data["events"]],
                         [("START", self.alice.pk), ("SIGN", self.alice.pk)])

    def test_author_launch_of_two_step_waits_for_approver(self):
        self.everything_allowed()
        response = self.call("post", self.alice, self.payload(workflow_type=TWO, verifier=None))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "APPROVE")

    def test_creator_launch_never_signs_for_author(self):
        self.everything_allowed()
        response = self.call("post", self.creator, self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual([(e["action"], e["actor"]["id"]) for e in response.data["events"]],
                         [("START", self.creator.pk)])
        denied = self.call("post", self.creator, {"action": "SIGN"}, action_view=True)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.call("post", self.alice, {"action": "SIGN"}, action_view=True).data["status"], "VERIFY")

    def test_launcher_cannot_substitute_the_assigned_document_author(self):
        self.everything_allowed()
        response = self.call("post", self.creator, self.payload(author=self.creator.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn("author", response.data)
        self.assertFalse(DocumentWorkflow.objects.exists())
        self.assertFalse(DocumentWorkflowEvent.objects.exists())

    def test_failed_author_signature_rolls_back_entire_launch(self):
        self.everything_allowed()
        with patch("project_management.views.document_workflow.perform_action", side_effect=ValidationError("signature failed")):
            response = self.call("post", self.alice, self.payload())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DocumentWorkflow.objects.exists())
        self.assertFalse(DocumentWorkflowEvent.objects.exists())
