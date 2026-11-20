"""PROSEVIEW MOD-01: Action Item templates (modeles d'actions) and progress by status.

Nothing here replaces the Action Item engine: a template only *describes* a parent Action Item
title and its ordered sub-actions, each with the points it contributes to the parent when it is
"in progress" and when it is "closed". Applying a template creates ordinary sub-action items and
COPIES the points onto them (ActionItemProgressSetting), so editing or deleting a template later
never changes the actions already created.
"""
from django.conf import settings
from django.db import models
from django.db.models import F, Q

from models.models import CompanyProfile

from .project import ActionItem, Project


def normalize_title(value):
    """Matching key of a title: surrounding/repeated whitespace ignored, case ignored.

    Accents and punctuation are kept on purpose: there is no approximate matching."""
    return " ".join(str(value or "").split()).casefold()


class ActionTemplate(models.Model):
    """A template belongs to a COMPANY (company configuration) and is usable in every project of
    that company. `origin_project` is provenance only: templates created before the scope
    correction (0236) were project-owned; it never grants or limits access."""

    company = models.ForeignKey(
        CompanyProfile, related_name="action_templates", on_delete=models.CASCADE, null=True, blank=True
    )
    origin_project = models.ForeignKey(
        Project, related_name="origin_action_templates", on_delete=models.SET_NULL, null=True, blank=True
    )
    title = models.CharField(max_length=255)  # original title, kept as typed
    normalized_title = models.CharField(max_length=255, editable=False)
    # Set by the 0237 data migration on templates whose normalized title collides inside one
    # company (they came from several projects). Never merged automatically: such a template is
    # listed with a warning and cannot be applied until a user renames it (see services).
    needs_review = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="+", on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "normalized_title"],
                condition=Q(needs_review=False),
                name="unique_action_template_title_per_company",
            )
        ]

    def save(self, *args, **kwargs):
        self.normalized_title = normalize_title(self.title)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "title" in update_fields:
            kwargs["update_fields"] = list(set(update_fields) | {"normalized_title"})
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class ActionTemplateSubAction(models.Model):
    template = models.ForeignKey(ActionTemplate, related_name="sub_actions", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    title = models.CharField(max_length=255)
    in_progress_points = models.PositiveSmallIntegerField(default=0)
    closed_points = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["template", "position"], name="unique_template_sub_action_position"),
            models.CheckConstraint(
                condition=Q(in_progress_points__lte=F("closed_points")) & Q(closed_points__lte=100),
                name="template_sub_action_points_range",
            ),
        ]


class ActionItemSubCounter(models.Model):
    """Locked per-mother sequence for sub-action references (same pattern as the other LINKS
    reference counters, e.g. InterventionReferenceCounter)."""

    parent = models.OneToOneField(ActionItem, related_name="sub_counter", on_delete=models.CASCADE)
    last_sequence = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(last_sequence__gte=0), name="sub_counter_non_negative"),
        ]


def allocate_sub_sequence(parent_id):
    """Next rank under a mother. Serialised by the mother's row lock, so two concurrent additions
    never get the same rank; the unique constraint (parent, sub_sequence) is the last backstop."""
    from django.db import transaction
    from django.db.models import Max

    with transaction.atomic():
        ActionItem.objects.select_for_update().only("id").get(pk=parent_id)
        counter = ActionItemSubCounter.objects.filter(parent_id=parent_id).first()
        if counter is None:
            highest = (
                ActionItem.objects.filter(parent_action_item_id=parent_id)
                .aggregate(top=Max("sub_sequence"))
                .get("top")
                or 0
            )
            counter = ActionItemSubCounter.objects.create(parent_id=parent_id, last_sequence=highest)
        counter.last_sequence += 1
        counter.save(update_fields=["last_sequence"])
        return counter.last_sequence


class ActionItemProgressSetting(models.Model):
    """Points copied onto a sub-action when a template is applied (never read back from the template)."""

    action_item = models.OneToOneField(ActionItem, related_name="progress_setting", on_delete=models.CASCADE)
    in_progress_points = models.PositiveSmallIntegerField(default=0)
    closed_points = models.PositiveSmallIntegerField(default=0)
    # PROSEVIEW MOD-02: "BPU" = service outside the initial framework contract. Independent for
    # every sub-action, false by default, never a default of a template.
    is_bpu = models.BooleanField(default=False)
    source_template = models.ForeignKey(
        ActionTemplate, related_name="+", on_delete=models.SET_NULL, null=True, blank=True
    )
    source_template_title = models.CharField(max_length=255, blank=True, default="")
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(in_progress_points__lte=F("closed_points")) & Q(closed_points__lte=100),
                name="action_progress_setting_points_range",
            )
        ]
