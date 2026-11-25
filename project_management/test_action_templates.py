"""PROSEVIEW MOD-01: company-level action templates, points validation, progress by status,
automatic closing, frozen copies, company isolation, migration of existing data and Action Item
non-regression."""
from datetime import date
from io import StringIO
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from models.models import CompanyProfile, User
from project_management.models import (
    ActionItem,
    ActionItemProgressSetting,
    ActionTemplate,
    ActionTemplateSubAction,
    Milestone,
    Project,
)
from project_management.models.action_template import normalize_title
from project_management.services.action_progress import (
    TemplateApplicationError,
    apply_template,
    compute_progress,
    find_template,
    refresh_review_flags,
    status_state,
    validate_points,
)
from project_management.test_action_item_query_services import ActionItemQueryFixtureMixin

# The example of the brief: sub-actions 0/10/30, 0/10/20, 0/10/50.
SUBS = [
    {"title": "Sous-action 1", "in_progress_points": 10, "closed_points": 30},
    {"title": "Sous-action 2", "in_progress_points": 10, "closed_points": 20},
    {"title": "Sous-action 3", "in_progress_points": 10, "closed_points": 50},
]
VIEW = "project_management.views.action_template"


def allow(view=True, create=True, update=True, delete=True):
    def check(user, level):
        return {"view": view, "create": create, "update": update, "delete": delete}[level]

    return patch(f"{VIEW}._config_allowed", side_effect=check)


def make_company(label):
    admin = User.objects.create_user(
        email=f"mod01-{label}@example.com", password="password", first_name=label,
        last_name="Owner", user_type="company_user",
    )
    company = CompanyProfile.objects.create(
        admin_user=admin, company_name=f"{label} Co", company_address="S", company_town="T",
        company_tell_no="1", company_mob_no="2", company_web_address=f"https://{label}.example.com",
        company_email_address=f"{label}@example.com", company_tax_reference=f"tax-{label}",
        company_iban=f"iban-{label}",
    )
    User.objects.filter(pk=admin.pk).update(company=company)
    admin.refresh_from_db()
    return admin, company


def make_project(user, company, name):
    return Project.objects.create(
        project_name=name, solution="Energy Consultant Solution", contract_value=1,
        company_profile=company, user=user, created_by=user,
        project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
    )


class TemplateFixture(ActionItemQueryFixtureMixin):
    def setUp(self):
        super().setUp()
        self.other_user, self.other_company = make_company("other")
        self.client = APIClient()

    def make_template(self, title="Confinement", subs=SUBS, company=None):
        template = ActionTemplate.objects.create(company=company or self.company, title=title)
        for index, sub in enumerate(subs, start=1):
            ActionTemplateSubAction.objects.create(template=template, position=index, **sub)
        return template

    def make_parent_with_template(self, title="Confinement"):
        parent = self.create_parent(title)
        template = self.make_template(title)
        subs = apply_template(parent, template, user=self.user)
        return parent, template, subs

    def second_project_parent(self, name="Second"):
        """A parent action in a SECOND project of the same company."""
        project = make_project(self.user, self.company, name)
        phase = Milestone.objects.create(
            project=project, name="P", colour="#111111", status="open",
            start_date=date(2026, 1, 1), due_date=date(2026, 6, 30),
        )
        return ActionItem.objects.create(
            milestone=phase, name=name, status="open", target_date=date(2026, 1, 31), created_by=self.user
        )

    def set_status(self, item, status):
        item.refresh_from_db()
        item.status = status
        if status == "closed":
            item.closed_at, item.closed_by = date(2026, 1, 20), self.user
        item.save()


class TitleMatchingTests(TemplateFixture, TestCase):
    def test_normalization_ignores_case_and_extra_spaces_only(self):
        self.assertEqual(normalize_title("  Confinement   Salle\tA "), normalize_title("confinement salle a"))
        self.assertNotEqual(normalize_title("Confinement"), normalize_title("Confinemen"))
        self.assertNotEqual(normalize_title("Étanchéité"), normalize_title("Etancheite"))

    def test_original_title_is_kept_and_matched_exactly_from_the_project(self):
        template = self.make_template("  Confinement   Salle A ")
        self.assertEqual(template.title, "  Confinement   Salle A ")
        self.assertEqual(find_template(self.project, "CONFINEMENT salle a"), template)
        self.assertIsNone(find_template(self.project, "Confinement salle"))  # no approximate match
        self.assertIsNone(find_template(self.project, "   "))

    def test_normalized_duplicate_refused_in_same_company_by_the_database(self):
        self.make_template("Confinement")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActionTemplate.objects.create(company=self.company, title=" confinement ")

    def test_same_title_allowed_in_another_company_and_never_matched_across(self):
        self.make_template("Confinement")
        self.make_template("Confinement", company=self.other_company)
        self.assertEqual(ActionTemplate.objects.count(), 2)
        other_project = make_project(self.other_user, self.other_company, "Other project")
        found = find_template(other_project, "confinement")
        self.assertEqual(found.company_id, self.other_company.id)
        self.assertIsNone(find_template(self.project, "Montage"))

    def test_template_is_usable_from_two_projects_of_the_company(self):
        template = self.make_template("Confinement")
        second = make_project(self.user, self.company, "Second")
        self.assertEqual(find_template(self.project, "Confinement"), template)
        self.assertEqual(find_template(second, "Confinement"), template)


class PointsValidationTests(TemplateFixture, TestCase):
    def test_valid_example_totals_100(self):
        validate_points(SUBS)

    def test_closed_total_must_be_100(self):
        for subs in (SUBS[:2], SUBS + [{"title": "x", "in_progress_points": 0, "closed_points": 10}]):
            with self.assertRaises(ValueError):
                validate_points(subs)

    def test_in_progress_cannot_exceed_closed_or_leave_range(self):
        for bad in ((31, 30), (-1, 30), (0, 101)):
            with self.assertRaises(ValueError):
                validate_points([{"title": "a", "in_progress_points": bad[0], "closed_points": bad[1]}])

    def test_zero_point_sub_action_is_allowed(self):
        validate_points([
            {"title": "a", "in_progress_points": 0, "closed_points": 100},
            {"title": "b", "in_progress_points": 0, "closed_points": 0},
        ])

    def test_empty_template_refused(self):
        with self.assertRaises(ValueError):
            validate_points([])

    def test_database_constraint_is_a_backstop(self):
        sub = ActionItem.objects.create(
            parent_action_item=self.create_parent("P"), name="s", status="open", target_date=date(2026, 1, 10)
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActionItemProgressSetting.objects.create(action_item=sub, in_progress_points=50, closed_points=10)
        template = self.make_template()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActionTemplateSubAction.objects.create(template=template, position=9, title="x", in_progress_points=5, closed_points=1)


class ProgressByStatusTests(TemplateFixture, TestCase):
    def test_status_mapping_and_unknown_status(self):
        self.assertEqual(status_state("open"), "not_started")
        self.assertEqual(status_state(" In  Progress "), "in_progress")
        self.assertEqual(status_state("closed"), "closed")
        self.assertEqual(status_state("En attente client"), "unknown")  # never assimilated to in progress

    def test_expected_path_0_10_30_40_50_60_100(self):
        parent, _, subs = self.make_parent_with_template()
        progress = lambda: compute_progress(parent)["progress"]  # noqa: E731
        path = [progress()]
        self.set_status(subs[0], "in progress"); path.append(progress())   # 10
        self.set_status(subs[0], "closed"); path.append(progress())        # 30
        self.set_status(subs[1], "in progress"); path.append(progress())   # 40
        self.set_status(subs[2], "in progress"); path.append(progress())   # 50
        self.set_status(subs[1], "closed"); path.append(progress())        # 60
        self.set_status(subs[2], "closed"); path.append(progress())        # 100
        self.assertEqual(path, [0, 10, 30, 40, 50, 60, 100])

    def test_same_path_in_a_second_project_with_the_same_company_template(self):
        parent, template, _ = self.make_parent_with_template()
        other_parent = self.second_project_parent()
        other_subs = apply_template(other_parent, template, user=self.user)
        self.set_status(other_subs[0], "closed")
        self.assertEqual(compute_progress(other_parent)["progress"], 30)
        self.assertEqual(compute_progress(parent)["progress"], 0)  # independent copies

    def test_unknown_status_contributes_zero_and_is_reported(self):
        parent, _, subs = self.make_parent_with_template()
        self.set_status(subs[0], "En attente client")
        result = compute_progress(parent)
        self.assertEqual(result["progress"], 0)
        self.assertEqual(result["unmapped_statuses"], ["En attente client"])

    def test_going_back_from_in_progress_to_open_lowers_progress(self):
        parent, _, subs = self.make_parent_with_template()
        self.set_status(subs[0], "in progress")
        self.set_status(subs[0], "open")
        self.assertEqual(compute_progress(parent)["progress"], 0)

    def test_parent_without_template_is_unweighted(self):
        parent = self.create_parent("Manual")
        ActionItem.objects.create(parent_action_item=parent, name="s", status="open", target_date=date(2026, 1, 10))
        result = compute_progress(parent)
        self.assertEqual((result["mode"], result["progress"]), ("unweighted", None))


class AutomaticClosingTests(TemplateFixture, TestCase):
    def test_parent_closes_when_last_sub_action_closes(self):
        parent, _, subs = self.make_parent_with_template()
        for sub in subs[:2]:
            self.set_status(sub, "closed")
        parent.refresh_from_db()
        self.assertEqual(parent.status, "in progress")  # a closed sub-action has started the mother
        self.set_status(subs[2], "closed")
        parent.refresh_from_db()
        self.assertEqual((parent.status, parent.closed_by_id), ("closed", self.user.id))
        self.assertEqual(parent.closed_at, date(2026, 1, 20))
        self.assertEqual(compute_progress(parent)["progress"], 100)

    def test_zero_point_sub_action_must_be_closed_too(self):
        parent = self.create_parent("Zero")
        template = self.make_template("Zero", subs=[
            {"title": "A", "in_progress_points": 0, "closed_points": 100},
            {"title": "B", "in_progress_points": 0, "closed_points": 0},
        ])
        a, b = apply_template(parent, template, user=self.user)
        self.set_status(a, "closed")
        parent.refresh_from_db()
        # 100 points reached, but the 0-point sub-action is still open: 100 % without closing.
        self.assertEqual((parent.status, compute_progress(parent)["progress"]), ("in progress", 100))  # started, not yet closable
        self.set_status(b, "closed")
        parent.refresh_from_db()
        self.assertEqual(parent.status, "closed")

    def test_manually_added_sub_action_blocks_closing(self):
        parent, _, subs = self.make_parent_with_template()
        manual = ActionItem.objects.create(
            parent_action_item=parent, name="Manuelle", status="open", target_date=date(2026, 1, 10)
        )
        for sub in subs:
            self.set_status(sub, "closed")
        parent.refresh_from_db()
        self.assertEqual((parent.status, compute_progress(parent)["progress"]), ("in progress", 100))  # started, not yet closable
        self.set_status(manual, "closed")
        parent.refresh_from_db()
        self.assertEqual(parent.status, "closed")

    def test_parent_without_template_points_keeps_manual_closing(self):
        parent = self.create_parent("Manual")
        sub = ActionItem.objects.create(parent_action_item=parent, name="s", status="open", target_date=date(2026, 1, 10))
        self.set_status(sub, "closed")
        parent.refresh_from_db()
        self.assertEqual(parent.status, "open")

    def test_existing_rule_still_blocks_manual_parent_close_with_open_sub(self):
        parent, _, subs = self.make_parent_with_template()
        self.client.force_authenticate(self.user)
        with patch("project_management.permissions.ActionItemPermission.has_permission", return_value=True), patch(
            "project_management.permissions.ActionItemPermission.has_object_permission", return_value=True
        ):
            response = self.client.patch(reverse("actionitem-detail", args=[parent.id]), {"status": "closed"}, format="json")
        parent.refresh_from_db()
        self.assertEqual(parent.status, "open")
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))


class ApplicationScopeTests(TemplateFixture, TestCase):
    def test_application_copies_points_and_creates_ordered_sub_actions(self):
        parent, template, subs = self.make_parent_with_template()
        self.assertEqual([s.name for s in subs], ["Sous-action 1", "Sous-action 2", "Sous-action 3"])
        self.assertEqual([s.progress_setting.closed_points for s in subs], [30, 20, 50])
        self.assertTrue(all(s.status == "open" and s.parent_action_item_id == parent.id for s in subs))
        self.assertEqual(subs[0].progress_setting.source_template_title, "Confinement")

    def test_changing_or_deleting_template_never_changes_created_actions(self):
        parent, template, subs = self.make_parent_with_template()
        template.sub_actions.all().update(closed_points=0, in_progress_points=0)
        template.title = "Renamed"
        template.save()
        self.assertEqual([ActionItemProgressSetting.objects.get(action_item=s).closed_points for s in subs], [30, 20, 50])
        self.set_status(subs[0], "closed")
        self.assertEqual(compute_progress(parent)["progress"], 30)
        template.delete()
        self.assertEqual(compute_progress(parent)["progress"], 30)
        self.assertEqual(ActionItemProgressSetting.objects.filter(action_item__in=subs, source_template__isnull=True).count(), 3)

    def test_template_of_another_company_is_refused(self):
        parent = self.create_parent("Confinement")
        foreign = self.make_template("Confinement", company=self.other_company)
        with self.assertRaises(TemplateApplicationError):
            apply_template(parent, foreign, user=self.user)
        self.assertFalse(parent.sub_action_items.exists())

    def test_inaccessible_project_is_refused_for_the_caller(self):
        parent = self.create_parent("Confinement")
        template = self.make_template("Confinement")
        with self.assertRaises(TemplateApplicationError):
            apply_template(parent, template, user=self.other_user)
        self.assertFalse(parent.sub_action_items.exists())

    def test_ambiguous_template_is_refused_until_reviewed(self):
        parent = self.create_parent("Confinement")
        template = self.make_template("Confinement")
        ActionTemplate.objects.filter(pk=template.pk).update(needs_review=True)
        template.refresh_from_db()
        with self.assertRaises(TemplateApplicationError):
            apply_template(parent, template, user=self.user)
        self.assertIsNone(find_template(self.project, "Confinement"))

    def test_second_application_closed_parent_and_sub_action_are_refused(self):
        parent, template, subs = self.make_parent_with_template()
        with self.assertRaises(TemplateApplicationError):
            apply_template(parent, template, user=self.user)
        closed = self.create_parent("Closed", status="closed")
        with self.assertRaises(TemplateApplicationError):
            apply_template(closed, template, user=self.user)
        with self.assertRaises(TemplateApplicationError):
            apply_template(subs[0], template, user=self.user)

    def test_existing_actions_are_untouched(self):
        existing = self.create_parent("Legacy")
        self.assertFalse(ActionItemProgressSetting.objects.filter(action_item__parent_action_item=existing).exists())
        self.assertEqual(compute_progress(existing)["mode"], "unweighted")


class TemplateApiTests(TemplateFixture, TestCase):
    def body(self, **extra):
        data = {"title": "Confinement", "sub_actions": SUBS}
        data.update(extra)
        return data

    def test_create_read_update_delete_at_company_level(self):
        self.client.force_authenticate(self.user)
        with allow():
            created = self.client.post(reverse("action-templates"), self.body(), format="json")
            self.assertEqual(created.status_code, 201, created.data)
            self.assertEqual([s["position"] for s in created.data["sub_actions"]], [1, 2, 3])
            pk = created.data["id"]
            template = ActionTemplate.objects.get(pk=pk)
            self.assertEqual((template.company_id, template.origin_project_id), (self.company.id, None))
            self.assertEqual(len(self.client.get(reverse("action-templates")).data), 1)
            patched = self.client.patch(
                reverse("action-template-detail", args=[pk]), {"sub_actions": list(reversed(SUBS))}, format="json"
            )
            self.assertEqual(patched.status_code, 200, patched.data)
            self.assertEqual(patched.data["sub_actions"][0]["title"], "Sous-action 3")
            self.assertEqual(self.client.delete(reverse("action-template-detail", args=[pk])).status_code, 204)

    def test_client_cannot_choose_the_company(self):
        self.client.force_authenticate(self.user)
        with allow():
            created = self.client.post(
                reverse("action-templates"), self.body(company=self.other_company.id), format="json"
            )
        self.assertEqual(ActionTemplate.objects.get(pk=created.data["id"]).company_id, self.company.id)

    def test_lookup_uses_the_company_of_the_authorized_project(self):
        template = self.make_template("Confinement")
        second = make_project(self.user, self.company, "Second")
        self.client.force_authenticate(self.user)
        url = reverse("action-template-lookup")
        with patch(f"{VIEW}.can_view_interventions", return_value=True):
            for project in (self.project, second):
                hit = self.client.get(url, {"project": project.id, "title": " CONFINEMENT "})
                self.assertEqual(hit.data["match"]["id"], template.id)
            miss = self.client.get(url, {"project": self.project.id, "title": "Confinemen"})
            self.assertIsNone(miss.data["match"])
            self.client.force_authenticate(self.other_user)
            self.assertEqual(self.client.get(url, {"project": self.project.id, "title": "Confinement"}).status_code, 404)
        self.client.force_authenticate(self.user)
        with patch(f"{VIEW}.can_view_interventions", return_value=False):
            self.assertEqual(self.client.get(url, {"project": self.project.id, "title": "Confinement"}).status_code, 403)

    def test_duplicate_normalized_title_and_bad_points_are_400(self):
        self.client.force_authenticate(self.user)
        with allow():
            self.assertEqual(self.client.post(reverse("action-templates"), self.body(), format="json").status_code, 201)
            self.assertEqual(
                self.client.post(reverse("action-templates"), self.body(title="  confinement"), format="json").status_code, 400
            )
            self.assertEqual(
                self.client.post(reverse("action-templates"), self.body(title="B", sub_actions=SUBS[:2]), format="json").status_code, 400
            )
            inverted = self.client.post(
                reverse("action-templates"),
                self.body(title="D", sub_actions=[
                    {"title": "x", "in_progress_points": 90, "closed_points": 60},
                    {"title": "y", "in_progress_points": 0, "closed_points": 40},
                ]),
                format="json",
            )
            self.assertEqual(inverted.status_code, 400)
        self.assertEqual(ActionTemplate.objects.count(), 1)

    def test_same_title_is_allowed_in_another_company(self):
        self.make_template("Confinement", company=self.other_company)
        self.client.force_authenticate(self.user)
        with allow():
            self.assertEqual(self.client.post(reverse("action-templates"), self.body(), format="json").status_code, 201)

    def test_reader_can_read_but_not_write(self):
        template = self.make_template()
        self.client.force_authenticate(self.user)
        with allow(view=True, create=False, update=False, delete=False):
            self.assertEqual(self.client.get(reverse("action-templates")).status_code, 200)
            self.assertEqual(self.client.get(reverse("action-template-detail", args=[template.id])).status_code, 200)
            self.assertEqual(self.client.post(reverse("action-templates"), self.body(title="Z"), format="json").status_code, 403)
            self.assertEqual(
                self.client.patch(reverse("action-template-detail", args=[template.id]), {"title": "X"}, format="json").status_code, 403
            )
            self.assertEqual(self.client.delete(reverse("action-template-detail", args=[template.id])).status_code, 403)
        template.refresh_from_db()
        self.assertEqual(template.title, "Confinement")

    def test_no_view_right_is_403_and_update_only_cannot_delete(self):
        template = self.make_template()
        self.client.force_authenticate(self.user)
        with allow(view=False, create=False, update=False, delete=False):
            self.assertEqual(self.client.get(reverse("action-templates")).status_code, 403)
        with allow(view=True, create=True, update=True, delete=False):
            self.assertEqual(self.client.delete(reverse("action-template-detail", args=[template.id])).status_code, 403)
        self.assertTrue(ActionTemplate.objects.filter(pk=template.pk).exists())

    def test_other_company_cannot_reach_templates_even_with_rights(self):
        template = self.make_template()
        self.client.force_authenticate(self.other_user)
        with allow():
            self.assertEqual(self.client.get(reverse("action-templates")).data, [])
            self.assertEqual(self.client.get(reverse("action-template-detail", args=[template.id])).status_code, 404)
            self.assertEqual(
                self.client.patch(reverse("action-template-detail", args=[template.id]), {"title": "Z"}, format="json").status_code, 404
            )
            self.assertEqual(self.client.delete(reverse("action-template-detail", args=[template.id])).status_code, 404)
        self.assertTrue(ActionTemplate.objects.filter(pk=template.pk).exists())

    def test_real_company_rights_are_enforced_without_patching(self):
        # self.user has no role at all in this fixture: the real check must refuse, not grant.
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(reverse("action-templates")).status_code, 403)
        self.assertEqual(self.client.post(reverse("action-templates"), self.body(), format="json").status_code, 403)
        self.assertEqual(ActionTemplate.objects.count(), 0)

    def test_weighted_progress_endpoint_and_isolation(self):
        parent, _, subs = self.make_parent_with_template()
        self.set_status(subs[0], "closed")
        self.client.force_authenticate(self.user)
        with patch(f"{VIEW}.can_view_interventions", return_value=True):
            response = self.client.get(reverse("action-item-weighted-progress", args=[parent.id]))
            self.assertEqual((response.status_code, response.data["progress"]), (200, 30))
            self.assertEqual(self.client.get(reverse("action-item-weighted-progress", args=[subs[0].id])).status_code, 404)
            self.client.force_authenticate(self.other_user)
            self.assertEqual(self.client.get(reverse("action-item-weighted-progress", args=[parent.id])).status_code, 404)
        self.client.force_authenticate(self.user)
        with patch(f"{VIEW}.can_view_interventions", return_value=False):
            self.assertEqual(self.client.get(reverse("action-item-weighted-progress", args=[parent.id])).status_code, 403)


class CollisionReviewTests(TemplateFixture, TestCase):
    def flag_pair(self):
        a = self.make_template("Confinement")
        with transaction.atomic():  # the constraint ignores flagged rows: build the collision the migration leaves
            ActionTemplate.objects.filter(pk=a.pk).update(needs_review=True)
        b = self.make_template("confinement ")
        ActionTemplate.objects.filter(pk=b.pk).update(needs_review=True)
        return a, b

    def test_flagged_templates_are_listed_with_the_warning_and_not_applicable(self):
        a, b = self.flag_pair()
        self.client.force_authenticate(self.user)
        with allow():
            data = self.client.get(reverse("action-templates")).data
        self.assertEqual([t["needs_review"] for t in data], [True, True])
        self.assertIsNone(find_template(self.project, "confinement"))

    def test_renaming_one_resolves_both_without_merging(self):
        a, b = self.flag_pair()
        self.client.force_authenticate(self.user)
        with allow():
            blocked = self.client.patch(reverse("action-template-detail", args=[a.id]), {"title": "CONFINEMENT"}, format="json")
            self.assertEqual(blocked.status_code, 400)  # still collides with the other one
            ok = self.client.patch(reverse("action-template-detail", args=[a.id]), {"title": "Confinement salle A"}, format="json")
            self.assertEqual(ok.status_code, 200, ok.data)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.needs_review, b.needs_review), (False, False))
        self.assertEqual(ActionTemplate.objects.count(), 2)
        self.assertEqual(find_template(self.project, "confinement"), b)

    def test_deleting_one_clears_the_other(self):
        a, b = self.flag_pair()
        self.client.force_authenticate(self.user)
        with allow():
            self.assertEqual(self.client.delete(reverse("action-template-detail", args=[a.id])).status_code, 204)
        b.refresh_from_db()
        self.assertFalse(b.needs_review)

    def test_refresh_never_clears_a_group_that_still_collides(self):
        a, b = self.flag_pair()
        refresh_review_flags(self.company.id, [a.normalized_title])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.needs_review, b.needs_review), (True, True))


class ScopeMigrationTests(TransactionTestCase):
    """0236 (project-owned templates) -> 0237 (company-owned) on real rows, collisions included."""

    available_apps = None
    before = [("project_management", "0236_action_templates_progress")]
    after = [("project_management", "0237_action_templates_company_scope")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_templates_are_kept_attached_and_collisions_flagged(self):
        old_apps = self.migrate(self.before)
        Company = old_apps.get_model("models", "CompanyProfile")
        User_ = old_apps.get_model("models", "User")
        Project_ = old_apps.get_model("project_management", "Project")
        Template = old_apps.get_model("project_management", "ActionTemplate")
        Sub = old_apps.get_model("project_management", "ActionTemplateSubAction")
        admin = User_.objects.create(email="mig@example.com", first_name="m", last_name="m", user_type="company_user")
        company = Company.objects.create(
            admin_user=admin, company_name="Mig", company_address="S", company_town="T", company_tell_no="1",
            company_mob_no="2", company_web_address="https://m.example.com", company_email_address="m@example.com",
            company_tax_reference="tm", company_iban="im",
        )
        orphan = Project_.objects.create(
            project_name="No company", solution="Energy Consultant Solution", contract_value=1, user=admin,
            created_by=admin, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
        )
        p1, p2 = (
            Project_.objects.create(
                project_name=n, solution="Energy Consultant Solution", contract_value=1, company_profile=company,
                user=admin, created_by=admin, project_start_date=date(2026, 1, 1), project_end_date=date(2026, 12, 31),
            )
            for n in ("P1", "P2")
        )
        unique = Template.objects.create(project=p1, title="Montage", normalized_title="montage")
        twin_a = Template.objects.create(project=p1, title="Confinement", normalized_title="confinement")
        twin_b = Template.objects.create(project=p2, title=" confinement", normalized_title="confinement")
        lost = Template.objects.create(project=orphan, title="Solo", normalized_title="solo")
        Sub.objects.create(template=twin_a, position=1, title="s", in_progress_points=0, closed_points=100)

        new_apps = self.migrate(self.after)
        T = new_apps.get_model("project_management", "ActionTemplate")
        S = new_apps.get_model("project_management", "ActionTemplateSubAction")
        self.assertEqual(T.objects.count(), 4)  # nothing deleted, nothing merged
        self.assertEqual(S.objects.count(), 1)
        row = {t.pk: t for t in T.objects.all()}
        self.assertEqual((row[unique.pk].company_id, row[unique.pk].needs_review), (company.pk, False))
        self.assertEqual(row[unique.pk].origin_project_id, p1.pk)
        for twin in (twin_a, twin_b):
            self.assertEqual((row[twin.pk].company_id, row[twin.pk].needs_review), (company.pk, True))
        self.assertEqual(row[twin_b.pk].origin_project_id, p2.pk)
        self.assertEqual((row[lost.pk].company_id, row[lost.pk].needs_review), (None, True))
