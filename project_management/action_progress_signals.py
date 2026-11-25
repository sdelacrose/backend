"""PROSEVIEW MOD-01: close a weighted parent action when its last sub-action is closed.

Kept apart from signals.py (shared, large) and loaded from AppConfig.ready."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from project_management.models import ActionItem
from project_management.services.action_progress import (
    close_parent_if_complete,
    start_parent_if_child_started,
)


@receiver(post_save, sender=ActionItem, dispatch_uid="mod01_close_weighted_parent")
def close_weighted_parent(sender, instance, created, raw=False, **kwargs):
    if raw or not instance.parent_action_item_id:
        return
    # closing keeps its existing rule; otherwise an open mother follows a started sub-action
    if not close_parent_if_complete(instance):
        start_parent_if_child_started(instance)
