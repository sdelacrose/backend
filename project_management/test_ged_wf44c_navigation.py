"""GED-WF.4.4-C: what the Transmittal -> document navigation relies on (item identifiers, document access rules) and the
register's opt-out of the linked-items preload. Real roles and services; only the licence check is stubbed."""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.test_ged_wf44b_history import HistoryBase
from project_management.test_transmittal_client_return import TVIEW, VIEW
from project_management.views.document import DocumentViewSet
from project_management.views.transmittal import TransmittalItemViewSet, TransmittalViewSet

RETRIEVE = DocumentViewSet.as_view({"get": "retrieve"})
LIST = DocumentViewSet.as_view({"get": "list"})


class NavigationBase(HistoryBase):
    def transmittal_of(self, document):
        return document.revisions.first().transmittal_items.first().transmittal

    def get(self, view, user, **kwargs):
        request = APIRequestFactory().get("/x/", kwargs.pop("params", {}))
        if user is not None:
            force_authenticate(request, user=user)
        response = view(request, **kwargs)
        if hasattr(response, "render"):
            response.render()
        return response


class ItemIdentifierTests(NavigationBase):
    def test_an_authorised_transmittal_reader_gets_the_document_and_revision_identifiers_of_each_item(self):
        transmittal = self.transmittal_of(self.doc1)
        response = self.get(TransmittalViewSet.as_view({"get": "retrieve"}), self.transmittal_reader, pk=transmittal.pk)
        self.assertEqual(response.status_code, 200)
        detail = response.data["items"][0]["document_revision_detail"]
        self.assertEqual(detail["document"]["id"], self.doc1.pk)
        self.assertEqual(detail["revision"]["id"], self.rev1.pk)
        self.assertIsNotNone(detail["revision"]["revision_label"])  # the transmitted revision label stays available

    def test_transmittal_view_alone_never_gives_the_document_itself(self):
        only_transmittals = self.grant("wf44c.tonly@links-local.invalid", self.project, [TVIEW])
        transmittal = self.transmittal_of(self.doc1)
        self.assertEqual(self.get(TransmittalViewSet.as_view({"get": "retrieve"}), only_transmittals, pk=transmittal.pk).status_code, 200)
        # ...but the document is refused by the backend (hence the front end never draws the link for this user)
        self.assertIn(self.get(RETRIEVE, only_transmittals, pk=self.doc1.pk).status_code, (403, 404))


class DocumentAccessTests(NavigationBase):
    def test_accessible_withdrawn_unknown_other_project_and_anonymous_documents(self):
        self.assertEqual(self.get(RETRIEVE, self.summary_reader, pk=self.doc1.pk).status_code, 200)
        from project_management.models import Document
        Document.objects.filter(pk=self.doc1.pk).update(withdrawn_at=timezone.now())
        self.assertEqual(self.get(RETRIEVE, self.summary_reader, pk=self.doc1.pk).status_code, 200)  # a withdrawn document stays consultable
        self.assertEqual(self.get(RETRIEVE, self.summary_reader, pk=99999999).status_code, 404)
        stranger = self.grant("wf44c.stranger@links-local.invalid", self.other_project, [VIEW])
        self.assertIn(self.get(RETRIEVE, stranger, pk=self.doc1.pk).status_code, (403, 404))  # known 403/404 oracle: separate security lot
        self.assertIn(self.get(RETRIEVE, None, pk=self.doc1.pk).status_code, (401, 403))

    def test_no_document_right_gets_nothing(self):
        nobody = self.grant("wf44c.nobody@links-local.invalid", self.project, ["documents.update"])
        self.assertIn(self.get(RETRIEVE, nobody, pk=self.doc1.pk).status_code, (403, 404))


class LinkedItemsPreloadTests(NavigationBase):
    def listing(self, user, **params):
        with CaptureQueriesContext(connection) as queries:
            response = self.get(LIST, user, params={"folder__project": self.project.pk, "page_size": 50, **params})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        return rows, len(queries)

    def test_the_opt_out_removes_the_preload_without_changing_rows_or_permissions(self):
        with_items, queries_with = self.listing(self.manager)
        without, queries_without = self.listing(self.manager, include_linked_items="false")
        self.assertEqual([r["id"] for r in with_items], [r["id"] for r in without])
        self.assertTrue(all(r.get("linked_items") is not None for r in with_items))  # default (Company) keeps the preload
        self.assertTrue(all(r.get("linked_items") is None for r in without))
        self.assertEqual([r["cycle_permissions"] for r in with_items], [r["cycle_permissions"] for r in without])
        self.assertEqual([r.get("client_control_status") for r in with_items], [r.get("client_control_status") for r in without])
        self.assertLess(queries_without, queries_with)

    def test_the_preload_cost_grows_per_row_and_the_opt_out_removes_it(self):
        _, with_before = self.listing(self.manager)
        _, without_before = self.listing(self.manager, include_linked_items="false")
        for _ in range(3):
            self.review()
        _, with_after = self.listing(self.manager)
        _, without_after = self.listing(self.manager, include_linked_items="false")
        # four .exists() per row disappear: the extra cost per added row is lower without the preload
        self.assertLess(without_after - without_before, with_after - with_before)

    def test_isolation_between_users_after_the_opt_out(self):
        reader_rows, _ = self.listing(self.summary_reader, include_linked_items="false")
        manager_rows, _ = self.listing(self.manager, include_linked_items="false")
        self.assertEqual({r["id"] for r in reader_rows}, {r["id"] for r in manager_rows})
        stranger = self.grant("wf44c.iso@links-local.invalid", self.other_project, [VIEW])
        response = self.get(LIST, stranger, params={"folder__project": self.project.pk, "include_linked_items": "false"})
        rows = response.data["results"] if response.status_code == 200 and isinstance(response.data, dict) else []
        self.assertFalse(rows)
