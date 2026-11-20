"""OPTION B1 / LOT 4: applicability per client company, client cycle of the register on the LOGICAL version, explicit new revision after a BPE.

Applicability is read from ClientSurveillanceFollowUp.applicable_at (no stored status): it belongs to one client company and one logical
version, it is never global, and a newer logical version does not inherit it."""
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext

from project_management.models import ClientSurveillance, ClientSurveillanceFollowUp, Document, DocumentRevision, TransmittalItem
from project_management.services import client_surveillance_followup as fu_service
from project_management.services.client_applicability import APPLICABLE, DISTRIBUTED, FOLLOWUP_IN_PROGRESS, IN_CLIENT_REVIEW, client_applicability
from project_management.services.client_register_results import client_register_for_document
from project_management.services.client_surveillance_return import record_client_return
from project_management.services.document_logical_version import logical_info
from project_management.services.document_register_status import annotate_register_control_status, compute_client_control_status
from project_management.services.document_revision import MAJOR, create_document_revision
from project_management.services.document_workflow import get_current_revision
from project_management.services.transmittal_send import send_transmittal
from project_management.test_client_surveillance_followup import SMTP_FAIL
from project_management.test_client_surveillance_return import MANAGE
from project_management.test_document_logical_version import LogicalBase


class AppBase(LogicalBase):
    def current(self, document=None):
        return {e["client_company_id"]: e for e in client_applicability(document or self.document)["current"]}

    def superseded(self, document=None):
        return client_applicability(document or self.document)["superseded"]

    def new_revision(self):
        return create_document_revision(self.document, SimpleUploadedFile("internal-change.txt", b"an explicit internal modification"), self.owner,
                                        MAJOR, "Internal modification after the BPE")

    def client_status(self):
        return compute_client_control_status(Document.objects.get(pk=self.document.pk))


class ApplicabilityTests(AppBase):
    def test_a_prel_to_a_bpe_becomes_applicable_only_after_the_successful_send_and_nothing_is_created(self):  # 1
        followup, bpe = self.journey("ACCEPTED")
        before = self.rows()
        self.assertEqual(self.current()[self.client_company.pk]["state"], FOLLOWUP_IN_PROGRESS)  # the client answered A, the BPE is not sent
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        entry = self.current()[self.client_company.pk]
        self.assertEqual((entry["state"], entry["applicable_at"], entry["derived_revision_id"], entry["category"]), (APPLICABLE, event.created_at, bpe.pk, "ACCEPTED"))
        self.assertEqual(entry["transmittal_reference"], event.transmittal.transmittal_number)
        self.assertEqual(self.rows(), before)  # no new version, no new file
        self.assertEqual(self.numbers(), logical_numbers_of(before))
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", "ACCEPTED"))
        self.assertEqual(self.status(), "APPROVED")  # Approved (internal) and Applicable (client) are two separate facts

    def test_b_prel_to_b_bpe_becomes_applicable_with_the_new_index(self):  # 2, 3
        initial = logical_info(self.rows())[self.revision.pk][1]
        followup, revision = self.journey("ACCEPTED_WITH_COMMENTS")
        waiting = self.current()[self.client_company.pk]  # the new logical version B is not sent yet: nothing is inherited from A
        self.assertEqual((waiting["state"], waiting["applicable_at"], waiting["revision_label"]), (FOLLOWUP_IN_PROGRESS, None, "B"))
        self.send_rev(revision, "FOR_INFORMATION")
        entry = self.current()[self.client_company.pk]
        self.assertEqual((entry["state"], entry["revision_label"], entry["logical_version_number"]), (APPLICABLE, "B", initial + 1))

    def test_c_and_d_start_a_new_prel_cycle_and_are_never_applicable_early(self):  # 4
        for category in ("COMMENTED",):
            followup, revision = self.journey(category)
            waiting = self.current()[self.client_company.pk]  # new PREL version approved but not sent: nothing applicable
            self.assertEqual((waiting["state"], waiting["applicable_at"]), (FOLLOWUP_IN_PROGRESS, None))
            self.send_rev(revision, "FOR_REVIEW")
            entry = self.current()[self.client_company.pk]
            self.assertEqual((entry["state"], entry["applicable_at"]), (IN_CLIENT_REVIEW, None))
            self.assertEqual(self.superseded(), [])  # a C/D never makes anything applicable
            self.assertIsNone(self.closed(followup).applicable_at)

    def test_a_failed_bpe_send_is_not_applicable(self):  # 5
        followup, bpe = self.journey("ACCEPTED")
        issued = self.issue([(bpe, "FOR_INFORMATION")])
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            send_transmittal(issued, self.owner)
        entry = self.current()[self.client_company.pk]
        self.assertEqual((entry["state"], entry["applicable_at"]), (FOLLOWUP_IN_PROGRESS, None))

    def test_a_double_notification_keeps_one_applicability(self):  # 6
        followup, bpe = self.journey("ACCEPTED")
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        first = self.current()[self.client_company.pk]["applicable_at"]
        fu_service.process_send_event(event)
        fu_service.process_send_event(event)
        self.assertEqual(self.current()[self.client_company.pk]["applicable_at"], first)
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(applicable_at__isnull=False).count(), 1)

    def test_one_client_applicable_while_another_is_still_in_review(self):  # 7, 8
        company, contact = self.second_client()
        self.issue_and_send([(self.revision, "FOR_REVIEW")], company=company, contacts=[contact])
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        states = self.current()
        self.assertEqual(states[self.client_company.pk]["state"], APPLICABLE)
        self.assertEqual((states[company.pk]["state"], states[company.pk]["applicable_at"]), (IN_CLIENT_REVIEW, None))  # no global applicability
        self.assertEqual(client_register_for_document(Document.objects.get(pk=self.document.pk))["client_count"], 2)

    def test_a_corrected_return_does_not_stay_applicable_before_the_send(self):  # 9
        record = self.ret("ACCEPTED")
        followup = self.confirm(record)
        fix = record_client_return(self.client_issue, self.manager, category="COMMENTED", received_at=record.received_at, source_channel="EMAIL",
                                   comment="wrong code", supersedes_id=record.pk, correction_reason="wrong code")
        self.assertEqual(self.refreshed(followup).needs_review, fu_service.RECONFIRM)
        entry = self.current()[self.client_company.pk]
        self.assertEqual((entry["state"], entry["applicable_at"]), (FOLLOWUP_IN_PROGRESS, None))
        self.assertTrue(fix.pk)


class ManualRevisionTests(AppBase):
    def test_an_explicit_new_revision_after_an_applicable_bpe_creates_the_next_logical_version_only(self):  # 10, 11, 12, 13
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        before_rows = self.rows()
        before_numbers = logical_info(before_rows)
        base = before_numbers[self.revision.pk][1]
        applicable_at = self.current()[self.client_company.pk]["applicable_at"]
        created = self.new_revision()
        rows = self.rows()
        info = logical_info(rows)
        self.assertEqual(len(rows), len(before_rows) + 1)  # exactly one new physical row, created by the explicit action
        self.assertEqual((info[created.pk][1], info[bpe.pk][1], info[self.revision.pk][1]), (base + 1, base, base))  # no jump, BPE still in V(base)
        self.assertEqual(created.version_number, max(r.version_number for r in before_rows) + 1)  # physical numbering continues
        self.assertEqual([r.version_number for r in before_rows], [r.version_number for r in rows][:len(before_rows)])  # history untouched
        # the old applicability is NOT inherited by the new version, and it stays consultable
        self.assertEqual(self.current(), {})
        old = self.superseded()
        self.assertEqual([(e["client_company_id"], e["applicable_at"], e["logical_version_number"], e["revision_label"]) for e in old],
                         [(self.client_company.pk, applicable_at, base, self.revision.revision_label)])
        listing = {r["id"] for r in self.api_list().data["results"]}
        self.assertTrue({self.revision.pk, bpe.pk, created.pk} <= listing)  # the older version and its files remain listed
        # the new revision has its own workflow; the old ones are unchanged; the document is not approved until it is
        workflows_before = {r.pk: r.workflows.count() for r in before_rows}
        self.assertEqual(created.workflows.count(), 0)  # the diffusion of the BPE and the creation start nothing by themselves
        self.assertNotEqual(self.status(), "APPROVED")  # the new logical version is not approved yet
        self.approve(created)  # the explicit internal validation of the NEW revision only
        self.assertEqual({r.pk: r.workflows.count() for r in before_rows}, workflows_before)
        self.assertEqual(created.workflows.count(), 1)
        self.assertEqual(self.status(), "APPROVED")
        self.assertEqual(get_current_revision(self.document).pk, created.pk)

    def test_the_bpe_send_itself_never_creates_a_revision_or_a_workflow(self):
        followup, bpe = self.journey("ACCEPTED")
        revisions, workflows = DocumentRevision.objects.count(), bpe.workflows.count()
        self.send_rev(bpe, "FOR_INFORMATION")
        self.assertEqual((DocumentRevision.objects.count(), bpe.workflows.count()), (revisions, workflows))


class RegisterClientCycleTests(AppBase):
    def test_the_register_client_status_follows_the_logical_version_not_the_latest_file(self):  # 14
        followup, bpe = self.journey("ACCEPTED")
        # PREL review answered A (ready to close), BPE provided but not yet sent: it used to read "to issue" (the BPE row has no send)
        self.assertEqual(self.client_status(), "TO_CLOSE")
        annotated = annotate_register_control_status(Document.objects.filter(pk=self.document.pk)).get()
        self.assertEqual(compute_client_control_status(annotated), "TO_CLOSE")  # the list path and the detail path agree
        self.send_rev(bpe, "FOR_INFORMATION")
        self.assertEqual(self.client_status(), "CLOSED")
        summary = client_register_for_document(Document.objects.get(pk=self.document.pk))
        self.assertEqual((summary["client_count"], summary["results"][0]["status"], summary["results"][0]["result"]["category"]), (1, "CLOSED", "ACCEPTED"))

    def test_a_new_revision_starts_a_new_client_cycle(self):
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        self.new_revision()
        self.assertNotEqual(self.client_status(), "CLOSED")  # the older version's closed review does not describe the new one
        self.assertIsNone(client_register_for_document(Document.objects.get(pk=self.document.pk)))

    def test_the_register_annotation_stays_a_single_query_and_the_applicability_has_no_n_plus_one(self):  # performance
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        with CaptureQueriesContext(connection) as register:
            list(annotate_register_control_status(Document.objects.filter(pk=self.document.pk)))
        self.assertEqual(len(register), 1)
        with CaptureQueriesContext(connection) as few:
            client_applicability(self.document)
        for index in range(3):
            company, contact = self.second_client()
            self.issue_and_send([(self.revision, "FOR_REVIEW")], company=company, contacts=[contact])
        with CaptureQueriesContext(connection) as many:
            result = client_applicability(self.document)
        self.assertGreaterEqual(len(result["current"]), 2)
        self.assertEqual(len(few), len(many))  # the number of queries does not depend on the number of clients


class SafetyTests(AppBase):
    def test_reading_applicability_and_the_register_writes_nothing(self):  # 15
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        snapshot = (list(DocumentRevision.objects.order_by("pk").values_list("pk", "version_number", "file", "file_sha256", "index_revision_id")),
                    list(TransmittalItem.objects.order_by("pk").values_list("pk", "evidence_snapshot", "document_revision_id")),
                    list(ClientSurveillanceFollowUp.objects.order_by("pk").values_list("pk", "applicable_at", "closed_at")),
                    list(ClientSurveillance.objects.order_by("pk").values_list("pk", "status", "result")))
        self.listing(self.manager)
        client_applicability(self.document)
        self.client_status()
        client_register_for_document(Document.objects.get(pk=self.document.pk))
        again = (list(DocumentRevision.objects.order_by("pk").values_list("pk", "version_number", "file", "file_sha256", "index_revision_id")),
                 list(TransmittalItem.objects.order_by("pk").values_list("pk", "evidence_snapshot", "document_revision_id")),
                 list(ClientSurveillanceFollowUp.objects.order_by("pk").values_list("pk", "applicable_at", "closed_at")),
                 list(ClientSurveillance.objects.order_by("pk").values_list("pk", "status", "result")))
        self.assertEqual(snapshot, again)

    def test_the_endpoint_gives_the_applicability_and_the_transmittal_reference_only_with_the_right(self):  # permissions
        followup, bpe = self.journey("ACCEPTED")
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        full = self.listing(self.manager).data["applicability"]["current"][0]
        self.assertEqual(full["state"], APPLICABLE)  # a manager without the right to open transmittals gets the state, not the reference
        self.assertNotIn("transmittal_reference", full)
        opener = self.grant("lot4.opener@links-local.invalid", self.project, ["documents.view", MANAGE, "documents.transmittal_view"])
        self.assertEqual(self.listing(opener).data["applicability"]["current"][0]["transmittal_reference"], event.transmittal.transmittal_number)
        reader = self.grant("lot4.reader@links-local.invalid", self.project, ["documents.view"])
        plain = self.listing(reader).data["applicability"]["current"][0]
        self.assertEqual(plain["state"], APPLICABLE)
        self.assertNotIn("transmittal_reference", plain)  # the Transmittal reference stays a detail right


def logical_numbers_of(rows):
    info = logical_info(rows)
    return [info[r.pk][1] for r in rows]
