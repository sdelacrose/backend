"""Manual intervention creation and company surveillance template access regression checks."""
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate
from project_management.models import ActionItem, InterventionRequest, SupplierSurveillanceTemplate
from project_management.test_intervention_plans_b import ImportedMotherFixture
from project_management.views.supplier_surveillance_template import SupplierSurveillanceTemplateViewSet

class ManualInterventionTests(ImportedMotherFixture, TestCase):
    def body(self, **extra):
        return {"name": "Manual intervention", "project": self.project.id,
                "target_date": "2026-12-01", "status": "open", "assignees": [self.owner.id],
                "create_intervention": True, **extra}

    def test_manual_creation_links_a_request_and_keeps_classic_actions_separate(self):
        before = InterventionRequest.objects.count()
        response = self.api("post", "actionitem-list", body=self.body())
        self.assertEqual(response.status_code, 201, response.data)
        mother = ActionItem.objects.get(pk=response.data["id"])
        request = mother.intervention_request
        self.assertIsNotNone(request)
        self.assertEqual(InterventionRequest.objects.count(), before + 1)
        self.assertEqual(request.source, "manual")
        self.assertEqual(request.project_id, self.project.id)
        self.assertEqual(request.title, "Manual intervention")
        self.assertEqual(str(request.expected_end_date), "2026-12-01")
        self.assertTrue(request.reference.startswith("IR-"))
        self.assertEqual(mother.intervention_action_key, "mother")
        second = self.api("post", "actionitem-list", body=self.body())
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(ActionItem.objects.get(pk=second.data["id"]).intervention_request.reference, request.reference)
        classic = self.api("post", "actionitem-list", body={"name": "Classic action", "project": self.project.id, "milestone": self.phase.id, "target_date": "2026-05-01", "status": "open", "assignees": [self.owner.id]})
        self.assertEqual(classic.status_code, 201, classic.data)
        self.assertIsNone(ActionItem.objects.get(pk=classic.data["id"]).intervention_request_id)

    def test_a_manual_sub_action_inherits_intervention_membership(self):
        response = self.api("post", "actionitem-list", body=self.body())
        self.assertEqual(response.status_code, 201, response.data)
        mother = ActionItem.objects.get(pk=response.data["id"])
        sub = self.add_sub(parent=mother, progress_setting={"in_progress_points": 10, "closed_points": 100})
        self.assertEqual(sub.status_code, 201, sub.data)
        from project_management.services.action_progress import is_intervention_action
        self.assertTrue(is_intervention_action(ActionItem.objects.get(pk=sub.data["id"])))

    def test_missing_deadline_is_rejected_without_creating_a_request(self):
        before = InterventionRequest.objects.count()
        response = self.api("post", "actionitem-list", body=self.body(target_date=None))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(InterventionRequest.objects.count(), before)

    def test_manual_creation_still_requires_create_permission(self):
        self.client.force_authenticate(self.owner)
        before = InterventionRequest.objects.count()
        response = self.client.post(reverse("actionitem-list"), self.body(), format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(InterventionRequest.objects.count(), before)

class CompanyTemplateAccessTests(ImportedMotherFixture, TestCase):
    @property
    def creator(self):
        return self.user

    def make_template(self):
        return SupplierSurveillanceTemplate.objects.create(company=self.company, name="Own template", created_by=self.user)

    def request(self, method="get", *, company=None, data=None, pk=None):
        factory = APIRequestFactory()
        company_id = company if company is not None else self.creator.company_id
        body = {"company": company_id, **(data or {})}
        request = getattr(factory, method)("/templates/", body, format="json")
        force_authenticate(request, self.creator)
        action = "list" if method == "get" else "create" if method == "post" else "partial_update"
        view = SupplierSurveillanceTemplateViewSet.as_view({method: action})
        return view(request, **({"pk": pk} if pk else {}))

    def test_company_list_needs_no_project_and_excludes_other_companies(self):
        own = self.make_template()
        with patch("companyprofileapis.permissions.CompanyConfigurationsPermission._check_view_permission", return_value=True):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", []) if isinstance(response.data, dict) else response.data
        self.assertIn(own.id, [row["id"] for row in rows])
        other_company = self.company.id + 999
        if other_company == self.creator.company_id:
            other_company += 999
        with patch("companyprofileapis.permissions.CompanyConfigurationsPermission._check_view_permission", return_value=True):
            denied = self.request(company=other_company)
        self.assertEqual(denied.status_code, 404)

    def test_company_permissions_are_required_and_checked_for_writes(self):
        with patch("companyprofileapis.permissions.CompanyConfigurationsPermission._check_view_permission", return_value=False):
            self.assertEqual(self.request().status_code, 403)
        before = SupplierSurveillanceTemplate.objects.count()
        with patch("companyprofileapis.permissions.CompanyConfigurationsPermission._check_update_permission", return_value=False):
            denied = self.request("post", data={"name": "Denied"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(SupplierSurveillanceTemplate.objects.count(), before)

    def test_create_and_patch_are_company_scoped_without_project(self):
        with patch("companyprofileapis.permissions.CompanyConfigurationsPermission._check_update_permission", return_value=True):
            created = self.request("post", data={"name": "Company template", "sections": []})
            self.assertEqual(created.status_code, 201, created.data)
            template = SupplierSurveillanceTemplate.objects.get(pk=created.data["id"])
            self.assertEqual(template.company_id, self.creator.company_id)
            updated = self.request("patch", pk=template.id, data={"name": "Updated company template"})
        self.assertEqual(updated.status_code, 200, updated.data)
        template.refresh_from_db()
        self.assertEqual(template.name, "Updated company template")
