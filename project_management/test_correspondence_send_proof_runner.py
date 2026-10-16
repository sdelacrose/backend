# MAIL-1.4-C: running the archiving of send proofs by itself -- the trigger after commit, the retry schedule,
# the periodic sweep, who reads the mailbox, the manual retry and the (simulated) backfill of older sends.
# Microsoft Graph is always a scripted reader, files go to a temporary folder, and nothing here can reach
# sendMail (requests.post raises during every run). Every address, subject and id is fictitious.
import io
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from models.models import User
from project_management.models import (
    Correspondence,
    CorrespondenceSendEvent,
    CorrespondenceSendProof,
    CorrespondenceSendProofFile,
)
from project_management.services.correspondence_send_proof_archive import ADVISORY_LOCK_NAMESPACE
from project_management.services.correspondence_send_proof_runner import (
    AUTO_RETRY_FAILURES,
    PERMANENT_FAILURES,
    RETRY_DELAYS,
    SWEEP_LOCK_NAMESPACE,
    SendProofRetryRefused,
    due_proofs,
    resolve_archive_actor,
    retry_delay,
    retry_send_proof_archive,
    run_archive,
    sweep_send_proofs,
)
from project_management.test_correspondence_send_proof_archive import ArchiveBase
from userapis.mailbox_read import MailboxReadError
from userapis.models import MailboxConnection, MailboxUserGrant
from userapis.test_mailbox import _make_company, _make_user

Result = CorrespondenceSendEvent.Result
State = CorrespondenceSendProof.State


class RunnerBase(ArchiveBase):
    def setUp(self):
        super().setUp()
        self.t0 = timezone.now()
        override = override_settings(
            MAIL_SEND_PROOF_ARCHIVING_ENABLED=True,
            MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE=self.t0 - timedelta(days=1),
        )
        override.enable()
        self.addCleanup(override.disable)
        send_right = patch("project_management.services.correspondence_send_proof_runner._may_send", return_value=True)
        self.may_send = send_right.start()
        self.addCleanup(send_right.stop)

    def proof_of(self, event):
        return CorrespondenceSendProof.objects.get(event=event)

    def fresh(self, proof):
        proof.refresh_from_db()
        return proof

    def run_at(self, proof, minutes=0, **kwargs):
        with patch("requests.post", side_effect=AssertionError("sendMail must never be called")):
            return run_archive(proof.pk, now=self.t0 + timedelta(minutes=minutes), reader=self.reader, **kwargs)

    def sweep_at(self, minutes=0, **kwargs):
        with patch("requests.post", side_effect=AssertionError("sendMail must never be called")):
            return sweep_send_proofs(now=self.t0 + timedelta(minutes=minutes), reader=self.reader, **kwargs)


class TriggerTests(RunnerBase):
    def test_the_first_attempt_is_queued_only_after_the_send_is_committed(self):
        corr = self.out_with()
        with patch("project_management.tasks.archive_send_proof_task.apply_async") as queued:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                event, _, _ = self.send(corr)
            self.assertEqual(queued.call_count, 0)                    # nothing before the commit
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()                                            # the commit happens
        proof = self.proof_of(event)
        queued.assert_called_once_with(args=[proof.pk], retry=False)
        self.assertEqual((proof.state, proof.attempts), (State.PENDING, 0))   # queuing archived nothing

    def test_nothing_is_queued_while_the_archiving_is_switched_off(self):
        corr = self.out_with()
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            with patch("project_management.tasks.archive_send_proof_task.apply_async") as queued:
                with self.captureOnCommitCallbacks(execute=True):
                    self.send(corr)
        self.assertEqual(queued.call_count, 0)

    def test_a_broker_that_cannot_be_reached_never_fails_the_send_and_leaves_the_proof_pending(self):
        corr = self.out_with()
        with patch("project_management.tasks.archive_send_proof_task.apply_async", side_effect=ConnectionError("broker down")):
            with self.captureOnCommitCallbacks(execute=True):
                event, _, _ = self.send(corr)
        self.assertEqual(Correspondence.objects.get(pk=corr.pk).status, Correspondence.Status.ISSUED)
        proof = self.proof_of(event)
        self.assertEqual((proof.state, proof.next_attempt_at), (State.PENDING, None))
        self.assertIn(proof.pk, list(due_proofs(self.t0 + timedelta(minutes=1)).values_list("pk", flat=True)))

    def test_the_tasks_do_nothing_unless_switched_on_and_never_for_older_sends(self):
        from project_management.tasks import archive_send_proof_task, sweep_send_proofs_task

        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            self.assertEqual(archive_send_proof_task(proof.pk)["outcome"], "skipped")
            self.assertEqual(sweep_send_proofs_task()["skipped"], "disabled")
        with override_settings(MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE=timezone.now() + timedelta(days=1)):
            self.assertEqual(archive_send_proof_task(proof.pk)["outcome"], "skipped")
        with patch("project_management.services.correspondence_send_proof_runner.run_archive") as run:
            run.return_value = MagicMock(outcome="archived", state="ARCHIVED")
            self.assertEqual(archive_send_proof_task(proof.pk)["outcome"], "archived")
        self.assertEqual(self.proof_of(event).attempts, 0)


class ScheduleTests(RunnerBase):
    def test_the_delays_are_1_5_15_minutes_1_6_24_hours_then_nothing(self):
        self.assertEqual(
            [retry_delay(n) for n in range(1, 8)],
            [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15),
             timedelta(hours=1), timedelta(hours=6), timedelta(hours=24), None],
        )
        self.assertEqual(RETRY_DELAYS[0], timedelta(minutes=1))
        self.assertIsNone(retry_delay(99))

    def test_the_two_code_families_do_not_overlap(self):
        self.assertFalse(AUTO_RETRY_FAILURES & PERMANENT_FAILURES)

    def test_a_copy_found_after_several_attempts_is_archived_and_scheduling_stops(self):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa")])
        proof = self.proof_of(event)
        copy_items = list(self.reader.sent_items)
        self.reader.sent_items = []                                   # not in Sent Items yet
        waits = []
        elapsed = 0
        for attempt in range(1, 4):
            self.run_at(proof, elapsed)
            self.fresh(proof)
            self.assertEqual((proof.state, proof.error_code, proof.attempts), (State.PENDING, "copy_not_found", attempt))
            waits.append(proof.next_attempt_at - (self.t0 + timedelta(minutes=elapsed)))
            elapsed = int((proof.next_attempt_at - self.t0).total_seconds() // 60)
        self.assertEqual(waits, [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15)])
        self.reader.sent_items = copy_items                            # the copy shows up
        result = self.run_at(proof, elapsed)
        self.fresh(proof)
        self.assertEqual((result.outcome, proof.state, proof.attempts, proof.next_attempt_at), ("archived", State.ARCHIVED, 4, None))
        self.assertEqual(self.sweep_at(elapsed + 10_000).examined, 0)    # nothing left to run

    def test_the_sweep_respects_next_attempt_at(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        self.reader.sent_items = []
        self.assertEqual(self.sweep_at(0).examined, 1)                  # never tried: due now
        self.assertEqual(self.sweep_at(0).examined, 0)                  # now waiting 1 minute
        self.assertEqual(self.sweep_at(0.5 * 1).examined, 0)
        self.assertEqual(self.sweep_at(1).examined, 1)
        self.assertEqual(self.fresh(proof).attempts, 2)

    def test_a_copy_that_never_shows_up_ends_unavailable_after_the_last_retry(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        self.reader.sent_items = []
        now = 0
        for run in range(7):
            self.run_at(proof, now)
            self.fresh(proof)
            if proof.next_attempt_at is not None:
                now = int((proof.next_attempt_at - self.t0).total_seconds() // 60)
        self.assertEqual((proof.state, proof.error_code, proof.attempts, proof.next_attempt_at), (State.UNAVAILABLE, "copy_not_found", 7, None))
        self.assertEqual(self.sweep_at(now + 100_000).examined, 0)
        self.assertEqual(self.fresh(proof).attempts, 7)

    def test_a_temporary_graph_error_is_retried_then_stays_failed_explicitly(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        original = self.reader.list_messages

        def down(folder, **kwargs):
            raise MailboxReadError("temporary")

        self.reader.list_messages = down
        now = 0
        for run in range(7):
            self.run_at(proof, now)
            self.fresh(proof)
            self.assertEqual((proof.state, proof.error_code), (State.FAILED, "temporary"))
            if proof.next_attempt_at is not None:
                now = int((proof.next_attempt_at - self.t0).total_seconds() // 60)
        self.assertIsNone(proof.next_attempt_at)                         # used up: no more automatic runs
        self.assertEqual(self.sweep_at(now + 100_000).examined, 0)
        self.reader.list_messages = original

    def test_an_expired_token_is_retried_but_access_denied_never_is(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        original = self.reader.list_messages

        def rejected(folder, **kwargs):
            raise MailboxReadError("token_rejected")

        self.reader.list_messages = rejected
        self.run_at(proof, 0)
        self.fresh(proof)
        self.assertEqual((proof.state, proof.error_code), (State.FAILED, "token_rejected"))
        self.assertEqual(proof.next_attempt_at, self.t0 + timedelta(minutes=1))     # after the 1st run: 1 minute
        self.reader.list_messages = original

    def test_a_security_failure_is_never_retried_by_the_schedule(self):
        for code in ("access_denied", "not_authorized"):
            _, event, _ = self.sent_and_copied()
            proof = self.proof_of(event)

            def refused(folder, _code=code, **kwargs):
                raise MailboxReadError(_code)

            self.reader.list_messages = refused
            self.run_at(proof, 0)
            self.fresh(proof)
            self.assertEqual((proof.state, proof.error_code, proof.next_attempt_at), (State.FAILED, code, None))
            self.assertNotIn(proof.pk, list(due_proofs(self.t0 + timedelta(days=30)).values_list("pk", flat=True)))

    def test_a_partial_proof_that_can_still_complete_is_retried_and_one_that_cannot_is_not(self):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa"), ("b.pdf", b"bbb")])
        proof = self.proof_of(event)
        full = list(self.reader.attachments[graph_id])
        self.reader.attachments[graph_id] = full[:1]
        self.run_at(proof, 0)
        self.fresh(proof)
        self.assertEqual((proof.state, proof.error_code), (State.PARTIAL, "attachment_missing"))
        self.assertIsNotNone(proof.next_attempt_at)
        self.reader.attachments[graph_id] = full
        self.assertEqual(self.sweep_at(1).examined, 1)
        self.assertEqual(self.fresh(proof).state, State.ARCHIVED)
        # a different content is not something waiting can fix
        _, event2, graph2 = self.sent_and_copied([("c.pdf", b"ccc")])
        proof2 = self.proof_of(event2)
        self.reader.attachments[graph2][0]["content"] = b"other"
        self.run_at(proof2, 0)
        self.fresh(proof2)
        self.assertEqual((proof2.state, proof2.error_code, proof2.next_attempt_at), (State.PARTIAL, "attachment_hash_mismatch", None))

    def test_a_mailbox_that_is_not_active_is_retried_without_touching_graph_and_works_once_reconnected(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status=MailboxConnection.Status.NEEDS_RECONSENT)
        with patch("requests.get", side_effect=AssertionError("no Graph call")):
            with patch("requests.post", side_effect=AssertionError("no sendMail")):
                run_archive(proof.pk, now=self.t0)
        self.fresh(proof)
        self.assertEqual((proof.state, proof.error_code, proof.attempts), (State.FAILED, "mailbox_inactive", 1))
        self.assertEqual(proof.next_attempt_at, self.t0 + timedelta(minutes=1))
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status=MailboxConnection.Status.ACTIVE)
        self.assertEqual(self.sweep_at(1).archived, 1)
        self.assertEqual(self.fresh(proof).state, State.ARCHIVED)

    def test_nobody_allowed_to_read_is_an_explicit_failure_without_any_graph_call(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        with patch("project_management.services.correspondence_send_proof_runner.resolve_archive_actor", return_value=None):
            with patch("requests.get", side_effect=AssertionError("no Graph call")), patch("requests.post", side_effect=AssertionError("no sendMail")):
                result = run_archive(proof.pk, now=self.t0)
        self.fresh(proof)
        self.assertEqual((result.outcome, proof.state, proof.error_code, proof.next_attempt_at), ("failed", State.FAILED, "not_authorized", None))


class SweepTests(RunnerBase):
    def test_the_sweep_is_bounded_and_only_takes_what_is_due(self):
        events = [self.sent_and_copied()[1] for _ in range(4)]
        first = self.sweep_at(0, limit=2)
        self.assertEqual((first.examined, first.archived), (2, 2))
        second = self.sweep_at(0, limit=10)
        self.assertEqual(second.examined, 2)                            # the other two, the first two are done
        self.assertEqual(self.sweep_at(0).examined, 0)
        self.assertTrue(all(self.proof_of(e).state == State.ARCHIVED for e in events))

    def test_the_sweep_only_looks_at_link_sends_and_never_discovers_messages_in_a_mailbox(self):
        self.sent_and_copied()
        self.sweep_at(0)
        self.assertEqual({c[1] for c in self.reader.of("list")}, {"sentitems"})   # only to find a send's own copy
        # no proof due: nothing at all is read
        self.reader.calls.clear()
        self.assertEqual(self.sweep_at(0).examined, 0)
        self.assertEqual(self.reader.calls, [])

    def test_the_sweep_is_off_by_default_and_ignores_sends_older_than_its_activation(self):
        _, event, _ = self.sent_and_copied()
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            result = self.sweep_at(0)
            self.assertEqual((result.enabled, result.skipped, result.examined), (False, "disabled", 0))
        with override_settings(MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE=None):
            self.assertEqual(self.sweep_at(0).examined, 0)
        with override_settings(MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE=timezone.now() + timedelta(hours=1)):
            self.assertEqual(self.sweep_at(0).examined, 0)             # an older send is never archived by itself
        self.assertEqual(self.proof_of(event).attempts, 0)

    def test_two_sweeps_never_run_together(self):
        self.sent_and_copied()
        other = connection.copy()
        self.other_sessions.append(other)
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, 0)", [SWEEP_LOCK_NAMESPACE])
            self.assertTrue(cursor.fetchone()[0])
        result = self.sweep_at(0)
        self.assertEqual((result.skipped, result.examined), ("sweep_in_progress", 0))
        self.assertEqual(self.reader.calls, [])
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, 0)", [SWEEP_LOCK_NAMESPACE])
        self.assertEqual(self.sweep_at(0).examined, 1)

    def test_a_proof_being_run_elsewhere_is_left_alone(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        other = connection.copy()
        self.other_sessions.append(other)
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, proof.pk])
        result = self.sweep_at(0)
        self.assertEqual((result.busy, result.archived), (1, 0))
        self.assertEqual(CorrespondenceSendProofFile.objects.count(), 0)
        self.assertEqual(self.fresh(proof).attempts, 0)

    def test_a_run_never_changes_the_send_or_the_correspondence(self):
        corr, event, _ = self.sent_and_copied([("a.pdf", b"aaa")])
        before_event = CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get()
        before_corr = Correspondence.objects.filter(pk=corr.pk).values().get()
        self.sweep_at(0)
        self.assertEqual(CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get(), before_event)
        self.assertEqual(Correspondence.objects.filter(pk=corr.pk).values().get(), before_corr)


class ActorTests(RunnerBase):
    def shared_event(self):
        corr = self.out_with()
        return CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, provider="graph", mailbox=self.shared_mailbox,
            result=Result.SUCCESS, sent_at=timezone.now(),
        )

    def test_the_owner_reads_a_personal_mailbox(self):
        _, event, _ = self.sent_and_copied()
        self.assertEqual(resolve_archive_actor(event), self.owner)

    def test_a_shared_mailbox_is_read_by_someone_who_really_may(self):
        event = self.shared_event()
        self.assertEqual(resolve_archive_actor(event), self.owner)            # the sender may read it
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.owner).update(can_read=False)
        self.assertIsNone(resolve_archive_actor(event))                        # sending is not reading
        MailboxUserGrant.objects.create(mailbox=self.shared_mailbox, user=self.delegate, can_read=True, can_send=False)
        self.assertEqual(resolve_archive_actor(event), self.delegate)          # the delegate who holds a read grant
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.delegate).update(can_read=False)
        self.assertIsNone(resolve_archive_actor(event))

    def test_an_inactive_user_is_never_used(self):
        event = self.shared_event()
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.owner).update(can_read=False)
        inactive = self._company_member("inactive-reader")
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        MailboxUserGrant.objects.create(mailbox=self.shared_mailbox, user=inactive, can_read=True)
        self.assertIsNone(resolve_archive_actor(event))

    def test_no_token_is_kept_in_a_proof(self):
        _, event, _ = self.sent_and_copied()
        proof = self.proof_of(event)
        self.run_at(proof, 0)
        dump = str(CorrespondenceSendProof.objects.filter(pk=proof.pk).values().get())
        for secret in ("stale-token", "refresh-", "TOKEN"):
            self.assertNotIn(secret, dump)


class ManualRetryTests(RunnerBase):
    def failed_proof(self, code="access_denied"):
        _, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa")])
        proof = self.proof_of(event)
        CorrespondenceSendProof.objects.filter(pk=proof.pk).update(state=State.FAILED, error_code=code, attempts=7)
        return event, proof

    def retry(self, proof, user=None, **kwargs):
        with patch("requests.post", side_effect=AssertionError("sendMail must never be called")):
            return retry_send_proof_archive(proof.pk, requested_by=user or self.owner, now=self.t0, reader=self.reader, **kwargs)

    def test_a_failed_proof_can_be_run_again_by_someone_who_may_send_and_resumes(self):
        event, proof = self.failed_proof()
        before = CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get()
        result = self.retry(proof)
        self.assertEqual((result.outcome, self.fresh(proof).state), ("archived", State.ARCHIVED))
        self.assertEqual(CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get(), before)
        self.assertEqual(proof.files.count(), 2)

    def test_unavailable_and_partial_proofs_can_be_retried_too(self):
        event, proof = self.failed_proof()
        CorrespondenceSendProof.objects.filter(pk=proof.pk).update(state=State.UNAVAILABLE, error_code="copy_not_found")
        self.assertEqual(self.retry(proof).outcome, "archived")
        _, partial = self.failed_proof()
        CorrespondenceSendProof.objects.filter(pk=partial.pk).update(state=State.PARTIAL, error_code="attachment_missing")
        self.assertEqual(self.retry(partial).outcome, "archived")

    def test_it_is_idempotent_an_archived_proof_is_refused_and_not_touched(self):
        _, proof = self.failed_proof()
        self.retry(proof)
        stamp = self.fresh(proof).updated_at
        with self.assertRaises(SendProofRetryRefused) as caught:
            self.retry(proof)
        self.assertEqual(caught.exception.code, "already_archived")
        self.assertEqual(self.fresh(proof).updated_at, stamp)

    def test_without_the_right_to_send_or_from_another_company_nothing_happens(self):
        _, proof = self.failed_proof()
        self.may_send.return_value = False
        with self.assertRaises(SendProofRetryRefused) as caught:
            self.retry(proof)
        self.assertEqual(caught.exception.code, "not_authorized")
        self.may_send.return_value = True
        foreign = _make_user("foreign-proof@example.com", _make_company("Foreign Co"))
        with self.assertRaises(SendProofRetryRefused):
            self.retry(proof, user=foreign)
        with self.assertRaises(SendProofRetryRefused):
            retry_send_proof_archive(proof.pk, requested_by=None)
        with self.assertRaises(SendProofRetryRefused):
            retry_send_proof_archive(10**9, requested_by=self.owner)
        self.assertEqual((self.fresh(proof).state, self.fresh(proof).attempts, self.reader.calls), (State.FAILED, 7, []))

    def test_a_manual_run_that_still_cannot_finish_does_not_restart_the_automatic_schedule(self):
        _, proof = self.failed_proof("temporary")
        self.reader.sent_items = []
        result = self.retry(proof)
        self.fresh(proof)
        self.assertEqual((result.outcome, proof.state, proof.attempts, proof.next_attempt_at), ("pending", State.UNAVAILABLE, 8, None))


class BackfillTests(RunnerBase):
    def historical(self, n=3):
        events = []
        for i in range(n):
            events.append(CorrespondenceSendEvent.objects.create(
                correspondence=self.out_with(), sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
                result=Result.SUCCESS, sent_at=self.t0 - timedelta(days=10 + i),
            ))
        return events

    def test_the_default_is_a_simulation_that_writes_nothing(self):
        from project_management.services.correspondence_send_proof import ensure_missing_send_proofs

        events = self.historical()
        with patch("requests.get", side_effect=AssertionError("no call")), patch("requests.post", side_effect=AssertionError("no call")):
            report = ensure_missing_send_proofs(limit=10)
        self.assertTrue(report.dry_run)
        self.assertEqual(sorted(report.created), sorted(e.pk for e in events))
        self.assertEqual(CorrespondenceSendProof.objects.count(), 0)

    def test_the_limit_is_explicit_and_applied(self):
        from project_management.services.correspondence_send_proof import ensure_missing_send_proofs

        events = self.historical(3)
        with self.assertRaises(TypeError):
            ensure_missing_send_proofs()
        report = ensure_missing_send_proofs(limit=2, dry_run=False)
        self.assertEqual((report.examined, CorrespondenceSendProof.objects.count()), (2, 2))

    def test_the_command_simulates_unless_told_to_apply_and_requires_a_limit(self):
        self.historical(2)
        out = io.StringIO()
        call_command("backfill_send_proofs", "--limit", "10", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertEqual(CorrespondenceSendProof.objects.count(), 0)
        with self.assertRaises(CommandError):
            call_command("backfill_send_proofs")
        out = io.StringIO()
        call_command("backfill_send_proofs", "--limit", "10", "--apply", stdout=out)
        self.assertIn("APPLIED", out.getvalue())
        self.assertEqual(CorrespondenceSendProof.objects.count(), 2)

    def test_proofs_prepared_for_older_sends_are_never_archived_by_the_schedule(self):
        from project_management.services.correspondence_send_proof import ensure_missing_send_proofs

        events = self.historical(2)
        ensure_missing_send_proofs(limit=10, dry_run=False)
        self.assertEqual(list(due_proofs(self.t0 + timedelta(days=100))), [])
        self.assertEqual(self.sweep_at(10_000).examined, 0)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(CorrespondenceSendProofFile.objects.count(), 0)
        # ... but one of them can be archived on purpose, one at a time, by someone allowed
        proof = self.proof_of(events[0])
        self.assertEqual(retry_send_proof_archive(proof.pk, requested_by=self.owner, now=self.t0, reader=self.reader).outcome, "pending")

    def test_uncertain_associations_are_reported(self):
        from project_management.services.correspondence_send_proof import ensure_missing_send_proofs

        corr = self.out_with()
        for days in (5, 3):
            CorrespondenceSendEvent.objects.create(
                correspondence=corr, sender=self.owner, provider="graph", mailbox=self.individual_mailbox,
                result=Result.SUCCESS, sent_at=self.t0 - timedelta(days=days),
            )
        from userapis.mailbox_messages import store_message_metadata
        from userapis.mailbox_read import normalize_message
        from userapis.models import MailboxMessage
        from userapis.test_mailbox_read import raw_message

        row, _ = store_message_metadata(self.individual_mailbox, normalize_message(raw_message(1, id="AAMk-old"), "sentitems"))
        MailboxMessage.objects.filter(pk=row.pk).update(correspondence=corr)
        report = ensure_missing_send_proofs(limit=10)
        self.assertEqual(len(report.uncertain_copy), 2)
