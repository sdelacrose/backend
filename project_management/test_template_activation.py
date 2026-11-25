from django.test import TestCase
from django.urls import reverse

from project_management.models import ActionTemplate, ActionItemProgressSetting
from project_management.services.action_progress import apply_template, find_template, TemplateApplicationError
from project_management.services.intervention_plan import template_plan, clean_plan, default_plan, PlanError
from project_management.test_action_templates import TemplateFixture, allow


class TemplateActivationTests(TemplateFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def test_toggle_round_trip_preserves_template_and_lines(self):
        template = self.make_template()
        original_lines = list(template.sub_actions.values_list("id", flat=True))
        self.assertTrue(template.is_active)
        url = reverse("action-template-detail", args=[template.pk])
        with allow():
            for enabled in [False, True]:
                response = self.client.patch(url, {"is_active": enabled}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["is_active"], enabled)
                template.refresh_from_db()
                self.assertEqual(template.is_active, enabled)
                self.assertEqual(list(template.sub_actions.values_list("id", flat=True)), original_lines)
                listed = self.client.get(reverse("action-templates")).data
                self.assertEqual(listed[0]["is_active"], enabled)
                self.assertEqual(listed[0]["id"], template.pk)

    def test_disabled_template_is_not_matched_or_applied_and_prior_actions_are_unchanged(self):
        parent, template, subs = self.make_parent_with_template()
        settings_before = list(ActionItemProgressSetting.objects.filter(action_item__in=subs).values_list("id", "in_progress_points", "closed_points"))
        # Even an object read before deactivation cannot bypass the persisted flag.
        ActionTemplate.objects.filter(pk=template.pk).update(is_active=False)
        self.assertIsNone(find_template(self.project, template.title))
        self.assertEqual(default_plan(self.project, template.title), {})
        with self.assertRaises(PlanError):
            template_plan(self.project, template.pk)
        with self.assertRaises(PlanError):
            clean_plan(self.project, {"template_id": template.pk, "sub_actions": [{"title": "A", "in_progress_points": 0, "closed_points": 100}]})
        with self.assertRaises(TemplateApplicationError):
            apply_template(self.create_parent("Another"), template, user=self.user)
        self.assertEqual(list(ActionItemProgressSetting.objects.filter(action_item__in=subs).values_list("id", "in_progress_points", "closed_points")), settings_before)
        ActionTemplate.objects.filter(pk=template.pk).update(is_active=True)
        self.assertEqual(find_template(self.project, template.title).pk, template.pk)

    def test_activation_requires_update_and_stays_within_the_company(self):
        template = self.make_template()
        other = self.make_template(title="Other", company=self.other_company)
        with allow(update=False):
            response = self.client.patch(reverse("action-template-detail", args=[template.pk]), {"is_active": False}, format="json")
            self.assertEqual(response.status_code, 403)
        with allow():
            response = self.client.patch(reverse("action-template-detail", args=[other.pk]), {"is_active": False}, format="json")
            self.assertEqual(response.status_code, 404)
        template.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(template.is_active)
        self.assertTrue(other.is_active)
