from django.core.files.base import ContentFile
from django.db import connection
from django.test.utils import CaptureQueriesContext

from project_management.models import Document
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import (
    annotate_current_workflow,
    get_internal_status,
    internal_status_from_workflow_status,
    perform_action,
)
from project_management.test_document_workflow import THREE, TWO, WorkflowTestBase


class InternalStatusTests(WorkflowTestBase):
    """Derived internal status of the CURRENT version: DRAFT / IN_VALIDATION / APPROVED."""

    def new_document(self, name):
        document = Document(
            folder=self.document.folder, file_name=name, created_by=self.creator
        )
        document.file.save(name, ContentFile(b"x"), save=False)
        document.save()
        return document

    def version(self, document):
        return add_document_revision(
            document, ContentFile(b"v", name="f.txt"), self.creator
        )

    def reload(self, document):
        return Document.objects.get(pk=document.pk)

    def internal(self, document, annotated=False):
        document = self.reload(document)
        if annotated:
            document = annotate_current_workflow(
                Document.objects.filter(pk=document.pk)
            ).get()
        return get_internal_status(document)

    def approve(self, revision):
        wf = self.start(TWO, revision=revision)
        perform_action(wf, self.alice, "SIGN", "s")
        return perform_action(wf, self.carol, "APPROVE", "a")

    def test_pure_derivation(self):
        derive = internal_status_from_workflow_status
        self.assertEqual(derive(None), "DRAFT")
        self.assertEqual(derive("REJECTED"), "DRAFT")
        for status in ("DRAFT", "VERIFY", "APPROVE"):
            self.assertEqual(derive(status), "IN_VALIDATION")
        self.assertEqual(derive("APPROVED"), "APPROVED")

    def test_version_without_workflow_is_draft(self):
        document = self.new_document("a.txt")
        self.assertEqual(self.internal(document), "DRAFT")  # legacy: no version yet
        self.version(document)
        for annotated in (False, True):
            self.assertEqual(self.internal(document, annotated), "DRAFT")

    def test_active_workflow_is_in_validation_at_every_step(self):
        document = self.new_document("a.txt")
        version = self.version(document)
        wf = self.start(THREE, revision=version)
        checks = [None, ("SIGN", self.alice), ("VALIDATE", self.bob)]
        for step in checks:  # DRAFT, VERIFY, APPROVE
            if step:
                perform_action(wf, step[1], step[0], "c")
            for annotated in (False, True):
                self.assertEqual(self.internal(document, annotated), "IN_VALIDATION")

    def test_rejected_current_version_is_draft(self):
        document = self.new_document("a.txt")
        version = self.version(document)
        perform_action(self.start(TWO, revision=version), self.alice, "REJECT", "no")
        for annotated in (False, True):
            self.assertEqual(self.internal(document, annotated), "DRAFT")

    def test_approved_current_version_is_approved(self):
        document = self.new_document("a.txt")
        self.approve(self.version(document))
        for annotated in (False, True):
            self.assertEqual(self.internal(document, annotated), "APPROVED")

    def test_only_the_current_version_counts(self):
        document = self.new_document("a.txt")
        self.approve(self.version(document))  # v old: APPROVED
        self.version(document)  # current v: no workflow
        self.assertEqual(self.internal(document), "DRAFT")
        self.assertEqual(self.internal(document, annotated=True), "DRAFT")
        # old approved + current version with an active workflow
        current = self.version(self.new_document("b.txt"))
        other = current.document
        self.approve(current)
        newer = self.version(other)
        self.start(TWO, revision=newer)
        self.assertEqual(self.internal(other), "IN_VALIDATION")

    def test_current_rejected_after_an_older_approved_is_draft(self):
        document = self.new_document("c.txt")
        self.approve(self.version(document))
        current = self.version(document)
        perform_action(self.start(TWO, revision=current), self.alice, "REJECT", "no")
        self.assertEqual(self.internal(document), "DRAFT")

    def test_business_status_and_internal_status_are_independent_dimensions(self):
        cases = [
            ("Issued", None, "DRAFT"),
            ("Sent", "start", "IN_VALIDATION"),
            ("For construction (IFC)", "approve", "APPROVED"),
        ]
        for index, (business, action, expected) in enumerate(cases):
            document = self.new_document(f"i{index}.txt")
            version = self.version(document)
            if action == "start":
                self.start(TWO, revision=version)
            elif action == "approve":
                self.approve(version)
            Document.objects.filter(pk=document.pk).update(status=business)
            stored = self.reload(document)
            self.assertEqual(stored.status, business)
            self.assertEqual(get_internal_status(stored), expected)

    def test_serializer_exposes_the_derived_value_only_in_the_register_context(self):
        document = self.new_document("s.txt")
        self.approve(self.version(document))
        stored = self.reload(document)
        with_context = DocumentSerializer(context={"include_control": True})
        self.assertEqual(with_context.get_internal_status(stored), "APPROVED")
        self.assertIsNone(DocumentSerializer(context={}).get_internal_status(stored))
        self.assertNotIn("internal_status", [f.name for f in Document._meta.get_fields()])

    def test_list_annotation_does_not_add_per_row_queries(self):
        for index in range(3):
            self.version(self.new_document(f"n{index}.txt"))
        with CaptureQueriesContext(connection) as queries:
            rows = list(annotate_current_workflow(Document.objects.all()))
            values = [get_internal_status(row) for row in rows]
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(values), len(rows))

