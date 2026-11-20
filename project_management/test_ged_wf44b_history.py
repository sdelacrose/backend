"""GED-WF.4.4-B: document timeline (real event types, SEND events, IN receptions), OUT distribution history (send
state, effective due date) and their visibility. Real services and roles; only the licence check is stubbed."""
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import ClientSurveillance, ClientSurveillanceEvent, TransmittalEvent
from project_management.services.client_distribution_history import list_client_distribution_history
from project_management.services.client_surveillance_return import record_client_return
from project_management.services.document_history_events import list_document_history_events
from project_management.test_client_surveillance_opening import SMTP_FAIL
from project_management.test_transmittal_client_return import ClientInBase, MANAGE, TVIEW, UPDATE, VIEW
from project_management.views.client_surveillance import ClientSurveillanceDueDateView, ClientSurveillanceView
from project_management.views.document_history_events import DocumentHistoryEventsView

ALLOWED_SEND_KEYS = {
    "transmittal_reference", "client_company_id", "client_company_name", "client_issue_id", "client_revision_label",
    "distribution_purpose", "attempt", "uncertain", "transmittal_id",
}
FORBIDDEN_TECHNICAL = ("provider_message_id", "mailbox", "smtp", "subject", "body", "attachments", "recipient", "sha256")


class HistoryBase(ClientInBase):
    def setUp(self):
        super().setUp()
        self.manager = self.grant("wf44b.manager@links-local.invalid", self.project, [VIEW, MANAGE, "documents.update"])
        self.summary_reader = self.grant("wf44b.summary@links-local.invalid", self.project, [VIEW])
        self.transmittal_reader = self.grant("wf44b.tview@links-local.invalid", self.project, [VIEW, TVIEW])
        self.doc1 = self.rev1.document

    def timeline(self, document, user=None):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=user or self.manager)
        response = DocumentHistoryEventsView.as_view()(request, document_id=document.pk)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data["results"]

    def of_type(self, events, *types):
        return [e for e in events if e["event_type"] in types]

    def record(self, issue, category="ACCEPTED", **kw):
        values = dict(category=category, received_at=timezone.now(), source_channel="EMAIL", comment="client remark")
        values.update(kw)
        return record_client_return(issue, self.manager, **values)

    def patch_surveillance(self, issue, body):
        request = APIRequestFactory().patch("/x/", body, format="json")
        force_authenticate(request, user=self.manager)
        return ClientSurveillanceView.as_view()(request, document_id=issue.document_revision.document_id, issue_id=issue.pk)

    def close(self, issue, retained):
        self.assertEqual(self.patch_surveillance(issue, {"status": "READY_FOR_CLOSURE"}).status_code, 200)
        response = self.patch_surveillance(issue, {"status": "CLOSED", "retained_return": retained.pk})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))


class RealEventTypeTests(HistoryBase):
    def setUp(self):
        super().setUp()
        # LOT 1: these cases cover the HISTORICAL manual path (a surveillance with no automated follow-up, e.g. returns
        # recorded before the follow-up existed). The automated path is covered by test_client_surveillance_followup.
        patcher = patch("project_management.services.client_surveillance_followup.register_return_safely")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_surveillance_events_are_titled_by_their_real_type_and_keep_the_status_aside(self):
        new_due = (timezone.now() + timedelta(days=30)).replace(microsecond=0)
        response = self.api(ClientSurveillanceDueDateView, self.manager, self.doc1, self.issue1.pk, "patch",
                            {"due_at": new_due.isoformat(), "reason": "private reason"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        first = self.record(self.issue1, "ACCEPTED_WITH_COMMENTS")
        correction = self.record(self.issue1, "COMMENTED", supersedes_id=first.pk, correction_reason="oops")
        self.close(self.issue1, correction)
        late = self.record(self.issue1, "REJECTED")
        events = self.timeline(self.doc1)
        by_type = {t: self.of_type(events, t) for t in
                   ("OPENED", "DUE_DATE_CHANGED", "RETURN_RECORDED", "RETURN_CORRECTED", "RETURN_LATE", "STATUS")}
        for kind, rows in by_type.items():
            self.assertTrue(rows, kind)
        self.assertEqual(by_type["OPENED"][0]["metadata"]["status"], "UNDER_REVIEW")
        from django.utils.dateparse import parse_datetime
        self.assertEqual(parse_datetime(by_type["DUE_DATE_CHANGED"][0]["metadata"]["due_at"]), new_due)
        self.assertIsNotNone(by_type["DUE_DATE_CHANGED"][0]["metadata"]["previous_due_at"])
        self.assertEqual((by_type["RETURN_RECORDED"][0]["metadata"]["category"], by_type["RETURN_RECORDED"][0]["metadata"]["code"]), ("ACCEPTED_WITH_COMMENTS", "B"))
        self.assertTrue(by_type["RETURN_CORRECTED"][0]["metadata"]["corrects_previous"])
        self.assertTrue(by_type["RETURN_LATE"][0]["metadata"]["late"])
        retained = [e for e in by_type["STATUS"] if "retained_result" in e["metadata"]]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["metadata"]["retained_result"]["category"], "COMMENTED")
        # a status name is never an event type any more
        self.assertFalse(self.of_type(events, "UNDER_REVIEW", "READY_FOR_CLOSURE", "CLOSED", "DRAFT"))
        # the late return never changed the retained result nor the closure
        surveillance = ClientSurveillance.objects.get(client_issue=self.issue1)
        self.assertEqual((surveillance.status, surveillance.result), ("CLOSED", "COMMENTED"))
        self.assertIsNotNone(late)

    def test_the_stored_events_are_not_modified_and_each_operation_appears_once(self):
        record = self.record(self.issue1)
        before = list(ClientSurveillanceEvent.objects.values_list("pk", "event_type", "status"))
        events = self.timeline(self.doc1)
        self.assertEqual(list(ClientSurveillanceEvent.objects.values_list("pk", "event_type", "status")), before)
        ids = [e["id"] for e in events]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(self.of_type(events, "RETURN_RECORDED")), 1)  # the return row is not a second event
        self.assertFalse([e for e in events if e["source_type"] == "client_surveillance_return"])
        self.assertIsNotNone(record)


class SendEventTests(HistoryBase):
    def test_issued_is_not_a_send_and_a_send_adds_a_whitelisted_event_for_this_document_only(self):
        revision = self.review()
        other = self.review()
        transmittal = self.issue([(revision, "FOR_REVIEW"), (other, "FOR_REVIEW")])
        events = self.timeline(revision.document)
        self.assertEqual(len(self.of_type(events, "ISSUED")), 1)
        self.assertFalse(self.of_type(events, "SEND_SUCCEEDED", "SEND_FAILED"))  # ISSUED, never sent
        self.send(transmittal)
        events = self.timeline(revision.document)
        sends = self.of_type(events, "SEND_SUCCEEDED")
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["document_revision_id"], revision.pk)
        self.assertEqual(sends[0]["event_group"], "CLIENT_DISTRIBUTION")
        self.assertLessEqual(set(sends[0]["metadata"]), ALLOWED_SEND_KEYS)
        self.assertEqual(sends[0]["metadata"]["attempt"], 1)
        self.assertEqual(sends[0]["metadata"]["client_company_name"], self.client_company.company_name)
        flat = repr(sends[0]).lower()
        for word in FORBIDDEN_TECHNICAL:
            self.assertNotIn(word, flat, word)
        # the other document gets its own event, never this one
        other_sends = self.of_type(self.timeline(other.document), "SEND_SUCCEEDED")
        self.assertEqual([e["document_revision_id"] for e in other_sends], [other.pk])

    def test_a_failed_send_is_shown_as_failed_never_as_success_and_never_with_the_raw_error(self):
        revision = self.review()
        transmittal = self.issue([(revision, "FOR_REVIEW")])
        with patch(SMTP_FAIL, side_effect=RuntimeError("secret-smtp-host refused")):
            self.assertEqual(send_failed(self, transmittal).event_type, "SEND_FAILED")
        events = self.timeline(revision.document)
        failed = self.of_type(events, "SEND_FAILED")
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["metadata"]["uncertain"])
        self.assertEqual(failed[0]["metadata"]["attempt"], 1)
        self.assertNotIn("secret-smtp-host", repr(events))
        self.assertFalse(self.of_type(events, "SEND_SUCCEEDED"))
        self.assertLessEqual(set(failed[0]["metadata"]), ALLOWED_SEND_KEYS)

    def test_recorded_attempts_are_kept_and_numbered_without_inventing_any(self):
        revision = self.review()
        transmittal = self.issue([(revision, "FOR_REVIEW")])
        first = TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_STARTED", actor=self.owner, metadata={})
        TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_FAILED", actor=self.owner,
                                        metadata={"attempt": first.pk, "error": "boom", "uncertain": False})
        second = TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_STARTED", actor=self.owner, metadata={})
        TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_SUCCEEDED", actor=self.owner,
                                        metadata={"attempt": second.pk, "provider_message_id": "<id@provider>"})
        events = self.timeline(revision.document)
        self.assertEqual([(e["event_type"], e["metadata"]["attempt"]) for e in sorted(self.of_type(events, "SEND_FAILED", "SEND_SUCCEEDED"), key=lambda e: e["source_id"])],
                         [("SEND_FAILED", 1), ("SEND_SUCCEEDED", 2)])
        self.assertFalse(self.of_type(events, "SEND_FAILED")[0]["metadata"]["uncertain"])
        self.assertNotIn("<id@provider>", repr(events))

    def test_a_resend_is_a_second_issued_occurrence_with_its_own_send_events(self):
        revision = self.review()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        self.issue_and_send([(revision, "FOR_REVIEW")])
        events = self.timeline(revision.document)
        self.assertEqual(sorted(e["event_type"] for e in events if e["event_group"] == "CLIENT_DISTRIBUTION"),
                         ["ISSUED", "RETRANSMITTED", "SEND_SUCCEEDED", "SEND_SUCCEEDED"])
        rows = list_client_distribution_history(revision.document)
        self.assertEqual([r["is_resend"] for r in rows], [False, True])


class DistributionHistoryTests(HistoryBase):
    def test_send_state_follows_the_real_attempts(self):
        revision = self.review()
        transmittal = self.issue([(revision, "FOR_REVIEW")])
        row = list_client_distribution_history(revision.document)[0]
        self.assertEqual((row["send_state"], row["sent_at"], row["is_resend"]), ("NOT_SENT", None, False))
        with patch(SMTP_FAIL, side_effect=RuntimeError("down")):
            send_failed(self, transmittal)
        row = list_client_distribution_history(revision.document)[0]
        self.assertEqual((row["send_state"], row["send_uncertain"], row["sent_at"]), ("SEND_FAILED", True, None))
        self.assertIsNotNone(row["send_failed_at"])
        self.assertEqual(row["send_attempts"], 1)
        revision2 = self.review()
        self.issue_and_send([(revision2, "FOR_REVIEW")])
        row2 = list_client_distribution_history(revision2.document)[0]
        self.assertEqual(row2["send_state"], "SEND_ACCEPTED")
        self.assertIsNotNone(row2["sent_at"])

    def test_the_effective_due_date_is_the_surveillance_due_at_only(self):
        row = [r for r in list_client_distribution_history(self.doc1)][0]
        surveillance = ClientSurveillance.objects.get(client_issue=self.issue1)
        self.assertEqual(row["due_at"], surveillance.due_at)
        self.assertTrue(row["due_active"])
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(due_at=timezone.now() - timedelta(hours=1))
        self.assertTrue(list_client_distribution_history(self.doc1)[0]["is_overdue"])
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(status="CLOSED")
        row = list_client_distribution_history(self.doc1)[0]
        self.assertFalse(row["is_overdue"])
        self.assertFalse(row["due_active"])


class IncomingReceptionTests(HistoryBase):
    def test_unqualified_reception_then_late_qualification_keeps_the_business_date(self):
        received = timezone.now()
        draft = self.draft()
        line = self.line(draft, self.issue1, category="REJECTED", received_at=received)
        self.assertFalse(self.of_type(self.timeline(self.doc1), "IN_RECEIVED_TO_QUALIFY", "IN_RECEIVED_QUALIFIED"))  # a draft is no reception
        self.confirm(draft, self.receiver)  # reception right only: the return stays to qualify
        events = self.timeline(self.doc1)
        reception = self.of_type(events, "IN_RECEIVED_TO_QUALIFY")
        self.assertEqual(len(reception), 1)
        self.assertEqual(reception[0]["occurred_at"], received)
        self.assertFalse(reception[0]["metadata"]["qualified"])
        self.assertNotIn("category", reception[0]["metadata"])  # no A/B/C/D before the qualification
        self.assertFalse(self.of_type(events, "RETURN_RECORDED"))
        from project_management.services.transmittal_client_return import qualify_client_return_line
        qualify_client_return_line(line.pk, self.qualifier)
        events = self.timeline(self.doc1)
        qualified = self.of_type(events, "IN_RECEIVED_QUALIFIED")
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["occurred_at"], received)  # never moved by the qualification
        self.assertIsNotNone(qualified[0]["metadata"]["qualified_at"])
        self.assertFalse(self.of_type(events, "IN_RECEIVED_TO_QUALIFY"))
        returned = self.of_type(events, "RETURN_RECORDED")
        self.assertEqual(len(returned), 1)
        self.assertTrue(returned[0]["metadata"]["qualified_later"])
        self.assertTrue(returned[0]["metadata"]["via_incoming_transmittal"])
        self.assertEqual(returned[0]["metadata"]["code"], "D")

    def test_immediate_qualification_and_a_multi_document_in_only_shows_its_own_line(self):
        draft = self.draft()
        self.line(draft, self.issue1, category="ACCEPTED")
        self.line(draft, self.issue2, category="COMMENTED")
        self.confirm(draft, self.controller)
        for revision, code in ((self.rev1, "A"), (self.rev2, "C")):
            events = self.timeline(revision.document)
            self.assertEqual(len(self.of_type(events, "IN_RECEIVED_QUALIFIED")), 1)
            self.assertEqual([e["document_revision_id"] for e in self.of_type(events, "IN_RECEIVED_QUALIFIED")], [revision.pk])
            self.assertEqual(self.of_type(events, "RETURN_RECORDED")[0]["metadata"]["code"], code)
        self.assertFalse(self.of_type(self.timeline(self.rev3.document), "IN_RECEIVED_QUALIFIED"))


class OrderingTests(HistoryBase):
    def test_same_instant_orders_the_accepted_send_before_the_surveillance_opening(self):
        opened = ClientSurveillanceEvent.objects.get(surveillance__client_issue=self.issue1, event_type="OPENED")
        send = TransmittalEvent.objects.get(event_type="SEND_SUCCEEDED", transmittal__items__client_issue=self.issue1)
        stamp = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE " + connection.ops.quote_name(opened._meta.db_table) + " SET created_at=%s WHERE id=%s", [stamp, opened.pk])
            cursor.execute("UPDATE " + connection.ops.quote_name(send._meta.db_table) + " SET created_at=%s WHERE id=%s", [stamp, send.pk])
        events = self.timeline(self.doc1)  # newest first
        position = {e["event_type"]: i for i, e in enumerate(events) if e["event_type"] in ("OPENED", "SEND_SUCCEEDED")}
        self.assertLess(position["OPENED"], position["SEND_SUCCEEDED"])  # chronologically: send, then opening
        again = [e["id"] for e in self.timeline(self.doc1)]
        self.assertEqual(again, [e["id"] for e in events])  # deterministic


class VisibilityTests(HistoryBase):
    def setUp(self):
        super().setUp()
        self.draft_in = self.draft()
        self.line(self.draft_in, self.issue1, category="ACCEPTED")
        self.confirm(self.draft_in, self.receiver)
        self.api(ClientSurveillanceDueDateView, self.manager, self.doc1, self.issue1.pk, "patch",
                 {"due_at": (timezone.now() + timedelta(days=20)).isoformat(), "reason": "confidential reason"})

    def test_a_document_reader_keeps_the_summary_and_never_the_protected_details(self):
        events = self.timeline(self.doc1, self.summary_reader)
        client = [e for e in events if e["event_group"] in ("CLIENT_DISTRIBUTION", "CLIENT_SURVEILLANCE")]
        self.assertTrue(self.of_type(client, "SEND_SUCCEEDED"))  # D2: type, date, company
        self.assertTrue(self.of_type(client, "IN_RECEIVED_TO_QUALIFY"))  # D3
        self.assertTrue(self.of_type(client, "DUE_DATE_CHANGED"))
        for event in client:
            self.assertIsNone(event["actor"], event["event_type"])
            for key in ("transmittal_id", "transmittal_reference", "comment", "reason"):
                self.assertNotIn(key, event["metadata"], (event["event_type"], key))
        self.assertNotIn("confidential reason", repr(events))
        self.assertNotIn("client remark", repr(events))

    def test_detail_readers_get_actors_and_only_the_transmittal_reader_gets_identifiers(self):
        managed = self.timeline(self.doc1, self.manager)  # surveillance right, no transmittal_view
        self.assertTrue(any(e["actor"] for e in managed if e["event_group"] == "CLIENT_SURVEILLANCE"))
        self.assertIn("confidential reason", repr(managed))
        self.assertFalse([e for e in managed if "transmittal_id" in e["metadata"]])
        viewer = self.timeline(self.doc1, self.transmittal_reader)
        with_ids = [e for e in viewer if "transmittal_id" in e["metadata"]]
        self.assertTrue(with_ids)
        self.assertTrue(all(isinstance(e["metadata"]["transmittal_id"], int) for e in with_ids))
        full = self.timeline(self.doc1, self.controller)
        self.assertTrue([e for e in full if "transmittal_id" in e["metadata"]])

    def test_reception_only_user_other_project_and_anonymous_get_nothing(self):
        stranger = self.grant("wf44b.stranger@links-local.invalid", self.other_project, [VIEW, MANAGE, TVIEW])
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=stranger)
        self.assertEqual(DocumentHistoryEventsView.as_view()(request, document_id=self.doc1.pk).status_code, 404)
        anonymous = APIRequestFactory().get("/x/")
        self.assertIn(DocumentHistoryEventsView.as_view()(anonymous, document_id=self.doc1.pk).status_code, (401, 403))
        # reception right alone (no documents.view): not a document reader at all
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=self.blind)
        self.assertIn(DocumentHistoryEventsView.as_view()(request, document_id=self.doc1.pk).status_code, (403, 404))

    def test_the_service_default_never_widens_the_link_rule(self):
        events = list_document_history_events(self.doc1, detail=False)
        self.assertFalse([e for e in events if "transmittal_id" in e["metadata"]])
        events = list_document_history_events(self.doc1, detail=True, transmittal_links=False)
        self.assertFalse([e for e in events if "transmittal_id" in e["metadata"]])


class PerformanceTests(HistoryBase):
    def count(self, document):
        with CaptureQueriesContext(connection) as queries:
            list_document_history_events(document)
        return len(queries)

    def test_the_number_of_queries_does_not_depend_on_the_number_of_events(self):
        baseline = self.count(self.doc1)
        draft = self.draft()
        self.line(draft, self.issue1)
        self.confirm(draft, self.receiver)
        self.record(self.issue1)
        for _ in range(3):
            self.issue_and_send([(self.rev1, "FOR_REVIEW")])
        self.assertLessEqual(self.count(self.doc1), baseline + 3)  # IN lines, their RECEIVED events: constant extras only
        more = self.count(self.doc1)
        for _ in range(3):
            self.issue_and_send([(self.rev1, "FOR_REVIEW")])
        self.assertEqual(self.count(self.doc1), more)


def send_failed(case, transmittal):
    from project_management.services.transmittal_send import send_transmittal
    return send_transmittal(transmittal, case.owner)


class LinkGatingTests(HistoryBase):
    """D5: a Transmittal identifier (hence a link) is only given to who really holds documents.transmittal_view."""

    def rows(self, view, user):
        response = self.api(view, user, self.doc1)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data

    def test_surveillance_listing_grants_identifiers_only_with_transmittal_view(self):
        manager = self.rows(ClientSurveillanceView, self.manager)  # surveillance right only
        self.assertFalse(manager["transmittal_links"])
        self.assertTrue(manager["distributions"])
        self.assertTrue(all(d["transmittal_id"] is None for d in manager["distributions"]))
        self.assertTrue(all(d["transmittal_reference"] for d in manager["distributions"]))  # detail keeps the reference
        viewer = self.rows(ClientSurveillanceView, self.transmittal_reader)
        self.assertTrue(viewer["transmittal_links"])
        self.assertTrue(all(isinstance(d["transmittal_id"], int) for d in viewer["distributions"]))
        summary = self.rows(ClientSurveillanceView, self.summary_reader)
        self.assertFalse(summary["transmittal_links"])
        self.assertTrue(all(d["transmittal_id"] is None and d["transmittal_reference"] is None for d in summary["distributions"]))

    def test_distribution_history_follows_the_same_rule_and_keeps_the_summary_for_a_reader(self):
        from project_management.views.client_distribution_history import ClientDistributionHistoryView
        reader = self.rows(ClientDistributionHistoryView, self.summary_reader)["results"][0]
        self.assertEqual((reader["transmittal_id"], reader["transmittal_reference"]), (None, None))
        self.assertEqual(reader["send_state"], "SEND_ACCEPTED")  # summary: state and date stay visible
        self.assertIsNotNone(reader["sent_at"])
        self.assertIsNotNone(reader["due_at"])
        manager = self.rows(ClientDistributionHistoryView, self.manager)["results"][0]
        self.assertIsNone(manager["transmittal_id"])
        self.assertTrue(manager["transmittal_reference"])
        viewer = self.rows(ClientDistributionHistoryView, self.transmittal_reader)["results"][0]
        self.assertIsInstance(viewer["transmittal_id"], int)
