from rest_framework.exceptions import ValidationError as ApiValidationError
from project_management.models import DocumentControl
from project_management.services.document_register import save_document_control
from project_management.services.document_workflow import change_actor, perform_action
from project_management.serializers.document import DocumentSerializer
from project_management.test_document_workflow import WorkflowTestBase, THREE, TWO

class ReviewAssignmentLockTests(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.control = DocumentControl.objects.create(document=self.document, verifier=self.bob, approver=self.carol)
        self.document.author = self.alice
        self.document.save(update_fields=["author"])

    def test_preference_changes_are_refused_without_side_effects(self):
        self.start(THREE, revision=self.revision2)
        for field in ("verifier", "approver"):
            with self.subTest(field=field), self.assertRaises(ApiValidationError):
                save_document_control(self.document, {field: self.dave})
        self.control.refresh_from_db()
        self.assertEqual(self.control.verifier, self.bob)
        self.assertEqual(self.control.approver, self.carol)

    def test_author_change_is_refused_before_other_metadata_is_saved(self):
        self.start(THREE, revision=self.revision2)
        title = self.document.file_name
        with self.assertRaises(ApiValidationError):
            DocumentSerializer().update(self.document, {"author": self.dave, "file_name": "changed.txt"})
        self.document.refresh_from_db()
        self.assertEqual(self.document.author, self.alice)
        self.assertEqual(self.document.file_name, title)

    def test_raw_preference_update_is_also_refused_before_metadata_save(self):
        self.start(THREE, revision=self.revision2)
        serializer = DocumentSerializer(data={"approver": self.dave.pk})
        with self.assertRaises(ApiValidationError):
            serializer.update(self.document, {"description": "must not persist"})
        self.document.refresh_from_db()
        self.assertNotEqual(self.document.description, "must not persist")

    def test_unchanged_preferences_are_accepted(self):
        self.start(THREE, revision=self.revision2)
        save_document_control(self.document, {"verifier": self.bob, "approver": self.carol})

    def test_controlled_replacement_remains_available_to_author_only(self):
        wf = self.start(THREE, revision=self.revision2)
        change_actor(wf, self.alice, "approver", self.dave, "absence")
        self.assertEqual(wf.events.filter(action="CHANGE_ACTOR").count(), 1)
        wf.refresh_from_db()
        self.assertEqual(wf.approver, self.dave)
        self.control.refresh_from_db()
        self.assertEqual(self.control.approver, self.carol)

    def test_preferences_can_change_after_rejection(self):
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "")
        perform_action(wf, self.carol, "REJECT", "correction")
        save_document_control(self.document, {"approver": self.dave})
        self.control.refresh_from_db()
        self.assertEqual(self.control.approver, self.dave)
