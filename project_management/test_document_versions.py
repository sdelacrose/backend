from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from project_management.models import Document, DocumentRevision
from project_management.services.document_revision import (
    add_document_revision,
    assign_revision_label,
    ensure_current_revision,
    next_revision_label,
)
from project_management.services.document_workflow import perform_action
from project_management.test_document_workflow import THREE, TWO, WorkflowTestBase


class VersionRevisionTests(WorkflowTestBase):
    """VERSION = sequential working file; REVISION = label given only at internal approval."""

    def new_document(self, name):
        document = Document(
            folder=self.document.folder, file_name=name, created_by=self.creator
        )
        document.file.save(name, ContentFile(b"x"), save=False)
        document.save()
        return document

    def new_version(self, document, content=b"v"):
        return add_document_revision(
            document, ContentFile(content, name="f.txt"), self.creator
        )

    def approve(self, revision, workflow_type=TWO):
        wf = self.start(workflow_type, revision=revision)
        perform_action(wf, self.alice, "SIGN", "s")
        if workflow_type == THREE:
            perform_action(wf, self.bob, "VALIDATE", "v")
        return perform_action(wf, self.carol, "APPROVE", "a")

    def reload(self, revision):
        return DocumentRevision.objects.get(pk=revision.pk)

    # --- version numbers --------------------------------------------------------------

    def test_first_version_is_1_and_the_next_is_2(self):
        document = self.new_document("a.txt")
        first = ensure_current_revision(document)
        self.assertEqual(first.version_number, 1)
        second = self.new_version(document)
        self.assertEqual(second.version_number, 2)
        self.assertEqual(self.new_version(document).version_number, 3)

    def test_documents_number_their_versions_independently(self):
        a, b = self.new_document("a.txt"), self.new_document("b.txt")
        self.assertEqual(ensure_current_revision(a).version_number, 1)
        self.assertEqual(ensure_current_revision(b).version_number, 1)
        self.assertEqual(self.new_version(a).version_number, 2)
        self.assertEqual(self.new_version(b).version_number, 2)

    def test_version_number_is_unique_per_document(self):
        document = self.new_document("a.txt")
        first = ensure_current_revision(document)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentRevision.objects.create(
                document=document,
                version_number=first.version_number,
                major_revision="9",
                minor_revision=9,
                file="documents/x.txt",
            )

    def test_version_number_cannot_be_modified(self):
        version = self.new_version(self.new_document("a.txt"))
        version.version_number = 99
        with self.assertRaises(ValidationError):
            version.save()

    # --- revision label stays empty until approval -------------------------------------

    def test_new_version_and_workflow_steps_leave_the_revision_empty(self):
        document = self.new_document("a.txt")
        version = self.new_version(document)
        self.assertIsNone(self.reload(version).revision_label)
        wf = self.start(THREE, revision=version)
        self.assertIsNone(self.reload(version).revision_label)  # start
        perform_action(wf, self.alice, "SIGN", "s")
        self.assertIsNone(self.reload(version).revision_label)  # author signature
        perform_action(wf, self.bob, "VALIDATE", "v")
        self.assertIsNone(self.reload(version).revision_label)  # verifier validation

    def test_rejection_leaves_the_revision_empty(self):
        version = self.new_version(self.new_document("a.txt"))
        wf = self.start(TWO, revision=version)
        perform_action(wf, self.alice, "REJECT", "no")
        self.assertIsNone(self.reload(version).revision_label)

    # --- approval assigns A, B, ... ------------------------------------------------------

    def test_first_approval_is_A_then_B(self):
        document = self.new_document("a.txt")
        v1 = self.new_version(document)
        self.approve(v1)
        self.assertEqual(self.reload(v1).revision_label, "A")
        v2 = self.new_version(document)
        self.assertIsNone(self.reload(v2).revision_label)
        self.approve(v2)
        self.assertEqual(self.reload(v2).revision_label, "B")
        self.assertEqual(self.reload(v1).revision_label, "A")  # never reassigned

    def test_a_rejected_version_does_not_consume_a_revision_label(self):
        document = self.new_document("a.txt")
        v1 = self.new_version(document)
        self.approve(v1)  # A
        v2 = self.new_version(document)
        perform_action(self.start(TWO, revision=v2), self.alice, "REJECT", "no")
        v3 = self.new_version(document)  # correction
        self.approve(v3)
        labels = {
            r.version_number: r.revision_label
            for r in DocumentRevision.objects.filter(document=document)
        }
        self.assertEqual(labels[v1.version_number], "A")
        self.assertIsNone(labels[v2.version_number])
        self.assertEqual(labels[v3.version_number], "B")  # not C

    def test_sequence_goes_from_Z_to_AA(self):
        self.assertEqual(next_revision_label([]), "A")
        self.assertEqual(next_revision_label(["A", "B"]), "C")
        self.assertEqual(next_revision_label(["Z"]), "AA")
        self.assertEqual(next_revision_label(["Z", "AA"]), "AB")
        self.assertEqual(next_revision_label(["AZ"]), "BA")
        document = self.new_document("a.txt")
        old = self.new_version(document)
        DocumentRevision.objects.filter(pk=old.pk).update(revision_label="Z")
        newer = self.new_version(document)
        self.approve(newer)
        self.assertEqual(self.reload(newer).revision_label, "AA")

    def test_an_assigned_revision_label_is_never_changed(self):
        version = self.new_version(self.new_document("a.txt"))
        self.approve(version)
        self.assertEqual(assign_revision_label(version), "A")
        self.assertEqual(assign_revision_label(self.reload(version)), "A")
        stored = self.reload(version)
        stored.revision_label = "Z"
        with self.assertRaises(ValidationError):  # stored versions stay immutable
            stored.save()
        self.assertEqual(self.reload(version).revision_label, "A")
        # the label is unique within the document
        other = self.new_version(version.document)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentRevision.objects.filter(pk=other.pk).update(revision_label="A")

    # --- isolation stays valid -----------------------------------------------------------

    def test_labels_workflows_and_files_stay_on_their_own_version_and_document(self):
        a, b = self.new_document("a.txt"), self.new_document("b.txt")
        va, vb = self.new_version(a, b"file-a"), self.new_version(b, b"file-b")
        wa, wb = self.start(TWO, revision=va), self.start(TWO, revision=vb)
        for wf in (wa, wb):
            perform_action(wf, self.alice, "SIGN", "s")
            perform_action(wf, self.carol, "APPROVE", "a")
        self.assertEqual(self.reload(va).revision_label, "A")
        self.assertEqual(self.reload(vb).revision_label, "A")  # independent per document
        self.assertEqual(wa.events.count(), 3)
        self.assertFalse(
            set(wa.events.values_list("id", flat=True))
            & set(wb.events.values_list("id", flat=True))
        )
        with va.file.open("rb") as handle:
            self.assertEqual(handle.read(), b"file-a")
        with vb.file.open("rb") as handle:
            self.assertEqual(handle.read(), b"file-b")
        self.assertEqual(wa.revision_id, va.pk)
        self.assertEqual(wb.revision_id, vb.pk)
