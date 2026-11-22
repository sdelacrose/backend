# QUALITY Q2: evaluation templates -- permissions, isolation, validation, ordering, duplication, snapshot.
import copy
import json

from django.db.models.signals import post_save, pre_delete
from django.test import TestCase

from project_management.models import (
    ContactCompany,
    EvaluationTemplate,
    EvaluationTemplateCriterion,
    EvaluationTemplateSection,
    SupplierEvaluation,
)
from project_management.services.supplier_evaluation_template import build_template_snapshot
from project_management.signals import (
    notify_on_contact_company_delete,
    notify_on_contact_company_save,
)
from project_management.test_document_revision import NoNotificationsMixin
from project_management.test_quality_q1 import grant, make_company
from rest_framework.test import APIClient
from unittest.mock import patch

from models.models import User

LIST = "/api/project_management/quality/templates"
ACCESS = "/api/project_management/quality/access"


def detail(pk):
    return f"{LIST}/{pk}"


def payload(name="Supplier audit", **extra):
    return {
        "name": name,
        "description": "Yearly",
        "comment_required_below": 3,
        "sections": [
            {"title": "Quality", "weight": "60", "criteria": [
                {"label": "Traceability", "weight": "50", "critical": True},
                {"label": "Packaging", "required": False, "allow_na": True},
            ]},
            {"title": "Delivery", "criteria": [{"label": "On time", "allow_na": False}]},
        ],
        **extra,
    }


class QualityQ2Tests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        for signal, receiver in (
            (post_save, notify_on_contact_company_save),
            (pre_delete, notify_on_contact_company_delete),
        ):
            signal.disconnect(receiver, sender=ContactCompany)
            self.addCleanup(signal.connect, receiver, sender=ContactCompany)
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.admin_a = self._user("q2a.admin@links-local.invalid")
        self.company_a = make_company(self.admin_a, "Q2A")
        self.admin_b = self._user("q2b.admin@links-local.invalid")
        self.company_b = make_company(self.admin_b, "Q2B")
        self.manager = self._member("q2.manager", self.company_a, "view", "template_manage")
        self.full = self._member("q2.full", self.company_a, "full_access")
        self.viewer = self._member("q2.viewer", self.company_a, "view")
        self.nobody = self._member("q2.nobody", self.company_a)
        self.manager_b = self._member("q2b.manager", self.company_b, "view", "template_manage")

    def _user(self, email):
        return User.objects.create_user(
            email=email, password="x", first_name="T", last_name="U", user_type="company_user"
        )

    def _member(self, tag, company, *actions):
        user = self._user(f"{tag}@links-local.invalid")
        user.company = company
        user.save(update_fields=["company"])
        if actions:
            grant(user, company, *actions)
        return user

    def api(self, user=None):
        client = APIClient()
        if user:
            client.force_authenticate(user)
        return client

    def create(self, user=None, **kwargs):
        return self.api(user or self.manager).post(LIST, kwargs.pop("data", payload()), format="json")

    # ----- creation, sections, criteria, order
    def test_create_builds_sections_and_criteria_in_payload_order(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["is_active"])
        self.assertEqual([s["title"] for s in body["sections"]], ["Quality", "Delivery"])
        self.assertEqual([s["order"] for s in body["sections"]], [0, 1])
        quality = body["sections"][0]
        self.assertEqual([c["label"] for c in quality["criteria"]], ["Traceability", "Packaging"])
        self.assertEqual([c["order"] for c in quality["criteria"]], [0, 1])
        self.assertEqual((body["sections_count"], body["criteria_count"]), (2, 3))
        self.assertEqual(EvaluationTemplate.objects.get().company, self.company_a)

    def test_reordering_is_persisted(self):
        pk = self.create().json()["id"]
        body = self.api(self.manager).get(detail(pk)).json()
        body["sections"].reverse()
        body["sections"][1]["criteria"].reverse()
        updated = self.api(self.manager).patch(detail(pk), {"sections": body["sections"]}, format="json").json()
        self.assertEqual([s["title"] for s in updated["sections"]], ["Delivery", "Quality"])
        self.assertEqual([c["label"] for c in updated["sections"][1]["criteria"]], ["Packaging", "Traceability"])
        self.assertEqual([s["order"] for s in updated["sections"]], [0, 1])

    def test_criterion_flags_and_na(self):
        sections = self.create().json()["sections"]
        traceability, packaging = sections[0]["criteria"]
        self.assertTrue(traceability["critical"])
        self.assertTrue(traceability["required"])  # critical forces required
        self.assertFalse(packaging["required"])
        self.assertTrue(packaging["allow_na"])
        self.assertFalse(sections[1]["criteria"][0]["allow_na"])

    def test_critical_cannot_stay_optional(self):
        data = payload()
        data["sections"][0]["criteria"][0].update({"critical": True, "required": False})
        criterion = self.create(data=data).json()["sections"][0]["criteria"][0]
        self.assertTrue(criterion["required"])

    # ----- validation
    def test_invalid_weights_are_rejected(self):
        for weight in ("0", "-1", "100.01", "abc"):
            data = payload()
            data["sections"][0]["criteria"][0]["weight"] = weight
            self.assertEqual(self.create(data=data).status_code, 400, weight)
            data = payload()
            data["sections"][0]["weight"] = weight
            self.assertEqual(self.create(data=data).status_code, 400, weight)
        self.assertEqual(EvaluationTemplate.objects.count(), 0)

    def test_valid_and_missing_weights(self):
        for weight in ("0.01", "100", None):
            data = payload(name=f"w-{weight}")
            data["sections"][0]["criteria"][0]["weight"] = weight
            self.assertEqual(self.create(data=data).status_code, 201, weight)

    def test_invalid_comment_threshold(self):
        for threshold in (0, 1, 6):
            self.assertEqual(self.create(data=payload(comment_required_below=threshold)).status_code, 400)
        data = payload()
        data["sections"][0]["criteria"][0]["comment_required_below"] = 6
        self.assertEqual(self.create(data=data).status_code, 400)
        self.assertEqual(self.create(data=payload(name="none", comment_required_below=None)).status_code, 201)

    def test_blank_names_and_titles_are_rejected(self):
        self.assertEqual(self.create(data=payload(name="  ")).status_code, 400)
        data = payload()
        data["sections"][0]["title"] = " "
        self.assertEqual(self.create(data=data).status_code, 400)
        data = payload()
        data["sections"][0]["criteria"][0]["label"] = ""
        self.assertEqual(self.create(data=data).status_code, 400)

    def test_duplicate_name_is_rejected_per_company_only(self):
        self.assertEqual(self.create().status_code, 201)
        self.assertEqual(self.create(data=payload(name="SUPPLIER AUDIT")).status_code, 400)
        self.assertEqual(self.create(self.manager_b).status_code, 201)  # other company: no clash

    # ----- permissions
    def test_permissions(self):
        self.assertEqual(self.create(self.viewer).status_code, 403)
        self.assertEqual(self.create(self.nobody).status_code, 403)
        self.assertEqual(self.create(self.full).status_code, 201)
        pk = EvaluationTemplate.objects.get().id
        self.assertEqual(self.api(self.viewer).get(LIST).status_code, 200)
        self.assertEqual(self.api(self.viewer).get(detail(pk)).status_code, 200)
        self.assertEqual(self.api(self.viewer).patch(detail(pk), {"name": "x"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.viewer).post(f"{detail(pk)}/duplicate").status_code, 403)
        self.assertEqual(self.api(self.nobody).get(LIST).status_code, 403)
        self.assertEqual(self.api(self.nobody).get(detail(pk)).status_code, 403)
        self.assertIn(self.api().get(LIST).status_code, (401, 403))

    def test_access_endpoint_reports_manage_right(self):
        self.assertTrue(self.api(self.manager).get(ACCESS).json()["can_manage_templates"])
        self.assertTrue(self.api(self.full).get(ACCESS).json()["can_manage_templates"])
        self.assertFalse(self.api(self.viewer).get(ACCESS).json()["can_manage_templates"])

    # ----- isolation
    def test_other_company_templates_are_invisible_and_untouchable(self):
        pk = self.create(self.manager_b).json()["id"]
        self.assertEqual(self.api(self.manager).get(LIST).json()["count"], 0)
        self.assertEqual(self.api(self.manager).get(detail(pk)).status_code, 404)
        self.assertEqual(self.api(self.manager).patch(detail(pk), {"name": "hack"}, format="json").status_code, 404)
        self.assertEqual(self.api(self.manager).post(f"{detail(pk)}/duplicate").status_code, 404)
        self.assertEqual(EvaluationTemplate.objects.get(pk=pk).name, "Supplier audit")

    def test_foreign_section_and_criterion_ids_are_not_trusted(self):
        foreign = self.create(self.manager_b).json()
        own_pk = self.create(data=payload(name="Mine")).json()["id"]
        foreign_section = foreign["sections"][0]
        stolen = {"sections": [{"id": foreign_section["id"], "title": "Stolen",
                                "criteria": [{"id": foreign_section["criteria"][0]["id"], "label": "Stolen"}]}]}
        self.api(self.manager).patch(detail(own_pk), stolen, format="json")
        untouched = EvaluationTemplateSection.objects.get(pk=foreign_section["id"])
        self.assertEqual(untouched.title, "Quality")
        self.assertEqual(EvaluationTemplateCriterion.objects.get(pk=foreign_section["criteria"][0]["id"]).label, "Traceability")

    # ----- activation, versions, duplication
    def test_activation_toggle_keeps_version_and_filters_list(self):
        pk = self.create().json()["id"]
        response = self.api(self.manager).patch(detail(pk), {"is_active": False}, format="json").json()
        self.assertFalse(response["is_active"])
        self.assertEqual(response["version"], 1)
        self.assertEqual(self.api(self.manager).get(LIST, {"is_active": "true"}).json()["count"], 0)
        self.assertEqual(self.api(self.manager).get(LIST, {"is_active": "false"}).json()["count"], 1)
        self.api(self.manager).patch(detail(pk), {"is_active": True}, format="json")
        self.assertEqual(self.api(self.manager).get(LIST, {"is_active": "true"}).json()["count"], 1)

    def test_content_edit_increments_version(self):
        pk = self.create().json()["id"]
        edited = self.api(self.manager).patch(detail(pk), {"description": "New"}, format="json").json()
        self.assertEqual(edited["version"], 2)
        same = self.api(self.manager).patch(detail(pk), {"description": "New"}, format="json").json()
        self.assertEqual(same["version"], 2)

    def test_duplicate_copies_the_tree_independently(self):
        pk = self.create().json()["id"]
        response = self.api(self.manager).post(f"{detail(pk)}/duplicate")
        self.assertEqual(response.status_code, 201, response.content)
        clone = response.json()
        self.assertNotEqual(clone["id"], pk)
        self.assertEqual(clone["name"], "Supplier audit (copy)")
        self.assertEqual((clone["version"], clone["is_active"]), (1, True))
        self.assertEqual(clone["criteria_count"], 3)
        original_ids = set(EvaluationTemplateCriterion.objects.filter(section__template_id=pk).values_list("id", flat=True))
        clone_ids = set(EvaluationTemplateCriterion.objects.filter(section__template_id=clone["id"]).values_list("id", flat=True))
        self.assertFalse(original_ids & clone_ids)
        self.api(self.manager).patch(detail(clone["id"]), {"sections": []}, format="json")
        self.assertEqual(self.api(self.manager).get(detail(pk)).json()["criteria_count"], 3)
        # a second copy of the same name is refused like any duplicate name
        self.assertEqual(self.api(self.manager).post(f"{detail(pk)}/duplicate").status_code, 400)
        self.assertEqual(self.api(self.manager).post(f"{detail(pk)}/duplicate", {"name": "Other"}, format="json").status_code, 201)

    # ----- snapshot preparation
    def test_snapshot_is_json_serialisable_and_ordered(self):
        template = EvaluationTemplate.objects.get(pk=self.create().json()["id"])
        snapshot = build_template_snapshot(template)
        json.dumps(snapshot)
        self.assertEqual([s["title"] for s in snapshot["sections"]], ["Quality", "Delivery"])
        self.assertEqual([x["value"] for x in snapshot["scale"]], [1, 2, 3, 4, 5])
        self.assertEqual(snapshot["not_applicable"], "N/A")
        self.assertEqual(snapshot, build_template_snapshot(template))

    def test_template_changes_never_alter_an_existing_evaluation(self):
        template = EvaluationTemplate.objects.get(pk=self.create().json()["id"])
        supplier = ContactCompany.objects.create(
            company_profile=self.company_a, company_name="S", contact_email="s@a.invalid"
        )
        frozen = build_template_snapshot(template)
        evaluation = SupplierEvaluation.objects.create(
            company=self.company_a, supplier=supplier, template=template,
            template_version=template.version, template_snapshot=copy.deepcopy(frozen),
        )
        self.api(self.manager).patch(detail(template.id), {"name": "Renamed", "sections": []}, format="json")
        self.api(self.manager).patch(detail(template.id), {"is_active": False}, format="json")
        evaluation.refresh_from_db()
        self.assertEqual(evaluation.template_snapshot, frozen)
        self.assertEqual(evaluation.template_version, 1)
        template.delete()
        evaluation.refresh_from_db()
        self.assertIsNone(evaluation.template)
        self.assertEqual(evaluation.template_snapshot, frozen)
