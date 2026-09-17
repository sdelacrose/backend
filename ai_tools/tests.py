from datetime import timedelta
from time import perf_counter
from unittest import skipUnless
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext
from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from ai_tools.models import AIToolAuditLog
from ai_tools import services
from ai_tools.exceptions import AIToolClarificationRequired
from ai_tools.localization import localize_action_item_name, localize_project_type
from ai_tools.serializers import AIToolRequestSerializer
from ecpmpapp.constants import StatusType
from models.models import CompanyConfig, CompanyProfile, Connection, User
from project_management.models import (
    ActionItem,
    Document,
    ExpenseCard,
    Folder,
    Invoice,
    Milestone,
    ObjectAssessment,
    Project,
    ProjectTeamMembers,
    ProjectPhase,
    PurchaseOrder,
    ECPMPConfig,
    TimeCard,
)
from project_management.models.contact import ContactUser
from project_management.signals import (
    action_item_assignees_post_save_operations,
    action_item_post_save_operations,
    create_default_folders,
    milestone_post_save_operations,
    notify_on_milestone_save,
    notify_on_object_assessment_delete,
    notify_on_object_assessment_save,
    notify_on_project_save,
)
from userapis.models import CalendarEvent

try:
    from project_management.models import AvailableProductPart, BillOfMaterialPart, Requirement, SolutionDemand

    MATRIX_AND_PARTS_AVAILABLE = True
except ImportError:
    AvailableProductPart = BillOfMaterialPart = Requirement = SolutionDemand = None
    MATRIX_AND_PARTS_AVAILABLE = False

ACTION_ITEM_HAS_FORECAST_DATE = any(field.name == "forecast_date" for field in ActionItem._meta.get_fields())
ACTION_ITEM_HAS_ASSIGNEES = any(field.name == "assignees" for field in ActionItem._meta.get_fields())
ACTION_ITEM_HAS_ASSIGNEE = any(field.name == "assignee" for field in ActionItem._meta.get_fields())


def action_item_payload(**kwargs):
    if not ACTION_ITEM_HAS_FORECAST_DATE:
        kwargs.pop("forecast_date", None)
    return kwargs


def assign_action_item(item, user):
    if ACTION_ITEM_HAS_ASSIGNEES:
        item.assignees.add(user)
    elif ACTION_ITEM_HAS_ASSIGNEE:
        item.assignee = user
        item.save(update_fields=["assignee"])


class AIToolTestCase(TestCase):
    def setUp(self):
        notification_patcher = patch("ecpmpapp.utils.should_notify_user_comprehensive", return_value=False)
        active_solution_patcher = patch("ai_tools.services.get_user_active_solution", return_value="Enterprise Solution")
        notification_patcher.start()
        active_solution_patcher.start()
        self.addCleanup(notification_patcher.stop)
        self.addCleanup(active_solution_patcher.stop)

        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        m2m_changed.disconnect(action_item_assignees_post_save_operations, sender=ActionItem.assignees.through)
        post_save.disconnect(milestone_post_save_operations, sender=Milestone)
        post_save.disconnect(notify_on_milestone_save, sender=Milestone)
        post_save.disconnect(notify_on_object_assessment_save, sender=ObjectAssessment)
        pre_delete.disconnect(notify_on_object_assessment_delete, sender=ObjectAssessment)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(m2m_changed.connect, action_item_assignees_post_save_operations, sender=ActionItem.assignees.through)
        self.addCleanup(post_save.connect, milestone_post_save_operations, sender=Milestone)
        self.addCleanup(post_save.connect, notify_on_milestone_save, sender=Milestone)
        self.addCleanup(post_save.connect, notify_on_object_assessment_save, sender=ObjectAssessment)
        self.addCleanup(pre_delete.connect, notify_on_object_assessment_delete, sender=ObjectAssessment)

        self.user = User.objects.create_user(
            email="ai-tools@example.com",
            password="password",
            first_name="AI",
            last_name="User",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="AI Tools Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="ai-tools@example.com",
            company_tax_reference="tax",
            company_iban="iban",
            company_searchable=True,
            services="Project management",
            sharemundo_links_id="links_company_ai",
        )
        self.user.company = self.company
        self.user.sharemundo_links_id = "links_user_ai"
        self.user.save(update_fields=["company", "sharemundo_links_id"])

        self.other_user = User.objects.create_user(
            email="other-ai-tools@example.com",
            password="password",
            first_name="Other",
            last_name="User",
            user_type="company_user",
            user_searchable=True,
            professional_headline="Project management expert",
            keywords="project management delivery",
        )
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_user,
            company_name="Other Searchable Company",
            company_address="Street 2",
            company_town="Other Town",
            company_tell_no="789",
            company_mob_no="987",
            company_web_address="https://other.example.com",
            company_email_address="other@example.com",
            company_tax_reference="tax2",
            company_iban="iban2",
            company_searchable=True,
            expertise="Software development",
            sharemundo_links_id="links_company_other",
        )
        self.other_user.company = self.other_company
        self.other_user.sharemundo_links_id = "links_user_other"
        self.other_user.save(update_fields=["company", "sharemundo_links_id"])

        self.project = Project.objects.create(
            project_name="AI Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.other_project = Project.objects.create(
            project_name="Other AI Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.milestone = Milestone.objects.create(
            project=self.project,
            name="AI Milestone",
            due_date=timezone.localdate() + timedelta(days=10),
        )
        self.other_milestone = Milestone.objects.create(
            project=self.other_project,
            name="Other Milestone",
            due_date=timezone.localdate() + timedelta(days=10),
        )

    def _patch_viewset_queryset(self):
        def _queryset(viewset_class, user, params):
            model = viewset_class.queryset.model
            return model.objects.all()

        return patch("ai_tools.services.viewset_queryset", side_effect=_queryset)

    def _patch_viewset_queryset_with_project_visibility(self):
        def _queryset(viewset_class, user, params):
            model = viewset_class.queryset.model
            if model is Project:
                return Project.objects.filter(company_profile=self.company)
            return model.objects.all()

        return patch("ai_tools.services.viewset_queryset", side_effect=_queryset)


class AIToolEndpointSecurityTests(AIToolTestCase):
    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_service_key_is_required_and_success_is_audited(self):
        ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Open item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        client = APIClient()
        client.force_authenticate(self.user)

        with self._patch_viewset_queryset():
            denied = client.post("/api/ai-tools/action-items/count/", {"scope": "project", "project_id": self.project.id}, format="json")
            allowed = client.post(
                "/api/ai-tools/action-items/count/",
                {"scope": "project", "project_id": self.project.id},
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data["summary"]["count"], 1)
        self.assertEqual(allowed.data["contract_version"], "6")
        self.assertTrue(allowed.data["authoritative"])
        self.assertEqual(allowed.data["query_spec"]["domain"], "action_items")
        self.assertEqual(allowed.data["query_spec"]["scope"]["project_id"], self.project.id)
        self.assertEqual(len(allowed.data["result_hash"]), 64)
        audit = AIToolAuditLog.objects.get(tool_name="action_items_count", status="success")
        self.assertEqual(audit.result_count, 1)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_validation_failure_is_audited(self):
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.post(
            "/api/ai-tools/action-items/count/",
            {"scope": "outside"},
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(response.status_code, 400)
        audit = AIToolAuditLog.objects.get(tool_name="action_items_count")
        self.assertEqual(audit.status, "error")
        self.assertIn("outside", audit.error_message)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_invalid_project_and_unknown_filters_are_rejected(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        invalid_project = client.post(
            "/api/ai-tools/action-items/count/",
            {"scope": "project", "project_id": 0, "filters": {"status": "open"}},
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )
        unknown_filter = client.post(
            "/api/ai-tools/action-items/search/",
            {"scope": "all_accessible", "filters": {"reference": "3539"}},
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(invalid_project.status_code, 400)
        self.assertEqual(unknown_filter.status_code, 400)
        self.assertEqual(invalid_project.data["error"]["code"], "invalid_query")
        self.assertIn("project_id", invalid_project.data["error"]["details"])
        self.assertIn("reference", str(unknown_filter.data))

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_unknown_status_returns_a_typed_clarification_instead_of_zero(self):
        ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Open item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        client = APIClient()
        client.force_authenticate(self.user)

        with self._patch_viewset_queryset():
            response = client.post(
                "/api/ai-tools/action-items/count/",
                {"scope": "project", "project_id": self.project.id, "filters": {"status": "Banana"}},
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "clarification_required")
        self.assertEqual(response.data["error"]["field"], "status")
        self.assertIn("open", [value.lower() for value in response.data["error"]["choices"]])

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_reversed_date_range_is_rejected_before_query_execution(self):
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.post(
            "/api/ai-tools/action-items/search/",
            {
                "scope": "all_accessible",
                "filters": {"target_date_from": "2026-07-31", "target_date_to": "2026-07-01"},
            },
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "invalid_query")
        self.assertIn("target_date_to", response.data["error"]["details"])

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_inaccessible_project_returns_safe_typed_permission_error(self):
        inaccessible_project = Project.objects.create(
            project_name="Hidden Project",
            contract_value=1000,
            company_profile=self.other_company,
            user=self.other_user,
            created_by=self.other_user,
        )
        client = APIClient()
        client.force_authenticate(self.user)

        with self._patch_viewset_queryset_with_project_visibility():
            response = client.post(
                "/api/ai-tools/action-items/count/",
                {
                    "scope": "project",
                    "project_id": inaccessible_project.id,
                    "filters": {"status": "open"},
                },
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["contract_version"], "6")
        self.assertFalse(response.data["authoritative"])
        self.assertEqual(response.data["error"]["code"], "permission_denied")
        self.assertNotIn("summary", response.data)
        self.assertNotIn("rows", response.data)
        self.assertNotIn(str(inaccessible_project.id), str(response.data))


class AIToolActionItemTests(AIToolTestCase):
    def setUp(self):
        super().setUp()
        self.oldest = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Big delay",
                target_date=timezone.localdate() - timedelta(days=20),
                forecast_date=timezone.localdate() - timedelta(days=15),
                status="open",
                created_by=self.user,
            )
        )
        self.late_done = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Late done",
                target_date=timezone.localdate() - timedelta(days=10),
                forecast_date=timezone.localdate() - timedelta(days=8),
                status="Completed",
                created_by=self.user,
            )
        )
        self.future = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Future",
                target_date=timezone.localdate() + timedelta(days=7),
                forecast_date=timezone.localdate() + timedelta(days=7),
                status="open",
                created_by=self.user,
            )
        )
        for item in (self.oldest, self.late_done, self.future):
            assign_action_item(item, self.user)

    def test_late_and_biggest_delay_are_date_based_independently_of_status(self):
        payload = {"scope": "project", "project_id": self.project.id, "filters": {"late_only": True}}
        with self._patch_viewset_queryset():
            count = services.count_action_items(self.user, payload)
            biggest = services.biggest_action_item_delay(self.user, {"scope": "project", "project_id": self.project.id})

        self.assertEqual(count["summary"]["count"], 2)
        self.assertEqual(biggest["rows"][0]["id"], self.oldest.id)
        self.assertEqual(biggest["summary"]["delay_days"], 20)

    def test_exact_open_and_non_closed_statuses_are_distinct(self):
        custom_late = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Custom review",
                target_date=timezone.localdate() - timedelta(days=5),
                forecast_date=timezone.localdate() - timedelta(days=4),
                status="Client Review",
                created_by=self.user,
            )
        )
        closed_late = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Closed late",
                target_date=timezone.localdate() - timedelta(days=3),
                forecast_date=timezone.localdate() - timedelta(days=2),
                status="Closed",
                created_by=self.user,
            )
        )
        assign_action_item(custom_late, self.user)
        assign_action_item(closed_late, self.user)

        with self._patch_viewset_queryset():
            exact_open = services.count_action_items(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {"status": "open"},
                },
            )
            late_non_closed = services.count_action_items(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {
                        "excluded_statuses": ["Closed"],
                        "late_only": True,
                    },
                },
            )

        direct_project_items = ActionItem.objects.filter(milestone__project=self.project)
        expected_exact_open = direct_project_items.filter(status__iexact="open").count()
        expected_late_non_closed = direct_project_items.exclude(
            status__iexact="closed"
        ).filter(target_date__lt=timezone.localdate()).count()
        self.assertEqual(exact_open["summary"]["count"], expected_exact_open)
        self.assertEqual(
            late_non_closed["summary"]["count"],
            expected_late_non_closed,
        )
        self.assertEqual(expected_late_non_closed, 3)

    def test_explicit_inaccessible_project_is_not_reported_as_zero(self):
        inaccessible_project = Project.objects.create(
            project_name="Inaccessible Project",
            contract_value=1000,
            company_profile=self.other_company,
            user=self.other_user,
            created_by=self.other_user,
        )
        payload = {"scope": "project", "project_id": inaccessible_project.id, "filters": {"status": "open"}}

        with self._patch_viewset_queryset_with_project_visibility():
            with self.assertRaises(PermissionDenied):
                services.count_action_items(self.user, payload)

    def test_action_item_queries_preserve_the_viewset_permission_boundary(self):
        def permission_filtered_queryset(viewset_class, user, params):
            model = viewset_class.queryset.model
            if model is ActionItem:
                return ActionItem.objects.filter(id=self.oldest.id)
            if model is Project:
                return Project.objects.filter(id=self.project.id)
            return model.objects.all()

        with patch(
            "ai_tools.services.viewset_queryset",
            side_effect=permission_filtered_queryset,
        ):
            result = services.count_action_items(
                self.user,
                {
                    "scope": "all_accessible",
                    "filters": {"excluded_statuses": ["Closed"]},
                },
            )

        expected = (
            ActionItem.objects.filter(id=self.oldest.id)
            .exclude(status__iexact="closed")
            .count()
        )
        self.assertEqual(result["summary"]["count"], expected)
        self.assertEqual(result["summary"]["count"], 1)

    def test_group_by_project_returns_chart_ready_data(self):
        ActionItem.objects.create(
            **action_item_payload(
                milestone=self.other_milestone,
                name="Other project item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        with self._patch_viewset_queryset():
            response = services.action_items_group_by_project(self.user, {"scope": "all_accessible", "filters": {"status": "open"}})

        self.assertEqual(response["answer_type"], "chart")
        self.assertIn("AI Project", response["chart"]["labels"])
        self.assertIn("Other AI Project", response["chart"]["labels"])

    def test_group_by_milestone_returns_only_the_requested_project(self):
        second_milestone = Milestone.objects.create(
            project=self.project,
            name="Delivery milestone",
            phase_title="Delivery",
            due_date=timezone.localdate() + timedelta(days=20),
        )
        second_item = ActionItem.objects.create(
            **action_item_payload(
                milestone=second_milestone,
                name="Delivery item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        assign_action_item(second_item, self.user)

        with self._patch_viewset_queryset():
            response = services.action_items_group_by_milestone(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {"excluded_statuses": ["Closed"]},
                },
            )

        self.assertEqual(response["answer_type"], "chart")
        self.assertEqual(response["summary"]["count"], 4)
        self.assertIn("Delivery", response["chart"]["labels"])
        self.assertNotIn(self.other_milestone.phase_title, response["chart"]["labels"])

    def test_company_scope_does_not_count_project_action_items(self):
        company_item = ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.company,
                name="Company only item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )

        with self._patch_viewset_queryset():
            response = services.count_action_items(
                self.user,
                {"scope": "company", "company_profile_id": self.company.id, "filters": {"status": "open"}},
            )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(
            response["rows"],
            [{"scope": "company", "project_id": None, "project_name": None, "count": 1}],
        )
        self.assertEqual(company_item.company_profile_id, self.company.id)

    @patch(
        "project_management.views.action_item.require_any_permission_multiple_modules"
    )
    def test_company_scope_counts_all_visible_sub_action_items(
        self, permission_mock
    ):
        permission_mock.side_effect = lambda user, modules, scope, context: (
            modules == {"action_item": ["view", "full_access"]}
            and scope == "company"
        )
        parent = ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.company,
                name="Company parent",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        first_child = ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=parent,
                name="First company child",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        second_child = ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=parent,
                name="Second company child",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="Client Review",
                created_by=self.other_user,
            )
        )
        assign_action_item(first_child, self.user)
        assign_action_item(second_child, self.other_user)

        response = services.count_action_items(
            self.user,
            {
                "scope": "company",
                "company_profile_id": self.company.id,
                "filters": {"excluded_statuses": ["Closed"]},
            },
        )

        self.assertEqual(response["summary"]["count"], 3)
        self.assertEqual(
            response["rows"],
            [
                {
                    "scope": "company",
                    "project_id": None,
                    "project_name": None,
                    "count": 3,
                }
            ],
        )

    def test_accessible_projects_scope_excludes_company_level_action_items(self):
        ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.company,
                name="Company only item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )

        with self._patch_viewset_queryset():
            response = services.count_action_items(
                self.user,
                {"scope": "accessible_projects", "filters": {"status": "open"}},
            )

        self.assertEqual(response["summary"]["count"], 2)
        self.assertTrue(all(row["scope"] == "project" for row in response["rows"]))

    def test_accessible_projects_scope_excludes_items_linked_to_inaccessible_projects(self):
        inaccessible_parent = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.other_milestone,
                name="Inaccessible project parent",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        inaccessible_child = ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=inaccessible_parent,
                name="Assigned child of inaccessible project",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        assign_action_item(inaccessible_child, self.user)
        accessible_projects = Project.objects.filter(id=self.project.id)

        with self._patch_viewset_queryset(), patch(
            "ai_tools.services.visible_projects",
            return_value=accessible_projects,
        ):
            response = services.count_action_items(
                self.user,
                {"scope": "accessible_projects", "filters": {"status": "open"}},
            )

        self.assertEqual(
            {row["project_id"] for row in response["rows"]},
            {self.project.id},
        )

    def test_all_accessible_scope_keeps_company_items_but_excludes_inaccessible_projects(self):
        company_item = ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.company,
                name="Visible company item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        inaccessible_parent = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.other_milestone,
                name="Inaccessible project parent",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        inaccessible_child = ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=inaccessible_parent,
                name="Assigned child of inaccessible project",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        assign_action_item(inaccessible_child, self.user)
        accessible_projects = Project.objects.filter(id=self.project.id)

        with self._patch_viewset_queryset(), patch(
            "ai_tools.services.visible_projects",
            return_value=accessible_projects,
        ):
            response = services.count_action_items(
                self.user,
                {"scope": "all_accessible", "filters": {"status": "open"}},
            )

        self.assertIn(
            {"scope": "company", "project_id": None, "project_name": None, "count": 1},
            response["rows"],
        )
        self.assertIn(self.project.id, {row["project_id"] for row in response["rows"]})
        self.assertNotIn(self.other_project.id, {row["project_id"] for row in response["rows"]})
        self.assertEqual(company_item.company_profile_id, self.company.id)

    def test_all_accessible_scope_excludes_assigned_items_from_another_company(self):
        company_item = ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.company,
                name="Visible company item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        other_company_item = ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.other_company,
                name="Assigned item from another company",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.other_user,
            )
        )
        assign_action_item(other_company_item, self.user)

        with self._patch_viewset_queryset():
            response = services.count_action_items(
                self.user,
                {"scope": "all_accessible", "filters": {"status": "open"}},
            )
            visible_ids = set(
                services.visible_action_items(
                    self.user,
                    {"scope": "all_accessible", "filters": {"status": "open"}},
                ).values_list("id", flat=True)
            )

        self.assertIn(
            {
                "scope": "company",
                "project_id": None,
                "project_name": None,
                "count": 1,
            },
            response["rows"],
        )
        self.assertEqual(company_item.company_profile_id, self.company.id)
        self.assertNotIn(other_company_item.id, visible_ids)

    def test_company_scope_defaults_to_users_company_when_company_id_is_omitted(self):
        company_item = ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.company,
                name="Company item without explicit scope id",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        child_company_item = ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=company_item,
                name="Company child item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.other_company,
                name="Other company item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.other_user,
            )
        )

        with self._patch_viewset_queryset():
            response = services.count_action_items(
                self.user,
                {"scope": "company", "filters": {"status": "open"}},
            )

        self.assertEqual(response["summary"]["count"], 2)
        self.assertEqual(child_company_item.parent_action_item_id, company_item.id)

    def test_company_scope_can_exclude_sub_actions(self):
        company_item = ActionItem.objects.create(
            **action_item_payload(
                company_profile=self.company,
                name="Company parent item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=company_item,
                name="Company child item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )

        with self._patch_viewset_queryset():
            response = services.count_action_items(
                self.user,
                {
                    "scope": "company",
                    "filters": {
                        "status": "open",
                        "include_sub_actions": False,
                    },
                },
            )

        self.assertEqual(response["summary"]["count"], 1)

    def test_exact_open_status_does_not_include_in_progress(self):
        in_progress = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="In progress item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="in progress",
                created_by=self.user,
            )
        )
        assign_action_item(in_progress, self.user)

        with self._patch_viewset_queryset():
            response = services.count_action_items(
                self.user,
                {"scope": "project", "project_id": self.project.id, "filters": {"status": "open"}},
            )

        self.assertEqual(response["summary"]["count"], 2)

    def test_late_only_defaults_to_every_non_closed_status(self):
        custom_late = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Custom status late item",
                target_date=timezone.localdate() - timedelta(days=2),
                forecast_date=timezone.localdate() - timedelta(days=2),
                status="Waiting for customer",
                created_by=self.user,
            )
        )
        closed_late = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Closed late item",
                target_date=timezone.localdate() - timedelta(days=3),
                forecast_date=timezone.localdate() - timedelta(days=3),
                status="Closed",
                created_by=self.user,
            )
        )
        assign_action_item(custom_late, self.user)
        assign_action_item(closed_late, self.user)

        with self._patch_viewset_queryset():
            response = services.count_action_items(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {"late_only": True},
                },
            )
            returned_ids = set(
                services.visible_action_items(
                    self.user,
                    {
                        "scope": "project",
                        "project_id": self.project.id,
                        "filters": {"late_only": True},
                    },
                ).values_list("id", flat=True)
            )

        self.assertIn(custom_late.id, returned_ids)
        self.assertNotIn(closed_late.id, returned_ids)
        self.assertEqual(response["summary"]["count"], len(returned_ids))

    def test_action_item_counts_match_direct_truth_for_core_client_questions(self):
        closed = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.other_milestone,
                name="Closed item",
                target_date=timezone.localdate() - timedelta(days=1),
                forecast_date=timezone.localdate() - timedelta(days=1),
                status="closed",
                closed_at=timezone.localdate() - timedelta(days=1),
                created_by=self.user,
            )
        )
        assigned_other_project = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.other_milestone,
                name="Assigned other project",
                target_date=timezone.localdate() + timedelta(days=3),
                forecast_date=timezone.localdate() + timedelta(days=3),
                status="in progress",
                created_by=self.other_user,
            )
        )
        assign_action_item(assigned_other_project, self.user)

        with self._patch_viewset_queryset():
            open_count = services.count_action_items(self.user, {"scope": "all_accessible", "filters": {"status": "open"}})
            related_to_me = services.count_action_items(self.user, {"scope": "all_accessible", "filters": {"status": "open", "related_to_me": True}})
            assigned_to_me_search = services.search_action_items(self.user, {"scope": "all_accessible", "filters": {"status": "open", "assignee": "me"}, "limit": 100})
            closed_between = services.action_items_closure_list(
                self.user,
                {
                    "scope": "all_accessible",
                    "filters": {
                        "closed_at_from": (timezone.localdate() - timedelta(days=2)).isoformat(),
                        "closed_at_to": timezone.localdate().isoformat(),
                    },
                },
            )
            grouped = services.action_items_group_by_project(self.user, {"scope": "all_accessible", "filters": {"status": "open"}})

        direct_open_queryset = ActionItem.objects.filter(status__iexact="open")
        direct_open = direct_open_queryset.count()
        direct_related_to_me = (
            direct_open_queryset
            .filter(Q(created_by=self.user) | Q(assignees=self.user))
            .distinct()
            .count()
            if ACTION_ITEM_HAS_ASSIGNEES
            else direct_open_queryset.filter(created_by=self.user).distinct().count()
        )
        direct_assigned_to_me = (
            direct_open_queryset.filter(assignees=self.user).distinct().count()
            if ACTION_ITEM_HAS_ASSIGNEES
            else direct_open_queryset.filter(assignee=self.user).distinct().count()
        )
        direct_closed_between = ActionItem.objects.filter(id=closed.id).count()
        direct_group_total = sum(
            row["count"]
            for row in services.action_item_scope_breakdown(
                direct_open_queryset
            )
            if row["scope"] == "project"
        )

        self.assertEqual(open_count["summary"]["count"], direct_open)
        self.assertEqual(open_count["summary"]["count"], sum(row["count"] for row in open_count["rows"]))
        self.assertEqual(
            {row["project_id"] for row in open_count["rows"] if row["scope"] == "project"},
            {self.project.id},
        )
        project_breakdown_rows = [row for row in open_count["rows"] if row["scope"] == "project"]
        self.assertEqual(
            len(project_breakdown_rows),
            len({row["project_id"] for row in project_breakdown_rows}),
        )
        self.assertEqual(related_to_me["summary"]["count"], direct_related_to_me)
        self.assertEqual(assigned_to_me_search["summary"]["count"], direct_assigned_to_me)
        self.assertEqual(closed_between["summary"]["count"], direct_closed_between)
        self.assertEqual(grouped["summary"]["count"], direct_group_total)
        self.assertEqual(grouped["summary"]["count"], sum(row["count"] for row in grouped["rows"]))

    def test_project_child_rows_and_sources_use_the_parent_project(self):
        parent = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Project parent",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        child = ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=parent,
                name="Project child",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        assign_action_item(child, self.user)

        with self._patch_viewset_queryset():
            response = services.search_action_items(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {"status": "open", "assignee": "current_user"},
                    "limit": 100,
                },
            )
            grouped = services.action_items_group_by_project(
                self.user,
                {"scope": "all_accessible", "filters": {"status": "open"}},
            )

        row = next(row for row in response["rows"] if row["id"] == child.id)
        source = next(source for source in response["sources"] if source["id"] == child.id)
        self.assertEqual(row["project"], {"id": self.project.id, "name": self.project.project_name})
        self.assertEqual(source["url"], f"/project-action-items?project_id={self.project.id}&id={child.id}")
        project_group = next(row for row in grouped["rows"] if row["project_id"] == self.project.id)
        self.assertGreaterEqual(project_group["count"], 2)

    def test_financial_summary_uses_allocated_and_approved_booked_values(self):
        self.oldest.allocated_budget = 500
        self.oldest.allocated_time = 8
        self.oldest.save(update_fields=["allocated_budget", "allocated_time"])
        TimeCard.objects.create(project=self.project, milestone=self.milestone, action_item=self.oldest, name="Approved time", hours=3, status=StatusType.APPROVED.value)
        TimeCard.objects.create(project=self.project, milestone=self.milestone, action_item=self.oldest, name="Pending time", hours=9, status=StatusType.PENDING.value)
        ExpenseCard.objects.create(project=self.project, milestone=self.milestone, action_item=self.oldest, name="Approved cost", description="cost", category="test", rate=100, quantity=1, total=100, converted_amount=100, status=StatusType.APPROVED.value)
        ExpenseCard.objects.create(project=self.project, milestone=self.milestone, action_item=self.oldest, name="Pending cost", description="cost", category="test", rate=700, quantity=1, total=700, converted_amount=700, status=StatusType.PENDING.value)

        with self._patch_viewset_queryset():
            response = services.action_items_financial_summary(self.user, {"scope": "project", "project_id": self.project.id, "filters": {"status": "open"}})

        self.assertEqual(response["summary"]["count"], 2)
        self.assertEqual(response["summary"]["allocated_budget"], 501.0)
        self.assertEqual(response["summary"]["allocated_hours"], 8.0)
        self.assertEqual(response["summary"]["booked_hours"], 3.0)
        self.assertEqual(response["summary"]["booked_costs"], 100.0)

    def test_closure_list_and_progress_summary(self):
        self.late_done.closed_at = timezone.localdate() - timedelta(days=3)
        self.late_done.save(update_fields=["closed_at"])

        with self._patch_viewset_queryset():
            closure = services.action_items_closure_list(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {
                        "closed_at_from": (timezone.localdate() - timedelta(days=7)).isoformat(),
                        "closed_at_to": timezone.localdate().isoformat(),
                    },
                },
            )
            progress = services.action_items_progress_summary(self.user, {"scope": "project", "project_id": self.project.id})

        self.assertEqual(closure["summary"]["count"], 1)
        self.assertEqual(closure["rows"][0]["id"], self.late_done.id)
        self.assertEqual(progress["answer_type"], "chart")
        self.assertEqual(progress["summary"]["closed_action_items"], 0)
        self.assertEqual(progress["summary"]["open_action_items"], 3)

    def test_invoice_candidates_returns_safe_warning(self):
        response = services.action_items_invoice_candidates(self.user, {"scope": "project", "project_id": self.project.id})

        self.assertEqual(response["summary"]["count"], 0)
        self.assertIn("not available", response["warnings"][0])

    def test_phase_milestone_financial_summary_uses_project_phase_and_milestone_values(self):
        ProjectPhase.objects.create(
            project=self.project,
            name="Design phase",
            status="open",
            amount=1250,
            created_by=self.user,
        )
        self.milestone.allocated_budget = 750
        self.milestone.allocated_time = 6
        self.milestone.save(update_fields=["allocated_budget", "allocated_time"])

        with self._patch_viewset_queryset_with_project_visibility():
            response = services.phase_milestone_financial_summary(
                self.user,
                {"scope": "project", "project_id": self.project.id},
            )

        self.assertEqual(response["summary"]["phase_count"], 1)
        self.assertEqual(response["summary"]["phase_total_amount"], 1250.0)
        self.assertEqual(response["summary"]["milestone_count"], 1)
        self.assertEqual(response["summary"]["milestone_total_allocated_budget"], 750.0)
        self.assertEqual(response["summary"]["milestone_total_allocated_hours"], 6.0)
        self.assertEqual(response["chart"]["labels"], ["Design phase"])

    def test_phase_milestone_financial_summary_requires_project_id(self):
        response = services.phase_milestone_financial_summary(self.user, {"scope": "all_accessible"})

        self.assertEqual(response["summary"]["count"], 0)
        self.assertIn("Project ID is required", response["warnings"][0])


class AIToolPerformanceTests(AIToolTestCase):
    def test_five_thousand_action_item_count_uses_bounded_queries(self):
        target_date = timezone.localdate() + timedelta(days=7)
        ActionItem.objects.bulk_create(
            [
                ActionItem(
                    **action_item_payload(
                        milestone=self.milestone,
                        name=f"Scale item {index}",
                        target_date=target_date,
                        forecast_date=target_date,
                        status="open",
                        created_by=self.user,
                    )
                )
                for index in range(5000)
            ],
            batch_size=500,
        )

        started_at = perf_counter()
        with self._patch_viewset_queryset(), CaptureQueriesContext(connection) as queries:
            response = services.count_action_items(
                self.user,
                {"scope": "project", "project_id": self.project.id, "filters": {"status": "open"}},
            )
        elapsed = perf_counter() - started_at

        self.assertEqual(response["summary"]["count"], 5000)
        self.assertLessEqual(len(queries), 3)
        self.assertLess(elapsed, 1.5)


class AIToolPurchaseOrderInvoiceTests(AIToolTestCase):
    def setUp(self):
        super().setUp()
        self.purchase_order_permission = patch(
            "ai_tools.services.PurchaseOrderPermission.get_view_scope",
            return_value="all",
        )
        self.invoice_permission = patch(
            "ai_tools.services.InvoicePermission.has_permission",
            return_value=True,
        )
        self.purchase_order_permission.start()
        self.invoice_permission.start()
        self.addCleanup(self.purchase_order_permission.stop)
        self.addCleanup(self.invoice_permission.stop)
        self.purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            reference="PO-1",
            customer_reference="CPO-1",
            po_date=timezone.localdate(),
            currency="EUR",
            status="open",
            amount_excl_vat=1000,
            vat_rate=20,
            vat_amount=200,
            amount_incl_vat=1200,
            created_by=self.user,
        )
        self.invoice = Invoice.objects.create(
            project=self.project,
            company_profile=self.company,
            purchase_order=self.purchase_order,
            invoice_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=14),
            status="paid",
            document_type=Invoice.DocumentType.INVOICE,
            reference_number="INV-1",
            grand_total=300,
            company_currency_amount=300,
        )

    def test_purchase_order_summary_rejects_users_without_module_access(self):
        with patch(
            "ai_tools.services.PurchaseOrderPermission.get_view_scope",
            return_value="none",
        ):
            with self.assertRaises(PermissionDenied):
                services.purchase_orders_summary(
                    self.user,
                    {"scope": "project", "project_id": self.project.id},
                )

    def test_purchase_order_summary_and_balance_use_visible_records(self):
        with self._patch_viewset_queryset():
            summary = services.purchase_orders_summary(self.user, {"scope": "project", "project_id": self.project.id})
            balance = services.purchase_order_balance(
                self.user,
                {"scope": "project", "project_id": self.project.id, "filters": {"purchase_order_id": self.purchase_order.id}},
            )

        self.assertEqual(summary["summary"]["count"], 1)
        self.assertEqual(summary["summary"]["total_amount_incl_vat"], 1200.0)
        self.assertEqual(summary["summary"]["invoiced_amount"], 300.0)
        self.assertEqual(summary["summary"]["remaining_balance"], 900.0)
        self.assertEqual(balance["rows"][0]["remaining_balance"], 900.0)
        self.assertEqual(balance["rows"][0]["invoice_count"], 1)

    def test_po_summary_applies_change_notices_credit_notes_and_reference_lookup(self):
        self.purchase_order.po_type = PurchaseOrder.POType.OUTGOING
        self.purchase_order.save(update_fields=["po_type"])
        PurchaseOrder.objects.create(
            project=self.project,
            source_po=self.purchase_order,
            reference="CN-PO-1",
            po_date=timezone.localdate(),
            currency="EUR",
            status="issued",
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
            amount_excl_vat=1100,
            change_amount_excl_vat=100,
            vat_rate=20,
            vat_amount=220,
            amount_incl_vat=1320,
            created_by=self.user,
        )
        Invoice.objects.create(
            project=self.project,
            company_profile=self.company,
            purchase_order=self.purchase_order,
            source_invoice=self.invoice,
            invoice_date=timezone.localdate(),
            due_date=timezone.localdate(),
            status="paid",
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            reference_number="CN-1",
            grand_total=-50,
            company_currency_amount=-50,
        )

        with self._patch_viewset_queryset():
            summary = services.purchase_orders_summary(
                self.user, {"scope": "project", "project_id": self.project.id}
            )
            balance = services.purchase_order_balance(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {"reference": "PO-1"},
                },
            )

        self.assertEqual(summary["summary"]["count"], 1)
        self.assertEqual(summary["summary"]["total_amount_incl_vat"], 1320.0)
        self.assertEqual(summary["summary"]["invoiced_amount"], 250.0)
        self.assertEqual(summary["summary"]["remaining_balance"], 1070.0)
        self.assertEqual(balance["rows"][0]["credit_note_amount"], -50.0)
        self.assertEqual(balance["rows"][0]["remaining_balance"], 1070.0)
        self.assertEqual(balance["rows"][0]["credit_note_count"], 1)

    def test_paid_invoice_search_returns_metadata_and_warns_for_download(self):
        Invoice.objects.create(
            project=self.project,
            company_profile=self.company,
            purchase_order=self.purchase_order,
            source_invoice=self.invoice,
            invoice_date=timezone.localdate(),
            due_date=timezone.localdate(),
            status="paid",
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            reference_number="CN-PAID",
            grand_total=-25,
            company_currency_amount=-25,
        )

        with self._patch_viewset_queryset():
            response = services.search_invoices(
                self.user,
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {"paid_only": True, "download_all": True},
                },
            )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["reference_number"], "INV-1")
        self.assertIn("Bulk invoice download", response["warnings"][0])

    def test_multi_currency_po_totals_are_grouped_without_mixing_units(self):
        usd_order = PurchaseOrder.objects.create(
            project=self.project,
            reference="PO-USD",
            po_date=timezone.localdate(),
            currency="USD",
            status="open",
            amount_excl_vat=500,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=500,
            created_by=self.user,
        )
        Invoice.objects.create(
            project=self.project,
            company_profile=self.company,
            purchase_order=usd_order,
            invoice_date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=14),
            status="paid",
            document_type=Invoice.DocumentType.INVOICE,
            reference_number="INV-USD",
            grand_total=100,
            company_currency_amount=90,
        )

        with self._patch_viewset_queryset():
            response = services.purchase_orders_summary(
                self.user, {"scope": "project", "project_id": self.project.id}
            )

        self.assertIsNone(response["summary"]["total_amount_incl_vat"])
        self.assertEqual(response["summary"]["currency_totals"]["EUR"]["remaining_balance"], 900.0)
        self.assertEqual(response["summary"]["currency_totals"]["USD"]["invoiced_amount"], 100.0)
        self.assertEqual(response["summary"]["currency_totals"]["USD"]["remaining_balance"], 400.0)
        self.assertIn("not added together", response["warnings"][0])


class AIToolDocumentMetadataTests(AIToolTestCase):
    def setUp(self):
        super().setUp()
        self.project_folder = Folder.objects.create(project=self.project, name="Project Docs")
        self.other_project_folder = Folder.objects.create(project=self.other_project, name="Other Docs")
        self.company_folder = Folder.objects.create(company_profile=self.company, name="Company Docs")
        self.project_document = Document.objects.create(
            folder=self.project_folder,
            file=SimpleUploadedFile("draft.txt", b"secret payment terms"),
            file_name="draft.txt",
            status="draft",
            document_type="Specification",
            author=self.user,
            assignee=self.user,
            created_by=self.user,
        )
        Document.objects.create(
            folder=self.other_project_folder,
            file=SimpleUploadedFile("other.txt", b"other"),
            file_name="other.txt",
            status="draft",
            document_type="Specification",
            author=self.user,
            assignee=self.user,
            created_by=self.user,
        )
        Document.objects.create(
            folder=self.company_folder,
            file=SimpleUploadedFile("company.txt", b"company"),
            file_name="company.txt",
            status="approved",
            document_type="Policy",
            author=self.user,
            assignee=self.user,
            created_by=self.user,
        )

    def test_project_filter_counts_only_project_document_metadata(self):
        payload = {
            "scope": "project",
            "project_id": self.project.id,
            "filters": {"status": "draft", "content_query": "payment terms", "due_within_days": 7},
        }
        with self._patch_viewset_queryset():
            response = services.search_documents(self.user, payload)

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["id"], self.project_document.id)
        self.assertIn("Document content search is not enabled", response["warnings"][0])
        self.assertTrue(any("due-date metadata is not available" in warning for warning in response["warnings"]))
        self.assertNotIn("secret payment terms", str(response))

    def test_document_responsibility_filter_supports_current_user(self):
        payload = {
            "scope": "project",
            "project_id": self.project.id,
            "filters": {"status": "draft", "assignee": "current_user"},
        }
        with self._patch_viewset_queryset():
            response = services.search_documents(self.user, payload)

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["id"], self.project_document.id)

    def test_document_creator_filter_uses_user_id_for_solution_lookup(self):
        payload = {
            "scope": "project",
            "project_id": self.project.id,
            "filters": {"status": "draft", "created_by": "current_user"},
        }
        with (
            self._patch_viewset_queryset(),
            patch("ai_tools.services.get_user_active_solution", return_value="Enterprise Solution") as active_solution,
        ):
            response = services.search_documents(self.user, payload)

        active_solution.assert_called_with(self.user.id)
        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["id"], self.project_document.id)

    def test_document_count_keeps_object_assessments_separate(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Assessment note",
            file=SimpleUploadedFile("assessment.txt", b"assessment"),
            file_name="assessment.txt",
            status="draft",
            author=self.user,
            assignee=self.user,
            created_by=self.user,
        )

        payload = {"scope": "project", "project_id": self.project.id, "filters": {"status": "draft"}}
        with self._patch_viewset_queryset():
            count = services.count_documents(self.user, payload)
            search = services.search_documents(self.user, payload)
            grouped = services.documents_group_by_status(self.user, payload)

        self.assertEqual(count["summary"]["count"], 1)
        self.assertEqual(search["summary"]["count"], 1)
        self.assertEqual(count["summary"]["folder_document_count"], 1)
        self.assertEqual(count["summary"]["object_assessment_record_count"], 1)
        self.assertEqual(count["summary"]["library_item_count"], 2)
        self.assertEqual([row["file_name"] for row in search["rows"]], [self.project_document.file_name])
        self.assertEqual(grouped["summary"]["count"], 1)
        self.assertEqual(grouped["summary"]["object_assessment_record_count"], 1)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_document_run_returns_executable_related_object_assessment_query(self):
        ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Related assessment",
            file=SimpleUploadedFile("related.txt", b"assessment"),
            file_name="related.txt",
            status="draft",
            author=self.user,
            assignee=self.user,
            created_by=self.user,
        )
        client = APIClient()
        client.force_authenticate(self.user)

        with self._patch_viewset_queryset_with_project_visibility():
            response = client.post(
                "/api/ai-tools/business-query/run/",
                {
                    "entity": "documents",
                    "operation": "count",
                    "scope": {"type": "project", "project_id": self.project.id},
                    "filters": {"status": "draft"},
                },
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        related = response.data["related_queries"]["object_assessments"]
        self.assertEqual(related["count"], 1)
        self.assertEqual(related["query_spec"]["entity"], "object_assessments")
        self.assertEqual(related["query_spec"]["operation"], "list")
        self.assertEqual(
            related["query_spec"]["scope"],
            {"type": "project", "project_id": self.project.id},
        )
        self.assertEqual(related["query_spec"]["filters"], {"status": "draft"})

    def test_object_assessment_records_are_first_class_query_results(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Draft assessment note",
            file=SimpleUploadedFile("assessment.txt", b"assessment"),
            file_name="assessment.txt",
            status="draft",
            author=self.user,
            assignee=self.user,
            created_by=self.user,
        )
        payload = {
            "scope": "project",
            "project_id": self.project.id,
            "filters": {"status": "draft"},
        }

        with self._patch_viewset_queryset():
            count = services.count_object_assessments(self.user, payload)
            search = services.search_object_assessments(self.user, payload)

        self.assertEqual(count["summary"]["count"], 1)
        self.assertEqual(search["summary"]["count"], 1)
        self.assertEqual(search["rows"][0]["id"], assessment.id)
        self.assertEqual(search["rows"][0]["source_kind"], "object_assessment")
        self.assertEqual(search["sources"][0]["type"], "object_assessment")


class BusinessQueryGatewayTests(AIToolTestCase):
    def setUp(self):
        super().setUp()
        self.item = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Confirmed query item",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="open",
                created_by=self.user,
            )
        )
        assign_action_item(self.item, self.user)

    def _client(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def test_project_type_localization_recognizes_all_supported_languages(self):
        expected = {
            "en": (
                "Offer / Quotation / Proposal",
                "Individual Renovation Roadmap",
                "Roof renovation / Roof refurbishment",
                "Other Project",
                "Engineering, Design & Studies",
                "EPC / Turnkey Project",
            ),
            "de": (
                "Angebot",
                "iSFP",
                "Dachsanierung",
                "Anderes Projekt",
                "Ingenieurwesen, Konstruktion & Studien",
                "EPC- / Schlüsselfertiges Projekt",
            ),
            "fr": (
                "Offre / Devis / Proposition",
                "Plan individuel de rénovation énergétique",
                "Rénovation / Réfection de toiture",
                "Autre projet",
                "Ingénierie, Conception & Études",
                "Projet EPC / Clé en main",
            ),
        }
        stored_values = (
            "Angebot",
            "iSFP",
            "Dachsanierung",
            "Other Project",
            "Engineering, Design & Studies",
            "EPC / Turnkey Project",
        )
        for language, translated_values in expected.items():
            with self.subTest(language=language):
                self.assertEqual(
                    tuple(
                        localize_project_type(value, language)
                        for value in stored_values
                    ),
                    translated_values,
                )
        self.assertEqual(localize_project_type("Testing", "fr"), "Testing")

    def test_action_item_template_localization_preserves_custom_names(self):
        self.assertEqual(
            localize_action_item_name("Daten bei Kunden abfragen", "en"),
            "Request client data for funding application",
        )
        self.assertEqual(
            localize_action_item_name("Daten bei Kunden abfragen", "fr"),
            "Demander les données au client pour la demande de subvention",
        )
        self.assertEqual(
            localize_action_item_name("Daten bei Kunden abfragen", "de"),
            "Daten bei Kunden abfragen",
        )
        self.assertEqual(
            localize_action_item_name("Customer-specific task", "en"),
            "Customer-specific task",
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_action_item_list_localizes_template_names_and_source_labels(self):
        self.item.name = "Daten bei Kunden abfragen"
        self.item.save(update_fields=["name"])
        query = {
            "entity": "action_items",
            "operation": "list",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {"status": "open"},
            "language": "en",
        }

        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["rows"][0]["name"],
            "Request client data for funding application",
        )
        self.assertEqual(
            response.data["sources"][0]["label"],
            "Request client data for funding application",
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_project_summary_localizes_types_using_confirmed_query_language(self):
        self.project.project_type = "Angebot"
        self.project.save(update_fields=["project_type"])
        self.other_project.project_type = "Testing"
        self.other_project.save(update_fields=["project_type"])
        client = self._client()

        with self._patch_viewset_queryset_with_project_visibility():
            preview = client.post(
                "/api/ai-tools/business-query/preview/",
                {
                    "entity": "projects",
                    "operation": "summary",
                    "language": "fr",
                },
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )
            executed = client.post(
                "/api/ai-tools/business-query/execute/",
                {"confirmation_token": preview.data["confirmation_token"]},
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(executed.status_code, 200)
        rows_by_id = {row["project_id"]: row for row in executed.data["rows"]}
        self.assertEqual(
            rows_by_id[self.project.id]["project_type"],
            "Offre / Devis / Proposition",
        )
        self.assertEqual(
            rows_by_id[self.other_project.id]["project_type"],
            "Testing",
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_schema_exposes_read_only_versioned_entities(self):
        CompanyConfig.objects.update_or_create(
            company_profile=self.company,
            solution="Enterprise Solution",
            defaults={
                "status_options": [
                    "Open:#0057B7",
                    "Client Test:#00A86B",
                ]
            },
        )
        ECPMPConfig.objects.update_or_create(
            project=self.project,
            defaults={
                "status_options": [
                    "In Progress:#FFB000",
                    "Project Review:#805AD5",
                ]
            },
        )
        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().get(
                "/api/ai-tools/business-query/schema/",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["contract_version"], "6")
        self.assertTrue(response.data["read_only"])
        self.assertIn("object_assessments", response.data["entities"])
        self.assertIn("action_items", response.data["entities"])
        self.assertIn("project_members", response.data["entities"])
        self.assertIn("company_members", response.data["entities"])
        self.assertIn(
            "assignee",
            response.data["entities"]["action_items"]["operations"]["count"]["filters"],
        )
        self.assertIn(
            "include_sub_actions",
            response.data["entities"]["action_items"]["operations"]["count"]["filters"],
        )
        visible_projects = {
            project["id"]: project["name"]
            for project in response.data["visible_projects"]
        }
        self.assertEqual(
            visible_projects[self.project.id],
            self.project.project_name,
        )
        self.assertFalse(response.data["visible_projects_truncated"])
        self.assertEqual(
            response.data["configured_values"]["action_item_statuses"],
            ["Open", "Client Test", "In Progress", "Project Review"],
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_preview_does_not_return_records_and_confirmed_token_executes_exact_query(self):
        client = self._client()
        query = {
            "entity": "action_items",
            "operation": "count",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {"status": "open", "assignee": "current_user"},
            "language": "en",
            "timezone": "Europe/Paris",
        }
        with self._patch_viewset_queryset_with_project_visibility():
            preview = client.post(
                "/api/ai-tools/business-query/preview/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.data["authoritative"])
        self.assertTrue(preview.data["requires_confirmation"])
        self.assertNotIn("rows", preview.data)
        self.assertEqual(AIToolAuditLog.objects.count(), 0)

        with self._patch_viewset_queryset():
            executed = client.post(
                "/api/ai-tools/business-query/execute/",
                {"confirmation_token": preview.data["confirmation_token"]},
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(executed.status_code, 200)
        self.assertTrue(executed.data["authoritative"])
        self.assertEqual(executed.data["contract_version"], "6")
        self.assertEqual(executed.data["intent"], "action_items.count")
        self.assertEqual(executed.data["applied_scope"], query["scope"])
        self.assertEqual(
            executed.data["applied_filters"],
            {"status": "open", "assignee": "current_user"},
        )
        self.assertIn("generated_at", executed.data)
        self.assertEqual(executed.data["summary"]["count"], 1)
        self.assertEqual(executed.data["query_spec"], preview.data["query_spec"])
        self.assertEqual(
            AIToolAuditLog.objects.get(tool_name="business_query_execute").result_count,
            1,
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_execute_requires_a_valid_untampered_confirmation_token(self):
        client = self._client()
        missing = client.post(
            "/api/ai-tools/business-query/execute/",
            {},
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )
        tampered = client.post(
            "/api/ai-tools/business-query/execute/",
            {"confirmation_token": "not-a-valid-token"},
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.data["error"]["code"], "confirmation_required")
        self.assertEqual(tampered.status_code, 400)
        self.assertEqual(
            AIToolAuditLog.objects.filter(
                tool_name="business_query_execute",
                status=AIToolAuditLog.STATUS_ERROR,
            ).count(),
            2,
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_executes_read_query_without_preview_confirmation(self):
        client = self._client()
        query = {
            "entity": "action_items",
            "operation": "count",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {"status": "open", "assignee": "current_user"},
            "language": "en",
            "timezone": "Europe/Paris",
        }

        with self._patch_viewset_queryset_with_project_visibility():
            response = client.post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["authoritative"])
        self.assertEqual(response.data["contract_version"], "6")
        self.assertEqual(response.data["summary"]["count"], 1)
        self.assertEqual(response.data["query_spec"]["entity"], query["entity"])
        self.assertEqual(response.data["query_spec"]["operation"], query["operation"])
        self.assertEqual(response.data["query_spec"]["scope"], query["scope"])
        self.assertEqual(response.data["query_spec"]["filters"], query["filters"])
        self.assertIsNotNone(response.data["audit_id"])
        audit = AIToolAuditLog.objects.get(tool_name="business_query_run")
        self.assertEqual(audit.result_count, 1)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_supports_non_closed_late_items_and_safe_target_date_sorting(self):
        custom = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Waiting for client",
                target_date=timezone.localdate() - timedelta(days=4),
                forecast_date=timezone.localdate() - timedelta(days=3),
                status="Client Review",
                created_by=self.user,
            )
        )
        ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Closed record",
                target_date=timezone.localdate() - timedelta(days=10),
                forecast_date=timezone.localdate() - timedelta(days=9),
                status="Closed",
                created_by=self.user,
            )
        )
        query = {
            "entity": "action_items",
            "operation": "list",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {
                "excluded_statuses": ["Closed"],
                "late_only": True,
            },
            "sort": ["target_date"],
        }

        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        expected_ids = list(
            ActionItem.objects.filter(
                milestone__project=self.project,
                target_date__lt=timezone.localdate(),
            )
            .exclude(status__iexact="closed")
            .order_by("target_date", "id")
            .values_list("id", flat=True)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in response.data["rows"]],
            expected_ids,
        )
        self.assertIn(custom.id, expected_ids)
        self.assertEqual(
            response.data["applied_filters"]["excluded_statuses"],
            ["Closed"],
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_can_exclude_sub_actions(self):
        parent = ActionItem.objects.create(
            **action_item_payload(
                milestone=self.milestone,
                name="Parent record",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="Open",
                created_by=self.user,
            )
        )
        child = ActionItem.objects.create(
            **action_item_payload(
                parent_action_item=parent,
                name="Child record",
                target_date=timezone.localdate(),
                forecast_date=timezone.localdate(),
                status="Open",
                created_by=self.user,
            )
        )
        query = {
            "entity": "action_items",
            "operation": "list",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {
                "status": "Open",
                "include_sub_actions": False,
            },
        }

        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        returned_ids = {row["id"] for row in response.data["rows"]}
        self.assertIn(parent.id, returned_ids)
        self.assertNotIn(child.id, returned_ids)
        self.assertFalse(response.data["applied_filters"]["include_sub_actions"])

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_supports_project_milestone_grouping(self):
        query = {
            "entity": "action_items",
            "operation": "group_by_milestone",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {"excluded_statuses": ["Closed"]},
            "language": "en",
            "timezone": "Europe/Paris",
        }

        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["answer_type"], "chart")
        self.assertEqual(response.data["summary"]["count"], 1)
        self.assertEqual(response.data["rows"][0]["milestone_id"], self.milestone.id)
        self.assertEqual(response.data["chart"]["values"], [1])

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_rejects_unknown_action_item_ordering(self):
        query = {
            "entity": "action_items",
            "operation": "list",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {"excluded_statuses": ["Closed"]},
            "sort": ["invented_field"],
        }

        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "invalid_query")
        self.assertIn("ordering", response.data["error"]["details"])
        self.assertNotIn("rows", response.data)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_lists_project_members_without_email_draft_context(self):
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    team_member=self.other_user,
                    role="Project member",
                )
            ]
        )
        query = {
            "entity": "project_members",
            "operation": "list",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {},
            "language": "en",
            "timezone": "Europe/Paris",
        }

        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["intent"], "project_members.list")
        self.assertEqual(response.data["answer_type"], "list")
        self.assertEqual(response.data["summary"]["count"], 1)
        self.assertNotIn("latest_milestone", response.data["summary"])
        self.assertEqual(response.data["rows"][0]["email"], self.other_user.email)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_lists_only_active_users_from_requesters_company(self):
        colleague = User.objects.create_user(
            email="colleague@example.com",
            password="password",
            first_name="Company",
            last_name="Colleague",
            user_type="company_user",
            company=self.company,
            job_title="Consultant",
            sharemundo_links_id="links_company_colleague",
        )
        User.objects.create_user(
            email="inactive-colleague@example.com",
            password="password",
            first_name="Inactive",
            last_name="Colleague",
            user_type="company_user",
            company=self.company,
            is_active=False,
        )
        query = {
            "entity": "company_members",
            "operation": "list",
            "scope": {"type": "company"},
            "filters": {},
            "language": "en",
            "timezone": "Europe/Paris",
        }

        response = self._client().post(
            "/api/ai-tools/business-query/run/",
            query,
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["intent"], "company_members.list")
        returned_ids = {row["id"] for row in response.data["rows"]}
        self.assertIn(self.user.id, returned_ids)
        self.assertIn(colleague.id, returned_ids)
        self.assertNotIn(self.other_user.id, returned_ids)
        returned_emails = {row["email"] for row in response.data["rows"]}
        self.assertNotIn("inactive-colleague@example.com", returned_emails)

    def test_company_members_rejects_freelancer_scope(self):
        freelancer = User.objects.create_user(
            email="freelancer-members@example.com",
            password="password",
            first_name="Free",
            last_name="Lancer",
            user_type="freelancer",
        )

        with self.assertRaises(PermissionDenied):
            services.company_team_members(
                freelancer,
                {"scope": "company", "filters": {}},
            )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_company_members_rejects_another_company_id(self):
        response = self._client().post(
            "/api/ai-tools/business-query/run/",
            {
                "entity": "company_members",
                "operation": "list",
                "scope": {
                    "type": "company",
                    "company_profile_id": self.other_company.id,
                },
                "filters": {},
            },
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "permission_denied")
        self.assertNotIn("rows", response.data)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_run_returns_typed_validation_error_without_executing(self):
        response = self._client().post(
            "/api/ai-tools/business-query/run/",
            {
                "entity": "documents",
                "operation": "count",
                "filters": {"invented_filter": "value"},
            },
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "invalid_query")
        self.assertNotIn("rows", response.data)
        self.assertEqual(
            AIToolAuditLog.objects.get(tool_name="business_query_run").status,
            AIToolAuditLog.STATUS_ERROR,
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_identical_run_queries_return_the_same_authoritative_hash(self):
        query = {
            "entity": "action_items",
            "operation": "count",
            "scope": {"type": "project", "project_id": self.project.id},
            "filters": {"status": "open", "assignee": "current_user"},
            "language": "en",
            "timezone": "Europe/Paris",
        }
        client = self._client()

        with self._patch_viewset_queryset_with_project_visibility():
            first = client.post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )
            second = client.post(
                "/api/ai-tools/business-query/run/",
                query,
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["result_hash"], second.data["result_hash"])
        self.assertEqual(first.data["summary"], second.data["summary"])

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_confirmation_token_is_bound_to_the_authenticated_user(self):
        client = self._client()
        preview = client.post(
            "/api/ai-tools/business-query/preview/",
            {"entity": "action_items", "operation": "count"},
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )
        other_client = APIClient()
        other_client.force_authenticate(self.other_user)

        response = other_client.post(
            "/api/ai-tools/business-query/execute/",
            {"confirmation_token": preview.data["confirmation_token"]},
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("another user", str(response.data))

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_preview_rejects_unknown_filters_and_ignored_query_fields(self):
        client = self._client()
        unknown_filter = client.post(
            "/api/ai-tools/business-query/preview/",
            {
                "entity": "documents",
                "operation": "count",
                "filters": {"invented_field": "value"},
            },
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )
        unsupported_grouping = client.post(
            "/api/ai-tools/business-query/preview/",
            {
                "entity": "documents",
                "operation": "count",
                "group_by": ["project"],
            },
            format="json",
            HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
        )

        self.assertEqual(unknown_filter.status_code, 400)
        self.assertEqual(unsupported_grouping.status_code, 400)

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_matrix_gap_preview_does_not_inject_matrix_search_filter(self):
        with self._patch_viewset_queryset_with_project_visibility():
            response = self._client().post(
                "/api/ai-tools/business-query/preview/",
                {
                    "entity": "demands",
                    "operation": "uncovered",
                    "scope": {"type": "project", "project_id": self.project.id},
                },
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["query_spec"]["filters"],
            {"gap_type": "demands_without_requirements"},
        )

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_preview_rejects_unknown_configured_choice_before_confirmation(self):
        with self._patch_viewset_queryset():
            response = self._client().post(
                "/api/ai-tools/business-query/preview/",
                {
                    "entity": "action_items",
                    "operation": "count",
                    "scope": {"type": "project", "project_id": self.project.id},
                    "filters": {"status": "Banana"},
                },
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "clarification_required")
        self.assertEqual(response.data["error"]["field"], "status")
        self.assertIn("open", [choice.lower() for choice in response.data["error"]["choices"]])


class AIToolCalendarTests(AIToolTestCase):
    @patch("ai_tools.services.is_outlook_token_valid", return_value=False)
    @patch("ai_tools.services.ContactPermission")
    def test_attendee_resolver_uses_only_company_users_and_visible_contacts(
        self,
        permission_class,
        _token_valid,
    ):
        permission_class.return_value._check_view_permission.return_value = True
        colleague = User.objects.create_user(
            email="calendar-colleague@example.com",
            password="password",
            first_name="Calendar",
            last_name="Colleague",
            user_type="company_user",
        )
        colleague.company = self.company
        colleague.save(update_fields=["company"])
        ContactUser.objects.create(
            company_profile=self.company,
            name="Patrick",
            surname="Contact",
            email="patrick.contact@example.com",
        )
        ContactUser.objects.create(
            company_profile=self.other_company,
            name="Hidden",
            surname="Contact",
            email="hidden.contact@example.com",
        )

        response = services.resolve_calendar_attendees(
            self.user,
            {
                "filters": {
                    "attendee_queries": [
                        colleague.email,
                        "Patrick Contact",
                        "Hidden Contact",
                    ]
                }
            },
        )

        rows = response["rows"]
        self.assertEqual(rows[0]["status"], "resolved")
        self.assertEqual(rows[0]["matches"][0]["source"], "links_user")
        self.assertEqual(rows[1]["status"], "resolved")
        self.assertEqual(rows[1]["matches"][0]["email"], "patrick.contact@example.com")
        self.assertEqual(rows[2]["status"], "not_found")
        self.assertNotIn(
            "hidden.contact@example.com",
            str(response),
        )

    def test_count_calendar_returns_only_the_authoritative_aggregate(self):
        start = timezone.now() + timedelta(days=3)
        CalendarEvent.objects.create(
            company_profile=self.company,
            name="Counted event",
            start_date=start,
            end_date=start + timedelta(minutes=30),
            created_by=self.user,
        )

        with patch("ai_tools.services.visible_calendar_events", return_value=CalendarEvent.objects.all()):
            response = services.count_calendar(
                self.user,
                {"scope": "current_user", "filters": {}},
            )

        self.assertEqual(response["answer_type"], "count")
        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"], [])

    def test_all_accessible_calendar_unions_company_and_visible_projects(self):
        start = timezone.now() + timedelta(days=4)
        company_event = CalendarEvent.objects.create(
            company_profile=self.company,
            name="Company event",
            start_date=start,
            end_date=start + timedelta(minutes=30),
            created_by=self.user,
        )
        project_event = CalendarEvent.objects.create(
            project=self.project,
            name="Project event",
            start_date=start + timedelta(hours=1),
            end_date=start + timedelta(hours=2),
            created_by=self.user,
        )
        CalendarEvent.objects.create(
            project=self.other_project,
            name="Hidden project event",
            start_date=start + timedelta(hours=3),
            end_date=start + timedelta(hours=4),
            created_by=self.other_user,
        )

        company = self.company

        class PermissionStub:
            def __init__(self, scope):
                self.scope = scope

            def filter_queryset(self, queryset, user, context=None):
                if self.scope == "company":
                    return queryset.filter(company_profile=company, project__isnull=True)
                return queryset.filter(project=(context or {}).get("project"))

        with patch(
            "ai_tools.services.viewset_queryset",
            side_effect=lambda viewset, user, params: Project.objects.filter(id=self.project.id),
        ), patch(
            "ai_tools.services.CalendarEventPermission",
            side_effect=lambda action, scope: PermissionStub(scope),
        ):
            response = services.search_calendar(
                self.user,
                {
                    "scope": "all_accessible",
                    "filters": {
                        "start_datetime": (start - timedelta(minutes=1)).isoformat(),
                        "end_datetime": (start + timedelta(hours=5)).isoformat(),
                    },
                },
            )

        self.assertEqual(response["summary"]["count"], 2)
        self.assertEqual({row["id"] for row in response["rows"]}, {company_event.id, project_event.id})
        rows_by_id = {row["id"]: row for row in response["rows"]}
        self.assertEqual(rows_by_id[company_event.id]["scope_type"], "company")
        self.assertIsNone(rows_by_id[company_event.id]["project_name"])
        self.assertEqual(rows_by_id[project_event.id]["scope_type"], "project")
        self.assertEqual(rows_by_id[project_event.id]["project_name"], self.project.project_name)
        self.assertEqual(len(response["sources"]), len(response["rows"]))

    def test_project_calendar_scope_requires_project_id(self):
        day = timezone.localdate() + timedelta(days=7)
        CalendarEvent.objects.create(
            company_profile=self.company,
            name="Company event",
            start_date=timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=9))),
            end_date=timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=10))),
            created_by=self.user,
        )

        response = services.search_calendar(self.user, {"scope": "project", "filters": {"date": day.isoformat()}})

        self.assertEqual(response["summary"]["count"], 0)
        self.assertIn("Project ID is required", response["warnings"][0])

    def test_current_user_calendar_includes_user_owned_synced_events(self):
        start = timezone.now() + timedelta(days=2)
        event = CalendarEvent.objects.create(
            company_profile=self.company,
            name="Synced Outlook event",
            start_date=start,
            end_date=start + timedelta(minutes=30),
            user=self.user,
        )

        with patch("ai_tools.services.CalendarEventPermission") as permission_class:
            permission_class.return_value.filter_queryset.return_value = CalendarEvent.objects.all()
            response = services.search_calendar(
                self.user,
                {
                    "scope": "current_user",
                    "filters": {
                        "start_datetime": (start - timedelta(minutes=5)).isoformat(),
                        "end_datetime": (start + timedelta(minutes=35)).isoformat(),
                    },
                },
            )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["id"], event.id)

    def test_private_events_mask_details_but_block_free_slot(self):
        day = timezone.localdate() + timedelta(days=30)
        start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=9)))
        end = start + timedelta(minutes=30)
        private_event = CalendarEvent.objects.create(
            company_profile=self.company,
            name="Private customer call",
            description="Confidential",
            is_private=True,
            start_date=start,
            end_date=end,
            created_by=self.user,
        )
        private_event.assignees.add(self.user)

        with patch("ai_tools.services.visible_calendar_events", return_value=CalendarEvent.objects.all()):
            search = services.search_calendar(self.user, {"scope": "current_user", "filters": {"date": day.isoformat()}})
            free = services.find_free_calendar_slot(
                self.user,
                {
                    "scope": "current_user",
                    "filters": {
                        "start_datetime": start.isoformat(),
                        "end_datetime": (start + timedelta(hours=1)).isoformat(),
                        "duration_minutes": 30,
                    },
                },
            )

        self.assertEqual(search["rows"][0]["name"], "Private event")
        self.assertIsNone(search["rows"][0]["description"])
        self.assertEqual(free["summary"]["slot_start"], end.isoformat())

    @patch("ai_tools.services.is_outlook_token_valid", return_value=False)
    @patch("ai_tools.services.build_outlook_oauth_url", return_value="https://example.test/connect-outlook")
    def test_free_slot_discloses_when_outlook_was_not_verified(self, _oauth_url, _token_valid):
        day = timezone.localdate() + timedelta(days=30)

        with patch("ai_tools.services.visible_calendar_events", return_value=CalendarEvent.objects.none()):
            response = services.find_free_calendar_slot(
                self.user,
                {
                    "scope": "current_user",
                    "filters": {
                        "date": day.isoformat(),
                        "duration_minutes": 30,
                    },
                },
            )

        self.assertTrue(response["summary"]["available"])
        self.assertIn("Outlook availability was not verified", response["warnings"][0])
        self.assertEqual(response["ui_actions"][0]["name"], "open_outlook_calendar_connection")

    def test_free_slot_contract_accepts_personal_filter(self):
        serializer = AIToolRequestSerializer(
            data={
                "scope": "current_user",
                "filters": {
                    "date": (timezone.localdate() + timedelta(days=30)).isoformat(),
                    "duration_minutes": 30,
                    "personal": True,
                    "timezone": "Asia/Karachi",
                },
            },
            tool_name="calendar_free_slot",
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertTrue(serializer.validated_data["filters"]["personal"])

    def test_create_calendar_event_requires_confirmation(self):
        start = timezone.now() + timedelta(days=1)
        response = services.create_calendar_event(
            self.user,
            {
                "filters": {
                    "title": "AI planned meeting",
                    "start_datetime": start.isoformat(),
                    "duration_minutes": 30,
                }
            },
        )

        self.assertEqual(response["answer_type"], "confirmation")
        self.assertTrue(response["summary"]["requires_confirmation"])
        self.assertIn("confirmation_token", response["summary"])
        self.assertEqual(response["ui_actions"][0]["label"], "Connect Outlook calendar")
        self.assertIn("url", response["ui_actions"][0]["payload"])
        self.assertIn("ai_assistant", response["ui_actions"][0]["payload"]["url"])
        self.assertEqual(CalendarEvent.objects.count(), 0)

    def test_create_calendar_event_rejects_direct_confirm_without_token(self):
        start = timezone.now() + timedelta(days=1)
        response = services.create_calendar_event(
            self.user,
            {
                "filters": {
                    "title": "AI planned meeting",
                    "start_datetime": start.isoformat(),
                    "duration_minutes": 30,
                    "confirmed": True,
                }
            },
        )

        self.assertEqual(response["answer_type"], "confirmation")
        self.assertTrue(response["summary"]["requires_confirmation"])
        self.assertEqual(CalendarEvent.objects.count(), 0)

    @patch("ai_tools.services.CalendarEventPermission")
    def test_calendar_event_create_permission_passes_context(self, permission_class):
        permission = permission_class.return_value
        permission._check_create_permission.return_value = True

        self.assertTrue(services._calendar_event_create_permission(self.user))
        permission_class.assert_called_once_with("create", "company")
        permission._check_create_permission.assert_called_once_with(self.user, {})

    @patch("ai_tools.services.create_outlook_calendar_event")
    @patch("ai_tools.services.is_outlook_token_valid", return_value=True)
    @patch("ai_tools.services._calendar_event_create_permission", return_value=True)
    @patch("userapis.serializers.calendar.schedule_reminder_to_sqs")
    def test_create_calendar_event_creates_internal_event_and_attempts_outlook(
        self,
        _schedule,
        _permission,
        _token_valid,
        outlook,
    ):
        outlook.return_value = {"success": True, "outlook_event_id": "graph-id", "meeting_link": "https://teams.example/meeting"}
        start = timezone.now() + timedelta(days=1)
        preview = services.create_calendar_event(
            self.user,
            {
                "filters": {
                    "title": "Confirmed AI meeting",
                    "description": "Discuss project status",
                    "start_datetime": start.isoformat(),
                    "duration_minutes": 30,
                    "attendee_emails": ["guest@example.com"],
                    "create_outlook_event": True,
                    "online_meeting": True,
                }
            },
        )
        response = services.create_calendar_event(
            self.user,
            {
                "filters": {
                    "title": "Confirmed AI meeting",
                    "description": "Discuss project status",
                    "start_datetime": start.isoformat(),
                    "duration_minutes": 30,
                    "attendee_emails": ["guest@example.com"],
                    "create_outlook_event": True,
                    "online_meeting": True,
                    "confirmed": True,
                    "confirmation_token": preview["summary"]["confirmation_token"],
                }
            },
        )

        self.assertEqual(response["answer_type"], "created")
        self.assertEqual(response["summary"]["calendar_event_id"], CalendarEvent.objects.get().id)
        self.assertTrue(response["summary"]["outlook_synced"])
        outlook.assert_called_once()

    @patch("ai_tools.services.is_outlook_token_valid", return_value=False)
    def test_confirmed_outlook_event_requires_connection_before_any_write(
        self,
        _token_valid,
    ):
        start = timezone.now() + timedelta(days=1)
        filters = {
            "title": "Reconnect first",
            "start_datetime": start.isoformat(),
            "duration_minutes": 30,
            "create_outlook_event": True,
            "online_meeting": True,
        }
        preview = services.create_calendar_event(self.user, {"filters": filters})

        response = services.create_calendar_event(
            self.user,
            {
                "filters": {
                    **filters,
                    "confirmed": True,
                    "confirmation_token": preview["summary"]["confirmation_token"],
                }
            },
        )

        self.assertEqual(response["answer_type"], "confirmation")
        self.assertFalse(CalendarEvent.objects.exists())
        self.assertEqual(
            response["ui_actions"][0]["name"],
            "open_outlook_calendar_connection",
        )


class AIToolProjectContextTests(AIToolTestCase):
    def test_exact_visible_project_name_resolves_to_project_id(self):
        payload = {"scope": "project", "project_name": self.project.project_name, "filters": {}}

        with self._patch_viewset_queryset_with_project_visibility():
            project_id = services.ensure_project_visible(self.user, payload)

        self.assertEqual(project_id, self.project.id)
        self.assertEqual(payload["project_id"], self.project.id)

    def test_ambiguous_visible_project_name_requires_numeric_id(self):
        Project.objects.create(
            project_name=self.project.project_name,
            contract_value=500,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )

        with self._patch_viewset_queryset_with_project_visibility():
            with self.assertRaises(AIToolClarificationRequired) as context:
                services.ensure_project_visible(
                    self.user,
                    {"scope": "project", "project_name": self.project.project_name, "filters": {}},
                )
        self.assertEqual(context.exception.field, "project")
        self.assertEqual(len(context.exception.choices), 2)

    def test_partial_visible_project_name_returns_choices(self):
        Project.objects.create(
            project_name="AI Project Follow-up",
            contract_value=500,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )

        with self._patch_viewset_queryset_with_project_visibility():
            with self.assertRaises(AIToolClarificationRequired) as context:
                services.ensure_project_visible(
                    self.user,
                    {"scope": "project", "project_name": "AI Proj", "filters": {}},
                )

        self.assertEqual(context.exception.field, "project")
        self.assertEqual(
            {choice["project_name"] for choice in context.exception.choices},
            {"AI Project", "AI Project Follow-up", "Other AI Project"},
        )

    def test_project_email_context_uses_only_visible_project_members(self):
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    team_member=self.other_user,
                    role="Project member",
                )
            ]
        )

        with self._patch_viewset_queryset_with_project_visibility():
            response = services.project_email_context(
                self.user,
                {"scope": "project", "project_id": self.project.id, "filters": {}},
            )

        self.assertEqual(response["summary"]["project_id"], self.project.id)
        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["email"], self.other_user.email)
        self.assertEqual(response["summary"]["latest_milestone"]["id"], self.milestone.id)

    def test_project_email_context_rejects_inaccessible_project(self):
        with patch(
            "ai_tools.services.viewset_queryset",
            side_effect=lambda viewset, user, params: Project.objects.none(),
        ):
            with self.assertRaises(PermissionDenied):
                services.project_email_context(
                    self.user,
                    {"scope": "project", "project_id": self.project.id, "filters": {}},
                )

    def test_project_team_members_returns_members_without_email_context(self):
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    team_member=self.other_user,
                    role="Project member",
                )
            ]
        )

        with self._patch_viewset_queryset_with_project_visibility():
            response = services.project_team_members(
                self.user,
                {"scope": "project", "project_id": self.project.id, "filters": {}},
            )

        self.assertEqual(response["answer_type"], "list")
        self.assertEqual(response["summary"]["count"], 1)
        self.assertNotIn("latest_milestone", response["summary"])
        self.assertEqual(response["rows"][0]["email"], self.other_user.email)

@skipUnless(MATRIX_AND_PARTS_AVAILABLE, "Matrix and parts models are not available on this backend branch.")
class AIToolMatrixAndPartsTests(AIToolTestCase):
    def setUp(self):
        super().setUp()
        self.uncovered_demand = SolutionDemand.objects.create(project=self.project, subject="Uncovered", demand_number="D1", sort_order=1)
        self.covered_demand = SolutionDemand.objects.create(project=self.project, subject="Covered", demand_number="D2", sort_order=2)
        self.requirement_without_part = Requirement.objects.create(project=self.project, name="No part", object_number="R1", sort_order=1)
        self.requirement_with_part = Requirement.objects.create(project=self.project, name="With part", object_number="R2", sort_order=2)
        self.requirement_with_part.solution_demands.add(self.covered_demand)
        self.bom_part = BillOfMaterialPart.objects.create(project=self.project, name="Part", part_number="P1", sort_order=1)
        self.bom_part.requirements.add(self.requirement_with_part)
        self.company_part = AvailableProductPart.objects.create(company_profile=self.company, name="Company part", sort_order=1)
        self.other_company_part = AvailableProductPart.objects.create(company_profile=self.other_company, name="Other company part", sort_order=1)

    def test_gap_queries_return_only_uncovered_rows(self):
        with self._patch_viewset_queryset():
            demands = services.matrix_gap_analysis(self.user, {"scope": "project", "project_id": self.project.id, "filters": {"gap_type": "demands_without_requirements"}})
            requirements = services.matrix_gap_analysis(self.user, {"scope": "project", "project_id": self.project.id, "filters": {"gap_type": "requirements_without_parts"}})

        self.assertEqual([row["id"] for row in demands["rows"]], [self.uncovered_demand.id])
        self.assertEqual([row["id"] for row in requirements["rows"]], [self.requirement_without_part.id])

    @override_settings(DEBUG=False, AI_TOOLS_SERVICE_API_KEY="secret")
    def test_gap_endpoint_supports_matrix_viewset_queryset(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        with self._patch_viewset_queryset():
            response = client.post(
                "/api/ai-tools/matrix/gaps/",
                {
                    "scope": "project",
                    "project_id": self.project.id,
                    "filters": {"gap_type": "demands_without_requirements"},
                    "include_sources": True,
                },
                format="json",
                HTTP_X_AI_TOOLS_SERVICE_KEY="secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["rows"]], [self.uncovered_demand.id])

    def test_available_parts_reject_other_company_scope(self):
        response = services.search_matrix_or_parts(
            self.user,
            {
                "scope": "company",
                "company_profile_id": self.other_company.id,
                "filters": {"entity": "available_parts"},
            },
        )

        self.assertEqual(response["summary"]["count"], 0)
        self.assertEqual(response["rows"], [])

    def test_matrix_search_returns_real_internal_source_links_for_each_entity(self):
        cases = (
            ("demands", "solution_demand", "/project-system-requirements/customer-demands"),
            ("requirements", "requirement", "/project-system-requirements/technical-requirements"),
            ("bom_parts", "bill_of_material_part", "/project-product-services/bill-of-material"),
        )

        with self._patch_viewset_queryset():
            for entity, source_type, route in cases:
                response = services.search_matrix_or_parts(
                    self.user,
                    {
                        "scope": "project",
                        "project_id": self.project.id,
                        "filters": {"entity": entity},
                    },
                )

                self.assertGreater(response["summary"]["count"], 0)
                self.assertTrue(all(source["type"] == source_type for source in response["sources"]))
                self.assertTrue(all(source["url"].startswith(route) for source in response["sources"]))

        with self._patch_viewset_queryset():
            response = services.search_matrix_or_parts(
                self.user,
                {
                    "scope": "company",
                    "company_profile_id": self.company.id,
                    "filters": {"entity": "available_parts"},
                },
            )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["sources"][0]["type"], "available_product_part")
        self.assertTrue(response["sources"][0]["url"].startswith("/company-product-services/available-products"))


class AIToolProfileAndNetworkTests(AIToolTestCase):
    def test_company_profile_search_ignores_boolean_and_generic_product_terms(self):
        response = services.public_company_profile_search(
            self.user,
            {
                "filters": {
                    "products": "pneumatic equipment OR pneumatic parts OR pneumatics",
                }
            },
        )

        self.assertEqual(response["summary"]["count"], 0)
        self.assertEqual(response["rows"], [])

    def test_public_profile_search_returns_only_public_safe_fields(self):
        hidden_user = User.objects.create_user(
            email="hidden@example.com",
            password="password",
            first_name="Hidden",
            last_name="User",
            user_searchable=False,
            keywords="project management",
        )
        hidden_company = CompanyProfile.objects.create(
            admin_user=hidden_user,
            company_name="Hidden Software Company",
            company_address="Hidden Street",
            company_town="Hidden",
            company_tell_no="1",
            company_mob_no="2",
            company_web_address="https://hidden.example.com",
            company_email_address="hidden@example.com",
            company_tax_reference="tax",
            company_iban="private-iban",
            company_searchable=False,
            expertise="software development",
        )
        hidden_user.company = hidden_company
        hidden_user.save(update_fields=["company"])

        users = services.public_user_profile_search(self.user, {"filters": {"search": "project management"}})
        companies = services.public_company_profile_search(self.user, {"filters": {"search": "software development"}})

        self.assertEqual([row["id"] for row in users["rows"]], [self.other_user.id])
        self.assertEqual([row["id"] for row in companies["rows"]], [self.other_company.id])
        self.assertEqual(
            users["rows"][0]["url"],
            f"/company-network?public_user_id={self.other_user.id}",
        )
        self.assertEqual(
            companies["rows"][0]["url"],
            f"/company-network?public_company_id={self.other_company.id}",
        )
        self.assertNotIn("email", users["rows"][0])
        self.assertNotIn("company_admin_email", users["rows"][0])
        self.assertNotIn("admin_email", users["rows"][0].get("company") or {})
        self.assertEqual(users["rows"][0]["company"]["company_name"], self.other_company.company_name)
        self.assertNotIn("company_iban", companies["rows"][0])

    def test_combined_public_profile_search_returns_ranked_users_and_companies(self):
        users = services.public_profile_search(
            self.user,
            {"scope": "all_accessible", "filters": {"search": "project management"}, "limit": 25},
        )
        companies = services.public_profile_search(
            self.user,
            {"scope": "all_accessible", "filters": {"search": "software development"}, "limit": 25},
        )

        self.assertEqual(users["summary"]["user_count"], 1)
        self.assertEqual(users["rows"][0]["profile_type"], "user")
        self.assertEqual(companies["summary"]["company_count"], 1)
        self.assertEqual(companies["rows"][0]["profile_type"], "company")
        self.assertNotIn("company_iban", str(companies))

    def test_public_user_profile_search_includes_public_role_and_resume_fields(self):
        role_match = User.objects.create_user(
            email="role-match@example.com",
            password="password",
            first_name="Role",
            last_name="Match",
            user_searchable=True,
            job_title="Project Management Lead",
        )
        resume_match = User.objects.create_user(
            email="resume-match@example.com",
            password="password",
            first_name="Resume",
            last_name="Match",
            user_searchable=True,
            resume="Led enterprise project management offices.",
        )

        result = services.public_user_profile_search(
            self.user,
            {"filters": {"search": "project management"}},
        )

        result_ids = [row["id"] for row in result["rows"]]
        self.assertIn(role_match.id, result_ids)
        self.assertIn(resume_match.id, result_ids)

    def test_company_profile_count_matches_ranked_results(self):
        location_only_match = CompanyProfile.objects.create(
            admin_user=User.objects.create_user(
                email="unranked-company@example.com",
                password="password",
            ),
            company_name="Unranked Company",
            company_address="Street 4",
            company_town="Paris",
            company_country="France",
            company_tell_no="111",
            company_mob_no="222",
            company_web_address="https://unranked.example.com",
            company_email_address="unranked@example.com",
            company_tax_reference="tax4",
            company_iban="iban4",
            company_searchable=True,
            expertise="Business consulting",
        )

        result = services.public_company_profile_search(
            self.user,
            {
                "filters": {
                    "search": "software development",
                    "location": "France",
                }
            },
        )

        self.assertEqual(result["summary"]["count"], len(result["rows"]))
        self.assertNotIn(location_only_match.id, [row["id"] for row in result["rows"]])

    def test_combined_profile_search_applies_user_only_availability_safely(self):
        self.other_user.country = "France"
        self.other_user.open_to_new_challenges = True
        self.other_user.save(update_fields=["country", "open_to_new_challenges"])

        result = services.public_profile_search(
            self.user,
            {
                "scope": "all_accessible",
                "filters": {
                    "search": "project management",
                    "location": "France",
                    "open_to_new_challenges": True,
                },
                "limit": 25,
            },
        )

        self.assertEqual(result["summary"]["user_count"], 1)
        self.assertTrue(all(row["profile_type"] == "user" for row in result["rows"]))

    def test_company_profile_search_uses_location_as_a_narrowing_filter(self):
        self.other_company.company_country = "France"
        self.other_company.save(update_fields=["company_country"])
        location_only_match = CompanyProfile.objects.create(
            admin_user=User.objects.create_user(
                email="location-only-company@example.com",
                password="password",
            ),
            company_name="France Consulting Company",
            company_address="Street 3",
            company_town="Paris",
            company_country="France",
            company_tell_no="111",
            company_mob_no="222",
            company_web_address="https://france-consulting.example.com",
            company_email_address="consulting@example.com",
            company_tax_reference="tax3",
            company_iban="iban3",
            company_searchable=True,
            expertise="Business consulting",
            sharemundo_links_id="links_company_france_consulting",
        )

        response = services.public_company_profile_search(
            self.user,
            {
                "filters": {
                    "search": "software development",
                    "location": "France",
                }
            },
        )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual([row["id"] for row in response["rows"]], [self.other_company.id])
        self.assertNotIn(location_only_match.id, [row["id"] for row in response["rows"]])

    def test_user_profile_search_requires_competence_beyond_location_words(self):
        self.other_user.country = "France"
        self.other_user.save(update_fields=["country"])
        location_only_match = User.objects.create_user(
            email="location-only-user@example.com",
            password="password",
            first_name="Public",
            last_name="Profile",
            country="France",
            user_searchable=True,
        )

        response = services.public_user_profile_search(
            self.user,
            {
                "filters": {
                    "search": "find public Links profiles in France who are experts in project management",
                    "location": "France",
                }
            },
        )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual([row["id"] for row in response["rows"]], [self.other_user.id])
        self.assertNotIn(location_only_match.id, [row["id"] for row in response["rows"]])

    def test_profile_search_ignores_generic_request_words(self):
        irrelevant_user = User.objects.create_user(
            username="new-admin-profile-search@example.com",
            email="new-admin-profile-search@example.com",
            password="password",
            first_name="New",
            last_name="Admin",
            user_searchable=True,
        )

        response = services.public_user_profile_search(
            self.user,
            {"filters": {"search": "new external team member with project management competence and availability"}},
        )

        self.assertNotIn(irrelevant_user.id, [row["id"] for row in response["rows"]])
        self.assertIn(self.other_user.id, [row["id"] for row in response["rows"]])

    def test_profile_search_combines_competence_location_and_declared_availability(self):
        self.other_user.country = "France"
        self.other_user.open_to_new_challenges = True
        self.other_user.save(update_fields=["country", "open_to_new_challenges"])
        availability_only_match = User.objects.create_user(
            email="available-only-user@example.com",
            password="password",
            first_name="Available",
            last_name="Candidate",
            country="France",
            user_searchable=True,
            open_to_new_challenges=True,
        )

        response = services.public_user_profile_search(
            self.user,
            {
                "filters": {
                    "search": (
                        "I need an external project manager in France who is open to new work; "
                        "rank public profiles and explain the matches"
                    ),
                    "location": "France",
                    "open_to_new_challenges": True,
                }
            },
        )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["id"], self.other_user.id)
        self.assertNotIn(availability_only_match.id, [row["id"] for row in response["rows"]])

    def test_public_user_profile_search_applies_declared_availability_filters(self):
        available_from = timezone.localdate() + timedelta(days=10)
        available_until = timezone.localdate() + timedelta(days=60)
        self.other_user.country = "France"
        self.other_user.availability_type = "Remote"
        self.other_user.availability_location = "France"
        self.other_user.availability_from = available_from
        self.other_user.availability_until = available_until
        self.other_user.open_to_new_challenges = True
        self.other_user.save(
            update_fields=[
                "country",
                "availability_type",
                "availability_location",
                "availability_from",
                "availability_until",
                "open_to_new_challenges",
            ]
        )

        response = services.public_user_profile_search(
            self.user,
            {
                "filters": {
                    "competence": "project management",
                    "location": "France",
                    "availability": "Remote",
                    "availability_from": available_from,
                    "availability_to": available_until,
                    "open_to_new_challenges": True,
                }
            },
        )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["id"], self.other_user.id)
        self.assertEqual(response["rows"][0]["availability_type"], "Remote")
        self.assertIn("availability_type", response["rows"][0]["match_reasons"])

    def test_network_search_returns_accepted_connections_only(self):
        Connection.objects.create(
            initiator_type="company",
            initiator_id=self.company.sharemundo_links_id,
            target_type="company",
            target_id=self.other_company.sharemundo_links_id,
            status="accepted",
        )
        Connection.objects.create(
            initiator_type="company",
            initiator_id=self.company.sharemundo_links_id,
            target_type="user",
            target_id=self.other_user.sharemundo_links_id,
            status="pending",
        )

        response = services.search_network(self.user, {})

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["status"], "accepted")
        self.assertEqual(response["rows"][0]["name"], self.other_company.company_name)

        filtered = services.search_network(
            self.user,
            {"filters": {"search": "not a connected company"}},
        )
        self.assertEqual(filtered["summary"]["count"], 0)

    def test_network_search_resolves_freelancer_profile_competence(self):
        self.other_user.current_position = "Senior Project Manager"
        self.other_user.qualifications = "Project management and delivery"
        self.other_user.save(
            update_fields=["current_position", "qualifications"]
        )
        Connection.objects.create(
            initiator_type="company",
            initiator_id=self.company.sharemundo_links_id,
            target_type="freelancer",
            target_id=self.other_user.sharemundo_links_id,
            status="accepted",
        )

        response = services.search_network(
            self.user,
            {
                "filters": {
                    "entity_type": "user",
                    "search": "project management",
                }
            },
        )

        self.assertEqual(response["summary"]["count"], 1)
        self.assertEqual(response["rows"][0]["name"], self.other_user.full_name)
        self.assertEqual(response["rows"][0]["entity_type"], "user")
        self.assertEqual(
            response["rows"][0]["current_position"],
            "Senior Project Manager",
        )
        self.assertIn(
            "qualifications",
            response["rows"][0]["match_reasons"],
        )
