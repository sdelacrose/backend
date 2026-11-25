# QUALITY Q5: validation workflow, locking, history, reviewer rules and isolation.
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.signals import post_save, pre_delete
from django.test import TestCase
from unittest.mock import patch
from rest_framework.test import APIClient

from project_management.models import ContactCompany, SupplierEvaluation, SupplierEvaluationEvent
from project_management.signals import notify_on_contact_company_delete, notify_on_contact_company_save
from project_management.test_document_revision import NoNotificationsMixin
from project_management.test_quality_q1 import grant
from project_management.test_quality_q3 import EVALS, World, answers_url, payload

S = SupplierEvaluation.Status
E = SupplierEvaluationEvent.EventType


def url(pk, action):
    return f"{EVALS}/{pk}/{action}"


class QualityQ5Tests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        for signal, receiver in ((post_save, notify_on_contact_company_save),
                                 (pre_delete, notify_on_contact_company_delete)):
            signal.disconnect(receiver, sender=ContactCompany)
            self.addCleanup(signal.connect, receiver, sender=ContactCompany)
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.w = World(self)
        self.owner = self.w.manager
        self.reviewer = self.w.member("q5.reviewer", self.w.company_a, "view", "evaluation_manage")
        self.admin = self.w.member("q5.admin", self.w.company_a, "view", "full_access")

    def api(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.owner)
        return client

    def post(self, pk, action, user=None, **body):
        return self.api(user).post(url(pk, action), body, format="json")

    def start(self):
        response = self.api().post(EVALS, payload(self.w), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["id"]

    def criteria(self, pk):
        sections = self.api().get(f"{EVALS}/{pk}").json()["snapshot"]["sections"]
        return [c["id"] for s in sections for c in s["criteria"]]  # traceability, packaging, on time

    def fill(self, pk, **scores):
        c1, c2, c3 = self.criteria(pk)
        answers = [{"criterion_id": c1, "score": scores.get("c1", 4)},
                   {"criterion_id": c3, "score": scores.get("c3", 4)}]
        response = self.api().put(answers_url(pk), {"answers": answers}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def choose(self, pk, user=None):
        response = self.api().put(f"{EVALS}/{pk}/reviewer", {"reviewer": (user or self.reviewer).id}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def ready(self):
        pk = self.start()
        self.fill(pk)
        self.choose(pk)
        return pk

    def under_review(self):
        pk = self.ready()
        self.assertEqual(self.post(pk, "submit").status_code, 200)
        self.assertEqual(self.post(pk, "start_review", self.reviewer).status_code, 200)
        return pk

    def status(self, pk):
        return SupplierEvaluation.objects.get(pk=pk).status

    def events(self, pk):
        return list(SupplierEvaluation.objects.get(pk=pk).events.values_list("event_type", flat=True))

    # ------------------------------------------------------------------------------------------ happy path
    def test_full_workflow_and_history(self):
        pk = self.start()
        self.assertEqual(self.status(pk), S.DRAFT)
        self.fill(pk)
        self.assertEqual(self.status(pk), S.IN_PROGRESS)
        self.choose(pk)
        self.assertEqual(self.post(pk, "submit").json()["status"], S.SUBMITTED)
        self.assertEqual(self.post(pk, "start_review", self.reviewer).json()["status"], S.UNDER_REVIEW)
        approved = self.post(pk, "approve", self.reviewer, comment="ok").json()
        self.assertEqual(approved["status"], S.APPROVED)
        self.assertEqual(self.post(pk, "close", self.reviewer).json()["status"], S.CLOSED)
        self.assertEqual(self.events(pk), [
            E.CREATED, E.STARTED, E.REVIEWER_ASSIGNED, E.SUBMITTED, E.REVIEW_STARTED, E.APPROVED, E.CLOSED])
        history = self.api().get(f"{EVALS}/{pk}/history").json()["results"]
        self.assertEqual([h["event_type"] for h in history][-1], E.CLOSED)
        self.assertTrue(all(h["actor"] for h in history))
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        self.assertEqual((evaluation.submitted_by, evaluation.reviewed_by, evaluation.approved_by, evaluation.closed_by),
                         (self.owner, self.reviewer, self.reviewer, self.reviewer))
        self.assertTrue(evaluation.submitted_at and evaluation.reviewed_at and evaluation.approved_at and evaluation.closed_at)

    def test_rejection_loop_resume_and_resubmission(self):
        pk = self.under_review()
        self.assertEqual(self.post(pk, "reject", self.reviewer).status_code, 400)  # reason mandatory
        self.assertEqual(self.post(pk, "reject", self.reviewer, comment="  ").status_code, 400)
        self.assertEqual(self.post(pk, "reject", self.reviewer, comment="Redo delivery").json()["status"], S.REJECTED)
        # nothing can be edited until the owner resumes
        c1, _, _ = self.criteria(pk)
        self.assertEqual(self.api().put(answers_url(pk), {"answers": [{"criterion_id": c1, "score": 5}]},
                                        format="json").status_code, 400)
        self.assertEqual(self.post(pk, "resume", self.reviewer).status_code, 403)
        self.assertEqual(self.post(pk, "resume").json()["status"], S.IN_PROGRESS)
        self.assertEqual(self.api().put(answers_url(pk), {"answers": [{"criterion_id": c1, "score": 5}]},
                                        format="json").status_code, 200)
        self.assertEqual(self.post(pk, "submit").json()["status"], S.SUBMITTED)
        self.assertEqual(self.events(pk)[-4:],
                         [E.REVIEW_STARTED, E.REJECTED, E.RESUMED, E.RESUBMITTED])
        self.assertEqual(self.api().get(f"{EVALS}/{pk}").json()["workflow"]["rejection_reason"], "Redo delivery")

    # ----------------------------------------------------------------------------------- forbidden moves
    def test_forbidden_transitions(self):
        pk = self.ready()
        for action in ("start_review", "reject", "approve", "close", "resume"):
            user = self.owner if action in ("resume", "close") else self.reviewer
            self.assertEqual(self.post(pk, action, user, comment="x").status_code, 400, action)
        self.post(pk, "submit")
        self.assertEqual(self.post(pk, "submit").status_code, 400)  # already submitted
        self.assertEqual(self.post(pk, "approve", self.reviewer).status_code, 400)  # review not started
        self.post(pk, "start_review", self.reviewer)
        self.assertEqual(self.post(pk, "start_review", self.reviewer).status_code, 400)
        self.assertEqual(self.post(pk, "close", self.reviewer).status_code, 400)  # not approved
        self.assertEqual(self.post(pk, "unknown", self.reviewer).status_code, 404)
        self.post(pk, "approve", self.reviewer)
        for action in ("submit", "start_review", "reject", "approve", "resume"):
            user = self.owner if action in ("submit", "resume") else self.reviewer
            self.assertEqual(self.post(pk, action, user, comment="x").status_code, 400, action)
        self.post(pk, "close", self.owner)
        for action in ("submit", "start_review", "reject", "approve", "resume", "close"):  # CLOSED is final
            user = self.owner if action in ("submit", "resume") else self.reviewer
            self.assertEqual(self.post(pk, action, user, comment="x").status_code, 400, action)
        self.assertEqual(self.status(pk), S.CLOSED)

    # ---------------------------------------------------------------------------------------- submission
    def test_incomplete_submission_explains_what_is_missing(self):
        pk = self.start()
        self.choose(pk)
        response = self.post(pk, "submit")
        self.assertEqual(response.status_code, 400)
        check = response.json()["submission"]
        self.assertEqual({i["label"] for i in check["missing_required"]}, {"Traceability", "On time"})
        self.assertEqual([i["label"] for i in check["missing_critical"]], ["Traceability"])
        self.assertEqual(self.status(pk), S.DRAFT)
        self.fill(pk)
        self.assertEqual(self.post(pk, "submit").status_code, 200)

    def test_missing_comment_blocks_submission(self):
        pk = self.start()
        self.choose(pk)
        self.fill(pk)
        c1, _, _ = self.criteria(pk)
        from project_management.models import EvaluationResponse
        EvaluationResponse.objects.filter(evaluation_id=pk, criterion_id=c1).update(score=2, comment="")
        response = self.post(pk, "submit")
        self.assertEqual(response.status_code, 400)
        self.assertEqual([i["label"] for i in response.json()["submission"]["missing_comments"]], ["Traceability"])

    def test_reviewer_absent_or_invalid(self):
        pk = self.start()
        self.fill(pk)
        response = self.post(pk, "submit")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["submission"]["reviewer_problem"], "reviewer_missing")
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        for bad in (self.owner.id, self.w.manager_b.id, self.w.viewer.id, self.w.nobody.id, 99999, None, "x", True):
            self.assertEqual(self.api().put(f"{EVALS}/{pk}/reviewer", {"reviewer": bad}, format="json").status_code,
                             400, bad)
        evaluation.refresh_from_db()
        self.assertIsNone(evaluation.reviewer_id)
        # even if the data were corrupted (reviewer == owner), submission is refused
        SupplierEvaluation.objects.filter(pk=pk).update(reviewer=self.owner)
        self.assertEqual(self.post(pk, "submit").json()["submission"]["reviewer_problem"], "reviewer_is_owner")

    def test_low_score_can_be_submitted_and_approved(self):
        pk = self.start()
        c1, c2, c3 = self.criteria(pk)
        self.api().put(answers_url(pk), {"answers": [{"criterion_id": c1, "score": 1, "comment": "bad"},
                                                     {"criterion_id": c3, "score": 1, "comment": "bad"}]}, format="json")
        self.choose(pk)
        self.assertEqual(self.post(pk, "submit").status_code, 200)
        self.post(pk, "start_review", self.reviewer)
        self.assertEqual(self.post(pk, "approve", self.reviewer).status_code, 200)
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        self.assertEqual((str(evaluation.overall_score), evaluation.rating), ("20.0", "UNSATISFACTORY"))

    # ----------------------------------------------------------------------------------------- permissions
    def test_admin_cannot_approve_and_is_not_an_implicit_reviewer(self):
        pk = self.ready()
        self.post(pk, "submit")
        self.assertEqual(self.post(pk, "start_review", self.admin).status_code, 403)
        self.post(pk, "start_review", self.reviewer)
        self.assertEqual(self.post(pk, "approve", self.admin).status_code, 403)
        self.assertEqual(self.post(pk, "reject", self.admin, comment="x").status_code, 403)
        self.assertEqual(self.post(pk, "approve", self.owner).status_code, 403)
        self.assertEqual(self.status(pk), S.UNDER_REVIEW)
        self.assertNotIn(self.admin.id, [u["id"] for u in self.api().get(f"{EVALS}/{pk}/reviewer").json()["results"]
                                         if u["id"] != self.admin.id])

    def test_owner_who_is_admin_cannot_approve_own_evaluation(self):
        w = self.w
        pk = self.api(self.admin).post(EVALS, payload(w), format="json").json()["id"]
        c1, c2, c3 = self.criteria(pk)
        self.api(self.admin).put(answers_url(pk), {"answers": [{"criterion_id": c1, "score": 4},
                                                               {"criterion_id": c3, "score": 4}]}, format="json")
        self.api(self.admin).put(f"{EVALS}/{pk}/reviewer", {"reviewer": self.reviewer.id}, format="json")
        self.post(pk, "submit", self.admin)
        self.assertEqual(self.post(pk, "start_review", self.admin).status_code, 403)
        SupplierEvaluation.objects.filter(pk=pk).update(reviewer=self.admin)  # even a corrupted assignment
        self.assertEqual(self.post(pk, "start_review", self.admin).status_code, 403)

    def test_other_users_cannot_act_or_edit(self):
        pk = self.ready()
        c1, _, _ = self.criteria(pk)
        for user in (self.reviewer, self.admin):
            self.assertEqual(self.api(user).put(answers_url(pk), {"answers": [{"criterion_id": c1, "score": 5}]},
                                                format="json").status_code, 403)
            self.assertEqual(self.post(pk, "submit", user).status_code, 403)
            self.assertEqual(self.api(user).put(f"{EVALS}/{pk}/reviewer", {"reviewer": self.admin.id},
                                                format="json").status_code, 403)
        self.assertEqual(self.post(pk, "submit", self.w.viewer).status_code, 403)  # no manage right
        self.assertEqual(self.post(pk, "submit", self.w.nobody).status_code, 403)

    def test_allowed_actions_follow_status_and_user(self):
        pk = self.ready()
        actions = lambda user: self.api(user).get(f"{EVALS}/{pk}").json()["allowed_actions"]  # noqa: E731
        self.assertEqual(actions(self.owner), ["assign_reviewer", "submit"])
        self.assertEqual(actions(self.reviewer), [])
        self.post(pk, "submit")
        self.assertEqual((actions(self.owner), actions(self.reviewer)), ([], ["start_review"]))
        self.post(pk, "start_review", self.reviewer)
        self.assertEqual(actions(self.reviewer), ["approve", "reject"])
        self.assertEqual(actions(self.admin), [])
        self.post(pk, "approve", self.reviewer)
        self.assertEqual(actions(self.owner), ["close"])

    # ----------------------------------------------------------------------------------------- locking
    def test_answers_are_locked_server_side_after_submission(self):
        pk = self.ready()
        c1, _, _ = self.criteria(pk)
        change = lambda: self.api().put(answers_url(pk), {"answers": [{"criterion_id": c1, "score": 5}]},  # noqa: E731
                                        format="json").status_code
        self.post(pk, "submit")
        self.assertEqual(change(), 400)
        self.post(pk, "start_review", self.reviewer)
        self.assertEqual(change(), 400)
        self.post(pk, "approve", self.reviewer)
        self.assertEqual(change(), 400)
        self.post(pk, "close", self.owner)
        self.assertEqual(change(), 400)
        self.assertEqual(self.api().get(f"{EVALS}/{pk}").json()["editable"], False)
        # the reviewer cannot be swapped once submitted either
        self.assertEqual(self.api().put(f"{EVALS}/{pk}/reviewer", {"reviewer": self.reviewer.id},
                                        format="json").status_code, 400)

    def test_snapshot_and_score_stay_frozen_after_approval(self):
        pk = self.under_review()
        self.post(pk, "approve", self.reviewer)
        before = SupplierEvaluation.objects.get(pk=pk)
        snapshot, score = before.template_snapshot, before.overall_score
        template = self.w.template
        template.name = "Renamed"
        template.save()
        self.w.c1.label = "Changed"
        self.w.c1.save()
        after = SupplierEvaluation.objects.get(pk=pk)
        self.assertEqual((after.template_snapshot, after.overall_score), (snapshot, score))

    # ----------------------------------------------------------------------------------------- history
    def test_history_is_insert_only(self):
        pk = self.ready()
        event = SupplierEvaluationEvent.objects.filter(evaluation_id=pk).first()
        with self.assertRaises(DjangoValidationError):
            SupplierEvaluationEvent.objects.filter(evaluation_id=pk).update(comment="x")
        with self.assertRaises(DjangoValidationError):
            SupplierEvaluationEvent.objects.filter(evaluation_id=pk).delete()
        with self.assertRaises(DjangoValidationError):
            event.delete()
        event.comment = "tampered"
        with self.assertRaises(DjangoValidationError):
            event.save()
        for method in ("put", "patch", "delete"):  # no API writes to the history
            self.assertEqual(getattr(self.api(), method)(f"{EVALS}/{pk}/history", {}, format="json").status_code, 405)

    def test_status_cannot_be_forced_through_other_endpoints(self):
        pk = self.ready()
        for method in ("patch", "put"):
            response = getattr(self.api(), method)(f"{EVALS}/{pk}", {"status": "APPROVED"}, format="json")
            self.assertEqual(response.status_code, 405)
        self.assertEqual(self.status(pk), S.IN_PROGRESS)

    # ------------------------------------------------------------------------------------------ tenancy
    def test_other_company_gets_404_everywhere(self):
        pk = self.under_review()
        intruder = self.w.manager_b
        self.assertEqual(self.api(intruder).get(f"{EVALS}/{pk}/history").status_code, 404)
        for action in ("submit", "start_review", "approve", "reject", "resume", "close"):
            self.assertEqual(self.post(pk, action, intruder, comment="x").status_code, 404, action)
        self.assertEqual(self.api(intruder).put(f"{EVALS}/{pk}/reviewer", {"reviewer": intruder.id},
                                                format="json").status_code, 404)
        self.assertEqual(self.api(intruder).get(f"{EVALS}/{pk}/reviewer").status_code, 404)
        self.assertEqual(self.status(pk), S.UNDER_REVIEW)

    def test_reviewer_candidates_are_company_users_with_the_right_and_exclude_the_owner(self):
        pk = self.start()
        listed = {u["id"] for u in self.api().get(f"{EVALS}/{pk}/reviewer").json()["results"]}
        self.assertIn(self.reviewer.id, listed)
        self.assertIn(self.admin.id, listed)  # eligible by right, but only if the owner chooses them
        self.assertNotIn(self.owner.id, listed)
        self.assertNotIn(self.w.manager_b.id, listed)
        self.assertNotIn(self.w.viewer.id, listed)
        self.assertEqual(self.api(self.reviewer).get(f"{EVALS}/{pk}/reviewer").json()["results"], [])

    def test_dashboard_counts_submitted_and_under_review(self):
        pk = self.under_review()
        self.assertEqual(self.api().get("/api/project_management/quality/dashboard").json()["pending_review"], 1)
        self.post(pk, "approve", self.reviewer)
        self.assertEqual(self.api().get("/api/project_management/quality/dashboard").json()["pending_review"], 0)
