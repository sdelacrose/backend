from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory, force_authenticate

from models.configuration_sync import PRESERVE_LOCAL, REPLACE
from models.document_control_configuration import get_document_control_configuration
from project_management.models import DocumentRevision, ECPMPConfig
from project_management.serializers import ECPMPCONFIGSerializer
from project_management.services.document_revision import (
    add_document_revision,
    assign_revision_label,
)
from project_management.services.document_workflow import (
    perform_action,
    start_workflow,
    start_workflow_for_document,
)
from project_management.services.project_codes import revision_convention_is_frozen
from project_management.services.project_configuration_templates import (
    ensure_project_config_template,
)
from project_management.test_document_control_reference import ProjectFixtureMixin
from project_management.test_document_workflow import THREE, TWO, WorkflowTestBase
from project_management.views.document_revision import serialize_revision
from project_management.views.ecpmp_config import ECPMPConfigViewSet


class ProjectRevisionBase(ProjectFixtureMixin, WorkflowTestBase):
    def project_document(self, name="Rev Project", **control):
        """A document of a (disposable) project configured with `control`."""
        self.project, self.folder = self.make_project(name, control=control)
        self.config = ECPMPConfig.objects.get(project=self.project)
        self.doc = self.create_doc(self.folder)
        return self.doc

    def new_version(self, content=b"x"):
        return add_document_revision(
            self.doc, ContentFile(content, name="f.txt"), self.creator
        )

    def approve(self, revision=None):
        revision = revision or self.new_version()
        wf = start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        revision.refresh_from_db()
        return revision

    def reject(self, revision=None):
        revision = revision or self.new_version()
        wf = start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        perform_action(wf, self.alice, "REJECT", "no")
        revision.refresh_from_db()
        return revision

    def stored(self):
        return get_document_control_configuration(
            ECPMPConfig.objects.get(project=self.project)
        )

    def change(self, **sections):
        serializer = ECPMPCONFIGSerializer(
            self.config, data={"document_control_configuration": sections}, partial=True
        )
        if not serializer.is_valid():
            return serializer.errors
        serializer.save()
        self.config.refresh_from_db()
        return None

    def call(self, action, data=None):
        request = APIRequestFactory().post("/x/", data or {}, format="json")
        force_authenticate(request, user=self.creator)
        view = ECPMPConfigViewSet.as_view({"post": action})
        with patch.object(ECPMPConfigViewSet, "get_permissions", return_value=[AllowAny()]):
            return view(request, pk=self.config.pk)


class AlphabeticRevisionTests(ProjectRevisionBase):
    def test_default_config_assigns_a_then_b(self):
        self.project_document()
        self.assertEqual(self.approve().revision_label, "A")
        self.assertEqual(self.approve().revision_label, "B")

    def test_z_increments_to_aa(self):
        self.project_document(revision={"scheme": "ALPHABETIC", "start": "Z"})
        self.assertEqual(self.approve().revision_label, "Z")
        self.assertEqual(self.approve().revision_label, "AA")

    def test_a_rejected_version_consumes_no_revision(self):
        self.project_document()
        rejected = self.reject()
        self.assertIsNone(rejected.revision_label)
        self.assertEqual(self.approve().revision_label, "A")
        self.assertIsNone(self.reject().revision_label)
        self.assertEqual(self.approve().revision_label, "B")

    def test_creating_a_version_assigns_no_revision_label(self):
        self.project_document()
        self.assertIsNone(self.new_version().revision_label)
        self.assertIsNone(self.new_version(b"y").revision_label)


class NumericRevisionTests(ProjectRevisionBase):
    def test_numeric_start_0(self):
        self.project_document(revision={"scheme": "NUMERIC", "start": "0"})
        self.assertEqual(self.approve().revision_label, "0")
        self.assertEqual(self.approve().revision_label, "1")

    def test_numeric_start_1(self):
        self.project_document(revision={"scheme": "NUMERIC", "start": "1"})
        self.assertEqual(self.approve().revision_label, "1")
        self.assertEqual(self.approve().revision_label, "2")

    def test_revision_label_zero_is_a_real_revision_in_the_api_payload(self):
        self.project_document(revision={"scheme": "NUMERIC", "start": "0"})
        revision = self.approve()
        self.assertEqual(revision.revision_label, "0")
        request = APIRequestFactory().get("/x/")
        payload = serialize_revision(revision, request, self.doc)
        self.assertEqual(payload["revision_label"], "0")
        self.assertIsNotNone(payload["revision_label"])


class RevisionHistoryTests(ProjectRevisionBase):
    def test_existing_labels_are_never_rewritten_and_are_respected(self):
        self.project_document()
        first = self.new_version()
        DocumentRevision.objects.filter(pk=first.pk).update(revision_label="A")
        second = self.new_version(b"2")
        DocumentRevision.objects.filter(pk=second.pk).update(revision_label="B")
        self.assertEqual(self.approve().revision_label, "C")
        for revision, label in ((first, "A"), (second, "B")):
            self.assertEqual(assign_revision_label(revision), label)
            self.assertEqual(DocumentRevision.objects.get(pk=revision.pk).revision_label, label)

    def test_numeric_history_is_respected(self):
        self.project_document(revision={"scheme": "NUMERIC", "start": "0"})
        for number, label in enumerate(("0", "1")):
            version = self.new_version(str(number).encode())
            DocumentRevision.objects.filter(pk=version.pk).update(revision_label=label)
        self.assertEqual(self.approve().revision_label, "2")

    def test_old_approved_revisions_are_unchanged_by_later_approvals_and_config(self):
        self.project_document()
        first = self.approve()
        self.change(workflow={"default": "TWO_STEP"})
        second = self.approve()
        self.assertEqual(DocumentRevision.objects.get(pk=first.pk).revision_label, "A")
        self.assertEqual(second.revision_label, "B")


class RevisionConfigGuardrailTests(ProjectRevisionBase):
    def test_scheme_and_start_can_change_before_the_first_assigned_revision(self):
        self.project_document()
        self.assertFalse(revision_convention_is_frozen(self.project))
        self.new_version()
        self.reject()  # a rejected version assigns nothing
        self.assertFalse(revision_convention_is_frozen(self.project))
        self.assertIsNone(self.change(revision={"scheme": "NUMERIC", "start": "1"}))
        self.assertEqual(self.stored()["revision"], {"scheme": "NUMERIC", "start": "1"})
        self.assertIsNone(self.change(revision={"scheme": "NUMERIC", "start": "5"}))
        self.assertEqual(self.stored()["revision"]["start"], "5")

    def test_scheme_change_is_rejected_after_the_first_assigned_revision(self):
        self.project_document()
        self.approve()
        self.assertTrue(revision_convention_is_frozen(self.project))
        errors = self.change(revision={"scheme": "NUMERIC", "start": "0"})
        self.assertIn("revision scheme", str(errors))
        self.assertEqual(self.stored()["revision"], {"scheme": "ALPHABETIC", "start": "A"})

    def test_start_change_is_rejected_after_the_first_assigned_revision(self):
        self.project_document()
        self.approve()
        errors = self.change(revision={"scheme": "ALPHABETIC", "start": "C"})
        self.assertIn("document_control_configuration", errors)
        self.assertEqual(self.stored()["revision"]["start"], "A")

    def test_sync_and_reset_keep_the_revision_config_once_a_label_is_assigned(self):
        self.project_document()
        self.approve()
        template = ensure_project_config_template(self.project)
        template.document_control_configuration = {
            "revision": {"scheme": "NUMERIC", "start": "0"},
            "workflow": {"default": "TWO_STEP"},
        }
        template.save()
        for strategy in (PRESERVE_LOCAL, REPLACE):
            response = self.call("sync_with_admin", {"strategy": strategy})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(self.stored()["revision"], {"scheme": "ALPHABETIC", "start": "A"})
        self.assertEqual(self.stored()["workflow"]["default"], "TWO_STEP")  # not frozen
        response = self.call("reset_config")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored()["revision"], {"scheme": "ALPHABETIC", "start": "A"})

    def test_sync_still_applies_the_template_before_the_first_assigned_revision(self):
        self.project_document()
        template = ensure_project_config_template(self.project)
        template.document_control_configuration = {
            "revision": {"scheme": "NUMERIC", "start": "0"}}
        template.save()
        response = self.call("sync_with_admin", {"strategy": REPLACE})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored()["revision"], {"scheme": "NUMERIC", "start": "0"})

    def test_workflow_default_remains_editable_after_approved_revisions(self):
        self.project_document()
        self.approve()
        self.assertIsNone(self.change(workflow={"default": "TWO_STEP"}))
        self.assertEqual(self.stored()["workflow"]["default"], "TWO_STEP")
        self.assertIsNone(self.change(workflow={"default": "THREE_STEP"}))
        self.assertEqual(self.stored()["workflow"]["default"], "THREE_STEP")


class WorkflowDefaultTests(ProjectRevisionBase):
    def start_without_type(self, verifier=None):
        return start_workflow(
            self.new_version(), None, self.alice, verifier, self.carol, self.creator
        )

    def test_missing_workflow_type_uses_three_step_with_the_default_config(self):
        self.project_document()
        self.assertEqual(self.start_without_type(self.bob).workflow_type, THREE)

    def test_configured_two_step_is_the_backend_default(self):
        self.project_document(workflow={"default": "TWO_STEP"})
        workflow = self.start_without_type()
        self.assertEqual(workflow.workflow_type, TWO)
        self.assertIsNone(workflow.verifier)

    def test_configured_three_step_is_the_backend_default(self):
        self.project_document(workflow={"default": "THREE_STEP"})
        self.assertEqual(self.start_without_type(self.bob).workflow_type, THREE)

    def test_explicit_workflow_type_overrides_the_configured_default(self):
        self.project_document(workflow={"default": "TWO_STEP"})
        workflow = start_workflow(
            self.new_version(), THREE, self.alice, self.bob, self.carol, self.creator
        )
        self.assertEqual(workflow.workflow_type, THREE)
        self.project_document("Other", workflow={"default": "THREE_STEP"})
        workflow = start_workflow(
            self.new_version(), TWO, self.alice, None, self.carol, self.creator
        )
        self.assertEqual(workflow.workflow_type, TWO)

    def test_document_start_uses_the_default_and_an_invalid_type_is_still_rejected(self):
        self.project_document(workflow={"default": "TWO_STEP"})
        self.new_version()
        workflow = start_workflow_for_document(
            self.doc, None, self.alice, None, self.carol, self.creator
        )
        self.assertEqual(workflow.workflow_type, TWO)
        self.project_document("Third")
        with self.assertRaises(ValidationError):
            start_workflow_for_document(
                self.doc, "FOUR_STEP", self.alice, self.bob, self.carol, self.creator
            )

    def test_a_document_without_project_defaults_to_three_step(self):
        # self.document lives in a company library folder (no project).
        workflow = start_workflow(
            self.revision, None, self.alice, self.bob, self.carol, self.creator
        )
        self.assertEqual(workflow.workflow_type, THREE)
