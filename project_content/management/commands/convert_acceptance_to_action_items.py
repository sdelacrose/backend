import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from project_management.models import ActionItem, ActionItemComment, Milestone, Project
from project_management.action_item_comment_mentions import (
    is_active_user,
    serialize_mention_user,
)

from project_content.audit import record_project_content_event
from project_content.models import (
    AcceptanceActionItemProvenance,
    AcceptanceChecklist,
    AcceptanceCriterion,
    AcceptanceGenerationRun,
)
from project_content.services import _current_acceptance_fields, _first_action_item_status


class BaseConversionError(RuntimeError):
    pass


class Command(BaseCommand):
    help = "Convert standalone FAT/CAT acceptance criteria into ordinary Action Items."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mapping",
            action="append",
            default=[],
            metavar="PROJECT_ID:FAT_PHASE_ID:CAT_PHASE_ID",
            help="Explicit project and Phase IDs. Repeat once per project.",
        )
        parser.add_argument("--apply", action="store_true")

    def _parse_mappings(self, values):
        mappings = {}
        for value in values:
            parts = str(value).split(":")
            if len(parts) != 3 or not all(part.isdigit() for part in parts):
                raise CommandError(
                    "Each --mapping must be PROJECT_ID:FAT_PHASE_ID:CAT_PHASE_ID."
                )
            project_id, fat_id, cat_id = map(int, parts)
            if project_id in mappings:
                raise CommandError(f"Project {project_id} has more than one mapping.")
            mappings[project_id] = {"fat": fat_id, "cat": cat_id}
        return mappings

    def _project_plan(self, project, mapping):
        if not mapping:
            raise BaseConversionError("missing_mapping")
        phases = {
            phase.id: phase
            for phase in Milestone.objects.filter(
                project=project,
                id__in=[mapping["fat"], mapping["cat"]],
            )
        }
        if mapping["fat"] == mapping["cat"] or len(phases) != 2:
            raise BaseConversionError("invalid_phase_mapping")
        if any(
            not phases[phase_id].start_date or not phases[phase_id].due_date
            for phase_id in mapping.values()
        ):
            raise BaseConversionError("phase_dates_required")
        criteria = list(
            AcceptanceCriterion.objects.filter(checklist__project=project)
            .select_related("checklist", "created_by", "modified_by")
            .prefetch_related(
                "assignees",
                "requirements",
                "documents",
                "linked_folders",
                "evidence",
                "comments__attachments",
                "comments__mentions",
                "comments__reactions",
            )
            .order_by("created_at", "id")
        )
        return phases, criteria

    def _valid_action_statuses(self, project):
        try:
            values = project.configurations.status_options
        except Exception:
            values = []
        return {
            str(value).split(":", 1)[0].strip()
            for value in values
            if str(value).split(":", 1)[0].strip()
        }

    def _source_evidence(self, criterion):
        evidence = []
        for row in criterion.evidence.all():
            evidence.append(
                {
                    "requirement_id": row.requirement_id,
                    "document_id": row.document_id,
                    "segment_id": row.segment_id,
                    "location": row.location,
                    "quote": row.quote,
                    "excerpt_hash": row.excerpt_hash,
                }
            )
        return evidence

    def _attachment_snapshot(self, document):
        try:
            url = document.file.url
        except Exception:
            url = document.file.name
        return {
            "url": url,
            "file_name": document.file_name or document.file.name.rsplit("/", 1)[-1],
        }

    def _phase_role(self, criterion, report):
        phase_roles = {
            AcceptanceChecklist.Phase.FACTORY: "fat",
            AcceptanceChecklist.Phase.SITE: "cat",
        }
        phase_role = phase_roles.get(criterion.checklist.phase)
        if phase_role is None:
            report["unmappable_records"].append(
                {
                    "criterion_id": str(criterion.id),
                    "reason": "unsupported_checklist_phase",
                }
            )
        return phase_role

    def _allowed_mention_user_ids(self, project):
        allowed_user_ids = set(
            project.team_members.values_list("team_member_id", flat=True)
        )
        allowed_user_ids.update(
            user_id
            for user_id in (project.user_id, project.created_by_id)
            if user_id
        )
        return allowed_user_ids

    def _legacy_mention_snapshots(
        self,
        *,
        comment,
        criterion,
        allowed_user_ids,
        report,
    ):
        snapshots = []
        for user in comment.mentions.all():
            if user.id in allowed_user_ids and is_active_user(user):
                snapshots.append(serialize_mention_user(user))
                continue
            report["unsupported_relationships"].append(
                {
                    "criterion_id": str(criterion.id),
                    "comment_id": str(comment.id),
                    "type": "mention_user_unavailable",
                    "user_id": user.id,
                }
            )
        return snapshots

    def _convert_project(self, project, mapping, report):
        phases, criteria = self._project_plan(project, mapping)
        actor = project.created_by or project.user
        run, _ = AcceptanceGenerationRun.objects.get_or_create(
            project=project,
            schema_version="2",
            source_hash=f"legacy-conversion:{project.id}:{mapping['fat']}:{mapping['cat']}",
            request_options={"legacy_acceptance_conversion": True},
            defaults={
                "fat_phase": phases[mapping["fat"]],
                "cat_phase": phases[mapping["cat"]],
                "status": AcceptanceGenerationRun.Status.APPLIED,
                "source_snapshot": {"conversion": "standalone_acceptance"},
                "draft_content": {"proposals": []},
                "created_by": actor,
                "modified_by": actor,
                "completed_at": timezone.now(),
                "hint_only": True,
            },
        )
        valid_statuses = self._valid_action_statuses(project)
        default_status = _first_action_item_status(project)
        allowed_mention_user_ids = self._allowed_mention_user_ids(project)
        criterion_items = {}
        for criterion in criteria:
            proposal_key = f"legacy:{criterion.id}"
            phase_role = self._phase_role(criterion, report)
            if phase_role is None:
                continue
            existing = AcceptanceActionItemProvenance.objects.filter(
                generation_run__project=project,
                proposal_key=proposal_key,
                phase_role=phase_role,
            ).first()
            if existing:
                criterion_items[criterion.id] = existing.action_item
                report["already_converted"] += 1
                continue
            assignees = list(criterion.assignees.all())
            phase = phases[mapping[phase_role]]
            status = criterion.status if criterion.status in valid_statuses else default_status
            if status != criterion.status:
                report["status_fallbacks"].append(
                    {
                        "criterion_id": str(criterion.id),
                        "from": criterion.status,
                        "to": status,
                    }
                )
            if criterion.parent_id:
                report["unsupported_relationships"].append(
                    {"criterion_id": str(criterion.id), "type": "parent"}
                )
            item = ActionItem.objects.create(
                milestone=phase,
                assignee=assignees[0] if assignees else None,
                name=criterion.name,
                notes=criterion.acceptance_criteria,
                start_date=criterion.start_date or phase.start_date,
                target_date=criterion.end_date or phase.due_date,
                forecast_date=criterion.forecast_date,
                status=status,
                priority_level=criterion.priority,
                labels=criterion.labels,
                created_by=criterion.created_by or actor,
            )
            item.assignees.set(assignees)
            item.requirements.set(criterion.requirements.all())
            item.documents.set(criterion.documents.all())
            item.linked_folders.set(criterion.linked_folders.all())
            ActionItem.objects.filter(pk=item.pk).update(
                created_at=criterion.created_at,
                updated_at=criterion.updated_at,
            )
            item.refresh_from_db()
            AcceptanceActionItemProvenance.objects.create(
                generation_run=run,
                proposal_key=proposal_key,
                phase_role=phase_role,
                pairing_key=criterion.pair_key,
                action_item=item,
                applied_fields=_current_acceptance_fields(item),
                source_evidence=self._source_evidence(criterion),
                created_by=criterion.created_by or actor,
            )
            criterion_items[criterion.id] = item
            report["converted"] += 1

            comments = list(criterion.comments.all().order_by("created_at", "id"))
            comment_map = {}
            for comment in comments:
                converted_comment = ActionItemComment.objects.create(
                    action_item=item,
                    sender=comment.sender,
                    content=comment.content,
                    attachments=[
                        self._attachment_snapshot(document)
                        for document in comment.attachments.all()
                    ],
                    mentions=self._legacy_mention_snapshots(
                        comment=comment,
                        criterion=criterion,
                        allowed_user_ids=allowed_mention_user_ids,
                        report=report,
                    ),
                )
                ActionItemComment.objects.filter(pk=converted_comment.pk).update(
                    created_at=comment.created_at,
                    updated_at=comment.updated_at,
                )
                comment_map[comment.id] = converted_comment
                report["comments"] += 1
            for comment in comments:
                converted_comment = comment_map[comment.id]
                if comment.parent_id and comment.parent_id in comment_map:
                    converted_comment.replied_to = comment_map[comment.parent_id]
                    converted_comment.save(update_fields=["replied_to"])
                for reaction in comment.reactions.all():
                    converted_reaction, _ = converted_comment.reactions.get_or_create(
                        user=reaction.user,
                        key=reaction.emoji,
                    )
                    converted_comment.reactions.filter(
                        pk=converted_reaction.pk
                    ).update(created_at=reaction.created_at)
                ActionItemComment.objects.filter(pk=converted_comment.pk).update(
                    created_at=comment.created_at,
                    updated_at=comment.updated_at,
                )

        record_project_content_event(
            project=project,
            event_type="acceptance_legacy_converted",
            instance=run,
            actor=actor,
            metadata={
                "converted": report["converted"],
                "status_fallback_count": len(report["status_fallbacks"]),
                "unsupported_count": len(report["unsupported_relationships"])
                + len(report["unmappable_records"]),
            },
        )

    def handle(self, *args, **options):
        mappings = self._parse_mappings(options["mapping"])
        project_ids = list(
            AcceptanceChecklist.objects.order_by("project_id")
            .values_list("project_id", flat=True)
            .distinct()
        )
        report = {
            "mode": "apply" if options["apply"] else "dry-run",
            "projects": len(project_ids),
            "converted": 0,
            "already_converted": 0,
            "comments": 0,
            "missing_mappings": [],
            "invalid_projects": [],
            "status_fallbacks": [],
            "unsupported_relationships": [],
            "unmappable_records": [],
        }
        for project_id in project_ids:
            project = Project.objects.filter(pk=project_id).first()
            if project is None:
                report["invalid_projects"].append(
                    {"project_id": project_id, "reason": "project_missing"}
                )
                continue
            mapping = mappings.get(project_id)
            try:
                _phases, criteria = self._project_plan(project, mapping)
            except BaseConversionError as exc:
                target = (
                    report["missing_mappings"]
                    if str(exc) == "missing_mapping"
                    else report["invalid_projects"]
                )
                target.append({"project_id": project_id, "reason": str(exc)})
                continue
            if not options["apply"]:
                valid_statuses = self._valid_action_statuses(project)
                default_status = _first_action_item_status(project)
                allowed_mention_user_ids = self._allowed_mention_user_ids(project)
                for criterion in criteria:
                    if self._phase_role(criterion, report) is None:
                        continue
                    report["converted"] += 1
                    if (
                        criterion.status not in valid_statuses
                        and criterion.status != default_status
                    ):
                        report["status_fallbacks"].append(
                            {
                                "criterion_id": str(criterion.id),
                                "from": criterion.status,
                                "to": default_status,
                            }
                        )
                    if criterion.parent_id:
                        report["unsupported_relationships"].append(
                            {"criterion_id": str(criterion.id), "type": "parent"}
                        )
                    for comment in criterion.comments.all():
                        self._legacy_mention_snapshots(
                            comment=comment,
                            criterion=criterion,
                            allowed_user_ids=allowed_mention_user_ids,
                            report=report,
                        )
                report["comments"] += sum(
                    criterion.comments.count()
                    for criterion in criteria
                    if criterion.checklist.phase
                    in {
                        AcceptanceChecklist.Phase.FACTORY,
                        AcceptanceChecklist.Phase.SITE,
                    }
                )
                continue
            project_report = {
                "converted": 0,
                "already_converted": 0,
                "comments": 0,
                "status_fallbacks": [],
                "unsupported_relationships": [],
                "unmappable_records": [],
            }
            try:
                with transaction.atomic():
                    locked_project = Project.objects.select_for_update().get(
                        pk=project_id
                    )
                    self._convert_project(locked_project, mapping, project_report)
            except Exception as exc:
                report["invalid_projects"].append(
                    {"project_id": project_id, "reason": exc.__class__.__name__}
                )
                continue
            for key in ("converted", "already_converted", "comments"):
                report[key] += project_report[key]
            for key in (
                "status_fallbacks",
                "unsupported_relationships",
                "unmappable_records",
            ):
                report[key].extend(project_report[key])

        self.stdout.write(json.dumps(report, sort_keys=True))
