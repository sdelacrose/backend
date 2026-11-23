# QUALITY Q6: PDF report filed as a LINKS Document, revisions, download, rights and isolation.
import shutil
import tempfile
from unittest.mock import patch

from django.db.models.signals import post_save, pre_delete
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from models.models import Permission, Role, RolePermission, UserRole
from project_management.models import (
    ContactCompany,
    Document,
    DocumentRevision,
    Folder,
    SupplierEvaluation,
    SupplierEvaluationReport,
)
from project_management.services import supplier_evaluation_report as report_service
from project_management.signals import notify_on_contact_company_delete, notify_on_contact_company_save
from project_management.test_document_revision import NoNotificationsMixin
from project_management.test_quality_q3 import EVALS, World, answers_url, payload

S = SupplierEvaluation.Status
MEDIA = tempfile.mkdtemp(prefix="q6-media-")


def report_url(pk, suffix=""):
    return f"{EVALS}/{pk}/report{suffix}"


def give(user, company, *keys):
    """ONE role carrying every key (only a user's first role counts for permission checks)."""
    role = Role.objects.create(name=f"Q6 {user.id}", company=company, type="solution", scope="company")
    for key in keys:
        permission, _ = Permission.objects.get_or_create(
            key=key, defaults={"name": key, "module": key.split(".")[0], "description": "test"})
        RolePermission.objects.create(role=role, permission=permission)
    UserRole.objects.create(user=user, role=role, company=company, assigned_by=user)


FULL = ("quality.view", "quality.evaluation_manage", "documents.view", "documents.create")


@override_settings(MEDIA_ROOT=MEDIA)
class QualityQ6Tests(NoNotificationsMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

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
        company = self.w.company_a
        self.owner = self.w.member("q6.owner", company)
        self.reviewer = self.w.member("q6.reviewer", company)
        self.no_docs = self.w.member("q6.nodocs", company)
        self.no_quality = self.w.member("q6.noquality", company)
        self.other = self.w.member("q6.other", self.w.company_b)
        give(self.owner, company, *FULL)
        give(self.reviewer, company, *FULL)
        give(self.no_docs, company, "quality.view", "quality.evaluation_manage")
        give(self.no_quality, company, "documents.view", "documents.create")
        give(self.other, self.w.company_b, *FULL)

    def api(self, user=None):
        client = APIClient()
        if user is not False:
            client.force_authenticate(user or self.owner)
        return client

    def post(self, pk, action, user=None, **body):
        return self.api(user).post(f"{EVALS}/{pk}/{action}", body, format="json")

    def approved(self, **extra):
        """An evaluation taken all the way to APPROVED through the real API."""
        response = self.api().post(EVALS, payload(self.w, **extra), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        pk = response.json()["id"]
        sections = self.api().get(f"{EVALS}/{pk}").json()["snapshot"]["sections"]
        c1, c2, c3 = [c["id"] for s in sections for c in s["criteria"]]
        answers = [{"criterion_id": c1, "score": 5, "comment": "Fully traceable"},
                   {"criterion_id": c2, "not_applicable": True},
                   {"criterion_id": c3, "score": 4}]
        self.assertEqual(self.api().put(answers_url(pk), {"answers": answers}, format="json").status_code, 200)
        self.api().put(f"{EVALS}/{pk}/reviewer", {"reviewer": self.reviewer.id}, format="json")
        self.assertEqual(self.post(pk, "submit").status_code, 200)
        self.post(pk, "start_review", self.reviewer)
        self.assertEqual(self.post(pk, "approve", self.reviewer, comment="Solid supplier").status_code, 200)
        return pk

    def generate(self, pk, user=None, language="en"):
        return self.api(user).post(report_url(pk), {"language": language}, format="json")

    # ----------------------------------------------------------------------------- gating
    def test_not_available_before_approval_and_status_never_changes(self):
        pk = self.api().post(EVALS, payload(self.w), format="json").json()["id"]
        for status in (S.DRAFT, S.IN_PROGRESS, S.SUBMITTED, S.UNDER_REVIEW, S.REJECTED):
            SupplierEvaluation.objects.filter(pk=pk).update(status=status)
            self.assertEqual(self.generate(pk).status_code, 400, status)
            self.assertFalse(self.api().get(f"{EVALS}/{pk}").json()["report"]["can_generate"], status)
            self.assertEqual(SupplierEvaluation.objects.get(pk=pk).status, status)
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(SupplierEvaluationReport.objects.count(), 0)

    def test_generation_after_approval_and_closure_leaves_the_workflow_untouched(self):
        pk = self.approved()
        before = SupplierEvaluation.objects.values().get(pk=pk)
        self.assertTrue(self.api().get(f"{EVALS}/{pk}").json()["report"]["can_generate"])
        self.assertEqual(self.generate(pk).status_code, 201)
        self.assertEqual(SupplierEvaluation.objects.values().get(pk=pk), before)  # not a single column changed
        self.assertEqual(self.post(pk, "close").status_code, 200)
        closed = SupplierEvaluation.objects.values().get(pk=pk)
        self.assertEqual(self.generate(pk).status_code, 201)
        self.assertEqual(SupplierEvaluation.objects.values().get(pk=pk), closed)
        self.assertEqual(SupplierEvaluation.objects.get(pk=pk).events.filter(event_type="CLOSED").count(), 1)

    # ----------------------------------------------------------------------- documents
    def test_document_folder_first_revision_and_stable_link(self):
        pk = self.approved()
        result = self.generate(pk).json()
        self.assertTrue(result["exists"])
        document = Document.objects.get(pk=result["document_id"])
        self.assertEqual(document.folder.name, "Quality Reports")
        self.assertEqual(document.folder.company_profile_id, self.w.company_a.id)
        self.assertEqual(document.folder.parent.name, f"{self.w.company_a.id}_company_root_folder")
        self.assertEqual(document.file_name, f"{SupplierEvaluation.objects.get(pk=pk).reference} - Rapport.pdf")
        revisions = DocumentRevision.objects.filter(document=document)
        self.assertEqual(revisions.count(), 1)
        self.assertTrue(revisions.first().file.name.endswith(".pdf"))
        self.assertEqual(SupplierEvaluationReport.objects.get(evaluation_id=pk).document_id, document.id)

    def test_regeneration_adds_a_revision_to_the_same_document(self):
        pk = self.approved()
        first = self.generate(pk).json()
        second = self.generate(pk).json()
        self.assertEqual(first["document_id"], second["document_id"])
        self.assertEqual((Document.objects.count(), SupplierEvaluationReport.objects.count()), (1, 1))
        revisions = list(DocumentRevision.objects.filter(document_id=first["document_id"]).order_by("id"))
        self.assertEqual(len(revisions), 2)
        self.assertNotEqual((revisions[0].major_revision, revisions[0].minor_revision),
                            (revisions[1].major_revision, revisions[1].minor_revision))
        self.assertEqual(second["revision_count"], 2)
        document = Document.objects.get(pk=first["document_id"])
        self.assertEqual(document.file.name, revisions[1].file.name)  # current file follows the latest revision

    def test_two_evaluations_get_two_documents_in_the_same_folder(self):
        a, b = self.approved(), self.approved()
        da, db = self.generate(a).json()["document_id"], self.generate(b).json()["document_id"]
        self.assertNotEqual(da, db)
        self.assertEqual(Folder.objects.filter(name="Quality Reports").count(), 1)

    # ---------------------------------------------------------------------------- content
    def context(self, pk, language="en"):
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        return report_service.build_report_context(evaluation, language, evaluation.updated_at)

    def test_pdf_is_a_real_pdf_and_complete(self):
        pk = self.approved(follows_fnc=True, fnc_reference="FNC-2026-001")
        self.generate(pk)
        pdf = b"".join(self.api().get(report_url(pk, "/download")).streaming_content)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 2000)
        try:
            from io import BytesIO
            from pypdf import PdfReader
        except ImportError:  # the reader is only in the document worker image
            return
        text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
        for expected in ("EVAL-", "Supplier A", "PO-A-1", "FNC-2026-001", "Traceability", "On time",
                         "Fully traceable", "Solid supplier", "Strengths"):
            self.assertIn(expected, text)

    def test_content_comes_from_the_snapshot_not_the_live_template(self):
        pk = self.approved()
        self.w.template.name = "Renamed live"
        self.w.template.save()
        self.w.c1.label = "Changed live label"
        self.w.c1.save()
        context = self.context(pk)
        labels = [row["label"] for section in context["sections"] for row in section["rows"]]
        self.assertIn("Traceability", labels)
        self.assertNotIn("Changed live label", labels)
        self.assertNotIn("Renamed live", context["template"])

    def test_score_and_rating_are_those_shown_on_screen(self):
        pk = self.approved()
        screen = self.api().get(f"{EVALS}/{pk}").json()["scoring"]
        context = self.context(pk)
        self.assertEqual(context["score"], f"{screen['overall_score']:.1f}")
        self.assertEqual(context["rating_code"], screen["rating"])
        shown = {s["title"]: s["score"] for s in context["sections"]}
        self.assertEqual(shown["Quality"], f"{screen['sections'][0]['score']:.1f}")

    def test_na_is_printed_and_fnc_only_when_applicable(self):
        pk = self.approved()
        context = self.context(pk)
        self.assertIn("N/A", [row["answer"] for s in context["sections"] for row in s["rows"]])
        self.assertIsNone(context["fnc_reference"])
        with_fnc = self.approved(follows_fnc=True, fnc_reference="FNC-9")
        self.assertEqual(self.context(with_fnc)["fnc_reference"], "FNC-9")

    def test_workflow_people_dates_conclusion_and_languages(self):
        pk = self.approved()
        context = self.context(pk)
        self.assertTrue(context["owner"] and context["reviewer"] and len(context["steps"]) >= 3)
        self.assertEqual(context["reviewer_comment"], "Solid supplier")
        self.assertIn("/ 100", context["conclusion"])
        self.assertTrue(context["strengths"] and context["labels"]["title"])
        for language in ("fr", "de"):
            self.assertNotEqual(self.context(pk, language)["labels"]["title"], context["labels"]["title"])
            self.assertEqual(self.generate(pk, language=language).status_code, 201)
        self.assertEqual(self.generate(pk, language="xx").status_code, 201)  # unknown language falls back to English

    # ------------------------------------------------------------------------------ rights
    def test_rights_and_isolation(self):
        pk = self.approved()
        self.assertEqual(self.generate(pk, user=False).status_code, 401)
        self.assertEqual(self.generate(pk, self.no_docs).status_code, 403)  # no Documents right
        self.assertEqual(self.generate(pk, self.no_quality).status_code, 403)  # no Quality right
        self.assertEqual(self.generate(pk, self.other).status_code, 404)  # other company
        self.assertEqual(Document.objects.count(), 0)
        self.generate(pk)
        for user, status in ((self.no_docs, 403), (self.no_quality, 403), (self.other, 404)):
            self.assertEqual(self.api(user).get(report_url(pk, "/download")).status_code, status)
        self.assertEqual(self.api(False).get(report_url(pk, "/download")).status_code, 401)
        self.assertEqual(self.api(self.other).get(report_url(pk)).status_code, 404)
        flags = self.api(self.no_docs).get(f"{EVALS}/{pk}").json()["report"]
        self.assertFalse(flags["can_view"])
        self.assertFalse(flags["can_generate"])

    def test_download_is_the_current_revision_and_never_cached(self):
        pk = self.approved()
        self.generate(pk)
        self.generate(pk)
        response = self.api().get(report_url(pk, "/download"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        inline = self.api().get(report_url(pk, "/download") + "?inline=1")
        self.assertIn("inline", inline["Content-Disposition"])
        self.assertEqual(self.api().get(report_url(self.approved(), "/download")).status_code, 404)  # nothing yet

    # ---------------------------------------------------------------------------- failures
    def test_rendering_failure_leaves_nothing_behind(self):
        pk = self.approved()
        before = SupplierEvaluation.objects.values().get(pk=pk)
        with patch.object(report_service, "render_report_pdf", side_effect=RuntimeError("pdf")):
            self.assertEqual(self.generate(pk).status_code, 503)
        self.assertEqual((Document.objects.count(), SupplierEvaluationReport.objects.count(),
                          DocumentRevision.objects.count()), (0, 0, 0))
        self.assertEqual(SupplierEvaluation.objects.values().get(pk=pk), before)

    def test_storage_failure_rolls_back_document_and_link(self):
        pk = self.approved()
        before = SupplierEvaluation.objects.values().get(pk=pk)
        with patch.object(report_service, "add_document_revision", side_effect=OSError("storage down")):
            self.assertEqual(self.generate(pk).status_code, 503)
        self.assertEqual((Document.objects.count(), SupplierEvaluationReport.objects.count()), (0, 0))
        self.assertEqual(SupplierEvaluation.objects.values().get(pk=pk), before)
        self.assertEqual(self.generate(pk).status_code, 201)  # and a retry works

    def test_failed_regeneration_keeps_the_existing_report(self):
        pk = self.approved()
        first = self.generate(pk).json()
        with patch.object(report_service, "add_document_revision", side_effect=OSError("storage down")):
            self.assertEqual(self.generate(pk).status_code, 503)
        self.assertEqual(DocumentRevision.objects.filter(document_id=first["document_id"]).count(), 1)
        self.assertEqual(self.api().get(report_url(pk)).json()["revision_count"], 1)

    # ------------------------------------------------------------------------------ Q6.2
    def html(self, pk, language="en", tz=None):
        from django.template.loader import render_to_string

        evaluation = SupplierEvaluation.objects.get(pk=pk)
        context = report_service.build_report_context(evaluation, language, evaluation.updated_at, tz)
        return render_to_string("supplier_evaluation_report.html", context)

    def test_section_table_has_its_own_headers(self):
        pk = self.approved()
        for language, section, score in (("en", "Section", "Score"), ("fr", "Section", "Score"), ("de", "Abschnitt", "Punktzahl")):
            page = self.html(pk, language)
            heading = self.context(pk, language)["labels"]["sections"]
            block = next(b for b in page.split("<h2>") if b.startswith(heading))
            self.assertIn(f">{section}</th>", block)
            self.assertIn(f">{score}</th>", block)
            self.assertNotIn(self.context(pk, language)["labels"]["criterion"], block.split("</thead>")[0])
            self.assertNotIn(self.context(pk, language)["labels"]["score"] + "</th>", block.split("</thead>")[0])

    def test_dates_are_localized_and_always_carry_their_time_zone(self):
        pk = self.approved()
        tokyo = self.context_tz(pk, "fr", "Asia/Tokyo")
        paris = self.context_tz(pk, "fr", "Europe/Paris")
        self.assertIn("UTC+09:00", tokyo["steps"][0]["when"])
        self.assertRegex(paris["steps"][0]["when"], r"UTC\+0[12]:00")
        self.assertNotEqual(tokyo["steps"][0]["when"], paris["steps"][0]["when"])
        self.assertEqual(tokyo["timezone"], "Asia/Tokyo")
        self.assertIn("novembre", tokyo["steps"][0]["when"])
        self.assertIn("November", self.context_tz(pk, "de", "UTC")["steps"][0]["when"])
        self.assertIn("November", self.context_tz(pk, "en", "UTC")["steps"][0]["when"])
        for bad in (None, "", "Not/AZone", "../../etc"):  # nothing is guessed: UTC, and it says so
            context = self.context_tz(pk, "en", bad)
            self.assertEqual(context["timezone"], "UTC")
            self.assertIn("UTC+00:00", context["steps"][0]["when"])
            self.assertIn("UTC+00:00", context["generated_at"])

    def context_tz(self, pk, language, tz):
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        return report_service.build_report_context(evaluation, language, evaluation.updated_at, tz)

    def test_validation_steps_never_concatenate_actor_and_date(self):
        pk = self.approved()
        steps = self.context(pk)["steps"]
        self.assertEqual([s["step"] for s in steps], ["Submitted", "Review started", "Approved"])
        for step in steps:
            self.assertEqual(set(step), {"step", "actor", "when"})
            self.assertNotIn(step["actor"], step["when"])
        page = self.html(pk)
        self.assertIn("<th", page.split("Step</th>")[1] if "Step</th>" in page else "")
        self.assertIn(">Actor</th>", page)
        self.assertIn(">Date and time</th>", page)

    def test_numbers_are_localized_without_changing_the_engine_values(self):
        pk = self.approved()
        screen = self.api().get(f"{EVALS}/{pk}").json()["scoring"]
        en, fr, de = (self.context(pk, lang) for lang in ("en", "fr", "de"))
        self.assertEqual(en["score"], f"{screen['overall_score']:.1f}")
        self.assertEqual(fr["score"], en["score"].replace(".", ","))
        self.assertEqual(de["score"], fr["score"])
        self.assertIn(",", fr["score"])
        self.assertEqual(fr["sections"][0]["score"], f"{screen['sections'][0]['score']:.1f}".replace(".", ","))

    def test_conclusion_separates_scored_na_and_unanswered(self):
        pk = self.approved()  # c1 scored, c2 N/A, c3 scored
        self.assertIn("3 criteria: 2 scored, 1 N/A, 0 not answered", self.context(pk)["counts"])
        self.assertIn("Sur 3 critères : 2 noté(s), 1 N/A, 0 sans réponse", self.context(pk, "fr")["counts"])
        self.assertIn("Von 3 Kriterien: 2 bewertet, 1 N/A, 0 nicht beantwortet", self.context(pk, "de")["counts"])
        from project_management.models import EvaluationResponse

        EvaluationResponse.objects.filter(evaluation_id=pk, not_applicable=True).delete()
        self.assertIn("2 scored, 0 N/A, 1 not answered", self.context(pk)["counts"])

    def test_report_is_multipage_with_long_content_and_repeated_headers(self):
        pk = self.approved()
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        sections = []
        for s in range(1, 5):
            sections.append({
                "id": s, "title": f"Section {s} - évaluation très détaillée des capacités", "weight": "25",
                "criteria": [{"id": s * 100 + c, "label": f"Critère {s}.{c} " + "libellé long " * 6, "weight": "10",
                              "required": c % 2 == 0, "critical": c == 4, "allow_na": True,
                              "comment_required_below": None, "description": ""} for c in range(1, 11)],
            })
        snapshot = {**evaluation.template_snapshot, "sections": sections}
        SupplierEvaluation.objects.filter(pk=pk).update(template_snapshot=snapshot)
        from project_management.models import EvaluationResponse

        EvaluationResponse.objects.filter(evaluation_id=pk).delete()
        for section in sections:
            for criterion in section["criteria"]:
                EvaluationResponse.objects.create(
                    evaluation_id=pk, criterion_id=criterion["id"], score=3 + criterion["id"] % 3,
                    comment="Commentaire accentué très long : " + "écarts constatés, qualité à surveiller. " * 12)
        evaluation = SupplierEvaluation.objects.get(pk=pk)
        pdf = report_service.render_report_pdf(evaluation, "fr", evaluation.updated_at, "Europe/Paris")
        self.assertTrue(pdf.startswith(b"%PDF"))
        from weasyprint import HTML

        pages = len(HTML(string=self.html(pk, "fr", "Europe/Paris")).render().pages)
        self.assertGreaterEqual(pages, 3)
        self.assertIn("<thead>", self.html(pk))  # table headers repeat on every page

    # ------------------------------------------------------------------------------ Q6.3
    def test_rating_badge_follows_the_interface_palette_and_lists_are_bulleted(self):
        pk = self.approved()
        page = self.html(pk)
        code = self.context(pk)["rating_code"]
        self.assertIn(f'class="badge {code}"', page)
        for rating, colors in (("EXCELLENT", "#e7f6ec"), ("GOOD", "#bdefe0"),
                               ("IMPROVEMENT_REQUIRED", "#fef6e7"), ("UNSATISFACTORY", "#fbeae9")):
            self.assertIn(f".badge.{rating} {{ background: {colors};", page)
        self.assertIn("#865503", page)  # amber text of IMPROVEMENT_REQUIRED, as in ratingColors.ts
        self.assertIn('<ul class="items">', page)  # strengths / improvements are real bulleted lists
        self.assertIn("<li>", page)
        # thresholds and rating values are untouched by the template
        self.assertEqual(self.context(pk)["rating_code"], self.api().get(f"{EVALS}/{pk}").json()["scoring"]["rating"])
