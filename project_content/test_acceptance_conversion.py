import io
import json
from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.db.models.signals import post_save
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from models.models import CompanyProfile, Permission, Role, RolePermission, User
from project_management.models import (
    ActionItem,
    ActionItemComment,
    ActionItemCommentReaction,
    Document,
    ECPMPConfig,
    Folder,
    Milestone,
    Project,
    ProjectTeamMembers,
    Requirement,
)
from project_management.signals import create_default_folders

from project_content.models import (
    AcceptanceActionItemProvenance,
    AcceptanceChecklist,
    AcceptanceCriterion,
    AcceptanceCriterionComment,
    AcceptanceCriterionCommentReaction,
    AcceptanceCriterionEvidence,
    AcceptanceGenerationRun,
    ProjectContentAuditEvent,
)
from project_content.services import _acceptance_prior_state


class AcceptanceConversionCommandTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        notification_patcher.start()
        self.addCleanup(notification_patcher.stop)
        self.user = User.objects.create_user(
            email=f"acceptance-conversion-{uuid4()}@example.com",
            password="password",
            first_name="Conversion",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Conversion Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.user.email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Legacy acceptance conversion",
            project_description="Explicit conversion fixture",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        project_role = Role.objects.create(
            name="Conversion Action Item Editor",
            company=self.company,
            type="solution",
            scope="project",
        )
        permission, _ = Permission.objects.get_or_create(
            key="action_item.view",
            defaults={"name": "action_item.view", "module": "action_item"},
        )
        RolePermission.objects.create(role=project_role, permission=permission)
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    role="Owner",
                    project_role=project_role,
                    project_access="RW",
                    team_member=self.user,
                )
            ]
        )
        self.fat = Milestone.objects.create(
            project=self.project,
            name="Werkstest",
            start_date=date(2026, 9, 1),
            due_date=date(2026, 9, 7),
        )
        self.cat = Milestone.objects.create(
            project=self.project,
            name="User-edited site phase",
            start_date=date(2026, 10, 1),
            due_date=date(2026, 10, 7),
        )
        config = ECPMPConfig.objects.create(project=self.project)
        config.status_options = ["Open:#fff", "Done:#000"]
        config.save(update_fields=["status_options"])
        folder = Folder.objects.create(project=self.project, name="Evidence")
        self.document = Document.objects.create(
            folder=folder,
            file=ContentFile(b"acceptance evidence", name="evidence.txt"),
            file_name="evidence.txt",
            created_by=self.user,
        )
        self.requirement = Requirement.objects.create(
            project=self.project,
            name="Safety requirement",
            object_number="REQ-CONVERT-1",
            created_by=self.user,
        )
        pair_key = uuid4()
        self.criteria = []
        for phase, name in (
            (AcceptanceChecklist.Phase.FACTORY, "Legacy FAT"),
            (AcceptanceChecklist.Phase.SITE, "Legacy CAT"),
        ):
            checklist = AcceptanceChecklist.objects.create(
                project=self.project,
                phase=phase,
                display_name=name,
                created_by=self.user,
            )
            criterion = AcceptanceCriterion.objects.create(
                checklist=checklist,
                stable_key=f"legacy-{phase}",
                pair_key=pair_key,
                name=name,
                acceptance_criteria="The documented result is accepted.",
                start_date=date(2026, 9, 2) if phase.endswith("factory_acceptance") else None,
                end_date=date(2026, 9, 6) if phase.endswith("factory_acceptance") else None,
                forecast_date=date(2026, 9, 5),
                status="Legacy Passed",
                priority="High",
                labels=["Safety"],
                created_by=self.user,
            )
            criterion.assignees.add(self.user)
            criterion.requirements.add(self.requirement)
            criterion.documents.add(self.document)
            criterion.linked_folders.add(folder)
            AcceptanceCriterionEvidence.objects.create(
                criterion=criterion,
                requirement=self.requirement,
                quote="Safety requirement",
                excerpt_hash="evidence-hash",
            )
            comment = AcceptanceCriterionComment.objects.create(
                criterion=criterion,
                sender=self.user,
                content="Legacy execution note",
            )
            comment.attachments.add(self.document)
            comment.mentions.add(self.user)
            self.criteria.append(criterion)

    def _run(self, *args):
        output = io.StringIO()
        with patch("project_management.signals.create_notification_with_recipients"):
            call_command("convert_acceptance_to_action_items", *args, stdout=output)
        return json.loads(output.getvalue())

    def test_dry_run_reports_without_writes_and_requires_explicit_mapping(self):
        missing = self._run()
        self.assertEqual(missing["missing_mappings"], [
            {"project_id": self.project.id, "reason": "missing_mapping"}
        ])
        mapped = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
        )
        self.assertEqual(mapped["mode"], "dry-run")
        self.assertEqual(mapped["converted"], 2)
        self.assertEqual(len(mapped["status_fallbacks"]), 2)
        self.assertFalse(ActionItem.objects.exists())
        self.assertFalse(AcceptanceActionItemProvenance.objects.exists())

    def test_dry_run_does_not_report_a_no_op_status_fallback(self):
        criterion = self.criteria[0]
        criterion.status = "Open"
        criterion.save(update_fields=["status", "updated_at"])

        report = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
        )

        self.assertEqual(len(report["status_fallbacks"]), 1)
        self.assertNotIn(str(criterion.id), str(report["status_fallbacks"]))

    def test_apply_preserves_supported_data_and_is_idempotent(self):
        # Legacy acceptance comments allowed project owners to be mentioned even
        # when they were not explicit project team members.
        ProjectTeamMembers.objects.filter(
            project=self.project,
            team_member=self.user,
        ).delete()
        source_parent = self.criteria[0].comments.get()
        source_reply = AcceptanceCriterionComment.objects.create(
            criterion=self.criteria[0],
            sender=self.user,
            parent=source_parent,
            content="Legacy reply",
        )
        source_reaction = AcceptanceCriterionCommentReaction.objects.create(
            comment=source_reply,
            user=self.user,
            emoji="thumbs_up",
        )
        parent_updated_at = datetime(2026, 1, 2, 3, 4, tzinfo=dt_timezone.utc)
        reply_created_at = datetime(2026, 1, 3, 3, 4, tzinfo=dt_timezone.utc)
        reply_updated_at = datetime(2026, 1, 4, 3, 4, tzinfo=dt_timezone.utc)
        reaction_created_at = datetime(2026, 1, 5, 3, 4, tzinfo=dt_timezone.utc)
        AcceptanceCriterionComment.objects.filter(pk=source_parent.pk).update(
            updated_at=parent_updated_at
        )
        AcceptanceCriterionComment.objects.filter(pk=source_reply.pk).update(
            created_at=reply_created_at,
            updated_at=reply_updated_at,
        )
        AcceptanceCriterionCommentReaction.objects.filter(
            pk=source_reaction.pk
        ).update(created_at=reaction_created_at)

        args = (
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
            "--apply",
        )
        first = self._run(*args)
        second = self._run(*args)

        self.assertEqual(first["converted"], 2)
        self.assertEqual(first["comments"], 3)
        self.assertEqual(second["converted"], 0)
        self.assertEqual(second["already_converted"], 2)
        self.assertEqual(ActionItem.objects.count(), 2)
        self.assertEqual(ActionItemComment.objects.count(), 3)
        self.assertEqual(AcceptanceActionItemProvenance.objects.count(), 2)
        self.assertEqual(AcceptanceGenerationRun.objects.count(), 1)
        provenance = list(
            AcceptanceActionItemProvenance.objects.select_related("action_item").order_by(
                "phase_role"
            )
        )
        self.assertEqual([row.phase_role for row in provenance], ["cat", "fat"])
        self.assertEqual(len({row.pairing_key for row in provenance}), 1)
        fat_item = next(row.action_item for row in provenance if row.phase_role == "fat")
        self.assertEqual(fat_item.start_date, date(2026, 9, 2))
        self.assertEqual(fat_item.target_date, date(2026, 9, 6))
        self.assertEqual(fat_item.forecast_date, date(2026, 9, 5))
        self.assertEqual(fat_item.status, "Open")
        self.assertEqual(fat_item.priority_level, "High")
        self.assertEqual(fat_item.labels, ["Safety"])
        self.assertEqual(list(fat_item.assignees.all()), [self.user])
        self.assertEqual(list(fat_item.requirements.all()), [self.requirement])
        self.assertEqual(list(fat_item.documents.all()), [self.document])
        converted_parent = fat_item.comments.get(content="Legacy execution note")
        converted_reply = fat_item.comments.get(content="Legacy reply")
        self.assertEqual(converted_parent.attachments[0]["file_name"], "evidence.txt")
        self.assertEqual(
            converted_parent.mentions,
            [
                {
                    "user_id": self.user.id,
                    "name": self.user.full_name,
                    "user_image": "",
                }
            ],
        )
        self.assertEqual(converted_parent.updated_at, parent_updated_at)
        self.assertEqual(converted_reply.replied_to_id, converted_parent.id)
        self.assertEqual(converted_reply.created_at, reply_created_at)
        self.assertEqual(converted_reply.updated_at, reply_updated_at)
        converted_reaction = ActionItemCommentReaction.objects.get(
            comment=converted_reply,
            user=self.user,
            key="thumbs_up",
        )
        self.assertEqual(converted_reaction.created_at, reaction_created_at)
        self.assertEqual(provenance[0].source_evidence[0]["quote"], "Safety requirement")
        self.assertEqual(
            ProjectContentAuditEvent.objects.filter(
                event_type="acceptance_legacy_converted"
            ).count(),
            2,
        )

    def test_invalid_mapping_skips_project_without_partial_conversion(self):
        report = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.fat.id}",
            "--apply",
        )
        self.assertEqual(report["invalid_projects"][0]["reason"], "invalid_phase_mapping")
        self.assertFalse(ActionItem.objects.exists())
        self.assertTrue(AcceptanceCriterion.objects.exists())

    def test_mid_conversion_failure_rolls_back_the_whole_project(self):
        original_create = AcceptanceActionItemProvenance.objects.create
        calls = 0

        def fail_second_provenance(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated provenance failure")
            return original_create(**kwargs)

        with patch.object(
            AcceptanceActionItemProvenance.objects,
            "create",
            side_effect=fail_second_provenance,
        ):
            report = self._run(
                "--mapping",
                f"{self.project.id}:{self.fat.id}:{self.cat.id}",
                "--apply",
            )

        self.assertEqual(report["invalid_projects"][0]["reason"], "RuntimeError")
        self.assertFalse(ActionItem.objects.exists())
        self.assertFalse(AcceptanceActionItemProvenance.objects.exists())
        self.assertFalse(AcceptanceGenerationRun.objects.exists())

    def test_missing_creator_uses_project_actor_and_parent_is_reported(self):
        criterion = self.criteria[0]
        criterion.created_by = None
        criterion.modified_by = None
        criterion.save(update_fields=["created_by", "modified_by", "updated_at"])
        self.criteria[1].parent = criterion
        self.criteria[1].save(update_fields=["parent", "updated_at"])

        report = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
            "--apply",
        )

        converted = AcceptanceActionItemProvenance.objects.get(
            proposal_key=f"legacy:{criterion.id}"
        ).action_item
        self.assertEqual(converted.created_by, self.user)
        self.assertEqual(
            report["unsupported_relationships"],
            [{"criterion_id": str(self.criteria[1].id), "type": "parent"}],
        )

    def test_empty_legacy_assignees_remain_empty(self):
        criterion = self.criteria[0]
        criterion.assignees.clear()

        dry_run = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
        )
        self.assertEqual(dry_run["converted"], 2)

        self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
            "--apply",
        )
        converted = AcceptanceActionItemProvenance.objects.get(
            proposal_key=f"legacy:{criterion.id}"
        ).action_item
        self.assertEqual(list(converted.assignees.all()), [])
        self.assertIsNone(converted.assignee)

    def test_out_of_scope_legacy_mentions_are_reported_instead_of_silently_lost(self):
        outsider = User.objects.create_user(
            email=f"acceptance-outsider-{uuid4()}@example.com",
            password="password",
            first_name="Former",
            last_name="Member",
        )
        comment = self.criteria[0].comments.get()
        comment.mentions.add(outsider)

        dry_run = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
        )
        expected = {
            "criterion_id": str(self.criteria[0].id),
            "comment_id": str(comment.id),
            "type": "mention_user_unavailable",
            "user_id": outsider.id,
        }
        self.assertIn(expected, dry_run["unsupported_relationships"])

        report = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
            "--apply",
        )

        converted = AcceptanceActionItemProvenance.objects.get(
            proposal_key=f"legacy:{self.criteria[0].id}"
        ).action_item.comments.get(content="Legacy execution note")
        self.assertEqual(
            converted.mentions,
            [
                {
                    "user_id": self.user.id,
                    "name": self.user.full_name,
                    "user_image": "",
                }
            ],
        )
        self.assertIn(
            expected,
            report["unsupported_relationships"],
        )

    def test_unmappable_checklist_phase_is_reported_and_not_converted(self):
        checklist = self.criteria[1].checklist
        AcceptanceChecklist.objects.filter(pk=checklist.pk).update(phase="unknown")

        report = self._run(
            "--mapping",
            f"{self.project.id}:{self.fat.id}:{self.cat.id}",
            "--apply",
        )

        self.assertEqual(report["converted"], 1)
        self.assertEqual(
            report["unmappable_records"],
            [
                {
                    "criterion_id": str(self.criteria[1].id),
                    "reason": "unsupported_checklist_phase",
                }
            ],
        )
        self.assertEqual(ActionItem.objects.count(), 1)

    def test_prior_state_scales_to_500_requirements_and_1000_action_items(self):
        Requirement.objects.bulk_create(
            [
                Requirement(
                    project=self.project,
                    name=f"Requirement {index}",
                    object_number=f"REQ-PERF-{index}",
                    created_by=self.user,
                )
                for index in range(499)
            ]
        )
        run = AcceptanceGenerationRun.objects.create(
            project=self.project,
            fat_phase=self.fat,
            cat_phase=self.cat,
            status=AcceptanceGenerationRun.Status.APPLIED,
            schema_version="2",
            source_snapshot={},
            source_hash="performance-fixture",
            created_by=self.user,
        )
        items = ActionItem.objects.bulk_create(
            [
                ActionItem(
                    milestone=self.fat if index % 2 == 0 else self.cat,
                    name=f"Acceptance {index}",
                    notes="Representative generated criterion",
                    start_date=self.fat.start_date,
                    target_date=self.cat.due_date,
                    status="Open",
                    created_by=self.user,
                )
                for index in range(1000)
            ]
        )
        AcceptanceActionItemProvenance.objects.bulk_create(
            [
                AcceptanceActionItemProvenance(
                    generation_run=run,
                    proposal_key=f"performance-{index}",
                    phase_role="fat" if index % 2 == 0 else "cat",
                    action_item=item,
                    applied_fields={
                        "name": item.name,
                        "notes": item.notes,
                        "requirement_ids": [],
                    },
                    source_evidence=[],
                    created_by=self.user,
                )
                for index, item in enumerate(items)
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            state = _acceptance_prior_state(self.project)

        self.assertEqual(Requirement.objects.filter(project=self.project).count(), 500)
        self.assertEqual(len(state), 1000)
        self.assertLessEqual(len(queries), 4)
