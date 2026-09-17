from datetime import date
from types import SimpleNamespace

from django.db.models import Q
from django.db.models.signals import m2m_changed, post_save
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from models.models import CompanyProfile, User
from companyprofileapis.models import Opportunity
from project_management.models import ActionItem, Milestone, Project
from project_management.services.action_item_queries import (
    ActionItemFilterService,
    ActionItemOrderingService,
    ActionItemQueryService,
    ActionItemVisibilityService,
)
from project_management.serializers import ActionItemListSerializer
from project_management.signals import (
    action_item_assignees_post_save_operations,
    action_item_post_save_operations,
    create_default_folders,
    milestone_post_save_operations,
    notify_on_milestone_save,
    notify_on_project_save,
)


class ActionItemQueryFixtureMixin:
    def setUp(self):
        for signal, sender in (
            (action_item_post_save_operations, ActionItem),
            (create_default_folders, Project),
            (notify_on_project_save, Project),
            (milestone_post_save_operations, Milestone),
            (notify_on_milestone_save, Milestone),
        ):
            post_save.disconnect(signal, sender=sender)
            self.addCleanup(post_save.connect, signal, sender=sender)
        m2m_changed.disconnect(
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )
        self.addCleanup(
            m2m_changed.connect,
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )

        self.user = User.objects.create_user(
            email="action-item-query@example.com",
            password="password",
            first_name="Query",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Action Item Query Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="query@example.com",
            company_tax_reference="tax-query",
            company_iban="iban-query",
        )
        User.objects.filter(pk=self.user.pk).update(company=self.company)
        self.user.refresh_from_db()
        self.project = Project.objects.create(
            project_name="Query Project",
            solution="Energy Consultant Solution",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
            project_start_date=date(2026, 1, 1),
            project_end_date=date(2026, 12, 31),
        )
        self.phase = Milestone.objects.create(
            project=self.project,
            name="Design",
            colour="#112233",
            status="open",
            start_date=date(2026, 1, 1),
            due_date=date(2026, 6, 30),
        )

    def create_parent(self, name, **overrides):
        values = {
            "milestone": self.phase,
            "created_by": self.user,
            "name": name,
            "start_date": date(2026, 1, 1),
            "target_date": date(2026, 1, 31),
            "status": "open",
        }
        values.update(overrides)
        return ActionItem.objects.create(**values)

    def create_child(self, parent, name, **overrides):
        values = {
            "parent_action_item": parent,
            "created_by": self.user,
            "name": name,
            "start_date": date(2026, 1, 1),
            "target_date": date(2026, 1, 31),
            "status": "open",
        }
        values.update(overrides)
        return ActionItem.objects.create(**values)


class ActionItemFilterServiceTests(ActionItemQueryFixtureMixin, TestCase):
    def test_invalid_date_is_rejected_before_query_execution(self):
        with self.assertRaises(ValidationError):
            ActionItemFilterService({"start_date": "not-a-date"}).filter_tree(
                ActionItem.objects.filter(parent_action_item__isnull=True)
            )

    def test_tree_search_keeps_matching_child_parent_and_prunes_siblings(self):
        parent = self.create_parent("Parent without the term")
        matching_child = self.create_child(parent, "Needle child")
        self.create_child(parent, "Other child")
        self.create_parent("Unrelated parent")

        service = ActionItemFilterService({"search": "needle"})

        parents = service.filter_tree(
            ActionItem.objects.filter(
                milestone=self.phase,
                parent_action_item__isnull=True,
            )
        )
        children = service.filter_children(
            ActionItem.objects.filter(parent_action_item=parent)
        )

        self.assertEqual(list(parents.values_list("id", flat=True)), [parent.id])
        self.assertEqual(
            list(children.values_list("id", flat=True)),
            [matching_child.id],
        )

    def test_closed_item_filters_follow_ui_precedence(self):
        open_item = self.create_parent("Open item")
        closed_in_range = self.create_parent(
            "Closed in range",
            status="closed",
            closed_at=date(2026, 2, 10),
        )
        self.create_parent(
            "Closed out of range",
            status="closed",
            closed_at=date(2026, 3, 10),
        )
        parents = ActionItem.objects.filter(
            milestone=self.phase,
            parent_action_item__isnull=True,
        )

        default_ids = set(
            ActionItemFilterService({})
            .filter_tree(parents)
            .values_list("id", flat=True)
        )
        included_ids = set(
            ActionItemFilterService({"show_closed": "true"})
            .filter_tree(parents)
            .values_list("id", flat=True)
        )
        explicit_closed_ids = set(
            ActionItemFilterService({"status": "Closed:#112233"})
            .filter_tree(parents)
            .values_list("id", flat=True)
        )
        closure_ids = set(
            ActionItemFilterService(
                {
                    "status": "open",
                    "closure_date_after": "2026-02-01",
                    "closure_date_before": "2026-02-28",
                }
            )
            .filter_tree(parents)
            .values_list("id", flat=True)
        )

        self.assertEqual(default_ids, {open_item.id})
        self.assertEqual(
            included_ids,
            {open_item.id, closed_in_range.id} | {
                ActionItem.objects.get(name="Closed out of range").id
            },
        )
        self.assertEqual(explicit_closed_ids, {closed_in_range.id} | {
            ActionItem.objects.get(name="Closed out of range").id
        })
        self.assertEqual(closure_ids, {closed_in_range.id})

    def test_status_filter_strips_color_without_collapsing_status_words(self):
        matching = self.create_parent("In progress", status="in progress")
        self.create_parent("Open")
        parents = ActionItem.objects.filter(
            milestone=self.phase,
            parent_action_item__isnull=True,
        )

        result = ActionItemFilterService(
            {"status": "In Progress:#112233"}
        ).filter_tree(parents)

        self.assertEqual(list(result.values_list("id", flat=True)), [matching.id])

    def test_date_modes_match_the_existing_frontend_rules(self):
        contained = self.create_parent(
            "Contained",
            start_date=date(2026, 2, 1),
            target_date=date(2026, 2, 20),
            forecast_date=date(2026, 2, 15),
        )
        self.create_parent(
            "Outside",
            start_date=date(2026, 1, 1),
            target_date=date(2026, 3, 1),
            forecast_date=date(2026, 3, 1),
        )
        parents = ActionItem.objects.filter(
            milestone=self.phase,
            parent_action_item__isnull=True,
        )

        exact_ids = set(
            ActionItemFilterService(
                {
                    "date_filter_mode": "date",
                    "start_date": "2026-02-01",
                    "target_date": "2026-02-20",
                    "forecast_date_after": "2026-02-15",
                }
            )
            .filter_tree(parents)
            .values_list("id", flat=True)
        )
        period_ids = set(
            ActionItemFilterService(
                {
                    "date_filter_mode": "period",
                    "start_date": "2026-02-01",
                    "target_date": "2026-02-28",
                    "forecast_date_after": "2026-02-01",
                    "forecast_date_before": "2026-02-28",
                }
            )
            .filter_tree(parents)
            .values_list("id", flat=True)
        )

        self.assertEqual(exact_ids, {contained.id})
        self.assertEqual(period_ids, {contained.id})

    def test_legacy_target_date_range_parameters_are_preserved(self):
        inside = self.create_parent(
            "Inside legacy range",
            target_date=date(2026, 2, 15),
        )
        self.create_parent(
            "Before legacy range",
            target_date=date(2026, 1, 31),
        )
        self.create_parent(
            "After legacy range",
            target_date=date(2026, 3, 1),
        )
        parents = ActionItem.objects.filter(
            milestone=self.phase,
            parent_action_item__isnull=True,
        )

        result = ActionItemFilterService(
            {
                "target_date_after": "2026-02-01",
                "target_date_before": "2026-02-28",
            }
        ).filter_tree(parents)

        self.assertEqual(list(result.values_list("id", flat=True)), [inside.id])

    def test_relation_filters_use_any_selected_value(self):
        second_user = User.objects.create_user(
            email="second-query@example.com",
            password="password",
            first_name="Second",
            last_name="User",
            user_type="company_user",
            company=self.company,
        )
        opportunity = Opportunity.objects.bulk_create(
            [
                Opportunity(
                    title="Selected opportunity",
                    company_profile=self.company,
                    created_by=self.user,
                )
            ]
        )[0]
        matching = self.create_parent(
            "Matching relations",
            priority_level="High",
            labels=["Risk", "Client"],
        )
        matching.assignees.add(second_user)
        matching.opportunities.add(opportunity)
        self.create_parent("Nonmatching relations", priority_level="Low")
        parents = ActionItem.objects.filter(
            milestone=self.phase,
            parent_action_item__isnull=True,
        )

        result = ActionItemFilterService(
            {
                "reporter": str(self.user.id),
                "assignee": f"999999,{second_user.id}",
                "opportunity": str(opportunity.id),
                "milestone": str(self.phase.id),
                "priority": "High",
                "labels": "Risk",
            }
        ).filter_tree(parents)

        self.assertEqual(list(result.values_list("id", flat=True)), [matching.id])

    def test_flat_mode_returns_a_matching_child_without_its_parent(self):
        parent = self.create_parent("Parent")
        child = self.create_child(parent, "Flat needle")
        self.create_child(parent, "Other child")

        result = ActionItemFilterService({"search": "needle"}).filter_flat(
            ActionItem.objects.filter(
                Q(milestone=self.phase)
                | Q(parent_action_item__milestone=self.phase)
            )
        )

        self.assertEqual(list(result.values_list("id", flat=True)), [child.id])


class ActionItemVisibilityServiceTests(ActionItemQueryFixtureMixin, TestCase):
    @staticmethod
    def permission_checker(*allowed_permissions):
        def check(user, modules, scope, context):
            module, actions = next(iter(modules.items()))
            return (scope, module, tuple(actions)) in allowed_permissions

        return check

    def test_company_permission_returns_company_action_items(self):
        company_item = self.create_parent(
            "Company item",
            milestone=None,
            company_profile=self.company,
        )
        request = SimpleNamespace(
            user=self.user,
            query_params={"company_profile": str(self.company.id)},
        )
        service = ActionItemVisibilityService(
            request,
            permission_checker=self.permission_checker(
                ("company", "action_item", ("view", "full_access")),
            ),
        )

        self.assertEqual(
            set(service.resolve().queryset.values_list("id", flat=True)),
            {company_item.id},
        )

    def test_project_scope_excludes_company_and_other_project_action_items(self):
        project_item = self.create_parent("Selected project item")
        company_item = self.create_parent(
            "Company item",
            milestone=None,
            company_profile=self.company,
        )
        other_project = Project.objects.create(
            project_name="Other Project",
            solution=self.project.solution,
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        other_phase = Milestone.objects.create(
            project=other_project,
            name="Other Phase",
            colour="#445566",
            status="open",
            due_date=date(2026, 6, 30),
        )
        other_project_item = self.create_parent(
            "Other project item",
            milestone=other_phase,
        )
        request = SimpleNamespace(
            user=self.user,
            query_params={"project": str(self.project.id)},
        )
        service = ActionItemVisibilityService(
            request,
            permission_checker=self.permission_checker(
                ("company", "action_item", ("view", "full_access")),
                (
                    "company",
                    "projects",
                    ("full_access", "view_all_access"),
                ),
                ("project", "action_item", ("view", "full_access")),
            ),
        )

        result_ids = set(service.resolve().queryset.values_list("id", flat=True))

        self.assertEqual(result_ids, {project_item.id})
        self.assertNotIn(company_item.id, result_ids)
        self.assertNotIn(other_project_item.id, result_ids)

    def test_company_scope_excludes_project_action_items(self):
        company_item = self.create_parent(
            "Company item",
            milestone=None,
            company_profile=self.company,
        )
        project_item = self.create_parent("Project item")
        request = SimpleNamespace(
            user=self.user,
            query_params={"company_profile": str(self.company.id)},
        )
        service = ActionItemVisibilityService(
            request,
            permission_checker=self.permission_checker(
                ("company", "action_item", ("view", "full_access")),
                (
                    "company",
                    "projects",
                    ("full_access", "view_all_access"),
                ),
                ("project", "action_item", ("view", "full_access")),
            ),
        )

        result_ids = set(service.resolve().queryset.values_list("id", flat=True))

        self.assertEqual(result_ids, {company_item.id})
        self.assertNotIn(project_item.id, result_ids)

    def test_company_projects_permission_returns_project_action_items(self):
        project_item = self.create_parent("Project item")
        request = SimpleNamespace(
            user=self.user,
            query_params={"project": str(self.project.id)},
        )
        service = ActionItemVisibilityService(
            request,
            permission_checker=self.permission_checker(
                (
                    "company",
                    "projects",
                    ("full_access", "view_all_access"),
                ),
            ),
        )

        self.assertEqual(
            set(service.resolve().queryset.values_list("id", flat=True)),
            {project_item.id},
        )

    def test_freelancer_keeps_own_company_action_items(self):
        freelancer = User.objects.create_user(
            email="freelancer-query@example.com",
            password="password",
            first_name="Free",
            last_name="Lancer",
            user_type="freelancer",
        )
        own_item = self.create_parent(
            "Freelancer item",
            milestone=None,
            freelancer=freelancer,
        )
        request = SimpleNamespace(
            user=freelancer,
            query_params={"freelancer": str(freelancer.id)},
        )
        service = ActionItemVisibilityService(
            request,
            permission_checker=self.permission_checker(),
        )

        self.assertEqual(
            set(service.resolve().queryset.values_list("id", flat=True)),
            {own_item.id},
        )


class ActionItemOrderingServiceTests(ActionItemQueryFixtureMixin, TestCase):
    def test_project_default_uses_phase_order_then_stable_id(self):
        later_phase = Milestone.objects.create(
            project=self.project,
            name="Later",
            colour="#445566",
            status="open",
            start_date=date(2026, 7, 1),
            due_date=date(2026, 12, 31),
            sort_order=2,
        )
        later_item = self.create_parent("A", milestone=later_phase)
        first_item = self.create_parent("B")

        ordered = ActionItemOrderingService.apply(
            ActionItem.objects.filter(parent_action_item__isnull=True),
            scope="project",
        )

        self.assertEqual(
            list(ordered.values_list("id", flat=True)),
            [first_item.id, later_item.id],
        )

    def test_explicit_sort_is_global_and_has_an_id_tie_breaker(self):
        later_phase = Milestone.objects.create(
            project=self.project,
            name="Later",
            colour="#445566",
            status="open",
            start_date=date(2026, 7, 1),
            due_date=date(2026, 12, 31),
            sort_order=2,
        )
        zulu = self.create_parent("Zulu", milestone=self.phase)
        alpha_first = self.create_parent("Alpha", milestone=later_phase)
        alpha_second = self.create_parent("Alpha", milestone=self.phase)

        ordered = ActionItemOrderingService.apply(
            ActionItem.objects.filter(parent_action_item__isnull=True),
            scope="project",
            ordering="name",
        )

        self.assertEqual(
            list(ordered.values_list("id", flat=True)),
            [alpha_first.id, alpha_second.id, zulu.id],
        )


class ActionItemListProjectionTests(ActionItemQueryFixtureMixin, TestCase):
    def test_compact_projection_serializes_prefetched_children_without_queries(self):
        parent = self.create_parent("Parent")
        parent.assignees.add(self.user)
        child = self.create_child(parent, "Child")
        child.assignees.add(self.user)

        projected = list(
            ActionItemQueryService.with_list_projection(
                ActionItem.objects.filter(id=parent.id)
            )
        )

        with self.assertNumQueries(0):
            data = ActionItemListSerializer(projected, many=True).data

        self.assertEqual(data[0]["milestone"]["name"], self.phase.name)
        self.assertEqual(data[0]["assignees"][0]["id"], self.user.id)
        self.assertEqual(data[0]["assignees"][0]["name"], self.user.full_name)
        self.assertEqual(data[0]["created_by"]["id"], self.user.id)
        self.assertEqual(data[0]["sub_action_items"][0]["id"], child.id)
        self.assertNotIn("documents", data[0])
        self.assertNotIn("requirements", data[0])
