from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Prefetch

from project_management.models import ActionItem, Milestone, Project
from project_management.models.constants import (
    PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS,
    PROJECT_MILESTONE_ACTION_CONFIG,
    PROJECT_PHASE_TEMPLATE_TRANSLATIONS,
    PROJECT_TEMPLATE_LOCALE_ALIASES,
    get_localized_project_template,
)


def _known_names(translations):
    return {name for name in translations.values() if name}


def _project_locale(project):
    for user in (project.created_by, project.user):
        preferred_language = getattr(user, "preferred_language", None)
        normalized = (
            str(preferred_language or "").strip().lower().replace("_", "-")
        )
        locale = PROJECT_TEMPLATE_LOCALE_ALIASES.get(normalized)
        if locale:
            return locale
    return "en"


class Command(BaseCommand):
    help = (
        "Localize existing system-generated project phases and action items "
        "using the project creator's preferred language."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report changes without updating records.",
        )
        parser.add_argument(
            "--project-id",
            action="append",
            type=int,
            dest="project_ids",
            help="Limit processing to one or more project IDs.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Project iterator batch size.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        project_ids = options.get("project_ids") or []
        batch_size = options["batch_size"]
        stats = Counter()

        action_items = ActionItem.objects.filter(
            parent_action_item__isnull=True,
        ).order_by("id")
        phases = Milestone.objects.order_by("sort_order", "id").prefetch_related(
            Prefetch("action_items", queryset=action_items)
        )
        projects = (
            Project.objects.select_related("created_by", "user")
            .prefetch_related(Prefetch("milestones", queryset=phases))
            .order_by("id")
        )
        if project_ids:
            projects = projects.filter(id__in=project_ids)

        if dry_run:
            self.stdout.write(self.style.WARNING("Running in dry-run mode"))

        for project in projects.iterator(chunk_size=batch_size):
            stats["projects_scanned"] += 1
            locale = _project_locale(project)
            stats[f"projects_{locale}"] += 1
            with transaction.atomic():
                if project.project_type in PROJECT_MILESTONE_ACTION_CONFIG:
                    self._process_template_project(
                        project,
                        locale,
                        dry_run,
                        stats,
                    )
                else:
                    self._process_default_phase(
                        project,
                        locale,
                        dry_run,
                        stats,
                    )

        action_label = "Would update" if dry_run else "Updated"
        self.stdout.write(self.style.SUCCESS("Generated content migration complete"))
        self.stdout.write(f"Projects scanned: {stats['projects_scanned']}")
        self.stdout.write(
            "Target locales: "
            f"en={stats['projects_en']}, "
            f"de={stats['projects_de']}, "
            f"fr={stats['projects_fr']}"
        )
        self.stdout.write(f"{action_label} phases: {stats['phases_updated']}")
        self.stdout.write(
            f"{action_label} action items: {stats['actions_updated']}"
        )
        self.stdout.write(f"Unchanged phases: {stats['phases_unchanged']}")
        self.stdout.write(f"Unchanged action items: {stats['actions_unchanged']}")
        self.stdout.write(f"Skipped phases: {stats['phases_skipped']}")
        self.stdout.write(f"Skipped action items: {stats['actions_skipped']}")
        self.stdout.write(f"Ambiguous matches: {stats['ambiguous_matches']}")

    def _process_template_project(self, project, locale, dry_run, stats):
        target_template = get_localized_project_template(
            project.project_type,
            locale,
        )
        project_phases = list(project.milestones.all())
        project_action_items = [
            action_item
            for phase in project_phases
            for action_item in phase.action_items.all()
        ]

        for phase_template in target_template["milestones"]:
            phase_key = phase_template["template_key"]
            phase_names = _known_names(
                PROJECT_PHASE_TEMPLATE_TRANSLATIONS[phase_key]
            )
            phase_matches = [
                phase for phase in project_phases if phase.name in phase_names
            ]
            phase_candidates = []
            for phase in phase_matches:
                phase_action_items = list(phase.action_items.all())
                recognized_action_count = sum(
                    1
                    for action_template in phase_template["action_items"]
                    for action_item in phase_action_items
                    if action_item.name
                    in _known_names(
                        PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS[
                            action_template["template_key"]
                        ]
                    )
                )
                phase_candidates.append(
                    (phase, phase_action_items, recognized_action_count)
                )

            had_multiple_phase_matches = len(phase_candidates) > 1
            if had_multiple_phase_matches and phase_template["action_items"]:
                phase_candidates = [
                    candidate
                    for candidate in phase_candidates
                    if candidate[2] > 0
                ]

            if len(phase_candidates) != 1:
                stats["phases_skipped"] += 1
                if had_multiple_phase_matches:
                    stats["ambiguous_matches"] += 1
                continue

            phase, phase_action_items, recognized_action_count = phase_candidates[0]
            if phase_template["action_items"] and not recognized_action_count:
                stats["phases_skipped"] += 1
                stats["actions_skipped"] += len(phase_template["action_items"])
                continue

            self._update_name(
                Milestone,
                phase,
                phase_template["name"],
                "phases",
                dry_run,
                stats,
            )
            self._process_action_items(
                phase_action_items,
                project_action_items,
                phase_template["action_items"],
                dry_run,
                stats,
            )

    def _process_action_items(
        self,
        phase_action_items,
        project_action_items,
        action_templates,
        dry_run,
        stats,
    ):
        for action_template in action_templates:
            action_key = action_template["template_key"]
            action_names = _known_names(
                PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS[action_key]
            )
            action_matches = [
                action_item
                for action_item in phase_action_items
                if action_item.name in action_names
            ]
            if not action_matches:
                action_matches = [
                    action_item
                    for action_item in project_action_items
                    if action_item.name in action_names
                ]
            if len(action_matches) != 1:
                stats["actions_skipped"] += 1
                if len(action_matches) > 1:
                    stats["ambiguous_matches"] += 1
                continue

            self._update_name(
                ActionItem,
                action_matches[0],
                action_template["name"],
                "actions",
                dry_run,
                stats,
            )

    def _process_default_phase(self, project, locale, dry_run, stats):
        translations = PROJECT_PHASE_TEMPLATE_TRANSLATIONS[
            "default.project_phase"
        ]
        known_names = _known_names(translations)
        phase_matches = [
            phase
            for phase in project.milestones.all()
            if phase.name in known_names
            and phase.start_date == project.project_start_date
            and phase.due_date == project.project_end_date
        ]
        if len(phase_matches) != 1:
            stats["phases_skipped"] += 1
            if len(phase_matches) > 1:
                stats["ambiguous_matches"] += 1
            return

        self._update_name(
            Milestone,
            phase_matches[0],
            translations[locale],
            "phases",
            dry_run,
            stats,
        )

    @staticmethod
    def _update_name(model, instance, target_name, prefix, dry_run, stats):
        if instance.name == target_name:
            stats[f"{prefix}_unchanged"] += 1
            return

        if not dry_run:
            updated = model.objects.filter(
                pk=instance.pk,
                name=instance.name,
            ).update(name=target_name)
            if not updated:
                stats[f"{prefix}_skipped"] += 1
                return
        stats[f"{prefix}_updated"] += 1
