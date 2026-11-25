"""GED-WF.4.2: automatic opening of client surveillances after a really successful OUT send, effective
due date, dedicated manual-change right and register status on real sends (real project roles, real
send service with the in-memory mail backend -- no permission stub besides the licence check)."""
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import Permission, Role, RolePermission
from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceEvent, ContactCompany, ContactUser, Document,
    DocumentTypeReviewDuration, ProjectTeamMembers, SupplierSurveillance, Transmittal, TransmittalEvent,
    TransmittalItem,
)
from project_management.services.client_surveillance_opening import (
    can_manage_client_surveillance, change_due_date, open_client_surveillances, plan_openings, resolve_duration_days,
)
from project_management.services.document_register_status import (
    ClientControlStatus, annotate_register_control_status, compute_client_control_status,
    filter_by_client_control_status,
)
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_send import send_transmittal
from project_management.test_transmittal import TransmittalTestBase, make_user
from project_management.views.client_surveillance import ClientSurveillanceDueDateView, ClientSurveillanceView

UTC = dt_timezone.utc
SMTP_FAIL = "project_management.services.transmittal_send._deliver_via_provider"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEFAULT_FROM_EMAIL="send@test.invalid")
class OpeningBase(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- fixtures -------------------------------------------------------------------------------
    def issue(self, items, company=None, contacts=None):
        """items: [(revision, purpose)] -> an ISSUED OUT transmittal (not sent)."""
        extra = {"external_company": company.id} if company else {}
        transmittal = self.create_transmittal(**extra)
        for contact in contacts or [self.client_contact]:
            transmittal.external_contacts.add(contact)
        for revision, purpose in items:
            add_transmittal_item(transmittal, revision, purpose, self.owner)
        return finalize_transmittal(transmittal, self.owner)

    def send(self, transmittal):
        event = send_transmittal(transmittal, self.owner)
        self.assertEqual(event.event_type, "SEND_SUCCEEDED")
        return event

    def issue_and_send(self, items, **kw):
        transmittal = self.issue(items, **kw)
        return transmittal, self.send(transmittal)

    def review(self, **kw):
        return self.make_approved_internal_revision(**kw)

    def set_type(self, revision, document_type="PLAN"):
        revision.document.document_type = document_type
        revision.document.save(update_fields=["document_type"])

    def surveillances(self):
        return ClientSurveillance.objects.order_by("id")

    def grant(self, email, project, keys, company=None, user=None):
        user = user or make_user(email)
        user.company = company or self.company
        user.save(update_fields=["company"])
        role = Role.objects.create(
            name=f"WF42 role {user.id}-{project.pk}", company=user.company, type="solution", scope="project",
            is_default=False, is_active=True, created_by=user,
        )
        for key in keys:
            perm, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": key.split(".")[0], "description": "test"})
            RolePermission.objects.get_or_create(role=role, permission=perm)
        ProjectTeamMembers.objects.update_or_create(
            project=project, team_member=user, defaults={"role": "Member", "project_role": role, "project_access": "RW"})
        return user

    def api(self, view, user, document, issue_id=None, method="get", body=None):
        factory = APIRequestFactory()
        url = "/x/"
        request = getattr(factory, method)(url, body, format="json") if body is not None else getattr(factory, method)(url)
        force_authenticate(request, user=user)
        kwargs = {"document_id": document.pk}
        if issue_id is not None:
            kwargs["issue_id"] = issue_id
        return view.as_view()(request, **kwargs)


class OpeningAfterSendTests(OpeningBase):
    def test_for_review_send_opens_one_surveillance_under_review_with_exact_links_and_due_at(self):
        revision = self.review()
        fixed = datetime(2026, 11, 15, 14, 30, tzinfo=UTC)
        with patch("django.utils.timezone.now", return_value=fixed):
            transmittal, event = self.issue_and_send([(revision, "FOR_REVIEW")])
        item = transmittal.items.get()
        surveillance = self.surveillances().get()
        self.assertEqual(surveillance.status, ClientSurveillance.Status.UNDER_REVIEW)
        self.assertEqual(surveillance.client_issue_id, item.client_issue_id)
        self.assertEqual(surveillance.client_issue.client_company_id, self.client_company.id)
        self.assertEqual(surveillance.client_issue.document_revision_id, revision.id)
        self.assertEqual(surveillance.opening_transmittal_item_id, item.pk)
        self.assertEqual(surveillance.opening_send_event_id, event.pk)
        self.assertEqual(surveillance.created_by_id, self.owner.id)
        self.assertEqual(surveillance.opened_at, event.created_at)
        self.assertEqual(surveillance.review_duration_days, 15)
        # exact instant: 15 November 14:30 UTC + 15 calendar days = 30 November 14:30 UTC
        self.assertEqual(surveillance.due_at, datetime(2026, 11, 30, 14, 30, tzinfo=UTC))
        opened = surveillance.events.get()
        self.assertEqual((opened.event_type, opened.status, opened.actor_id), ("OPENED", "UNDER_REVIEW", self.owner.id))
        self.assertEqual(opened.data["send_event"], event.pk)
        self.assertEqual(opened.data["transmittal"], transmittal.pk)

    def test_item_historic_due_date_and_duration_are_not_touched(self):
        revision = self.review()
        transmittal, event = self.issue_and_send([(revision, "FOR_REVIEW")])
        item = TransmittalItem.objects.get(transmittal=transmittal)
        self.assertEqual(item.review_due_date, transmittal.issued_at.date() + timedelta(days=15))
        self.assertEqual(item.review_duration_days, 15)

    def test_for_information_creates_nothing_and_does_not_touch_an_existing_surveillance(self):
        revision = self.review()
        self.issue_and_send([(revision, "FOR_INFORMATION")])
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        self.assertEqual(ClientIssue.objects.count(), 1)  # the ClientIssue is still created at issuing
        other = self.review()
        self.issue_and_send([(other, "FOR_REVIEW")])
        surveillance = self.surveillances().get()
        before = (surveillance.due_at, surveillance.events.count())
        self.issue_and_send([(other, "FOR_INFORMATION")])
        surveillance.refresh_from_db()
        self.assertEqual((surveillance.due_at, surveillance.events.count()), before)

    def test_mixed_transmittal_opens_only_for_the_review_item(self):
        review, info = self.review(), self.review()
        transmittal, _ = self.issue_and_send([(review, "FOR_REVIEW"), (info, "FOR_INFORMATION")])
        self.assertEqual(self.surveillances().count(), 1)
        self.assertEqual(self.surveillances().get().client_issue.document_revision_id, review.id)

    def test_issued_but_not_sent_opens_nothing(self):
        self.issue([(self.review(), "FOR_REVIEW")])
        self.assertEqual(ClientSurveillance.objects.count(), 0)

    def test_draft_and_started_only_open_nothing(self):
        transmittal = self.create_transmittal()
        add_transmittal_item(transmittal, self.review(), "FOR_REVIEW", self.owner)
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        issued = self.issue([(self.review(), "FOR_REVIEW")])
        started = TransmittalEvent.objects.create(transmittal=issued, event_type="SEND_STARTED", actor=self.owner)
        self.assertEqual(open_client_surveillances(started), [])
        self.assertEqual(ClientSurveillance.objects.count(), 0)

    def test_smtp_failure_and_uncertain_failure_open_nothing(self):
        transmittal = self.issue([(self.review(), "FOR_REVIEW")])
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            event = send_transmittal(transmittal, self.owner)
        self.assertEqual(event.event_type, "SEND_FAILED")
        self.assertTrue(event.metadata["uncertain"])  # SMTP failures are always uncertain
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        self.assertEqual(open_client_surveillances(event), [])

    def test_clear_graph_failure_opens_nothing_then_the_retry_opens_exactly_one(self):
        transmittal = self.issue([(self.review(), "FOR_REVIEW")])
        mailbox = type("Mailbox", (), {"email_address": "sender@test.invalid"})()

        def run(deliver):
            with patch("project_management.services.transmittal_send.MailboxConnection.objects.filter") as query, \
                    patch("project_management.services.transmittal_send.validate_mailbox_for_send"), \
                    patch("project_management.services.transmittal_send._resolve_mail_sender_user", return_value=self.owner), \
                    patch("project_management.services.transmittal_send._deliver_via_graph", **deliver):
                query.return_value.first.return_value = mailbox
                return send_transmittal(transmittal, self.owner, mailbox_id=999)

        failed = run({"side_effect": Exception("graph 400")})
        self.assertEqual((failed.event_type, failed.metadata["uncertain"]), ("SEND_FAILED", False))
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        succeeded = run({"return_value": "accepted"})
        self.assertEqual(succeeded.event_type, "SEND_SUCCEEDED")
        surveillance = self.surveillances().get()
        self.assertEqual(surveillance.opening_send_event_id, succeeded.pk)
        self.assertEqual(surveillance.events.count(), 1)

    def test_supplier_document_for_review_opens_a_client_surveillance_without_touching_the_supplier_flow(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED)
        revisions_before = revision.document.revisions.count()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        self.assertEqual(self.surveillances().get().client_issue.document_revision_id, revision.id)
        supplier = SupplierSurveillance.objects.get(document_revision=revision)
        self.assertEqual((supplier.status, supplier.result), ("CLOSED", "REVIEWED"))
        self.assertEqual(revision.document.revisions.count(), revisions_before)  # no revision created automatically

    def test_recipient_that_is_not_the_client_never_opens_one(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED)
        document = revision.document
        document.external_issuer_company = self.client_company  # the exact company that issued it
        document.save(update_fields=["external_issuer_company"])
        _, event = self.issue_and_send([(revision, "FOR_REVIEW")])
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        self.assertEqual(plan_openings(event)[0]["anomalies"], ["recipient_is_not_the_client"])


class IdentityTests(OpeningBase):
    def test_replay_of_the_same_send_event_never_duplicates(self):
        _, event = self.issue_and_send([(self.review(), "FOR_REVIEW")])
        self.assertEqual(open_client_surveillances(event), [])
        self.assertEqual(open_client_surveillances(event), [])
        self.assertEqual(ClientSurveillance.objects.count(), 1)
        self.assertEqual(ClientSurveillanceEvent.objects.count(), 1)

    def test_database_refuses_a_second_surveillance_for_the_same_send_event(self):
        _, event = self.issue_and_send([(self.review(), "FOR_REVIEW")])
        other_issue = ClientIssue.objects.create(
            document_revision=self.review(), client_company=self.client_company, created_by=self.owner)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientSurveillance.objects.create(client_issue=other_issue, opening_send_event=event)

    def test_resend_reuses_the_surveillance_adds_an_event_and_keeps_the_manually_changed_due_date(self):
        revision = self.review()
        first, _ = self.issue_and_send([(revision, "FOR_REVIEW")])
        surveillance = self.surveillances().get()
        manual = datetime(2027, 1, 10, 9, 0, tzinfo=UTC)
        change_due_date(surveillance, manual, "client asked for more time", self.owner)
        second, second_event = self.issue_and_send([(revision, "FOR_REVIEW")])
        self.assertEqual(self.surveillances().count(), 1)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.due_at, manual)
        self.assertEqual(surveillance.opening_send_event_id, TransmittalEvent.objects.filter(
            transmittal=first, event_type="SEND_SUCCEEDED").get().pk)
        types = list(surveillance.events.values_list("event_type", flat=True))
        self.assertEqual(types, ["OPENED", "DUE_DATE_CHANGED", "RESENT"])
        resent = surveillance.events.get(event_type="RESENT")
        self.assertEqual((resent.data["send_event"], resent.data["transmittal"]), (second_event.pk, second.pk))
        self.assertEqual(surveillance.status, "UNDER_REVIEW")
        self.assertEqual(open_client_surveillances(second_event), [])  # the resend itself is idempotent too

    def test_closed_surveillance_is_never_reopened_and_the_case_is_flagged(self):
        revision = self.review()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        surveillance = self.surveillances().get()
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(status="CLOSED", result="REVIEWED")
        self.issue_and_send([(revision, "FOR_REVIEW")])
        surveillance.refresh_from_db()
        self.assertEqual((surveillance.status, surveillance.result), ("CLOSED", "REVIEWED"))
        resent = surveillance.events.get(event_type="RESENT")
        self.assertTrue(resent.data["closed"] and resent.data["needs_business_review"])

    def test_new_revision_gets_its_own_surveillance_and_due_date(self):
        revision = self.review()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        new_revision = self.approve(self.add_revision(revision.document, b"v2"))
        self.issue_and_send([(new_revision, "FOR_REVIEW")])
        rows = list(self.surveillances())
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].client_issue_id, rows[1].client_issue_id)
        self.assertGreater(rows[1].due_at, rows[0].due_at)

    def test_one_surveillance_per_client_company(self):
        revision = self.review()
        other = ContactCompany.objects.create(
            company_profile=self.company, company_name="Second Client", company_role="Client", contact_email="b@x.invalid")
        other_contact = ContactUser.objects.create(
            company_profile=self.company, contact_company=other, name="Bob", surname="Roe", email="bob@second.invalid")
        self.issue_and_send([(revision, "FOR_REVIEW")])
        self.issue_and_send([(revision, "FOR_REVIEW")], company=other, contacts=[other_contact])
        self.assertEqual(self.surveillances().count(), 2)
        self.assertEqual({s.client_issue.client_company_id for s in self.surveillances()}, {self.client_company.id, other.id})

    def test_several_contacts_of_one_company_give_one_surveillance_and_keep_recipients_in_the_send_evidence(self):
        second = ContactUser.objects.create(
            company_profile=self.company, contact_company=self.client_company, name="Al", surname="Poe", email="al@acme.invalid")
        transmittal, event = self.issue_and_send([(self.review(), "FOR_REVIEW")], contacts=[self.client_contact, second])
        self.assertEqual(self.surveillances().count(), 1)
        started = transmittal.events.get(event_type="SEND_STARTED")
        self.assertEqual(sorted(started.metadata["recipient_contacts"]), sorted([self.client_contact.pk, second.pk]))
        self.assertEqual(len(mail.outbox), 1)

    def test_historic_surveillance_created_by_hand_is_adopted_by_the_first_send(self):
        revision = self.review()
        transmittal = self.issue([(revision, "FOR_REVIEW")])
        issue = transmittal.items.get().client_issue
        manual = ClientSurveillance.objects.create(client_issue=issue, created_by=self.owner)
        ClientSurveillanceEvent.objects.create(surveillance=manual, actor=self.owner, status="DRAFT")
        event = self.send(transmittal)
        manual.refresh_from_db()
        self.assertEqual((self.surveillances().count(), manual.status, manual.opening_send_event_id), (1, "UNDER_REVIEW", event.pk))
        self.assertEqual(list(manual.events.values_list("event_type", flat=True)), ["STATUS", "OPENED"])  # history kept


class DurationAndFailureTests(OpeningBase):
    def test_frozen_item_duration_wins_over_a_later_configuration_change(self):
        revision = self.review()
        self.set_type(revision)
        DocumentTypeReviewDuration.objects.create(project=self.project, document_type="PLAN", client_review_duration_days=7)
        transmittal, event = self.issue_and_send([(revision, "FOR_REVIEW")])
        DocumentTypeReviewDuration.objects.filter(project=self.project).update(client_review_duration_days=40)
        surveillance = self.surveillances().get()
        self.assertEqual(surveillance.review_duration_days, 7)
        self.assertEqual(surveillance.due_at, event.created_at + timedelta(days=7))

    def test_fallback_to_the_effective_configuration_when_the_item_has_no_frozen_duration(self):
        revision = self.review()
        self.set_type(revision)
        DocumentTypeReviewDuration.objects.create(project=self.project, document_type="PLAN", client_review_duration_days=9)
        transmittal = self.issue([(revision, "FOR_REVIEW")])
        item = transmittal.items.get()
        item.review_duration_days = None  # an older item without a frozen duration (never saved)
        self.assertEqual(resolve_duration_days(item), (9, None))

    def test_absent_duration_leaves_due_at_empty_and_logs_the_anomaly(self):
        with patch("project_management.services.client_surveillance_opening.resolve_duration_days",
                   return_value=(None, "duration_unresolved")):
            _, event = self.issue_and_send([(self.review(), "FOR_REVIEW")])
        surveillance = self.surveillances().get()
        self.assertIsNone(surveillance.due_at)
        self.assertEqual(surveillance.events.get().data["anomaly"], "duration_unresolved")

    def test_a_failure_while_opening_never_undoes_or_repeats_the_send_and_reconciliation_fixes_it(self):
        transmittal = self.issue([(self.review(), "FOR_REVIEW")])
        with patch("project_management.services.client_surveillance_opening.open_client_surveillances",
                   side_effect=RuntimeError("database hiccup")):
            event = send_transmittal(transmittal, self.owner)
        self.assertEqual(event.event_type, "SEND_SUCCEEDED")
        self.assertTrue(TransmittalEvent.objects.filter(pk=event.pk).exists())
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual([r["action"] for r in plan_openings(event)], ["OPEN"])
        self.assertEqual(open_client_surveillances(event)[0][0], "OPEN")
        self.assertEqual(open_client_surveillances(event), [])
        self.assertEqual(len(mail.outbox), 1)  # never re-sent to fix an opening error
        with self.assertRaises(ValidationError):
            send_transmittal(transmittal, self.owner)  # a sent transmittal cannot be sent twice

    def test_simulation_command_lists_without_writing_and_refuses_apply(self):
        transmittal = self.issue([(self.review(), "FOR_REVIEW"), (self.review(), "FOR_INFORMATION")])
        with patch("project_management.services.client_surveillance_opening.open_client_surveillances",
                   side_effect=RuntimeError("down")):
            self.send(transmittal)
        self.issue([(self.review(), "FOR_REVIEW")])  # issued only: must never appear
        out = StringIO()
        call_command("reconcile_client_surveillance", stdout=out)
        text = out.getvalue()
        self.assertIn("OPEN", text)
        self.assertIn("SIMULATION ONLY", text)
        self.assertEqual(text.count("FOR_REVIEW | SEND_SUCCEEDED"), 1)
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        self.assertEqual(ClientSurveillanceEvent.objects.count(), 0)
        with self.assertRaises(CommandError):
            call_command("reconcile_client_surveillance", "--apply", stdout=StringIO())
        self.assertEqual(ClientSurveillance.objects.count(), 0)


class ManualDueDateTests(OpeningBase):
    def setUp(self):
        super().setUp()
        self.revision = self.review()
        self.transmittal, self.event = self.issue_and_send([(self.revision, "FOR_REVIEW")])
        self.document = self.revision.document
        self.surveillance = self.surveillances().get()
        self.issue_id = self.surveillance.client_issue_id
        self.new_due = "2027-02-01T10:30:00+01:00"

    def patch_due(self, user, body=None):
        return self.api(ClientSurveillanceDueDateView, user, self.document, self.issue_id, "patch",
                        body or {"due_at": self.new_due, "reason": "client request"})

    def test_authorised_change_is_traced_in_an_immutable_event_and_keeps_the_utc_instant(self):
        user = self.grant("wf42.manager@links-local.invalid", self.project, ["documents.view", "documents.client_surveillance_manage"])
        old = self.surveillance.due_at
        response = self.patch_due(user)
        self.assertEqual(response.status_code, 200, response.data)
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.due_at, datetime(2027, 2, 1, 9, 30, tzinfo=UTC))
        event = self.surveillance.events.get(event_type="DUE_DATE_CHANGED")
        self.assertEqual(event.actor_id, user.id)
        self.assertEqual(event.data, {"old": old.isoformat(), "new": "2027-02-01T09:30:00+00:00", "reason": "client request"})
        self.assertIsNotNone(event.created_at)
        with self.assertRaises(ValidationError):
            event.comment = "tampered"
            event.save()
        with self.assertRaises(ValidationError):
            ClientSurveillanceEvent.objects.filter(pk=event.pk).update(data={})
        with self.assertRaises(ValidationError):
            event.delete()

    def test_timezone_conversion_never_changes_the_stored_instant(self):
        paris = datetime(2027, 3, 1, 12, 0, tzinfo=ZoneInfo("Europe/Paris"))
        change_due_date(self.surveillance, paris, "", self.owner)
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.due_at, datetime(2027, 3, 1, 11, 0, tzinfo=UTC))
        with self.assertRaises(ValidationError):
            change_due_date(self.surveillance, datetime(2027, 3, 2, 12, 0), "", self.owner)  # naive
        with self.assertRaises(ValidationError):
            change_due_date(self.surveillance, paris, "", self.owner)  # unchanged

    def test_refused_without_the_dedicated_right(self):
        cases = {
            "reader": ["documents.view"],
            "update_only": ["documents.view", "documents.update"],
            "issue_only": ["documents.view", "documents.transmittal_issue", "documents.transmittal_update"],
        }
        for name, keys in cases.items():
            user = self.grant(f"wf42.{name}@links-local.invalid", self.project, keys)
            self.assertEqual(self.patch_due(user).status_code, 403, name)
        self.assertEqual(self.surveillance.events.filter(event_type="DUE_DATE_CHANGED").count(), 0)

    def test_right_in_another_project_or_company_does_not_apply(self):
        user = self.grant("wf42.elsewhere@links-local.invalid", self.project, ["documents.view"])
        self.grant("wf42.elsewhere@links-local.invalid", self.other_project, ["documents.client_surveillance_manage"], user=user)
        self.assertEqual(self.patch_due(user).status_code, 403)
        outsider = self.grant("wf42.outsider@links-local.invalid", self.other_tenant_project,
                              ["documents.view", "documents.client_surveillance_manage"], company=self.other_company)
        self.assertEqual(self.patch_due(outsider).status_code, 404)  # the document is invisible to another company

    def test_full_access_is_honoured_and_a_closed_surveillance_cannot_be_changed(self):
        user = self.grant("wf42.full@links-local.invalid", self.project, ["documents.full_access"])
        self.assertEqual(self.patch_due(user).status_code, 200)
        ClientSurveillance.objects.filter(pk=self.surveillance.pk).update(status="CLOSED", result="REVIEWED")
        response = self.patch_due(user, {"due_at": "2027-05-01T10:00:00Z"})
        self.assertEqual(response.status_code, 400)

    def test_create_and_patch_endpoints_still_work_and_the_listing_exposes_the_new_data(self):
        manager = self.grant("wf42.m2@links-local.invalid", self.project, ["documents.view", "documents.client_surveillance_manage"])
        reader = self.grant("wf42.r2@links-local.invalid", self.project, ["documents.view"])
        row = self.api(ClientSurveillanceView, manager, self.document).data["results"][0]
        surveillance = row["surveillance"]
        self.assertEqual((surveillance["status"], row["can_manage"]), ("UNDER_REVIEW", True))
        self.assertEqual(surveillance["opening_transmittal"], {"id": self.transmittal.pk, "reference": self.transmittal.transmittal_number})
        self.assertEqual(surveillance["opening_send_event_id"], self.event.pk)
        self.assertEqual(surveillance["review_duration_days"], 15)
        self.assertEqual(surveillance["due_at"], self.surveillance.due_at)
        self.assertEqual(surveillance["events"][0]["event_type"], "OPENED")
        self.assertFalse(self.api(ClientSurveillanceView, reader, self.document).data["results"][0]["can_manage"])
        # manual POST stays idempotent: it returns the automatically opened surveillance
        poster = self.grant("wf42.upd@links-local.invalid", self.project, ["documents.view", "documents.update"])
        response = self.api(ClientSurveillanceView, poster, self.document, self.issue_id, "post")
        self.assertEqual((response.status_code, response.data["id"]), (200, self.surveillance.pk))
        # the existing one-step status flow keeps working from UNDER_REVIEW
        response = self.api(ClientSurveillanceView, poster, self.document, self.issue_id, "patch", {"status": "READY_FOR_CLOSURE"})
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_right_is_in_the_catalogue_but_granted_to_nobody_by_default(self):
        self.assertTrue(Permission.objects.filter(key="documents.client_surveillance_manage", module="documents").exists())
        self.assertEqual(RolePermission.objects.filter(
            permission__key="documents.client_surveillance_manage", role__is_default=True).count(), 0)
        legacy = ClientSurveillanceEvent.objects.create(surveillance=self.surveillance, actor=self.owner, status="UNDER_REVIEW")
        self.assertEqual(legacy.event_type, "STATUS")  # the default keeps every older event valid


class RegisterStatusTests(OpeningBase):
    def status(self, revision):
        return compute_client_control_status(Document.objects.get(pk=revision.document_id))

    def test_register_follows_real_sends_not_the_issued_status(self):
        revision = self.review()
        self.assertEqual(self.status(revision), ClientControlStatus.TO_ISSUE)  # nothing yet
        transmittal = self.create_transmittal()
        transmittal.external_contacts.add(self.client_contact)
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(self.status(revision), ClientControlStatus.TO_ISSUE)  # DRAFT
        finalize_transmittal(transmittal, self.owner)
        self.assertEqual(self.status(revision), ClientControlStatus.ISSUED_NOT_SENT)  # ISSUED without SEND_SUCCEEDED (WF.4.4-A: own state)
        transmittal = Transmittal.objects.get(pk=transmittal.pk)
        with patch(SMTP_FAIL, side_effect=RuntimeError("down")):
            send_transmittal(transmittal, self.owner)
        self.assertEqual(self.status(revision), ClientControlStatus.SEND_FAILED)  # SEND_FAILED (WF.4.4-A: own state)
        TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_SUCCEEDED", actor=self.owner,
                                        metadata={"attempt": 0})
        self.assertEqual(self.status(revision), ClientControlStatus.UNDER_REVIEW)  # sent FOR_REVIEW, legacy: no row

    def test_sent_for_review_is_under_review_then_issued_once_the_surveillance_is_closed(self):
        revision = self.review()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        self.assertEqual(self.status(revision), ClientControlStatus.UNDER_REVIEW)
        ClientSurveillance.objects.update(status="CLOSED", result="REVIEWED")
        self.assertEqual(self.status(revision), ClientControlStatus.CLOSED)  # WF.4.4-A: closed is its own state (the result is a separate column)

    def test_sent_for_information_is_issued_not_under_review(self):
        revision = self.review()
        self.issue_and_send([(revision, "FOR_INFORMATION")])
        self.assertEqual(self.status(revision), ClientControlStatus.ISSUED)

    def test_a_new_undistributed_revision_does_not_inherit_the_previous_one(self):
        revision = self.review()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        new_revision = self.approve(self.add_revision(revision.document, b"v2"))
        self.assertEqual(self.status(new_revision), ClientControlStatus.TO_ISSUE)

    def test_list_filter_matches_the_computed_status_and_consulting_creates_nothing(self):
        sent, unsent = self.review(), self.review()
        self.issue_and_send([(sent, "FOR_REVIEW")])
        self.issue([(unsent, "FOR_REVIEW")])
        count = ClientSurveillance.objects.count()
        queryset = annotate_register_control_status(Document.objects.filter(pk__in=[sent.document_id, unsent.document_id]))
        under = filter_by_client_control_status(queryset, ClientControlStatus.UNDER_REVIEW)
        to_issue = filter_by_client_control_status(
            annotate_register_control_status(Document.objects.filter(pk__in=[sent.document_id, unsent.document_id])),
            ClientControlStatus.TO_ISSUE)
        self.assertEqual(list(under.values_list("pk", flat=True)), [sent.document_id])
        self.assertEqual(list(to_issue.values_list("pk", flat=True)), [])  # WF.4.4-A: issued but unsent is ISSUED_NOT_SENT
        not_sent = filter_by_client_control_status(
            annotate_register_control_status(Document.objects.filter(pk__in=[sent.document_id, unsent.document_id])),
            ClientControlStatus.ISSUED_NOT_SENT)
        self.assertEqual(list(not_sent.values_list("pk", flat=True)), [unsent.document_id])
        self.assertEqual(ClientSurveillance.objects.count(), count)
