# QUALITY Q3: evaluation creation from a real outgoing order, snapshot, references, questionnaire answers.
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import patch

from django.db import connection
from django.db.models.signals import post_save, pre_delete
from django.test import TestCase, TransactionTestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from models.models import User
from project_management.models import (
    ContactCompany,
    EvaluationReferenceCounter,
    EvaluationResponse,
    EvaluationTemplate,
    EvaluationTemplateCriterion,
    EvaluationTemplateSection,
    PurchaseOrder,
    SupplierEvaluation,
)
from project_management.services.supplier_evaluation import create_supplier_evaluation
from project_management.signals import (
    notify_on_contact_company_delete,
    notify_on_contact_company_save,
)
from project_management.test_document_revision import NoNotificationsMixin
from project_management.test_quality_q1 import grant, make_company

ORDERS = "/api/project_management/quality/orders-to-evaluate"
EVALS = "/api/project_management/quality/evaluations"
ACCESS = "/api/project_management/quality/access"


def detail(pk):
    return f"{EVALS}/{pk}"


def answers_url(pk):
    return f"{EVALS}/{pk}/responses"


class World:
    """Two companies, users with the Quality rights, suppliers, orders and a template (built through the ORM)."""

    def __init__(self, test):
        self.test = test
        self.admin_a = self.user("q3a.admin")
        self.company_a = make_company(self.admin_a, "Q3A")
        self.admin_b = self.user("q3b.admin")
        self.company_b = make_company(self.admin_b, "Q3B")
        self.manager = self.member("q3.manager", self.company_a, "view", "evaluation_manage", "template_manage")
        self.viewer = self.member("q3.viewer", self.company_a, "view")
        self.nobody = self.member("q3.nobody", self.company_a)
        self.manager_b = self.member("q3b.manager", self.company_b, "view", "evaluation_manage")
        self.supplier_a = ContactCompany.objects.create(
            company_profile=self.company_a, company_name="Supplier A", contact_email="a@a.invalid")
        self.supplier_b = ContactCompany.objects.create(
            company_profile=self.company_b, company_name="Supplier B", contact_email="b@b.invalid")
        self.order = self.po(self.company_a, self.supplier_a, "PO-A-1")
        self.order_b = self.po(self.company_b, self.supplier_b, "PO-B-1")
        self.template = self.make_template(self.company_a, "Audit")

    def user(self, tag):
        return User.objects.create_user(
            email=f"{tag}@links-local.invalid", password="x", first_name="T", last_name="U",
            user_type="company_user")

    def member(self, tag, company, *actions):
        user = self.user(tag)
        user.company = company
        user.save(update_fields=["company"])
        if actions:
            grant(user, company, *actions)
        return user

    def po(self, company, supplier, reference, po_type=PurchaseOrder.POType.OUTGOING, **extra):
        return PurchaseOrder.objects.create(
            company_profile=company, po_type=po_type, reference=reference, po_date=date(2026, 11, 3),
            currency="EUR", status="Sent", amount_excl_vat=100, vat_amount=12, amount_incl_vat=112,
            receiving_company=supplier, **extra)

    def make_template(self, company, name):
        template = EvaluationTemplate.objects.create(company=company, name=name, comment_required_below=3)
        s1 = EvaluationTemplateSection.objects.create(template=template, title="Quality", order=0)
        s2 = EvaluationTemplateSection.objects.create(template=template, title="Delivery", order=1)
        self.c1 = EvaluationTemplateCriterion.objects.create(
            section=s1, label="Traceability", order=0, required=True, critical=True, allow_na=False)
        self.c2 = EvaluationTemplateCriterion.objects.create(
            section=s1, label="Packaging", order=1, required=False, allow_na=True, comment_required_below=4)
        self.c3 = EvaluationTemplateCriterion.objects.create(
            section=s2, label="On time", order=0, required=True, allow_na=True)
        return template


def payload(world, **extra):
    return {"purchase_order": world.order.id, "template": world.template.id,
            "evaluation_type": "PARTIAL", **extra}


class QualityQ3Tests(NoNotificationsMixin, TestCase):
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

    def api(self, user=None):
        client = APIClient()
        if user:
            client.force_authenticate(user)
        return client

    def start(self, user=None, **extra):
        return self.api(user or self.w.manager).post(EVALS, payload(self.w, **extra), format="json")

    def answer(self, pk, *answers, user=None):
        return self.api(user or self.w.manager).put(answers_url(pk), {"answers": list(answers)}, format="json")

    # ----- orders to evaluate
    def test_only_outgoing_real_orders_of_the_company_are_listed(self):
        self.w.po(self.w.company_a, self.w.supplier_a, "PO-IN", po_type=PurchaseOrder.POType.INCOMING)
        self.w.po(self.w.company_a, self.w.supplier_a, "CHN-1",
                  document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE, source_po=self.w.order)
        no_supplier = self.w.po(self.w.company_a, None, "PO-NOSUP")
        body = self.api(self.w.manager).get(ORDERS).json()
        self.assertEqual([row["reference"] for row in body["results"]], ["PO-A-1"])
        row = body["results"][0]
        self.assertEqual(row["supplier"]["name"], "Supplier A")
        self.assertEqual(row["po_date"], "2026-11-03")
        self.assertEqual((row["status"], row["evaluations_count"]), ("Sent", 0))
        self.assertNotIn("end_date", row)
        self.assertNotIn(no_supplier.reference, str(body))

    def test_search_by_order_reference_or_supplier(self):
        self.w.po(self.w.company_a, self.w.supplier_a, "ZZ-9")
        client = self.api(self.w.manager)
        self.assertEqual(client.get(ORDERS, {"search": "ZZ"}).json()["count"], 1)
        self.assertEqual(client.get(ORDERS, {"search": "supplier a"}).json()["count"], 2)
        self.assertEqual(client.get(ORDERS, {"search": "nothing"}).json()["count"], 0)

    def test_other_company_orders_never_listed(self):
        body = self.api(self.w.manager_b).get(ORDERS).json()
        self.assertEqual([row["reference"] for row in body["results"]], ["PO-B-1"])

    # ----- creation
    def test_outgoing_order_is_accepted_and_supplier_comes_from_the_order(self):
        response = self.start(supplier=self.w.supplier_b.id)  # a client-sent supplier is ignored
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        evaluation = SupplierEvaluation.objects.get(pk=body["id"])
        self.assertEqual(evaluation.supplier, self.w.supplier_a)
        self.assertEqual(evaluation.owner, self.w.manager)
        self.assertEqual(evaluation.company, self.w.company_a)
        self.assertEqual(evaluation.status, "DRAFT")
        self.assertEqual(body["purchase_order"]["reference"], "PO-A-1")
        self.assertEqual(body["purchase_order"]["po_date"], "2026-11-03")
        self.assertNotIn("end_date", body["purchase_order"])
        self.assertEqual(body["supplier"]["name"], "Supplier A")

    def test_incoming_and_foreign_orders_are_refused(self):
        incoming = self.w.po(self.w.company_a, self.w.supplier_a, "PO-IN", po_type=PurchaseOrder.POType.INCOMING)
        for order_id in (incoming.id, self.w.order_b.id, 999999):
            response = self.api(self.w.manager).post(
                EVALS, {**payload(self.w), "purchase_order": order_id}, format="json")
            self.assertEqual(response.status_code, 400, order_id)
            self.assertIn("purchase_order", response.json())
        self.assertEqual(SupplierEvaluation.objects.count(), 0)

    def test_supplier_of_another_company_is_refused(self):
        stray = self.w.po(self.w.company_a, self.w.supplier_b, "PO-STRAY")
        response = self.api(self.w.manager).post(EVALS, {**payload(self.w), "purchase_order": stray.id}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_inactive_missing_foreign_and_empty_templates_are_refused(self):
        self.w.template.is_active = False
        self.w.template.save()
        self.assertEqual(self.start().status_code, 400)
        foreign = self.w.make_template(self.w.company_b, "Foreign")
        self.assertEqual(self.start(template=foreign.id).status_code, 400)
        self.assertEqual(self.start(template=999999).status_code, 400)
        empty = EvaluationTemplate.objects.create(company=self.w.company_a, name="Empty")
        self.assertEqual(self.start(template=empty.id).status_code, 400)
        self.assertEqual(SupplierEvaluation.objects.count(), 0)

    def test_type_period_and_fnc_rules(self):
        self.assertEqual(self.start(evaluation_type="WHATEVER").status_code, 400)
        self.assertEqual(self.start(period_start="2026-02-01", period_end="2026-01-01").status_code, 400)
        self.assertEqual(self.start(period_start="not-a-date").status_code, 400)
        self.assertEqual(self.start(follows_fnc=True).status_code, 400)
        self.assertEqual(self.start(follows_fnc=True, fnc_reference="  ").status_code, 400)
        ok = self.start(follows_fnc=True, fnc_reference="FNC-12", evaluation_type="FINAL",
                        period_start="2026-01-01", period_end="2026-03-31")
        self.assertEqual(ok.status_code, 201, ok.content)
        body = ok.json()
        self.assertEqual((body["follows_fnc"], body["fnc_reference"], body["evaluation_type"]),
                         (True, "FNC-12", "FINAL"))
        self.assertEqual((body["period_start"], body["period_end"]), ("2026-01-01", "2026-03-31"))
        # a reference sent while "no FNC" is dropped, period stays optional
        plain = self.start(follows_fnc=False, fnc_reference="IGNORED").json()
        self.assertEqual((plain["fnc_reference"], plain["period_start"]), ("", None))

    def test_several_partial_evaluations_of_one_order_are_allowed(self):
        refs = [self.start().json()["reference"] for _ in range(3)]
        self.assertEqual(len(set(refs)), 3)
        self.assertEqual(SupplierEvaluation.objects.filter(purchase_order=self.w.order).count(), 3)
        self.assertEqual(self.start(evaluation_type="FINAL").status_code, 201)
        self.assertEqual(self.api(self.w.manager).get(ORDERS).json()["results"][0]["evaluations_count"], 4)

    # ----- references
    def test_reference_format_sequence_and_per_company_counters(self):
        first = self.start().json()["reference"]
        second = self.start().json()["reference"]
        self.assertRegex(first, r"^EVAL-\d{6}-00001$")
        self.assertRegex(second, r"^EVAL-\d{6}-00002$")
        other = self.api(self.w.manager_b).post(EVALS, {
            "purchase_order": self.w.order_b.id,
            "template": self.w.make_template(self.w.company_b, "B tpl").id,
            "evaluation_type": "PARTIAL"}, format="json").json()["reference"]
        self.assertRegex(other, r"^EVAL-\d{6}-00001$")  # unique per company, not global

    def test_the_reference_cannot_be_typed(self):
        body = self.start(reference="EVAL-HACK").json()
        self.assertNotEqual(body["reference"], "EVAL-HACK")

    # ----- snapshot
    def test_snapshot_is_immutable(self):
        pk = self.start().json()["id"]
        frozen = SupplierEvaluation.objects.get(pk=pk).template_snapshot
        self.assertEqual([s["title"] for s in frozen["sections"]], ["Quality", "Delivery"])
        self.assertEqual(frozen["version"], 1)
        self.assertEqual(frozen["comment_required_below"], 3)
        first = frozen["sections"][0]["criteria"][0]
        self.assertEqual((first["critical"], first["required"], first["allow_na"]), (True, True, False))
        # edit through the real API, then delete the template
        self.api(self.w.manager).patch(
            f"/api/project_management/quality/templates/{self.w.template.id}",
            {"name": "Renamed", "comment_required_below": 5, "sections": []}, format="json")
        self.w.template.refresh_from_db()
        EvaluationTemplate.objects.filter(pk=self.w.template.id).delete()
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        self.assertEqual(evaluation.template_snapshot, frozen)
        self.assertEqual(evaluation.template_version, 1)
        body = self.api(self.w.manager).get(detail(pk)).json()
        self.assertEqual(len(body["snapshot"]["sections"]), 2)  # the questionnaire survives
        self.assertEqual(self.answer(pk, {"criterion_id": frozen["sections"][1]["criteria"][0]["id"], "score": 4}).status_code, 200)

    # ----- answers
    def ids(self, pk):
        snapshot = SupplierEvaluation.objects.get(pk=pk).template_snapshot
        return [c["id"] for s in snapshot["sections"] for c in s["criteria"]]

    def test_score_must_be_1_to_5(self):
        pk = self.start().json()["id"]
        c1 = self.ids(pk)[0]
        for bad in (0, 6, -1, 2.5, "3", True):
            self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": bad, "comment": "x"}).status_code, 400, bad)
        for good in (1, 2, 3, 4, 5):
            self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": good, "comment": "x"}).status_code, 200, good)
        self.assertEqual(EvaluationResponse.objects.filter(evaluation_id=pk).count(), 1)  # one per criterion

    def test_na_allowed_only_where_the_criterion_allows_it_and_never_with_a_score(self):
        pk = self.start().json()["id"]
        c1, c2, _ = self.ids(pk)
        self.assertEqual(self.answer(pk, {"criterion_id": c1, "not_applicable": True}).status_code, 400)
        self.assertEqual(self.answer(pk, {"criterion_id": c2, "not_applicable": True}).status_code, 200)
        self.assertEqual(self.answer(pk, {"criterion_id": c2, "not_applicable": True, "score": 3, "comment": "x"}).status_code, 400)
        self.assertTrue(self.api(self.w.manager).get(detail(pk)).json()["responses"][str(c2)]["not_applicable"])

    def test_comment_required_below_criterion_or_template_threshold(self):
        pk = self.start().json()["id"]
        c1, c2, c3 = self.ids(pk)
        self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": 2}).status_code, 400)   # template threshold 3
        self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": 3}).status_code, 200)   # not below
        self.assertEqual(self.answer(pk, {"criterion_id": c2, "score": 3}).status_code, 400)   # own threshold 4
        self.assertEqual(self.answer(pk, {"criterion_id": c2, "score": 3, "comment": "  "}).status_code, 400)
        self.assertEqual(self.answer(pk, {"criterion_id": c2, "score": 3, "comment": "Dented"}).status_code, 200)
        self.assertEqual(self.answer(pk, {"criterion_id": c3, "score": 1, "comment": "Late"}).status_code, 200)

    def test_one_invalid_answer_saves_nothing_and_errors_are_per_criterion(self):
        pk = self.start().json()["id"]
        c1, c2, c3 = self.ids(pk)
        response = self.answer(pk, {"criterion_id": c3, "score": 5}, {"criterion_id": c1, "score": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(response.json()["answers"]), [str(c1)])
        self.assertEqual(EvaluationResponse.objects.count(), 0)

    def test_only_criteria_of_the_snapshot_and_no_duplicates(self):
        pk = self.start().json()["id"]
        other = self.start().json()["id"]
        foreign_id = max(self.ids(pk)) + 1000
        self.assertEqual(self.answer(pk, {"criterion_id": foreign_id, "score": 5}).status_code, 400)
        self.assertEqual(self.answer(pk, {"criterion_id": None, "score": 5}).status_code, 400)
        c1 = self.ids(pk)[0]
        self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": 5}, {"criterion_id": c1, "score": 4}).status_code, 400)
        self.assertEqual(self.answer(pk, "garbage").status_code, 400)
        self.assertEqual(self.api(self.w.manager).put(answers_url(pk), {"answers": "x"}, format="json").status_code, 400)
        self.assertEqual(EvaluationResponse.objects.filter(evaluation_id=other).count(), 0)

    def test_save_resume_update_and_clear(self):
        pk = self.start().json()["id"]
        c1, c2, c3 = self.ids(pk)
        self.answer(pk, {"criterion_id": c1, "score": 4}, {"criterion_id": c3, "not_applicable": True})
        resumed = self.api(self.w.manager).get(detail(pk)).json()
        self.assertEqual(resumed["status"], "IN_PROGRESS")
        self.assertEqual(resumed["responses"][str(c1)], {"score": 4, "not_applicable": False, "comment": ""})
        self.answer(pk, {"criterion_id": c1, "score": 5, "comment": "Great"})
        self.assertEqual(EvaluationResponse.objects.get(evaluation_id=pk, criterion_id=c1).score, 5)
        self.answer(pk, {"criterion_id": c1})  # empty answer clears the row
        self.assertFalse(EvaluationResponse.objects.filter(evaluation_id=pk, criterion_id=c1).exists())
        self.assertEqual(EvaluationResponse.objects.filter(evaluation_id=pk).count(), 1)

    def test_progress_counts_answered_required_and_critical(self):
        pk = self.start().json()["id"]
        c1, c2, c3 = self.ids(pk)
        empty = self.api(self.w.manager).get(detail(pk)).json()["progress"]
        self.assertEqual(empty, {"total": 3, "answered": 0, "required_total": 2, "required_answered": 0,
                                 "critical_total": 1, "critical_answered": 0})
        self.answer(pk, {"criterion_id": c1, "score": 4}, {"criterion_id": c2, "not_applicable": True},
                    {"criterion_id": c3, "comment": "note only"})
        progress = self.api(self.w.manager).get(detail(pk)).json()["progress"]
        self.assertEqual(progress, {"total": 3, "answered": 2, "required_total": 2, "required_answered": 1,
                                    "critical_total": 1, "critical_answered": 1})

    def test_answers_are_locked_after_the_editable_statuses(self):
        pk = self.start().json()["id"]
        c1 = self.ids(pk)[0]
        for status in ("SUBMITTED", "UNDER_REVIEW", "REJECTED", "APPROVED", "CLOSED"):
            SupplierEvaluation.objects.filter(pk=pk).update(status=status)
            self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": 4}).status_code, 400, status)
            self.assertFalse(self.api(self.w.manager).get(detail(pk)).json()["editable"])
        self.assertEqual(EvaluationResponse.objects.count(), 0)

    # ----- permissions and isolation
    def test_permissions(self):
        self.assertEqual(self.start(self.w.viewer).status_code, 403)
        self.assertEqual(self.start(self.w.nobody).status_code, 403)
        pk = self.start().json()["id"]
        c1 = self.ids(pk)[0]
        self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": 4}, user=self.w.viewer).status_code, 403)
        self.assertEqual(self.api(self.w.viewer).get(detail(pk)).status_code, 200)
        self.assertFalse(self.api(self.w.viewer).get(detail(pk)).json()["editable"])
        self.assertTrue(self.api(self.w.manager).get(detail(pk)).json()["editable"])
        for user in (self.w.nobody, None):
            client = self.api(user)
            self.assertIn(client.get(ORDERS).status_code, (401, 403))
            self.assertIn(client.get(detail(pk)).status_code, (401, 403))
        flags = self.api(self.w.manager).get(ACCESS).json()
        self.assertTrue(flags["can_manage_evaluations"])
        self.assertFalse(self.api(self.w.viewer).get(ACCESS).json()["can_manage_evaluations"])

    def test_other_company_evaluation_is_invisible_and_untouchable(self):
        pk = self.start().json()["id"]
        c1 = self.ids(pk)[0]
        self.assertEqual(self.api(self.w.manager_b).get(detail(pk)).status_code, 404)
        self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": 5}, user=self.w.manager_b).status_code, 404)
        self.assertEqual(self.api(self.w.manager_b).get(EVALS).json()["count"], 0)
        self.assertEqual(EvaluationResponse.objects.count(), 0)

    def test_list_exposes_owner_and_fnc_flag(self):
        self.start(follows_fnc=True, fnc_reference="F-1")
        row = self.api(self.w.manager).get(EVALS).json()["results"][0]
        self.assertTrue(row["follows_fnc"])
        self.assertEqual(row["owner"], "T U")


class QualityQ3ConcurrencyTests(NoNotificationsMixin, TransactionTestCase):
    serialized_rollback = True

    def test_concurrent_creations_get_distinct_consecutive_references(self):
        self.silence_notifications()
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        post_save.disconnect(notify_on_contact_company_save, sender=ContactCompany)
        self.addCleanup(post_save.connect, notify_on_contact_company_save, sender=ContactCompany)
        world = World(self)
        workers = 8

        def create(_):
            try:
                return create_supplier_evaluation(
                    user=world.manager, purchase_order_id=world.order.id,
                    template_id=world.template.id, evaluation_type="PARTIAL").reference
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            references = list(pool.map(create, range(workers)))
        self.assertEqual(len(set(references)), workers)
        sequences = sorted(int(re.search(r"(\d{5})$", ref).group(1)) for ref in references)
        self.assertEqual(sequences, list(range(1, workers + 1)))
        self.assertEqual(EvaluationReferenceCounter.objects.get(company=world.company_a).last_sequence, workers)
        self.assertEqual(
            SupplierEvaluation.objects.filter(company=world.company_a).values("reference").distinct().count(), workers)
