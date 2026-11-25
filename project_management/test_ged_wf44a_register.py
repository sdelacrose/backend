"""GED-WF.4.4-A: client status, client RESULT and client DUE DATE of the project register (read only).

Real services (issue, send, return, closure) and the real DocumentViewSet list; nothing is stored for the register.
"""
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import ClientSurveillance, ContactCompany, ContactUser, SupplierSurveillance
from project_management.services.client_surveillance_return import record_client_return
from project_management.services.document_register_status import (
    ClientControlStatus as S, annotate_register_control_status, compute_client_control_status,
)
from project_management.test_client_surveillance_opening import OpeningBase, SMTP_FAIL
from project_management.views.client_surveillance import ClientSurveillanceView
from project_management.views.document import DocumentViewSet
from project_management.models import Document

MANAGE = "documents.client_surveillance_manage"
LIST = DocumentViewSet.as_view({"get": "list"})


_MEDIA = tempfile.mkdtemp(prefix="wf44a-media-")


@override_settings(MEDIA_ROOT=_MEDIA)
class RegisterBase(OpeningBase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        # LOT 1: the register cases drive the HISTORICAL manual closure; the automated path has its own tests.
        patcher = patch("project_management.services.client_surveillance_followup.register_return_safely")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = self.grant("wf44.manager@links-local.invalid", self.project, ["documents.view", MANAGE, "documents.update"])
        self.reader = self.grant("wf44.reader@links-local.invalid", self.project, ["documents.view"])

    # --- helpers ---------------------------------------------------------------------------------
    def status_of(self, document):
        annotated = annotate_register_control_status(Document.objects.filter(pk=document.pk)).get()
        return compute_client_control_status(annotated)

    def register(self, user=None, **params):
        request = APIRequestFactory().get("/x/", {"folder__project": self.project.pk, "page_size": 50, **params})
        force_authenticate(request, user=user or self.reader)
        response = LIST(request)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data

    def rows(self, user=None, **params):
        data = self.register(user, **params)
        return {row["id"]: row for row in (data["results"] if isinstance(data, dict) else data)}

    def sent_review(self, company=None):
        revision = self.review()
        transmittal, event = self.issue_and_send([(revision, "FOR_REVIEW")], **({"company": company} if company else {}))
        surveillance = ClientSurveillance.objects.get(client_issue__document_revision=revision, client_issue__client_company=company or self.client_company)
        return revision, surveillance

    def record(self, surveillance, category="ACCEPTED", comment="", **kw):
        values = dict(category=category, received_at=timezone.now(), source_channel="EMAIL", comment=comment)
        values.update(kw)
        return record_client_return(surveillance.client_issue, self.manager, **values)

    def patch(self, surveillance, body):
        request = APIRequestFactory().patch("/x/", body, format="json")
        force_authenticate(request, user=self.manager)
        return ClientSurveillanceView.as_view()(
            request, document_id=surveillance.client_issue.document_revision.document_id, issue_id=surveillance.client_issue_id)

    def close(self, surveillance, retained):
        self.assertEqual(self.patch(surveillance, {"status": "READY_FOR_CLOSURE"}).status_code, 200)
        response = self.patch(surveillance, {"status": "CLOSED", "retained_return": retained.pk})
        self.assertEqual(response.status_code, 200, response.data)
        surveillance.refresh_from_db()

    def entry(self, document, user=None):
        return self.rows(user)[document.pk]["client_register"]


class StatusMappingTests(RegisterBase):
    def test_draft_transmittal_is_still_to_issue(self):
        revision = self.review()
        transmittal = self.create_transmittal()
        from project_management.services.transmittal_items import add_transmittal_item
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(self.status_of(revision.document), S.TO_ISSUE)

    def test_issued_but_not_sent_then_failed(self):
        revision = self.review()
        transmittal = self.issue([(revision, "FOR_REVIEW")])
        self.assertEqual(self.status_of(revision.document), S.ISSUED_NOT_SENT)
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            from project_management.services.transmittal_send import send_transmittal
            self.assertEqual(send_transmittal(transmittal, self.owner).event_type, "SEND_FAILED")
        self.assertEqual(self.status_of(revision.document), S.SEND_FAILED)
        self.assertEqual(ClientSurveillance.objects.count(), 0)  # nothing is opened by a failed send

    def test_for_information_send_is_issued_and_for_review_send_is_under_review(self):
        info = self.review()
        self.issue_and_send([(info, "FOR_INFORMATION")])
        self.assertEqual(self.status_of(info.document), S.ISSUED)
        revision, surveillance = self.sent_review()
        self.assertEqual(self.status_of(revision.document), S.UNDER_REVIEW)

    def test_a_send_to_the_supplier_issuer_alone_is_not_a_client_send(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED)
        document = revision.document
        document.external_issuer_company = self.client_company  # the recipient IS the issuer: not the client
        document.save(update_fields=["external_issuer_company"])
        self.issue_and_send([(revision, "FOR_REVIEW")])
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        self.assertEqual(self.status_of(document), S.TO_ISSUE)
        self.assertIsNone(self.entry(document))

    def test_ready_for_closure_and_closed(self):
        revision, surveillance = self.sent_review()
        retained = self.record(surveillance, "ACCEPTED_WITH_COMMENTS", "remarks")
        self.assertEqual(self.patch(surveillance, {"status": "READY_FOR_CLOSURE"}).status_code, 200)
        self.assertEqual(self.status_of(revision.document), S.TO_CLOSE)
        self.assertEqual(self.patch(surveillance, {"status": "CLOSED", "retained_return": retained.pk}).status_code, 200)
        self.assertEqual(self.status_of(revision.document), S.CLOSED)

    def test_status_filter_and_row_agree_for_every_state(self):
        docs = {}
        docs[S.TO_ISSUE] = self.review().document
        not_sent = self.review()
        self.issue([(not_sent, "FOR_REVIEW")])
        docs[S.ISSUED_NOT_SENT] = not_sent.document
        info = self.review()
        self.issue_and_send([(info, "FOR_INFORMATION")])
        docs[S.ISSUED] = info.document
        docs[S.UNDER_REVIEW] = self.sent_review()[0].document
        close_rev, close_s = self.sent_review()
        self.close(close_s, self.record(close_s, "ACCEPTED"))
        docs[S.CLOSED] = close_rev.document
        close_rev, ready_s = self.sent_review()
        self.record(ready_s, "REJECTED", "no")
        self.assertEqual(self.patch(ready_s, {"status": "READY_FOR_CLOSURE"}).status_code, 200)
        docs[S.TO_CLOSE] = close_rev.document
        failed = self.review()
        transmittal = self.issue([(failed, "FOR_REVIEW")])
        with patch(SMTP_FAIL, side_effect=RuntimeError("down")):
            from project_management.services.transmittal_send import send_transmittal
            send_transmittal(transmittal, self.owner)
        docs[S.SEND_FAILED] = failed.document
        rows = self.rows()
        for status, document in docs.items():
            self.assertEqual(rows[document.pk]["client_control_status"], status, status)
            filtered = self.rows(client_control_status=status)
            self.assertIn(document.pk, filtered, status)
            self.assertEqual({r["client_control_status"] for r in filtered.values()}, {status}, status)


class ResultTests(RegisterBase):
    def test_each_category_shows_its_frozen_code_only_once_retained(self):
        for category, code in (("ACCEPTED", "A"), ("ACCEPTED_WITH_COMMENTS", "B"), ("COMMENTED", "C"), ("REJECTED", "D")):
            revision, surveillance = self.sent_review()
            self.assertIsNone(self.entry(revision.document)["results"][0]["result"])  # nothing retained while open
            self.close(surveillance, self.record(surveillance, category, "c" if category != "ACCEPTED" else ""))
            result = self.entry(revision.document)["results"][0]["result"]
            self.assertEqual((result["category"], result["code"], result["legacy_reviewed"]), (category, code, False), category)
            self.assertIn(revision.document.pk, self.rows(client_result=category), category)

    def test_a_provisional_return_is_not_a_retained_result(self):
        revision, surveillance = self.sent_review()
        self.record(surveillance, "REJECTED", "not yet")
        entry = self.entry(revision.document)["results"][0]
        self.assertIsNone(entry["result"])
        self.assertEqual(entry["provisional"]["category"], "REJECTED")
        self.assertNotIn(revision.document.pk, self.rows(client_result="REJECTED"))
        self.assertIn(revision.document.pk, self.rows(client_result="NONE"))

    def test_corrected_return_and_retained_result_and_late_return(self):
        revision, surveillance = self.sent_review()
        original = self.record(surveillance, "REJECTED", "typo")
        corrected = self.record(surveillance, "ACCEPTED", supersedes_id=original.pk, correction_reason="wrong category")
        self.assertEqual(self.entry(revision.document)["results"][0]["provisional"]["category"], "ACCEPTED")
        self.close(surveillance, corrected)
        late = self.record(surveillance, "REJECTED", "after closure")
        self.assertGreater(late.recorded_at, surveillance.closed_at)
        entry = self.entry(revision.document)["results"][0]
        self.assertEqual(entry["result"]["category"], "ACCEPTED")  # the retained one, never the latest/late one
        self.assertTrue(entry["late_return"])
        self.assertEqual(self.status_of(revision.document), S.CLOSED)

    def test_legacy_reviewed_is_neutral_never_accepted(self):
        revision, surveillance = self.sent_review()
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(status="CLOSED", result="REVIEWED")
        entry = self.entry(revision.document)["results"][0]
        self.assertEqual(entry["result"], {"category": None, "legacy_reviewed": True})
        self.assertIn(revision.document.pk, self.rows(client_result="LEGACY_REVIEWED"))
        self.assertNotIn(revision.document.pk, self.rows(client_result="ACCEPTED"))
        self.assertNotIn(revision.document.pk, self.rows(client_result="NONE"))

    def test_new_revision_never_inherits_the_previous_result(self):
        revision, surveillance = self.sent_review()
        self.close(surveillance, self.record(surveillance, "ACCEPTED"))
        self.assertEqual(self.entry(revision.document)["results"][0]["result"]["category"], "ACCEPTED")
        self.add_revision(revision.document, content=b"v2")  # a new, not distributed revision
        row = self.rows()[revision.document.pk]
        self.assertIsNone(row["client_register"])
        self.assertIsNone(row["client_control_status"])
        self.assertNotIn(revision.document.pk, self.rows(client_result="ACCEPTED"))

    def test_several_clients_give_one_entry_each_and_never_a_single_picked_result(self):
        other = ContactCompany.objects.create(
            company_profile=self.company, company_name="Second Client", company_role="Client", contact_email="second@x.invalid")
        other_contact = ContactUser.objects.create(
            company_profile=self.company, contact_company=other, name="Sam", surname="Roe", email="sam@second.invalid")
        revision = self.review()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        self.issue_and_send([(revision, "FOR_REVIEW")], company=other, contacts=[other_contact])
        first = ClientSurveillance.objects.get(client_issue__document_revision=revision, client_issue__client_company=self.client_company)
        self.close(first, self.record(first, "ACCEPTED"))
        entry = self.entry(revision.document)
        self.assertEqual(entry["client_count"], 2)
        by_company = {r["client_company_id"]: r for r in entry["results"]}
        self.assertEqual(by_company[self.client_company.id]["result"]["category"], "ACCEPTED")
        self.assertIsNone(by_company[other.id]["result"])
        self.assertIn(revision.document.pk, self.rows(client_result="ACCEPTED"))
        self.assertIn(revision.document.pk, self.rows(client_result="NONE"))  # one client is still undecided
        self.assertEqual(self.status_of(revision.document), S.UNDER_REVIEW)  # the most pending step wins


class DueDateTests(RegisterBase):
    def test_due_at_comes_from_the_surveillance_not_from_the_transmittal_item(self):
        revision, surveillance = self.sent_review()
        item = surveillance.opening_transmittal_item
        past = timezone.now() - timedelta(days=3)
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(due_at=past)
        type(item).objects.filter(pk=item.pk)  # review_due_date is not used: it stays whatever it was
        entry = self.entry(revision.document)
        self.assertEqual(entry["due_at"], past)
        self.assertTrue(entry["overdue"])
        self.assertTrue(entry["due_active"])
        self.assertIn(revision.document.pk, self.rows(client_due="overdue"))
        self.assertNotIn(revision.document.pk, self.rows(client_due="upcoming"))

    def test_future_due_date_is_upcoming_and_a_closed_surveillance_is_never_overdue(self):
        revision, surveillance = self.sent_review()
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(due_at=timezone.now() + timedelta(days=4))
        self.assertIn(revision.document.pk, self.rows(client_due="upcoming"))
        self.assertNotIn(revision.document.pk, self.rows(client_due="overdue"))
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(due_at=timezone.now() - timedelta(days=4))
        self.close(surveillance, self.record(surveillance, "ACCEPTED"))
        entry = self.entry(revision.document)
        self.assertFalse(entry["overdue"])
        self.assertFalse(entry["due_active"])
        self.assertIsNotNone(entry["due_at"])  # still shown, but not as an active delay
        self.assertNotIn(revision.document.pk, self.rows(client_due="overdue"))

    def test_no_surveillance_means_no_due_date_and_the_stored_value_is_untouched(self):
        document = self.review().document
        self.assertIsNone(self.rows()[document.pk]["client_register"])
        revision, surveillance = self.sent_review()
        before = ClientSurveillance.objects.values_list("due_at", "status", "result").get(pk=surveillance.pk)
        self.rows(client_due="overdue", ordering="client_due_at")
        self.assertEqual(ClientSurveillance.objects.values_list("due_at", "status", "result").get(pk=surveillance.pk), before)


class ListBehaviourTests(RegisterBase):
    def test_pagination_filters_sort_and_search_stay_server_side(self):
        docs = []
        for index in range(4):
            revision, surveillance = self.sent_review()
            ClientSurveillance.objects.filter(pk=surveillance.pk).update(due_at=timezone.now() + timedelta(days=10 - index))
            docs.append(revision.document)
        page = self.register(page_size=2, client_control_status="UNDER_REVIEW", ordering="client_due_at")
        self.assertEqual((page["count"], len(page["results"])), (4, 2))
        ordered = [row["id"] for row in page["results"]]
        self.assertEqual(ordered, [docs[3].pk, docs[2].pk])  # soonest due date first
        desc = self.register(page_size=2, client_control_status="UNDER_REVIEW", ordering="-client_due_at")
        self.assertEqual([row["id"] for row in desc["results"]], [docs[0].pk, docs[1].pk])
        second = self.register(page_size=2, page=2, client_control_status="UNDER_REVIEW", ordering="client_due_at")
        self.assertEqual([row["id"] for row in second["results"]], [docs[1].pk, docs[0].pk])
        self.assertEqual(self.register(client_control_status="UNDER_REVIEW", search=docs[1].file_name)["count"], 4)  # same file name
        self.assertEqual(self.register(search="zzz-no-match")["count"], 0)

    def test_sort_by_retained_result(self):
        closed = []
        for category in ("REJECTED", "ACCEPTED", "COMMENTED"):
            revision, surveillance = self.sent_review()
            self.close(surveillance, self.record(surveillance, category, "c"))
            closed.append((category, revision.document.pk))
        page = self.register(client_result="ACCEPTED,COMMENTED,REJECTED", ordering="client_result")
        self.assertEqual([r["id"] for r in page["results"]], [pk for _, pk in sorted(closed)])

    def test_the_register_does_not_add_queries_per_document(self):
        for _ in range(2):
            self.sent_review()
        with CaptureQueriesContext(connection) as small:
            self.register()
        for _ in range(4):
            self.sent_review()
        with CaptureQueriesContext(connection) as large:
            self.register()
        # the client register adds a constant number of queries (the other per-document queries of the list --
        # permissions, linked items -- exist independently of this lot and are not what is checked here)
        mine = lambda captured: [q for q in captured.captured_queries if "clientsurveillance" in q["sql"] or "clientissue" in q["sql"]]
        self.assertEqual(len(mine(small)), len(mine(large)))
        self.assertGreater(len(mine(large)), 0)


class PermissionAndIsolationTests(RegisterBase):
    def test_a_reader_sees_the_summary_and_nothing_of_the_detail(self):
        revision, surveillance = self.sent_review()
        self.close(surveillance, self.record(surveillance, "ACCEPTED_WITH_COMMENTS", "confidential remark"))
        entry = self.entry(revision.document, user=self.reader)
        result = entry["results"][0]
        self.assertEqual(result["result"]["code"], "B")
        self.assertIsNotNone(entry["due_at"])
        flat = repr(entry)
        for forbidden in ("confidential remark", "download", "TR-", "recorded_by", "comment", "transmittal_reference"):
            self.assertNotIn(forbidden, flat, forbidden)

    def test_a_user_without_document_access_in_another_project_gets_nothing(self):
        revision, surveillance = self.sent_review()
        stranger = self.grant("wf44.stranger@links-local.invalid", self.project, ["documents.view"])
        from project_management.models import ProjectTeamMembers
        ProjectTeamMembers.objects.filter(team_member=stranger).delete()
        request = APIRequestFactory().get("/x/", {"folder__project": self.project.pk})
        force_authenticate(request, user=stranger)
        response = LIST(request)
        rows = response.data["results"] if response.status_code == 200 and isinstance(response.data, dict) else []
        self.assertNotIn(revision.document.pk, [r["id"] for r in rows])
