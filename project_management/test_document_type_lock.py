"""GED-L1.1: the document type is frozen once the official reference has been assigned.

The rule lives in DocumentSerializer.validate_document_type, which the single edit, the API
(PATCH/PUT) and the bulk update all go through -- these tests exercise each entry point.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Document, DocumentRevision
from project_management.permissions import DocumentPermission
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_bulk_update import DocumentBulkUpdateService
from project_management.test_document_control_reference import ProjectFixtureMixin
from project_management.test_document_workflow import WorkflowTestBase
from project_management.views.document import DocumentViewSet

LOCKED = "The document type cannot be changed once the official reference"


class DocumentTypeLockTests(ProjectFixtureMixin, WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.project, self.folder = self.make_project("Marseille Office Renovation")
        self.project.project_code = "MRS"
        self.project.save()

    def unreferenced(self, document_type="Drawing"):
        """A document that never received an official reference (legacy / external origin)."""
        document = Document(folder=self.folder, file_name="plain.txt", created_by=self.creator,
                            document_type=document_type)
        document.file.save("plain.txt", ContentFile(b"x"), save=False)
        document.save()
        return document

    def attempt(self, document, **data):
        serializer = DocumentSerializer(
            Document.objects.get(pk=document.pk), data=data, partial=True,
            context={"user": self.creator})
        with self.relaxed():
            return serializer, serializer.is_valid()

    # -- creation and editing before the reference -------------------------------------------

    def test_creation_with_a_document_type_is_allowed(self):
        document = self.create_doc(self.folder, document_type="Report", discipline="Electrical")
        self.assertEqual(document.document_type, "Report")
        self.assertEqual(document.full_document_number, "MRS-ELE-REP-00001")

    def test_the_type_stays_editable_before_a_reference_exists(self):
        document = self.unreferenced()
        self.assertFalse(document.full_document_number)
        updated = self.update_doc(document, document_type="Report")
        self.assertEqual(updated.document_type, "Report")
        self.assertFalse(updated.full_document_number)          # nothing was invented

    # -- after the reference ------------------------------------------------------------------

    def test_changing_the_type_after_referencing_is_refused_with_a_business_message(self):
        document = self.create_doc(self.folder)
        reference = document.full_document_number
        serializer, valid = self.attempt(document, document_type="Report")
        self.assertFalse(valid)
        message = str(serializer.errors["document_type"][0])
        self.assertIn(LOCKED, message)
        self.assertIn(reference, message)
        stored = Document.objects.get(pk=document.pk)
        self.assertEqual((stored.document_type, stored.full_document_number), ("Drawing", reference))

    def test_clearing_the_type_after_referencing_is_refused_too(self):
        document = self.create_doc(self.folder)
        for cleared in ("", None):
            with self.subTest(cleared=cleared):
                serializer, valid = self.attempt(document, document_type=cleared)
                self.assertFalse(valid)
                self.assertIn(LOCKED, str(serializer.errors["document_type"][0]))
        self.assertEqual(Document.objects.get(pk=document.pk).document_type, "Drawing")

    def test_a_legacy_reference_locks_the_type_as_well(self):
        legacy = Document(folder=self.folder, file_name="old.txt", created_by=self.creator,
                          document_type="Drawing", full_document_number="LEGACY-0001-00")
        legacy.file.save("old.txt", ContentFile(b"x"), save=False)
        legacy.save()
        _, valid = self.attempt(legacy, document_type="Report")
        self.assertFalse(valid)
        self.assertEqual(Document.objects.get(pk=legacy.pk).full_document_number, "LEGACY-0001-00")

    def test_other_fields_and_the_unchanged_type_remain_editable(self):
        document = self.create_doc(self.folder)
        reference = document.full_document_number
        # an edit form re-sends the whole record: the same type is not a change
        updated = self.update_doc(document, document_type="Drawing", status="Issued", category="Design")
        self.assertEqual((updated.status, updated.category), ("Issued", "Design"))
        self.assertEqual(updated.document_type, "Drawing")
        self.assertEqual(updated.full_document_number, reference)

    def test_a_refused_change_touches_neither_revisions_nor_history(self):
        document = self.create_doc(self.folder)
        revisions = list(DocumentRevision.objects.filter(document=document).values_list("pk", flat=True))
        history = Document.history.filter(id=document.id).count()
        _, valid = self.attempt(document, document_type="Report")
        self.assertFalse(valid)
        self.assertEqual(
            list(DocumentRevision.objects.filter(document=document).values_list("pk", flat=True)), revisions)
        self.assertEqual(Document.history.filter(id=document.id).count(), history)

    # -- direct API ---------------------------------------------------------------------------

    def api_patch(self, document, data):
        request = APIRequestFactory().patch(f"/document/{document.pk}", data, format="json")
        force_authenticate(request, user=self.creator)
        view = DocumentViewSet.as_view({"patch": "partial_update"})
        with self.relaxed(), \
                patch.object(DocumentViewSet, "get_permissions", return_value=[]), \
                patch.object(DocumentViewSet, "get_object",
                             lambda _view: Document.objects.get(pk=document.pk)):
            return view(request, pk=document.pk)

    def test_a_direct_api_call_cannot_change_the_type_of_a_referenced_document(self):
        document = self.create_doc(self.folder)
        response = self.api_patch(document, {"document_type": "Report"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(LOCKED, str(response.data["document_type"][0]))
        self.assertEqual(Document.objects.get(pk=document.pk).document_type, "Drawing")

    def test_a_direct_api_call_still_edits_the_other_fields(self):
        document = self.create_doc(self.folder)
        response = self.api_patch(document, {"status": "Issued"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(Document.objects.get(pk=document.pk).status, "Issued")

    def test_a_direct_api_call_may_still_set_the_type_before_a_reference(self):
        document = self.unreferenced()
        response = self.api_patch(document, {"document_type": "Report"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(Document.objects.get(pk=document.pk).document_type, "Report")

    # -- bulk update --------------------------------------------------------------------------

    def run_bulk(self, ids, changes, dry_run=False):
        service = DocumentBulkUpdateService(
            request=SimpleNamespace(user=self.creator), serializer_context={})
        with self.relaxed(), patch.object(DocumentPermission, "can_update_object", return_value=True):
            return service.execute(document_ids=ids, changes=changes, dry_run=dry_run)

    def test_bulk_update_refuses_referenced_documents_and_keeps_the_others_working(self):
        referenced = self.create_doc(self.folder)
        free = self.unreferenced()
        result = self.run_bulk([referenced.pk, free.pk], {"document_type": "Report"})
        self.assertEqual(result["updated_ids"], [free.pk])
        self.assertEqual([f["id"] for f in result["failures"]], [referenced.pk])
        self.assertIn(LOCKED, result["failures"][0]["error"])
        self.assertEqual(Document.objects.get(pk=referenced.pk).document_type, "Drawing")
        self.assertEqual(Document.objects.get(pk=free.pk).document_type, "Report")

    def test_the_bulk_preview_reports_the_same_refusal_without_writing(self):
        referenced = self.create_doc(self.folder)
        result = self.run_bulk([referenced.pk], {"document_type": "Report"}, dry_run=True)
        self.assertEqual(result["updated_ids"], [])
        self.assertIn(LOCKED, result["failures"][0]["error"])

    def test_bulk_changes_of_other_fields_are_unaffected(self):
        referenced = self.create_doc(self.folder)
        result = self.run_bulk([referenced.pk], {"status": "Issued"})
        self.assertEqual(result["updated_ids"], [referenced.pk])
        self.assertEqual(result["failures"], [])
        self.assertEqual(Document.objects.get(pk=referenced.pk).status, "Issued")


class DocumentDisciplineLockTests(ProjectFixtureMixin, WorkflowTestBase):
    """GED-L1.2: the discipline is frozen once the official reference has been assigned (and the
    type lock of GED-L1.1 keeps working next to it)."""

    LOCKED = "The discipline cannot be changed once the official reference"

    def setUp(self):
        super().setUp()
        self.project, self.folder = self.make_project("Marseille Office Renovation")
        self.project.project_code = "MRS"
        self.project.save()

    # the other helpers of the type-lock tests, shared as they are
    unreferenced = DocumentTypeLockTests.unreferenced
    attempt = DocumentTypeLockTests.attempt
    api_patch = DocumentTypeLockTests.api_patch
    run_bulk = DocumentTypeLockTests.run_bulk

    # -- creation and editing before the reference -------------------------------------------

    def test_creation_with_a_discipline_is_allowed(self):
        document = self.create_doc(self.folder, discipline="Electrical")
        self.assertEqual(document.discipline, "Electrical")
        self.assertEqual(document.full_document_number, "MRS-ELE-DWG-00001")

    def test_the_discipline_stays_editable_before_a_reference_exists(self):
        document = self.unreferenced()
        updated = self.update_doc(document, discipline="Electrical")
        self.assertEqual(updated.discipline, "Electrical")
        self.assertFalse(updated.full_document_number)

    # -- after the reference ------------------------------------------------------------------

    def test_changing_the_discipline_after_referencing_is_refused_with_a_business_message(self):
        document = self.create_doc(self.folder)
        reference = document.full_document_number
        serializer, valid = self.attempt(document, discipline="Electrical")
        self.assertFalse(valid)
        message = str(serializer.errors["discipline"][0])
        self.assertIn(self.LOCKED, message)
        self.assertIn(reference, message)
        stored = Document.objects.get(pk=document.pk)
        self.assertEqual((stored.discipline, stored.full_document_number), ("Structure", reference))

    def test_clearing_the_discipline_after_referencing_is_refused_too(self):
        document = self.create_doc(self.folder)
        for cleared in ("", None):
            with self.subTest(cleared=cleared):
                serializer, valid = self.attempt(document, discipline=cleared)
                self.assertFalse(valid)
                self.assertIn(self.LOCKED, str(serializer.errors["discipline"][0]))
        self.assertEqual(Document.objects.get(pk=document.pk).discipline, "Structure")

    def test_a_locked_document_gets_the_business_message_even_for_an_unknown_discipline(self):
        # the field validator checks the lock BEFORE the allowed choices (here the choices
        # are the real, un-relaxed ones: "Nonsense" is not one of them)
        document = self.create_doc(self.folder)
        serializer = DocumentSerializer(
            Document.objects.get(pk=document.pk), data={"discipline": "Nonsense"}, partial=True,
            context={"user": self.creator})
        with patch("project_management.serializers.document.get_valid_choices",
                   return_value=["Structure", "Electrical"]):
            self.assertFalse(serializer.is_valid())
        self.assertIn(self.LOCKED, str(serializer.errors["discipline"][0]))

    def test_a_legacy_reference_locks_the_discipline_as_well(self):
        legacy = Document(folder=self.folder, file_name="old.txt", created_by=self.creator,
                          discipline="Structure", full_document_number="LEGACY-0001-00")
        legacy.file.save("old.txt", ContentFile(b"x"), save=False)
        legacy.save()
        _, valid = self.attempt(legacy, discipline="Electrical")
        self.assertFalse(valid)

    def test_resending_the_same_discipline_and_editing_other_fields_stay_possible(self):
        document = self.create_doc(self.folder)
        reference = document.full_document_number
        updated = self.update_doc(document, discipline="Structure", status="Issued", category="Design")
        self.assertEqual((updated.discipline, updated.status, updated.category),
                         ("Structure", "Issued", "Design"))
        self.assertEqual(updated.full_document_number, reference)

    def test_the_hierarchical_option_decides_when_the_document_already_stores_one(self):
        document = self.create_doc(self.folder)
        Document.objects.filter(pk=document.pk).update(
            discipline_group_id="eng", discipline_option_id="opt-structure")
        stored = Document.objects.get(pk=document.pk)

        def message(instance, **attrs):
            serializer = DocumentSerializer(instance, context={"user": self.creator})
            return serializer._discipline_lock_message(attrs)

        self.assertIsNone(message(stored, discipline="Structure", discipline_option_id="opt-structure"))
        self.assertIsNone(message(stored, discipline_group_id="eng"))      # nothing about the discipline
        self.assertIn(self.LOCKED, message(stored, discipline="Structure", discipline_option_id="opt-other"))
        self.assertIn(self.LOCKED, message(stored, discipline="", discipline_group_id="",
                                           discipline_option_id=""))
        # a legacy document without option id may gain the ids of its own discipline...
        Document.objects.filter(pk=document.pk).update(discipline_group_id="", discipline_option_id="")
        legacy = Document.objects.get(pk=document.pk)
        self.assertIsNone(message(legacy, discipline="Structure", discipline_group_id="eng",
                                  discipline_option_id="opt-structure"))
        # ...but not swap it for another one
        self.assertIn(self.LOCKED, message(legacy, discipline="Electrical",
                                           discipline_option_id="opt-electrical"))

    def test_a_refused_change_touches_neither_revisions_nor_history(self):
        document = self.create_doc(self.folder)
        revisions = list(DocumentRevision.objects.filter(document=document).values_list("pk", flat=True))
        history = Document.history.filter(id=document.id).count()
        _, valid = self.attempt(document, discipline="Electrical")
        self.assertFalse(valid)
        self.assertEqual(
            list(DocumentRevision.objects.filter(document=document).values_list("pk", flat=True)), revisions)
        self.assertEqual(Document.history.filter(id=document.id).count(), history)

    # -- direct API and bulk ------------------------------------------------------------------

    def test_a_direct_api_call_cannot_change_the_discipline_of_a_referenced_document(self):
        document = self.create_doc(self.folder)
        response = self.api_patch(document, {"discipline": "Electrical"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(self.LOCKED, str(response.data["discipline"][0]))
        self.assertEqual(Document.objects.get(pk=document.pk).discipline, "Structure")

    def test_a_direct_api_call_still_edits_other_fields_and_pre_reference_disciplines(self):
        document = self.create_doc(self.folder)
        self.assertEqual(self.api_patch(document, {"status": "Issued"}).status_code, 200)
        free = self.unreferenced()
        response = self.api_patch(free, {"discipline": "Electrical"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(Document.objects.get(pk=free.pk).discipline, "Electrical")

    def hierarchical_referenced(self):
        """A referenced document created through the hierarchical picker (it stores its option)."""
        document = self.create_doc(self.folder)
        Document.objects.filter(pk=document.pk).update(
            discipline_group_id="eng", discipline_option_id="opt-structure")
        return document

    def test_bulk_update_refuses_referenced_documents(self):
        referenced = self.hierarchical_referenced()
        changes = {"discipline": "Electrical", "discipline_group_id": "eng",
                   "discipline_option_id": "opt-electrical"}
        result = self.run_bulk([referenced.pk], changes)
        self.assertEqual([f["id"] for f in result["failures"]], [referenced.pk])
        self.assertIn(self.LOCKED, result["failures"][0]["error"])
        self.assertEqual(result["updated_ids"], [])
        self.assertEqual(Document.objects.get(pk=referenced.pk).discipline, "Structure")

    def test_bulk_update_still_edits_other_fields_of_referenced_documents(self):
        referenced = self.hierarchical_referenced()
        result = self.run_bulk([referenced.pk], {"status": "Issued"})
        self.assertEqual((result["updated_ids"], result["failures"]), ([referenced.pk], []))

    def test_the_bulk_preview_reports_the_same_refusal_without_writing(self):
        referenced = self.hierarchical_referenced()
        changes = {"discipline": "Electrical", "discipline_group_id": "eng",
                   "discipline_option_id": "opt-electrical"}
        result = self.run_bulk([referenced.pk], changes, dry_run=True)
        self.assertEqual(result["updated_ids"], [])
        self.assertIn(self.LOCKED, result["failures"][0]["error"])

    # -- non-regression of GED-L1.1 -----------------------------------------------------------

    def test_the_document_type_lock_still_works(self):
        document = self.create_doc(self.folder)
        serializer, valid = self.attempt(document, document_type="Report")
        self.assertFalse(valid)
        self.assertIn(LOCKED, str(serializer.errors["document_type"][0]))
        self.assertEqual(self.api_patch(document, {"document_type": "Report"}).status_code, 400)
        result = self.run_bulk([document.pk], {"document_type": "Report"})
        self.assertIn(LOCKED, result["failures"][0]["error"])
