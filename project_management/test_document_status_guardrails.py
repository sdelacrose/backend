from unittest.mock import patch

from django.core.files.base import ContentFile

from project_management.models import Document
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import create_document_revision
from project_management.services.document_workflow import perform_action
from project_management.test_document_workflow import THREE, TWO, WorkflowTestBase

CHOICES = ["Draft", "Approved", "Sent", "Issued", "For construction (IFC)", "On Hold"]


class BusinessStatusIndependenceTests(WorkflowTestBase):
    """Document.status is a configurable BUSINESS status: the validation workflow and new
    versions never write it, and it is never locked by a workflow."""

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

    def test_workflow_progression_never_writes_the_business_status(self):
        self.set_status("Issued")
        wf = self.start(THREE, revision=self.revision2)
        self.assertEqual(self.status(), "Issued")
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.bob, "VALIDATE", "v")
        self.assertEqual(self.status(), "Issued")
        perform_action(wf, self.carol, "APPROVE", "a")  # final approval
        self.assertEqual(self.status(), "Issued")  # not "Approved"

    def test_rejection_never_writes_the_business_status(self):
        self.set_status("On Hold")
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "REJECT", "no")
        self.assertEqual(self.status(), "On Hold")

    def test_new_versions_never_write_the_business_status(self):
        self.set_status("For construction (IFC)")
        for kind in ("minor", "major"):
            self.revise(kind)
            self.assertEqual(self.status(), "For construction (IFC)")  # not "Draft"

    def test_business_status_can_be_changed_during_an_active_workflow(self):
        self.set_status("Draft")
        wf = self.start(THREE, revision=self.revision2)
        for step in (None, ("SIGN", self.alice), ("VALIDATE", self.bob)):
            if step:
                perform_action(wf, step[1], step[0], "c")
            valid, errors = self.try_status("Sent")
            self.assertTrue(valid, errors)

    def test_configurable_status_validation_still_applies(self):
        wf = self.start(TWO, revision=self.revision2)
        valid, errors = self.try_status("Not a configured status")
        self.assertFalse(valid)
        self.assertIn("status", errors)
        self.assertIn("Invalid document status", str(errors["status"]))
        self.assertTrue(self.try_status("Issued")[0])
        self.assertEqual(wf.status, "DRAFT")
