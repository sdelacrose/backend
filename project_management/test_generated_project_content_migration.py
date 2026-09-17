from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from models.models import User
from project_management.models import ActionItem, Milestone, Project
from project_management.models.constants import get_localized_project_template


class GeneratedProjectContentMigrationTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            email="generated-content@example.com",
            password="password",
            first_name="Generated",
            last_name="Creator",
            user_type="freelancer",
            preferred_language="French",
        )

    def create_template_project(self, *, created_by=True):
        project = Project(
            project_name="Generated project",
            project_type="Angebot",
            contract_value=0,
            project_start_date=date(2026, 1, 1),
            project_end_date=date(2026, 12, 31),
            user=self.creator,
            created_by=self.creator if created_by else None,
        )
        Project.objects.bulk_create([project])
        template = get_localized_project_template("Angebot", "de")

        for index, phase_template in enumerate(template["milestones"], start=1):
            phase = Milestone(
                project=project,
                sort_order=index,
                name=phase_template["name"],
                start_date=project.project_start_date,
                due_date=project.project_start_date
                + timedelta(days=phase_template["due_date"]),
                reminder_setting="2 days",
            )
            Milestone.objects.bulk_create([phase])
            ActionItem.objects.bulk_create(
                [
                    ActionItem(
                        milestone=phase,
                        name=action_template["name"],
                        start_date=project.project_start_date,
                        target_date=project.project_start_date
                        + timedelta(days=action_template["due_date"]),
                        reminder="2 days",
                        status="open",
                    )
                    for action_template in phase_template["action_items"]
                ]
            )
        return project

    def test_dry_run_reports_without_updating(self):
        project = self.create_template_project()
        output = StringIO()

        call_command(
            "migrate_generated_project_content",
            "--dry-run",
            "--project-id",
            str(project.id),
            stdout=output,
        )

        self.assertEqual(
            project.milestones.order_by("sort_order").first().name,
            "Anfrage",
        )
        self.assertIn("Would update phases: 3", output.getvalue())
        self.assertIn("Would update action items: 6", output.getvalue())

    def test_apply_uses_creator_preferred_language(self):
        project = self.create_template_project()

        call_command(
            "migrate_generated_project_content",
            "--project-id",
            str(project.id),
            stdout=StringIO(),
        )

        first_phase = project.milestones.order_by("sort_order").first()
        first_action = first_phase.action_items.order_by("id").first()
        self.assertEqual(first_phase.name, "Demande initiale")
        self.assertEqual(
            first_action.name,
            "Responsable de la demande assigné",
        )

    def test_user_language_is_used_when_created_by_is_missing(self):
        project = self.create_template_project(created_by=False)

        call_command(
            "migrate_generated_project_content",
            "--project-id",
            str(project.id),
            stdout=StringIO(),
        )

        self.assertEqual(
            project.milestones.order_by("sort_order").first().name,
            "Demande initiale",
        )

    def test_renamed_phase_and_its_actions_are_skipped(self):
        project = self.create_template_project()
        phase = project.milestones.order_by("sort_order").first()
        phase.name = "Customer-specific phase"
        Milestone.objects.filter(pk=phase.pk).update(name=phase.name)

        call_command(
            "migrate_generated_project_content",
            "--project-id",
            str(project.id),
            stdout=StringIO(),
        )

        phase.refresh_from_db()
        first_action = phase.action_items.order_by("id").first()
        self.assertEqual(phase.name, "Customer-specific phase")
        self.assertEqual(first_action.name, "Anfrageperson zugeteilt")

    def test_empty_duplicate_phase_does_not_hide_generated_phase(self):
        project = self.create_template_project()
        generated_phase = project.milestones.order_by("sort_order").first()
        duplicate_phase = Milestone(
            project=project,
            sort_order=99,
            name=generated_phase.name,
            start_date=project.project_start_date,
            due_date=project.project_start_date,
            reminder_setting="none",
        )
        Milestone.objects.bulk_create([duplicate_phase])
        output = StringIO()

        call_command(
            "migrate_generated_project_content",
            "--project-id",
            str(project.id),
            stdout=output,
        )

        generated_phase.refresh_from_db()
        duplicate_phase.refresh_from_db()
        self.assertEqual(generated_phase.name, "Demande initiale")
        self.assertEqual(duplicate_phase.name, "Anfrage")
        self.assertIn("Ambiguous matches: 0", output.getvalue())

    def test_generated_action_moved_to_another_phase_is_localized(self):
        project = self.create_template_project()
        generated_phase = project.milestones.order_by("sort_order").first()
        moved_action = generated_phase.action_items.order_by("id").first()
        custom_phase = Milestone(
            project=project,
            sort_order=99,
            name="Customer phase",
            start_date=project.project_start_date,
            due_date=project.project_end_date,
            reminder_setting="none",
        )
        Milestone.objects.bulk_create([custom_phase])
        ActionItem.objects.filter(pk=moved_action.pk).update(
            milestone=custom_phase,
        )

        call_command(
            "migrate_generated_project_content",
            "--project-id",
            str(project.id),
            stdout=StringIO(),
        )

        moved_action.refresh_from_db()
        self.assertEqual(
            moved_action.name,
            "Responsable de la demande assigné",
        )
