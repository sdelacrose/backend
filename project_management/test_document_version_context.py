from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from project_management.services.document_revision import create_document_revision
from project_management.services.document_version_context import CONTEXTS, change_contexts_for_files
from project_management.test_document_revision import MEDIA, NoNotificationsMixin, make_document


@override_settings(MEDIA_ROOT=MEDIA)
class VersionChangeContextTests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.user, self.document = make_document()

    def upload(self, context=None, kind="minor", content=b"changed"):
        revision = create_document_revision(self.document, ContentFile(content, name="plan.txt"),
            self.user, kind, "User supplied reason", change_context=context)
        self.document.refresh_from_db()
        return revision

    def context_of(self, revision):
        return change_contexts_for_files(self.document, [revision.file.name]).get(revision.file.name)

    def test_internal_iteration_is_explicitly_audited_without_changing_the_comment(self):
        revision = self.upload("internal_iteration")
        self.assertEqual(self.context_of(revision), "internal_iteration")
        self.assertEqual(revision.comment, "User supplied reason")
        self.assertIsNone(revision.approval_snapshot)
        self.assertIsNone(revision.revision_label)
        reason = self.document.history.filter(file=revision.file.name).order_by("history_date", "history_id").first().history_change_reason
        self.assertEqual(reason, CONTEXTS["internal_iteration"][1])

    def test_client_feedback_is_a_declaration_not_an_approval(self):
        revision = self.upload("client_feedback", "major")
        self.assertEqual(self.context_of(revision), "client_feedback")
        self.assertEqual((revision.major_revision, revision.minor_revision), ("1", 0))
        self.assertIsNone(revision.revision_label)
        self.assertFalse(revision.workflows.exists())

    def test_legacy_minor_and_major_are_never_inferred_as_business_nature(self):
        self.assertIsNone(change_contexts_for_files(self.document, [self.document.file.name]).get(self.document.file.name))
        minor = self.upload(None)
        major = self.upload(None, "major", b"changed again")
        contexts = change_contexts_for_files(self.document, [minor.file.name, major.file.name])
        self.assertIsNone(contexts.get(minor.file.name))
        self.assertIsNone(contexts.get(major.file.name))

    def test_invalid_or_conflicting_nature_creates_nothing(self):
        for context, kind in [("invented", "minor"), ("client_feedback", "minor"), ("internal_iteration", "major")]:
            with self.subTest(context=context, kind=kind), self.assertRaises(ValidationError):
                self.upload(context, kind)
        self.assertFalse(self.document.revisions.exists())

    def test_external_documents_keep_their_existing_revision_semantics(self):
        self.document.document_origin = "SUPPLIER"
        self.document.save(update_fields=["document_origin"])
        with self.assertRaises(ValidationError):
            self.upload("internal_iteration")
        self.assertIsNone(self.context_of(self.upload(None)))

    def test_later_metadata_edits_do_not_relabel_the_version(self):
        revision = self.upload("internal_iteration")
        self.document.description = "Later metadata edit"
        self.document.save(update_fields=["description"])
        self.assertEqual(self.context_of(revision), "internal_iteration")

    def test_a_new_declared_nature_cannot_bypass_the_duplicate_control(self):
        self.upload("internal_iteration")
        count = self.document.revisions.count()
        with self.assertRaises(ValidationError):
            self.upload("client_feedback", "major")
        self.assertEqual(self.document.revisions.count(), count)

    def test_first_file_has_no_invented_change_nature(self):
        self.document.file = ""
        self.document.save(update_fields=["file"])
        revision = create_document_revision(self.document, ContentFile(b"first", name="first.txt"), self.user, None, None)
        self.assertIsNone(self.context_of(revision))
