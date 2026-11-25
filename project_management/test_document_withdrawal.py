"""GED-L1.4B.2: logical withdrawal of a Document (Company library and Project library).

Nothing is deleted: reference, versions, files, workflows, events, transmittal items and history
stay; the withdrawal records who / when / why and closes the document to edits, new versions,
validation and transmission. Reuses the Transmittal fixtures (company + project + other tenant).
"""
from contextlib import ExitStack
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework.exceptions import ValidationError as ApiValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    Document,
    DocumentRevision,
    DocumentWorkflow,
    DocumentWorkflowEvent,
    Folder,
    TransmittalItem,
)
from project_management.permissions import DocumentPermission
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_bulk_update import DocumentBulkUpdateService
from project_management.services.document_register import assert_can_update_document
from project_management.services.document_revision import add_document_revision
from project_management.services.document_withdrawal import (
    ALREADY_WITHDRAWN_MESSAGE,
    REASON_REQUIRED_MESSAGE,
    TRANSMITTED_DELETE_MESSAGE,
    WITHDRAWN_DELETE_MESSAGE,
    WITHDRAWN_MESSAGE,
    filter_by_withdrawal,
    withdraw_document,
)
from project_management.services.document_workflow import perform_action, start_workflow
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_transmittal import TransmittalTestBase
from project_management.views.document import DocumentViewSet

REASON = "Superseded by the new layout"


class WithdrawalBase(TransmittalTestBase):
    """self.folder = Project library, self.company_folder = Company library."""

    def document_in(self, folder, name="plan.txt", reference="TST-0001"):
        document = self.make_document(folder=folder, name=name)
        document.full_document_number = reference
        document.sequence_number = 1
        document.save(update_fields=["full_document_number", "sequence_number"])
        return document

    def stored(self, document):
        return Document.objects.get(pk=document.pk)

    def withdraw(self, document, user=None, reason=REASON):
        return withdraw_document(document, user or self.owner, reason)

    def relaxed(self):
        stack = ExitStack()
        stack.enter_context(patch(
            "project_management.serializers.document.get_valid_choices",
            return_value=["Drawing", "Report", "Other", "Design"]))
        stack.enter_context(patch.object(
            DocumentSerializer, "_get_valid_choices_for_config_key", return_value=[]))
        stack.enter_context(patch.object(DocumentSerializer, "validate_folder", lambda s, v: v))
        stack.enter_context(patch.object(DocumentSerializer, "validate_discipline", lambda s, v: v))
        return stack

    def serializer_for(self, document, **data):
        return DocumentSerializer(
            self.stored(document), data=data, partial=True, context={"user": self.owner})

    def api(self, method, view_action, document, user=None, data=None, permissions=None,
            query=None):
        factory = APIRequestFactory()
        url = f"/document/{document.pk}"
        request = getattr(factory, method)(url, data if data is not None else query, format="json")
        force_authenticate(request, user=user or self.owner)
        view = DocumentViewSet.as_view({method: view_action})
        stack = ExitStack()
        stack.enter_context(self.relaxed())
        if permissions is not None:
            stack.enter_context(patch.object(DocumentViewSet, "get_permissions", return_value=[]))
        with stack:
            return view(request, pk=document.pk)


class WithdrawServiceTests(WithdrawalBase):
    def check_withdrawal(self, folder):
        document = self.document_in(folder)
        history_before = Document.history.filter(id=document.id).count()
        withdrawn = self.withdraw(document)
        stored = self.stored(document)
        self.assertEqual(withdrawn.pk, document.pk)
        self.assertIsNotNone(stored.withdrawn_at)
        self.assertEqual(stored.withdrawn_by_id, self.owner.pk)
        self.assertEqual(stored.withdrawal_reason, REASON)
        # the identity of the document is untouched and the history keeps the withdrawal
        self.assertEqual((stored.pk, stored.full_document_number), (document.pk, "TST-0001"))
        last = Document.history.filter(id=document.id).order_by("-history_date", "-history_id").first()
        self.assertEqual(Document.history.filter(id=document.id).count(), history_before + 1)
        self.assertEqual((last.history_user_id, last.withdrawal_reason), (self.owner.pk, REASON))
        self.assertEqual(last.history_change_reason, "Withdrawn")
        self.assertIsNotNone(last.withdrawn_at)

    def test_a_company_document_can_be_withdrawn(self):
        self.check_withdrawal(self.company_folder)

    def test_a_project_document_can_be_withdrawn(self):
        self.check_withdrawal(self.folder)

    def test_the_reason_is_mandatory(self):
        document = self.document_in(self.folder)
        for reason in ("", "   ", None, 12):
            with self.subTest(reason=reason):
                with self.assertRaises(ValidationError) as caught:
                    self.withdraw(document, reason=reason)
                self.assertIn(REASON_REQUIRED_MESSAGE, str(caught.exception))
        stored = self.stored(document)
        self.assertEqual((stored.withdrawn_at, stored.withdrawn_by_id, stored.withdrawal_reason),
                         (None, None, ""))

    def test_the_reason_is_trimmed_and_bounded(self):
        document = self.document_in(self.folder)
        with self.assertRaises(ValidationError):
            self.withdraw(document, reason="x" * 2001)
        self.withdraw(document, reason="  needed no more  ")
        self.assertEqual(self.stored(document).withdrawal_reason, "needed no more")

    def test_a_second_withdrawal_is_refused_and_keeps_the_first_record(self):
        document = self.document_in(self.folder)
        self.withdraw(document)
        first = self.stored(document)
        with self.assertRaises(ValidationError) as caught:
            self.withdraw(document, user=self.approver, reason="another reason")
        self.assertIn(ALREADY_WITHDRAWN_MESSAGE, str(caught.exception))
        again = self.stored(document)
        self.assertEqual((again.withdrawn_at, again.withdrawn_by_id, again.withdrawal_reason),
                         (first.withdrawn_at, self.owner.pk, REASON))

    def test_everything_documentary_is_preserved(self):
        document = self.document_in(self.folder)
        revision = self.approve(self.add_revision(document))
        self.add_revision(document, content=b"v2")
        self.add_revision(document, content=b"v3")
        transmittal = self.create_transmittal()
        document.transmittal_category = "For review"
        document.save(update_fields=["transmittal_category"])
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        counts = lambda: (  # noqa: E731
            DocumentRevision.objects.filter(document=document).count(),
            DocumentWorkflow.objects.filter(revision__document=document).count(),
            DocumentWorkflowEvent.objects.filter(workflow__revision__document=document).count(),
            TransmittalItem.objects.filter(document_revision__document=document).count(),
        )
        files = sorted(DocumentRevision.objects.filter(document=document).values_list("file", flat=True))
        label = DocumentRevision.objects.get(pk=revision.pk).revision_label
        before = counts()
        # (the first upload is snapshotted as a version too, hence 4 versions for 3 uploads)
        self.assertEqual((before[0] >= 3, before[1:]), (True, (1, 3, 1)))
        self.withdraw(document)
        self.assertEqual(counts(), before)
        self.assertEqual(
            sorted(DocumentRevision.objects.filter(document=document).values_list("file", flat=True)),
            files)
        self.assertEqual(DocumentRevision.objects.get(pk=revision.pk).revision_label, label)
        self.assertEqual(self.stored(document).full_document_number, "TST-0001")
        self.assertTrue(self.stored(document).file)


class WithdrawnDocumentIsClosedTests(WithdrawalBase):
    def setUp(self):
        super().setUp()
        self.document = self.document_in(self.folder)
        self.revision = self.approve(self.add_revision(self.document))
        self.document.transmittal_category = "For review"
        self.document.save(update_fields=["transmittal_category"])

    # -- editing -------------------------------------------------------------------------------

    def test_an_active_document_is_still_editable(self):
        serializer = self.serializer_for(self.document, status="On Hold")
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_single_edit_is_refused_after_withdrawal(self):
        self.withdraw(self.document)
        serializer = self.serializer_for(self.document, status="On Hold")
        with self.relaxed():
            self.assertFalse(serializer.is_valid())
        self.assertIn(WITHDRAWN_MESSAGE, str(serializer.errors["detail"][0]))
        self.assertNotEqual(self.stored(self.document).status, "On Hold")

    def test_api_patch_is_refused_after_withdrawal(self):
        self.withdraw(self.document)
        response = self.api("patch", "partial_update", self.document, data={"status": "On Hold"},
                            permissions=[])
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertIn(WITHDRAWN_MESSAGE, str(response.data["detail"][0]))

    def test_bulk_update_reports_the_withdrawn_document_and_edits_the_others(self):
        active = self.document_in(self.folder, name="other.txt", reference="TST-0002")
        self.withdraw(self.document)
        service = DocumentBulkUpdateService(
            request=type("R", (), {"user": self.owner})(), serializer_context={})
        with self.relaxed(), patch.object(DocumentPermission, "can_update_object", return_value=True):
            result = service.execute(document_ids=[self.document.pk, active.pk],
                                     changes={"status": "On Hold"})
        self.assertEqual(result["updated_ids"], [active.pk])
        self.assertEqual([f["id"] for f in result["failures"]], [self.document.pk])
        self.assertIn(WITHDRAWN_MESSAGE, result["failures"][0]["error"])

    def test_the_client_cannot_write_the_withdrawal_fields(self):
        response = self.api("patch", "partial_update", self.document, permissions=[], data={
            "withdrawn_at": "2026-01-01T00:00:00Z", "withdrawn_by": self.owner.pk,
            "withdrawal_reason": "sneaky"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        stored = self.stored(self.document)
        self.assertEqual((stored.withdrawn_at, stored.withdrawn_by_id, stored.withdrawal_reason),
                         (None, None, ""))

    def test_the_serializer_exposes_the_withdrawal_read_only(self):
        self.withdraw(self.document)
        data = DocumentSerializer(self.stored(self.document), context={"user": self.owner}).data
        self.assertEqual((data["withdrawn_by"], data["withdrawal_reason"]), (self.owner.pk, REASON))
        self.assertIsNotNone(data["withdrawn_at"])

    # -- versions, workflow, transmission -----------------------------------------------------

    def test_no_new_version_after_withdrawal(self):
        self.withdraw(self.document)
        count = DocumentRevision.objects.filter(document=self.document).count()
        with self.assertRaises(ValidationError) as caught:
            self.add_revision(self.document, content=b"late")
        self.assertIn(WITHDRAWN_MESSAGE, str(caught.exception))
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), count)

    def test_the_shared_update_guard_refuses_a_withdrawn_document(self):
        # register edit, attachments, versions, workflow and surveillance endpoints
        with patch("project_management.services.document_register.can_update_document",
                   return_value=True):
            assert_can_update_document(self.owner, self.document)
            self.withdraw(self.document)
            with self.assertRaises(ApiValidationError) as caught:
                assert_can_update_document(self.owner, self.document)
        self.assertIn(WITHDRAWN_MESSAGE, str(caught.exception.detail))

    def test_no_workflow_can_start_on_a_withdrawn_document(self):
        new_revision = self.add_revision(self.document, content=b"v2")
        self.withdraw(self.document)
        with self.assertRaises(ValidationError) as caught:
            start_workflow(new_revision, "TWO_STEP", self.owner, None, self.approver, self.owner)
        self.assertIn(WITHDRAWN_MESSAGE, str(caught.exception))
        self.assertFalse(DocumentWorkflow.objects.filter(revision=new_revision).exists())

    def test_a_running_workflow_cannot_be_withdrawn(self):
        document = self.document_in(self.company_folder, name="wf.txt", reference="TST-0009")
        revision = self.add_revision(document)
        workflow = start_workflow(revision, "THREE_STEP", self.owner, self.approver,
                                  self.approver, self.owner)
        events = workflow.events.count()
        with self.assertRaises(ValidationError) as caught:
            self.withdraw(document)
        self.assertIn("active workflow", str(caught.exception))
        self.assertIsNone(self.stored(document).withdrawn_at)
        workflow.refresh_from_db()
        self.assertEqual((workflow.status, workflow.events.count()), ("DRAFT", events))

    def test_no_transmission_of_a_withdrawn_document(self):
        transmittal = self.create_transmittal()
        self.withdraw(self.document)
        with self.assertRaises(ValidationError) as caught:
            add_transmittal_item(transmittal, self.revision, "FOR_REVIEW", self.owner)
        self.assertIn(WITHDRAWN_MESSAGE, str(caught.exception))
        self.assertFalse(TransmittalItem.objects.filter(transmittal=transmittal).exists())

    def test_a_draft_transmittal_cannot_be_finalized_with_a_withdrawn_document(self):
        transmittal = self.create_transmittal()
        add_transmittal_item(transmittal, self.revision, "FOR_REVIEW", self.owner)
        self.withdraw(self.document)
        with self.assertRaises(ValidationError) as caught:
            finalize_transmittal(transmittal, self.owner)
        self.assertIn(WITHDRAWN_MESSAGE, str(caught.exception))
        self.assertEqual(TransmittalItem.objects.filter(transmittal=transmittal).count(), 1)

    def test_an_incoming_supplier_revision_of_a_withdrawn_document_is_refused_too(self):
        revision = self.make_supplier_revision()
        transmittal = self.create_in_transmittal()
        self.withdraw(revision.document)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, None, self.owner)


class PhysicalDeletionTests(WithdrawalBase):
    def test_a_withdrawn_document_cannot_be_deleted(self):
        document = self.document_in(self.folder)
        history = Document.history.filter(id=document.id).count()
        self.withdraw(document)
        history += 1
        with self.assertRaises(ValidationError) as caught:
            self.stored(document).delete()
        self.assertIn(WITHDRAWN_DELETE_MESSAGE, str(caught.exception))
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())
        self.assertEqual(Document.history.filter(id=document.id).count(), history)

    def test_a_cascade_cannot_remove_a_withdrawn_document(self):
        document = self.document_in(self.folder)
        self.withdraw(document)
        with self.assertRaises(ValidationError), transaction.atomic():
            Document.objects.filter(pk=document.pk).delete()
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())

    def test_the_delete_endpoint_answers_with_a_business_message_for_a_withdrawn_document(self):
        document = self.document_in(self.folder)
        self.withdraw(document)
        response = self.api("delete", "destroy", document, permissions=[])
        self.assertEqual(response.status_code, 400)
        self.assertIn(WITHDRAWN_DELETE_MESSAGE, response.data["detail"])
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())

    def test_a_transmitted_document_without_workflow_gets_a_message_not_a_500(self):
        # incoming supplier revision: no workflow, no approval label -> not "evidence", yet the
        # transmittal item protects the version (PROTECT): formerly an unhandled ProtectedError
        revision = self.make_supplier_revision()
        transmittal = self.create_in_transmittal()
        add_transmittal_item(transmittal, revision, None, self.owner)
        document = revision.document
        with self.assertRaises(ValidationError) as caught:
            self.stored(document).delete()
        self.assertIn(TRANSMITTED_DELETE_MESSAGE, str(caught.exception))
        response = self.api("delete", "destroy", document, permissions=[])
        self.assertEqual(response.status_code, 400)
        self.assertIn(TRANSMITTED_DELETE_MESSAGE, response.data["detail"])
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())
        self.assertEqual(TransmittalItem.objects.filter(transmittal=transmittal).count(), 1)

    def test_an_approved_document_is_still_refused_with_the_evidence_message(self):
        document = self.document_in(self.folder)
        self.approve(self.add_revision(document))
        response = self.api("delete", "destroy", document, permissions=[])
        self.assertEqual(response.status_code, 400)
        self.assertIn("validation workflow history", response.data["detail"])

    def test_a_plain_draft_can_still_be_deleted(self):
        document = self.document_in(self.folder)
        response = self.api("delete", "destroy", document, permissions=[])
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Document.objects.filter(pk=document.pk).exists())

    def test_a_folder_holding_a_transmitted_document_is_refused_with_a_message(self):
        folder = Folder.objects.create(project=self.project, name="Transmitted docs")
        revision = self.make_supplier_revision(folder=folder)
        transmittal = self.create_in_transmittal()
        add_transmittal_item(transmittal, revision, None, self.owner)
        request = APIRequestFactory().delete(f"/folder/{folder.pk}")
        force_authenticate(request, user=self.owner)
        from project_management.views.document import FolderViewSet

        with patch.object(FolderViewSet, "get_permissions", return_value=[]):
            response = FolderViewSet.as_view({"delete": "destroy"})(request, pk=folder.pk)
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertTrue(Folder.objects.filter(pk=folder.pk).exists())


class ListFilterTests(WithdrawalBase):
    def setUp(self):
        super().setUp()
        self.active = self.document_in(self.folder, name="a.txt", reference="TST-0001")
        self.gone = self.document_in(self.folder, name="b.txt", reference="TST-0002")
        self.company_gone = self.document_in(self.company_folder, name="c.txt", reference="TST-0003")
        self.withdraw(self.gone)
        self.withdraw(self.company_gone)

    def ids(self, mode=None):
        queryset = Document.objects.all()
        return set(filter_by_withdrawal(queryset, mode).values_list("pk", flat=True))

    def test_active_documents_are_the_default(self):
        self.assertEqual(self.ids(), {self.active.pk})
        self.assertEqual(self.ids("exclude"), {self.active.pk})

    def test_withdrawn_documents_can_be_consulted(self):
        self.assertEqual(self.ids("only"), {self.gone.pk, self.company_gone.pk})
        self.assertEqual(self.ids("include"),
                         {self.active.pk, self.gone.pk, self.company_gone.pk})

    def test_an_unknown_value_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            self.ids("everything")

    def list_ids(self, query):
        factory = APIRequestFactory()
        request = factory.get("/document", query)
        force_authenticate(request, user=self.owner)
        with patch.object(DocumentViewSet, "get_permissions", return_value=[]), \
                patch.object(DocumentViewSet, "get_queryset",
                             lambda _view: Document.objects.all()):
            response = DocumentViewSet.as_view({"get": "list"})(request)
        return response

    def test_the_list_endpoint_hides_withdrawn_documents_by_default(self):
        response = self.list_ids({})
        self.assertEqual(response.status_code, 200)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual({row["id"] for row in rows}, {self.active.pk})

    def test_the_list_endpoint_can_return_the_withdrawn_ones(self):
        response = self.list_ids({"withdrawn": "only"})
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual({row["id"] for row in rows}, {self.gone.pk, self.company_gone.pk})
        self.assertEqual(self.list_ids({"withdrawn": "nope"}).status_code, 400)

    def test_the_detail_of_a_withdrawn_document_stays_reachable(self):
        response = self.api("get", "retrieve", self.gone, permissions=[])
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.data["withdrawal_reason"], REASON)


class WithdrawEndpointTests(WithdrawalBase):
    """POST document/<id>/withdraw with the REAL permission class; the module-permission lookup
    is stubbed so each case states exactly which rights the caller holds."""

    def call(self, document, user=None, grants=("delete",), reason=REASON, seen=None):
        def fake_check(perm, caller, module, action, scope, context):
            if seen is not None:
                seen.append((module, action, scope))
            return module == "documents" and action in grants

        request = APIRequestFactory().post(
            f"/document/{document.pk}/withdraw", {"reason": reason}, format="json")
        if user is not False:
            force_authenticate(request, user=user or self.owner)
        view = DocumentViewSet.as_view({"post": "withdraw"})
        with patch.object(DocumentPermission, "_check_permission", fake_check), \
                patch("project_management.permissions.check_company_project_permission",
                      return_value=False):
            return view(request, pk=document.pk)

    def test_company_and_project_documents_withdraw_with_the_delete_permission(self):
        for folder, scope in ((self.company_folder, "company"), (self.folder, "project")):
            with self.subTest(scope=scope):
                document = self.document_in(folder, reference=f"TST-{scope}")
                seen = []
                response = self.call(document, seen=seen)
                self.assertEqual(response.status_code, 200, getattr(response, "data", None))
                self.assertEqual(response.data["withdrawal_reason"], REASON)
                self.assertEqual(response.data["withdrawn_by"], self.owner.pk)
                self.assertEqual(response.data["full_document_number"], f"TST-{scope}")
                self.assertIsNotNone(self.stored(document).withdrawn_at)
                # each library is checked in its own scope: no Company/Project mix-up
                self.assertIn(("documents", "delete", scope), seen)
                self.assertNotIn(("documents", "delete",
                                  "project" if scope == "company" else "company"), seen)

    def test_without_the_delete_permission_nothing_is_withdrawn(self):
        for folder in (self.company_folder, self.folder):
            for grants in ((), ("view",), ("update",), ("create",)):
                with self.subTest(folder=folder.pk, grants=grants):
                    document = self.document_in(folder, name=f"d{len(grants)}.txt")
                    response = self.call(document, grants=grants)
                    self.assertEqual(response.status_code, 403)
                    self.assertIsNone(self.stored(document).withdrawn_at)

    def test_delete_assigned_created_only_covers_own_documents(self):
        for folder in (self.company_folder, self.folder):
            with self.subTest(folder=folder.pk):
                mine = self.document_in(folder, name="mine.txt")         # created_by = owner
                theirs = self.document_in(folder, name="theirs.txt")
                Document.objects.filter(pk=theirs.pk).update(created_by=self.approver, assignee=None)
                grants = ("delete_assigned_created",)
                self.assertEqual(self.call(mine, grants=grants).status_code, 200)
                self.assertEqual(self.call(theirs, grants=grants).status_code, 403)
                self.assertIsNone(self.stored(theirs).withdrawn_at)
                # assigned to the caller counts as well
                Document.objects.filter(pk=theirs.pk).update(assignee=self.owner)
                self.assertEqual(self.call(theirs, grants=grants).status_code, 200)

    def test_a_user_of_another_company_cannot_withdraw_even_with_the_right(self):
        self.other_owner.company = self.other_company
        self.other_owner.save(update_fields=["company"])
        document = self.document_in(self.company_folder)
        response = self.call(document, user=self.other_owner, grants=("delete", "full_access"))
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.stored(document).withdrawn_at)

    def test_an_anonymous_caller_is_refused(self):
        document = self.document_in(self.folder)
        response = self.call(document, user=False)
        self.assertIn(response.status_code, (401, 403))
        self.assertIsNone(self.stored(document).withdrawn_at)

    def test_the_reason_is_mandatory_on_the_endpoint(self):
        document = self.document_in(self.folder)
        for reason in ("", "   ", None):
            response = self.call(document, reason=reason)
            self.assertEqual(response.status_code, 400)
            self.assertIn(REASON_REQUIRED_MESSAGE, str(response.data["reason"]))
        self.assertIsNone(self.stored(document).withdrawn_at)

    def test_a_second_withdrawal_through_the_endpoint_is_refused(self):
        document = self.document_in(self.folder)
        self.assertEqual(self.call(document).status_code, 200)
        response = self.call(document, reason="again")
        self.assertEqual(response.status_code, 400)
        self.assertIn(ALREADY_WITHDRAWN_MESSAGE, response.data["detail"])
        self.assertEqual(self.stored(document).withdrawal_reason, REASON)
