# QUALITY Q4: scoring engine, weight rule, persistence, synthesis and isolation.
from decimal import Decimal
from unittest.mock import patch

from django.db.models.signals import post_save, pre_delete
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from project_management.models import (
    ContactCompany,
    EvaluationTemplate,
    EvaluationTemplateCriterion,
    EvaluationTemplateSection,
    SupplierEvaluation,
)
from project_management.services.supplier_evaluation_scoring import (
    compute_scoring,
    rating_for,
    recalculate_evaluation_score,
)
from project_management.services.supplier_evaluation_template import weight_set_problem
from project_management.signals import notify_on_contact_company_delete, notify_on_contact_company_save
from project_management.test_document_revision import NoNotificationsMixin
from project_management.test_quality_q3 import World

EVALS = "/api/project_management/quality/evaluations"
TEMPLATES = "/api/project_management/quality/templates"


def snap(*sections):
    """sections: (weight, [(criterion_id, weight, extra)...]) -> snapshot dict."""
    out = []
    for index, (weight, criteria) in enumerate(sections, start=1):
        out.append({
            "id": index, "title": f"S{index}", "weight": weight,
            "criteria": [
                {"id": cid, "label": f"C{cid}", "weight": w, "required": False, "critical": False,
                 "allow_na": True, "comment_required_below": None, **(extra or {})}
                for cid, w, extra in criteria
            ],
        })
    return {"sections": out}


def ans(**by_id):
    """ans(c1=5, c2="na") -> answers keyed by criterion id."""
    out = {}
    for key, value in by_id.items():
        cid = int(key[1:])
        out[cid] = ({"score": None, "not_applicable": True, "comment": ""} if value == "na"
                    else {"score": value, "not_applicable": False, "comment": ""})
    return out


def overall(snapshot, answers):
    return compute_scoring(snapshot, answers)["overall_score"]


class ScoringEngineTests(SimpleTestCase):
    def test_conversion_of_each_score(self):
        for score, expected in ((1, "20.0"), (2, "40.0"), (3, "60.0"), (4, "80.0"), (5, "100.0")):
            self.assertEqual(overall(snap((None, [(1, None, None)])), ans(c1=score)), Decimal(expected))

    def test_rating_thresholds(self):
        cases = {"0": "UNSATISFACTORY", "49.9": "UNSATISFACTORY", "50.0": "IMPROVEMENT_REQUIRED",
                 "69.9": "IMPROVEMENT_REQUIRED", "70.0": "GOOD", "84.9": "GOOD",
                 "85.0": "EXCELLENT", "100.0": "EXCELLENT"}
        for score, rating in cases.items():
            self.assertEqual(rating_for(Decimal(score)), rating, score)
        self.assertIsNone(rating_for(None))

    def test_thresholds_through_real_weighted_calculations(self):
        # two criteria scored 5 (100 %) and 1 (20 %): weight w on the 5 gives 20 + 0.8 * w
        cases = [("36.25", "49.0", "UNSATISFACTORY"), ("37.5", "50.0", "IMPROVEMENT_REQUIRED"),
                 ("62.5", "70.0", "GOOD"), ("81.25", "85.0", "EXCELLENT")]
        for weight, expected, rating in cases:
            other = str(Decimal("100") - Decimal(weight))
            result = compute_scoring(snap((None, [(1, weight, None), (2, other, None)])), ans(c1=5, c2=1))
            self.assertEqual((result["overall_score"], result["rating"]), (Decimal(expected), rating), weight)

    def test_rounding_is_one_final_half_up_and_rating_follows_the_rounded_value(self):
        # 84.96 -> 85.0 (Excellent: the value the user sees and that is stored); 49.96 -> 50.0
        high = compute_scoring(snap((None, [(1, "81.2", None), (2, "18.8", None)])), ans(c1=5, c2=1))
        self.assertEqual((high["overall_score"], high["rating"]), (Decimal("85.0"), "EXCELLENT"))
        low = compute_scoring(snap((None, [(1, "37.45", None), (2, "62.55", None)])), ans(c1=5, c2=1))
        self.assertEqual((low["overall_score"], low["rating"]), (Decimal("50.0"), "IMPROVEMENT_REQUIRED"))
        mean = overall(snap((None, [(1, None, None), (2, None, None), (3, None, None)])), ans(c1=5, c2=5, c3=4))
        self.assertEqual(mean, Decimal("93.3"))  # 93.333... rounded once

    def test_na_is_excluded_and_never_a_zero(self):
        snapshot = snap((None, [(1, None, None), (2, None, None)]))
        self.assertEqual(overall(snapshot, ans(c1=5, c2="na")), Decimal("100.0"))
        self.assertEqual(overall(snapshot, ans(c1=5)), Decimal("100.0"))  # missing answer: excluded too

    def test_no_answer_or_only_na_gives_null_score_and_no_rating(self):
        snapshot = snap((None, [(1, None, None)]), (None, [(2, None, None)]))
        for answers in ({}, ans(c1="na", c2="na")):
            result = compute_scoring(snapshot, answers)
            self.assertIsNone(result["overall_score"])
            self.assertIsNone(result["rating"])
            self.assertTrue(all(section["score"] is None for section in result["sections"]))

    def test_weights_are_renormalised_on_scored_criteria(self):
        snapshot = snap((None, [(1, "50", None), (2, "30", None), (3, "20", None)]))
        # c3 (weight 20) has no answer: (100*50 + 60*30) / 80 = 85
        self.assertEqual(overall(snapshot, ans(c1=5, c2=3)), Decimal("85.0"))
        self.assertEqual(overall(snapshot, ans(c1=5, c2=3, c3="na")), Decimal("85.0"))

    def test_without_weights_the_mean_is_simple(self):
        snapshot = snap((None, [(1, None, None), (2, None, None), (3, None, None), (4, None, None)]))
        self.assertEqual(overall(snapshot, ans(c1=5, c2=4, c3=3, c4=3)), Decimal("75.0"))

    def test_legacy_partial_weights_fall_back_to_simple_mean(self):
        snapshot = snap((None, [(1, "70", None), (2, None, None)]))
        self.assertEqual(overall(snapshot, ans(c1=5, c2=1)), Decimal("60.0"))

    def test_section_weights_are_renormalised_on_sections_with_a_score(self):
        snapshot = snap(("60", [(1, None, None)]), ("40", [(2, None, None)]))
        both = compute_scoring(snapshot, ans(c1=5, c2=1))
        self.assertEqual(both["overall_score"], Decimal("68.0"))  # 0.6*100 + 0.4*20
        only_first = compute_scoring(snapshot, ans(c1=5, c2="na"))
        self.assertEqual(only_first["overall_score"], Decimal("100.0"))
        self.assertEqual([s["score"] for s in only_first["sections"]], [Decimal("100.0"), None])

    def test_sections_without_weights_use_the_simple_mean_of_section_scores(self):
        snapshot = snap((None, [(1, None, None)]), (None, [(2, None, None), (3, None, None)]))
        # section 1 = 100 ; section 2 = mean(20, 40) = 30 -> simple mean of sections = 65 (not criteria mean)
        self.assertEqual(overall(snapshot, ans(c1=5, c2=1, c3=2)), Decimal("65.0"))

    def test_section_scores_are_exposed(self):
        snapshot = snap((None, [(1, None, None), (2, None, None)]))
        section = compute_scoring(snapshot, ans(c1=4, c2=5))["sections"][0]
        self.assertEqual((section["score"], section["scored"], section["answered"], section["total"]),
                         (Decimal("90.0"), 2, 2, 2))

    def test_synthesis_strengths_improvements_and_critical_points(self):
        snapshot = snap((None, [
            (1, None, {"critical": True}), (2, None, None), (3, None, {"critical": True}),
            (4, None, None), (5, None, None)]))
        result = compute_scoring(snapshot, ans(c1=1, c2=5, c3=4, c4=2, c5=3))
        self.assertEqual([i["criterion_id"] for i in result["strengths"]], [2])
        self.assertEqual([i["criterion_id"] for i in result["improvements"]], [1, 4])
        self.assertEqual([i["criterion_id"] for i in result["critical_points"]], [1])  # critical AND 1 or 2
        item = result["critical_points"][0]
        self.assertEqual((item["score"], item["label"], item["section_title"]), (1, "C1", "S1"))

    def test_required_remaining_lists_unanswered_required_criteria_only(self):
        snapshot = snap((None, [
            (1, None, {"required": True, "critical": True}), (2, None, {"required": True}),
            (3, None, {"required": True}), (4, None, None)]))
        result = compute_scoring(snapshot, ans(c2=3, c3="na"))
        self.assertEqual([r["criterion_id"] for r in result["required_remaining"]], [1])
        self.assertTrue(result["required_remaining"][0]["critical"])

    def test_weight_set_rule(self):
        D = Decimal
        self.assertIsNone(weight_set_problem([]))
        self.assertIsNone(weight_set_problem([None, None]))
        self.assertIsNone(weight_set_problem([D("100")]))
        self.assertIsNone(weight_set_problem([D("33.33"), D("33.33"), D("33.34")]))
        self.assertEqual(weight_set_problem([D("60"), None]), "partial")
        self.assertEqual(weight_set_problem([D("60"), D("30")]), "sum")
        self.assertEqual(weight_set_problem([D("60"), D("50")]), "sum")


class QualityQ4ApiTests(NoNotificationsMixin, TestCase):
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
        return self.api(user or self.w.manager).post(EVALS, {
            "purchase_order": self.w.order.id, "template": self.w.template.id,
            "evaluation_type": "PARTIAL", **extra}, format="json")

    def answer(self, pk, *answers, user=None):
        return self.api(user or self.w.manager).put(f"{EVALS}/{pk}/responses", {"answers": list(answers)}, format="json")

    def ids(self, pk):
        snapshot = SupplierEvaluation.objects.get(pk=pk).template_snapshot
        return [c["id"] for s in snapshot["sections"] for c in s["criteria"]]

    def detail(self, pk, user=None):
        return self.api(user or self.w.manager).get(f"{EVALS}/{pk}").json()

    # ----- weights validated when saving a template
    def tpl(self, sections, name="T", **extra):
        return self.api(self.w.manager).post(TEMPLATES, {"name": name, "sections": sections, **extra}, format="json")

    def test_template_weights_all_absent_or_all_present_summing_to_100(self):
        def section(weight, *weights):
            return {"title": "S", "weight": weight, "criteria": [{"label": f"c{i}", "weight": w} for i, w in enumerate(weights)]}
        ok = [section(None, None, None), section(None, "60", "40")]
        self.assertEqual(self.tpl(ok, "ok1").status_code, 201)
        self.assertEqual(self.tpl([section("70", "100"), section("30", "50", "50")], "ok2").status_code, 201)
        for name, bad in {
            "partial-criteria": [section(None, "60", None)],
            "criteria-sum": [section(None, "60", "30")],
            "partial-sections": [section("60", None), section(None, None)],
            "sections-sum": [section("60", None), section("30", None)],
            "over-100": [section(None, "60", "50")],
        }.items():
            response = self.tpl(bad, name)
            self.assertEqual(response.status_code, 400, name)
            self.assertIn("sections", response.json(), name)
        self.assertEqual(EvaluationTemplate.objects.filter(name__startswith="partial").count(), 0)

    def test_patch_revalidates_the_whole_tree(self):
        pk = self.tpl([{"title": "S", "criteria": [{"label": "a", "weight": "50"}, {"label": "b", "weight": "50"}]}], "P").json()["id"]
        body = self.api(self.w.manager).get(f"{TEMPLATES}/{pk}").json()
        body["sections"][0]["criteria"][1]["weight"] = "40"
        self.assertEqual(self.api(self.w.manager).patch(f"{TEMPLATES}/{pk}", {"sections": body["sections"]}, format="json").status_code, 400)
        body["sections"][0]["criteria"][1]["weight"] = "50"
        self.assertEqual(self.api(self.w.manager).patch(f"{TEMPLATES}/{pk}", {"sections": body["sections"]}, format="json").status_code, 200)

    def test_legacy_template_with_partial_weights_cannot_start_an_evaluation(self):
        EvaluationTemplateCriterion.objects.filter(pk=self.w.c1.pk).update(weight=Decimal("40"))
        response = self.start()
        self.assertEqual(response.status_code, 400)
        self.assertIn("template", response.json())

    def test_historic_snapshots_are_not_rewritten(self):
        pk = self.start().json()["id"]
        frozen = SupplierEvaluation.objects.get(pk=pk).template_snapshot
        EvaluationTemplateCriterion.objects.filter(pk=self.w.c1.pk).update(weight=Decimal("40"))
        self.assertEqual(SupplierEvaluation.objects.get(pk=pk).template_snapshot, frozen)

    # ----- persistence and recalculation
    def test_score_and_rating_are_persisted_on_every_save(self):
        pk = self.start().json()["id"]
        c1, c2, c3 = self.ids(pk)
        self.assertIsNone(SupplierEvaluation.objects.get(pk=pk).overall_score)
        self.answer(pk, {"criterion_id": c1, "score": 4}, {"criterion_id": c3, "score": 5})
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        # sections: Quality = 80 (c1 only scored), Delivery = 100 -> simple mean 90
        self.assertEqual((evaluation.overall_score, evaluation.rating), (Decimal("90.0"), "EXCELLENT"))
        self.answer(pk, {"criterion_id": c3, "score": 1, "comment": "Always late"})
        evaluation.refresh_from_db()
        self.assertEqual((evaluation.overall_score, evaluation.rating), (Decimal("50.0"), "IMPROVEMENT_REQUIRED"))
        self.answer(pk, {"criterion_id": c1}, {"criterion_id": c3})  # clear everything
        evaluation.refresh_from_db()
        self.assertIsNone(evaluation.overall_score)
        self.assertIsNone(evaluation.rating)

    def test_failed_save_leaves_the_stored_score_untouched(self):
        pk = self.start().json()["id"]
        c1, c2, c3 = self.ids(pk)
        self.answer(pk, {"criterion_id": c1, "score": 5})
        self.assertEqual(self.answer(pk, {"criterion_id": c3, "score": 1}).status_code, 400)  # comment missing
        self.assertEqual(SupplierEvaluation.objects.get(pk=pk).overall_score, Decimal("100.0"))

    def test_approved_and_closed_evaluations_are_not_recalculated(self):
        pk = self.start().json()["id"]
        c1 = self.ids(pk)[0]
        self.answer(pk, {"criterion_id": c1, "score": 5})
        for status in ("APPROVED", "CLOSED"):
            SupplierEvaluation.objects.filter(pk=pk).update(status=status, overall_score=Decimal("77.7"), rating="GOOD")
            evaluation = recalculate_evaluation_score(SupplierEvaluation.objects.get(pk=pk))
            self.assertEqual((evaluation.overall_score, evaluation.rating), (Decimal("77.7"), "GOOD"), status)
            payload = self.detail(pk)["scoring"]
            self.assertEqual((payload["overall_score"], payload["rating"], payload["provisional"]), (77.7, "GOOD", False))
            self.assertEqual(self.answer(pk, {"criterion_id": c1, "score": 1, "comment": "x"}).status_code, 400)
        evaluation.refresh_from_db()
        self.assertEqual(evaluation.overall_score, Decimal("77.7"))

    # ----- synthesis payload
    def test_detail_exposes_scoring_progress_and_synthesis_separately(self):
        pk = self.start().json()["id"]
        c1, c2, c3 = self.ids(pk)
        empty = self.detail(pk)
        self.assertIsNone(empty["scoring"]["overall_score"])
        self.assertIsNone(empty["scoring"]["rating"])
        self.assertTrue(empty["scoring"]["provisional"])
        self.assertEqual(empty["progress"]["answered"], 0)
        self.assertEqual({r["criterion_id"] for r in empty["scoring"]["required_remaining"]}, {c1, c3})
        self.answer(pk, {"criterion_id": c1, "score": 2, "comment": "Gaps"}, {"criterion_id": c2, "score": 5},
                    {"criterion_id": c3, "not_applicable": True})
        body = self.detail(pk)
        scoring = body["scoring"]
        self.assertEqual(scoring["overall_score"], 70.0)  # Quality = mean(40, 100) = 70 ; Delivery has no score, so it is excluded
        self.assertEqual(scoring["rating"], "GOOD")
        self.assertTrue(scoring["provisional"])
        self.assertEqual([s["score"] for s in scoring["sections"]], [70.0, None])
        self.assertEqual([i["criterion_id"] for i in scoring["strengths"]], [c2])
        self.assertEqual([i["criterion_id"] for i in scoring["improvements"]], [c1])
        self.assertEqual([i["criterion_id"] for i in scoring["critical_points"]], [c1])  # c1 is critical
        self.assertEqual(scoring["required_remaining"], [])
        self.assertEqual(body["progress"]["answered"], 3)

    def test_list_exposes_the_stored_score(self):
        pk = self.start().json()["id"]
        self.answer(pk, {"criterion_id": self.ids(pk)[0], "score": 5})
        row = self.api(self.w.manager).get(EVALS).json()["results"][0]
        self.assertEqual((row["overall_score"], row["rating"]), (100.0, "EXCELLENT"))

    # ----- isolation
    def test_scoring_is_not_readable_across_companies(self):
        pk = self.start().json()["id"]
        self.answer(pk, {"criterion_id": self.ids(pk)[0], "score": 5})
        self.assertEqual(self.api(self.w.manager_b).get(f"{EVALS}/{pk}").status_code, 404)
        self.assertEqual(self.api(self.w.nobody).get(f"{EVALS}/{pk}").status_code, 403)
        self.assertEqual(self.api(self.w.manager_b).get(EVALS).json()["count"], 0)
        self.assertEqual(self.answer(pk, {"criterion_id": self.ids(pk)[0], "score": 1, "comment": "x"}, user=self.w.manager_b).status_code, 404)
        self.assertEqual(SupplierEvaluation.objects.get(pk=pk).overall_score, Decimal("100.0"))
