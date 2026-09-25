from unittest.mock import patch

from django.core.files.base import ContentFile

from project_management.models import Document
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import create_document_revision
from project_management.services.document_workflow import perform_action
from project_management.test_document_workflow import THREE, TWO, WorkflowTestBase

CHOICES = ["Draft", "Approved", "Sent", "Issued", "Under review", "To be updated"]


class StatusGuardrailTests(WorkflowTestBase):
    """Document.status and workflow status stay separate; only two automatic transitions."""

    def status(self):
        return Document.objects.get(pk=self.document.pk).status

    def set_status(self, value):
        self.document.status = value
        self.document.save(update_fields=["status"])

    def revise(self, kind="minor"):
        return create_document_revision(
            self.document,
            ContentFile(b"next", name="plan.txt"),
            self.creator,
            kind,
            "Next version",
        )

    def try_status(self, value):
        self.document.refresh_from_db()
        serializer = DocumentSerializer(
            self.document,
            data={"status": value},
            partial=True,
            context={"user": self.creator},
        )
        with patch.object(
            DocumentSerializer,
            "_get_valid_choices_for_config_key",
            return_value=CHOICES,
        ):
            return serializer.is_valid(), serializer.errors

    def approve_current(self, revision=None):
        wf = self.start(TWO, revision=revision or self.revision2)
        perform_action(wf, self.alice, "SIGN", "s")
        return perform_action(wf, self.carol, "APPROVE", "a")

    # --- no automatic mapping of the workflow steps ----------------------------------

    def test_start_signature_and_validation_do_not_change_the_status(self):
        self.set_status("Sent")
        wf = self.start(THREE, revision=self.revision2)
        self.assertEqual(self.status(), "Sent")
        perform_action(wf, self.alice, "SIGN", "s")
        self.assertEqual(self.status(), "Sent")
        perform_action(wf, self.bob, "VALIDATE", "v")
        self.assertEqual(self.status(), "Sent")

    def test_final_approval_sets_the_current_status_to_approved(self):
        self.set_status("Draft")
        self.approve_current()
        self.assertEqual(self.status(), "Approved")

    def test_rejection_does_not_force_to_be_updated(self):
        self.set_status("Issued")
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "REJECT", "no")
        self.assertEqual(self.status(), "Issued")

    def test_an_old_revision_cannot_overwrite_the_document_status(self):
        # Workflow on the OLDER revision while a newer current revision exists.
        self.set_status("Issued")
        wf = self.start(TWO, revision=self.revision)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        self.assertEqual(wf.__class__.objects.get(pk=wf.pk).status, "APPROVED")
        self.assertEqual(self.status(), "Issued")

    # --- manual status guard -----------------------------------------------------------

    def test_active_workflow_blocks_a_manual_status_change(self):
        self.set_status("Sent")
        wf = self.start(THREE, revision=self.revision2)
        for step in (None, ("SIGN", self.alice), ("VALIDATE", self.bob)):
            if step:
                perform_action(wf, step[1], step[0], "c")
            valid, errors = self.try_status("Issued")
            self.assertFalse(valid)
            self.assertIn("status", errors)
            self.assertIn("workflow", str(errors["status"]))
        # an unchanged value is still accepted while the workflow runs
        self.assertTrue(self.try_status("Sent")[0])

    def test_workflow_on_an_old_revision_does_not_freeze_the_status(self):
        self.start(TWO, revision=self.revision)  # not the current revision
        self.assertTrue(self.try_status("Issued")[0])

    def test_terminal_workflows_allow_status_changes_again(self):
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "s")
        self.assertFalse(self.try_status("Issued")[0])
        perform_action(wf, self.carol, "APPROVE", "a")
        self.assertEqual(self.status(), "Approved")
        self.assertTrue(self.try_status("Issued")[0])  # business status after approval

    def test_rejected_workflow_allows_a_later_status_change(self):
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "REJECT", "no")
        self.assertTrue(self.try_status("To be updated")[0])
        self.assertTrue(self.try_status("Issued")[0])

    # --- new revision restarts the lifecycle --------------------------------------------

    def test_new_minor_version_sets_the_status_to_draft(self):
        self.set_status("Issued")
        self.revise("minor")
        self.assertEqual(self.status(), "Draft")

    def test_new_major_revision_sets_the_status_to_draft(self):
        self.approve_current()
        self.assertEqual(self.status(), "Approved")
        self.revise("major")
        self.assertEqual(self.status(), "Draft")

    def test_correction_after_rejection_sets_the_status_to_draft(self):
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "REJECT", "no")
        self.set_status("To be updated")
        corrected = self.revise("minor")
        self.assertEqual(self.status(), "Draft")
        # the rejected revision and its workflow are unchanged
        wf.refresh_from_db()
        self.assertEqual(wf.status, "REJECTED")
        self.assertEqual(wf.revision_id, self.revision2.pk)
        self.assertNotEqual(corrected.pk, self.revision2.pk)
