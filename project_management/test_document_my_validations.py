from unittest.mock import patch

from django.core.files.base import ContentFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.filters import DocumentFilter
from project_management.models import Document
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import (
    annotate_current_workflow,
    perform_action,
)
from project_management.test_document_workflow import (
    THREE,
    TWO,
    WorkflowTestBase,
)
from project_management.views.document import DocumentViewSet


class MyValidationsTests(WorkflowTestBase):
    """Register filter "my validations": CURRENT revision, step expected from the user."""

    def make_doc(self, name):
        document = Document(
            folder=self.document.folder, file_name=name, created_by=self.creator
        )
        document.file.save(name, ContentFile(b"x"), save=False)
        document.save()
        revision = add_document_revision(
            document, ContentFile(b"v", name=name), self.creator
        )
        return document, revision

    def ids_for(self, user, base=None):
        request = type("R", (), {"user": user})()
        queryset = (base if base is not None else Document.objects.all()).order_by("id")
        return list(
            DocumentFilter(
                data={"my_validation": "true"}, queryset=queryset, request=request
            ).qs.values_list("id", flat=True)
        )

    def setUp(self):
        super().setUp()
        # self.document: workflow on the OLDER revision, newer current revision without one.
        self.start(THREE, revision=self.revision)  # DRAFT, alice is author (old revision)
        self.draft, draft_rev = self.make_doc("draft.txt")
        self.start(TWO, revision=draft_rev)  # DRAFT: alice author, carol approver
        self.verify, verify_rev = self.make_doc("verify.txt")
        wf = self.start(THREE, revision=verify_rev)  # alice, bob, carol
        perform_action(wf, self.alice, "SIGN", "s")  # -> VERIFY
        self.approve, approve_rev = self.make_doc("approve.txt")
        wf = self.start(TWO, revision=approve_rev)
        perform_action(wf, self.alice, "SIGN", "s")  # -> APPROVE
        self.approved, approved_rev = self.make_doc("approved.txt")
        wf = self.start(TWO, revision=approved_rev)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        self.rejected, rejected_rev = self.make_doc("rejected.txt")
        wf = self.start(TWO, revision=rejected_rev)
        perform_action(wf, self.alice, "REJECT", "no")
        self.plain, _ = self.make_doc("plain.txt")  # no workflow

    def test_draft_author_included_other_actors_excluded(self):
        self.assertIn(self.draft.id, self.ids_for(self.alice))
        self.assertNotIn(self.draft.id, self.ids_for(self.carol))  # approver: not yet
        self.assertNotIn(self.draft.id, self.ids_for(self.bob))

    def test_verify_verifier_included_author_excluded(self):
        self.assertIn(self.verify.id, self.ids_for(self.bob))
        self.assertNotIn(self.verify.id, self.ids_for(self.alice))
        self.assertNotIn(self.verify.id, self.ids_for(self.carol))  # future actor

    def test_approve_approver_included(self):
        self.assertIn(self.approve.id, self.ids_for(self.carol))
        self.assertNotIn(self.approve.id, self.ids_for(self.alice))

    def test_terminal_and_workflowless_documents_are_excluded(self):
        everyone = set()
        for user in (self.alice, self.bob, self.carol, self.dave, self.creator):
            everyone |= set(self.ids_for(user))
        for document in (self.approved, self.rejected, self.plain):
            self.assertNotIn(document.id, everyone)

    def test_only_the_current_revision_counts(self):
        # An older revision still waits for alice, but a newer current revision exists.
        self.assertNotIn(self.document.id, self.ids_for(self.alice))

    def test_filter_is_a_noop_when_not_requested(self):
        request = type("R", (), {"user": self.alice})()
        queryset = Document.objects.all()
        filtered = DocumentFilter(
            data={"my_validation": "false"}, queryset=queryset, request=request
        ).qs
        self.assertEqual(filtered.count(), queryset.count())

    def test_indicator_reflects_current_revision_and_current_user(self):
        by_name = {d.file_name: d for d in annotate_current_workflow(Document.objects.all())}

        def indicator(name, user):
            serializer = DocumentSerializer(
                context={"include_control": True, "user": user}
            )
            return serializer.get_workflow(by_name[name])

        self.assertEqual(indicator("draft.txt", self.alice), {"status": "DRAFT", "awaiting_me": True})
        self.assertEqual(indicator("draft.txt", self.carol), {"status": "DRAFT", "awaiting_me": False})
        self.assertEqual(indicator("verify.txt", self.bob)["awaiting_me"], True)
        self.assertEqual(indicator("approved.txt", self.carol), {"status": "APPROVED", "awaiting_me": False})
        self.assertEqual(indicator("rejected.txt", self.alice), {"status": "REJECTED", "awaiting_me": False})
        self.assertIsNone(indicator("plain.txt", self.alice))  # no workflow -> no indicator
        # the document whose OLD revision has a workflow shows nothing for its current one
        self.assertIsNone(indicator("plan.txt", self.alice))
        # not exposed outside the Register/list context
        self.assertIsNone(
            DocumentSerializer(context={"user": self.alice}).get_workflow(by_name["draft.txt"])
        )

    def test_annotation_is_a_single_query_no_n_plus_one(self):
        with CaptureQueriesContext(connection) as queries:
            rows = list(annotate_current_workflow(Document.objects.all()))
        self.assertEqual(len(queries), 1)
        self.assertGreaterEqual(len(rows), 6)

    def test_list_endpoint_filters_server_side_with_correct_pagination_and_count(self):
        # alice must act on: the draft document only (self.document is on an old revision).
        # Give her a second pending action to exercise pagination.
        extra, extra_rev = self.make_doc("extra.txt")
        self.start(TWO, revision=extra_rev)
        accessible = Document.objects.exclude(pk=self.plain.pk)  # plain is inaccessible

        def call(params, user):
            view = DocumentViewSet.as_view({"get": "list"})
            request = APIRequestFactory().get("/x/", params)
            force_authenticate(request, user=user)
            with patch.object(DocumentViewSet, "get_permissions", return_value=[]), patch(
                "project_management.views.document.get_accessible_document_queryset",
                return_value=accessible,
            ):
                return view(request)

        page1 = call({"my_validation": "true", "page": 1, "page_size": 1}, self.alice)
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.data["count"], 2)
        self.assertEqual(len(page1.data["results"]), 1)
        page2 = call({"my_validation": "true", "page": 2, "page_size": 1}, self.alice)
        seen = {r["id"] for r in page1.data["results"] + page2.data["results"]}
        self.assertEqual(seen, {self.draft.id, extra.id})
        for row in page1.data["results"] + page2.data["results"]:
            self.assertEqual(row["workflow"], {"status": "DRAFT", "awaiting_me": True})
        # Without the filter every accessible document is listed, with its own indicator.
        everything = call({"page": 1, "page_size": 50}, self.alice)
        by_id = {r["id"]: r["workflow"] for r in everything.data["results"]}
        self.assertEqual(len(by_id), accessible.count())
        self.assertNotIn(self.plain.id, by_id)  # inaccessible stays inaccessible
        self.assertEqual(by_id[self.approved.id]["status"], "APPROVED")
        # Another user sees a different list.
        carol = call({"my_validation": "true"}, self.carol)
        self.assertEqual({r["id"] for r in carol.data["results"]}, {self.approve.id})
