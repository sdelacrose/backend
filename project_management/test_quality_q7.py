# QUALITY Q7: supplier quality list and profile (read-only aggregates over existing objects).
from datetime import date, datetime, timezone as tz
from unittest.mock import patch

from django.db import connection
from django.db.models.signals import post_save, pre_delete
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from project_management.models import (
    ContactCompany,
    Document,
    EvaluationResponse,
    Folder,
    PurchaseOrder,
    SupplierEvaluation,
    SupplierEvaluationReport,
)
from project_management.signals import notify_on_contact_company_delete, notify_on_contact_company_save
from project_management.test_document_revision import NoNotificationsMixin
from project_management.test_quality_q3 import World
from project_management.test_quality_q4 import snap
from project_management.test_quality_q6 import give

SUPPLIERS = "/api/project_management/quality/suppliers"
S = SupplierEvaluation.Status


def at(day, month=1, year=2026):
    return datetime(year, month, day, 10, 0, tzinfo=tz.utc)


class QualityQ7Tests(NoNotificationsMixin, TestCase):
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
        self.company = self.w.company_a
        self.user = self.w.member("q7.user", self.company)
        self.no_docs = self.w.member("q7.nodocs", self.company)
        self.nobody = self.w.member("q7.nobody", self.company)
        self.other = self.w.member("q7.other", self.w.company_b)
        give(self.user, self.company, "quality.view", "documents.view")
        give(self.no_docs, self.company, "quality.view")
        give(self.other, self.w.company_b, "quality.view", "documents.view")
        self.supplier = self.w.supplier_a  # receives the OUTGOING order PO-A-1

    def api(self, user=None):
        client = APIClient()
        if user is not False:
            client.force_authenticate(user or self.user)
        return client

    def evaluation(self, kind="FINAL", status=S.APPROVED, score="80.0", rating="GOOD", supplier=None,
                   approved_at="default", sections=None, **extra):
        approved = at(10) if approved_at == "default" else approved_at
        return SupplierEvaluation.objects.create(
            company=self.company, supplier=supplier or self.supplier, evaluation_type=kind, status=status,
            overall_score=score, rating=rating,
            approved_at=approved if status in (S.APPROVED, S.CLOSED) else None,
            owner=self.user, template_snapshot=sections or snap((None, [(1, None, None)]), (None, [(2, None, None)])),
            reference=f"EVAL-T-{SupplierEvaluation.objects.count() + 1:05d}", **extra)

    def profile(self, supplier=None, user=None):
        return self.api(user).get(f"{SUPPLIERS}/{(supplier or self.supplier).id}")

    def row(self, supplier=None):
        data = self.api().get(SUPPLIERS).json()["results"]
        return next((r for r in data if r["id"] == (supplier or self.supplier).id), None)

    def make_supplier(self, name):
        return ContactCompany.objects.create(
            company_profile=self.company, company_name=name, contact_email=f"{name.replace(' ', '')}@a.invalid")

    # --------------------------------------------------------------------- which companies are suppliers
    def test_supplier_with_an_outgoing_order_is_listed_even_without_evaluation(self):
        row = self.row()
        self.assertIsNotNone(row)
        self.assertIsNone(row["current"])  # "not evaluated"
        self.assertEqual((row["open"], row["partial"], row["fnc"], row["reports"]), (0, 0, 0, 0))

    def test_client_company_and_incoming_only_company_are_not_listed(self):
        client = self.make_supplier("A client")
        incoming_only = self.make_supplier("Incoming only")
        self.w.po(self.company, incoming_only, "PO-IN-1", po_type=PurchaseOrder.POType.INCOMING)
        ids = {r["id"] for r in self.api().get(SUPPLIERS).json()["results"]}
        self.assertNotIn(client.id, ids)
        self.assertNotIn(incoming_only.id, ids)
        self.assertEqual(self.profile(client).status_code, 404)
        self.assertEqual(self.profile(incoming_only).status_code, 404)

    def test_history_stays_reachable_when_the_company_has_no_order_any_more(self):
        old = self.make_supplier("Former supplier")
        self.evaluation(supplier=old)
        self.assertIsNotNone(self.row(old))
        self.assertEqual(self.profile(old).status_code, 200)

    def test_search_by_name(self):
        zeta = self.make_supplier("Zeta Metals")
        self.evaluation(supplier=zeta)
        names = [r["name"] for r in self.api().get(SUPPLIERS, {"search": "zeta"}).json()["results"]]
        self.assertEqual(names, ["Zeta Metals"])

    # ------------------------------------------------------------------------------ reference score
    def test_only_final_approved_or_closed_evaluations_define_the_current_score(self):
        self.evaluation(kind="PARTIAL", score="95.0", rating="EXCELLENT")
        self.assertIsNone(self.row()["current"])  # partial alone: not evaluated
        self.evaluation(kind="FINAL", status=S.UNDER_REVIEW, score="90.0", rating="EXCELLENT")
        self.assertIsNone(self.row()["current"])  # final not approved
        approved = self.evaluation(kind="FINAL", score="72.5", rating="GOOD")
        current = self.row()["current"]
        self.assertEqual((current["evaluation_id"], current["score"], current["rating"]), (approved.id, 72.5, "GOOD"))
        self.evaluation(kind="PARTIAL", score="20.0", rating="UNSATISFACTORY", approved_at=at(20))  # later partial
        self.assertEqual(self.row()["current"]["evaluation_id"], approved.id)

    def test_latest_final_wins_and_closed_at_is_never_the_reference(self):
        first = self.evaluation(score="60.0", rating="IMPROVEMENT_REQUIRED", approved_at=at(10, 1), status=S.CLOSED,
                                closed_at=at(1, 12))  # closed long after: must not make it the latest
        second = self.evaluation(score="88.0", rating="EXCELLENT", approved_at=at(5, 2))
        self.assertEqual(self.row()["current"]["evaluation_id"], second.id)
        self.assertEqual(self.profile().json()["current"]["score"], 88.0)
        self.assertNotEqual(first.id, second.id)

    def test_reference_date_falls_back_to_period_end_then_creation(self):
        from project_management.services.supplier_quality import reference_date

        a = self.evaluation(approved_at=None, status=S.APPROVED, period_end=date(2026, 3, 31))
        self.assertEqual(reference_date(a).date(), date(2026, 3, 31))
        b = self.evaluation(approved_at=None, status=S.APPROVED)
        self.assertEqual(reference_date(b), b.created_at)

    def test_stored_score_is_displayed_never_recalculated(self):
        evaluation = self.evaluation(score="33.3", rating="UNSATISFACTORY")  # no answers at all
        self.assertEqual(self.row()["current"]["score"], 33.3)
        self.assertEqual(self.profile().json()["history"][0]["score"], 33.3)
        evaluation.refresh_from_db()
        self.assertEqual(str(evaluation.overall_score), "33.3")

    # ------------------------------------------------------------------------------------- profile
    def test_overview_history_data_and_no_invented_trend(self):
        self.evaluation(kind="FINAL", approved_at=at(10, 1), score="60.0", rating="IMPROVEMENT_REQUIRED")
        self.evaluation(kind="FINAL", approved_at=at(10, 2), score="75.0", rating="GOOD",
                        follows_fnc=True, fnc_reference="FNC-1")
        self.evaluation(kind="PARTIAL", status=S.IN_PROGRESS, score=None, rating=None)
        data = self.profile().json()
        self.assertEqual(data["supplier"]["name"], "Supplier A")
        self.assertEqual(data["supplier"]["network_id"], self.supplier.id)
        self.assertEqual(data["overview"], {"total": 3, "open": 1, "partial": 1, "final": 2, "fnc": 1, "reports": 0})
        self.assertEqual({h["evaluation_type"] for h in data["history"]}, {"FINAL", "PARTIAL"})
        self.assertEqual([p["score"] for p in data["performance"]], [60.0, 75.0])  # oldest first, finals only
        self.assertEqual(data["current"]["score"], 75.0)
        self.assertNotIn("trend", str(data).lower())
        self.assertEqual(self.row()["fnc"], 1)

    def test_section_comparison_only_groups_truly_comparable_evaluations(self):
        same = snap((None, [(1, None, None)]), (None, [(2, None, None)]))
        other = snap((None, [(3, None, None)]), (None, [(4, None, None)]), (None, [(5, None, None)]))
        e1 = self.evaluation(approved_at=at(1, 1), sections=same)
        e2 = self.evaluation(approved_at=at(1, 2), sections=same)
        e3 = self.evaluation(approved_at=at(1, 3), sections=other)
        for e, scores in ((e1, (5, 3)), (e2, (4, 5))):
            for cid, score in zip((1, 2), scores):
                EvaluationResponse.objects.create(evaluation=e, criterion_id=cid, score=score)
        EvaluationResponse.objects.create(evaluation=e3, criterion_id=3, score=5)
        groups = self.profile().json()["sections"]
        self.assertEqual(len(groups), 2)  # different section structures are never merged
        comparable = next(g for g in groups if len(g["series"]) == 2)
        self.assertTrue(comparable["comparable"])
        self.assertEqual(comparable["sections"], ["S1", "S2"])
        self.assertEqual([s["scores"] for s in comparable["series"]], [[100.0, 60.0], [80.0, 100.0]])
        lone = next(g for g in groups if len(g["series"]) == 1)
        self.assertFalse(lone["comparable"])  # one evaluation: no chart
        self.assertEqual(lone["series"][0]["scores"][1:], [None, None])  # unanswered sections stay empty

    # ------------------------------------------------------------------------------------- reports
    def test_reports_flags_counters_and_rights(self):
        evaluation = self.evaluation()
        document = Document.objects.create(
            folder=Folder.objects.create(company_profile=self.company, name="Quality Reports"), file_name="r.pdf")
        SupplierEvaluationReport.objects.create(evaluation=evaluation, document=document)
        self.evaluation(kind="PARTIAL", status=S.IN_PROGRESS, score=None, rating=None)
        self.assertEqual(self.row()["reports"], 1)
        data = self.profile().json()
        self.assertEqual([h["has_report"] for h in data["history"]].count(True), 1)
        self.assertEqual(data["overview"]["reports"], 1)
        self.assertTrue(data["can_view_reports"])
        self.assertFalse(self.profile(user=self.no_docs).json()["can_view_reports"])  # Documents right needed

    # ------------------------------------------------------------------------- rights and isolation
    def test_permissions_and_multitenant_isolation(self):
        self.evaluation()
        self.assertEqual(self.api(False).get(SUPPLIERS).status_code, 401)
        self.assertEqual(self.api(self.nobody).get(SUPPLIERS).status_code, 403)
        self.assertEqual(self.profile(user=self.nobody).status_code, 403)
        theirs = {r["id"] for r in self.api(self.other).get(SUPPLIERS).json()["results"]}
        self.assertEqual(theirs, {self.w.supplier_b.id})  # only their own supplier, nothing of company A
        self.assertEqual(self.profile(user=self.other).status_code, 404)
        foreign = self.w.supplier_b
        self.assertEqual(self.profile(foreign).status_code, 404)
        self.assertEqual(self.api().get(SUPPLIERS, {"company": foreign.id}).json()["count"], 0)
        # another company's evaluation of the same supplier never leaks into the counters
        SupplierEvaluation.objects.create(
            company=self.w.company_b, supplier=self.supplier, status=S.APPROVED, evaluation_type="FINAL",
            overall_score="10.0", rating="UNSATISFACTORY", approved_at=at(30), reference="EVAL-B-1")
        self.assertEqual(self.profile().json()["overview"]["total"], 1)
        self.assertEqual(self.row()["current"]["score"], 80.0)

    def test_list_query_count_does_not_grow_with_the_number_of_suppliers(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.api().get(SUPPLIERS)
            return len(ctx)

        self.evaluation()
        baseline = count()
        for i in range(6):
            self.evaluation(supplier=self.make_supplier(f"Extra {i}"))
        self.assertEqual(count(), baseline)

    def test_reading_writes_nothing(self):
        self.evaluation()
        before = list(SupplierEvaluation.objects.values())
        self.profile()
        self.api().get(SUPPLIERS)
        self.assertEqual(list(SupplierEvaluation.objects.values()), before)
