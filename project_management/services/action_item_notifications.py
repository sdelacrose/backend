import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.utils.text import capfirst

from ecpmpapp.constants import CUSTOM_LABELS_ACTION_ITEMS, UserType
from ecpmpapp.utils import (
    create_notification_with_recipients,
    format_notification_message,
    get_actor_name,
    get_validate_recipients,
    should_notify_user_comprehensive,
)
from models.models import User
from project_management.models import ActionItem, ProjectTeamMembers


logger = logging.getLogger(__name__)


def _field_label(field_name):
    if field_name in CUSTOM_LABELS_ACTION_ITEMS:
        return CUSTOM_LABELS_ACTION_ITEMS[field_name]
    try:
        return capfirst(ActionItem._meta.get_field(field_name).verbose_name)
    except FieldDoesNotExist:
        return field_name.replace("_", " ").title()


def _project_recipients(project):
    recipients = {project.user, project.created_by}
    if project.company_profile_id:
        recipients.update(
            User.objects.filter(
                company_id=project.company_profile_id,
                groups__name=UserType.ADMIN.value,
                is_active=True,
            ).distinct()
        )
    recipients.update(
        User.objects.filter(
            id__in=ProjectTeamMembers.objects.filter(
                project=project
            ).values_list("team_member_id", flat=True),
            is_active=True,
        )
    )
    return {recipient for recipient in recipients if recipient}


def _project(action_item):
    owner = action_item.parent_action_item or action_item
    return owner.milestone.project if owner.milestone_id else None


def _general_recipients(action_item):
    recipients = {action_item.created_by}
    owner = action_item.parent_action_item or action_item
    if owner.company_profile_id and owner.company_profile.admin_user:
        recipients.add(owner.company_profile.admin_user)
    project = _project(action_item)
    if project:
        recipients.update(_project_recipients(project))
    return {recipient for recipient in recipients if recipient}


def _action_item_queryset():
    return ActionItem.objects.select_related(
        "created_by",
        "milestone__project__user",
        "milestone__project__created_by",
        "milestone__project__company_profile",
        "company_profile__admin_user",
        "freelancer",
        "parent_action_item__milestone__project__user",
        "parent_action_item__milestone__project__created_by",
        "parent_action_item__milestone__project__company_profile",
        "parent_action_item__company_profile__admin_user",
        "parent_action_item__freelancer",
    ).prefetch_related("assignees")


def send_action_item_saved_notification(
    *, action_item_id, actor_id, created, changed_fields, notify_assignees
):
    action_item = _action_item_queryset().filter(pk=action_item_id).first()
    if not action_item:
        logger.info(
            "Skipping Action Item notification because item %s no longer exists.",
            action_item_id,
        )
        return

    actor = User.objects.filter(pk=actor_id).first()
    actor_name = get_actor_name(actor)
    phrase = "Sub Action Item" if action_item.parent_action_item_id else "Action Item"
    project = _project(action_item)
    action_type = "create" if created else "update"
    labels = ", ".join(_field_label(field) for field in changed_fields)
    message = format_notification_message(
        action_type,
        f'{phrase} "{action_item.name}"',
        actor_name,
        project_name=project.project_name if project else None,
        field_changes=[labels] if labels else None,
    )

    if notify_assignees and not created:
        assignee_recipients = get_validate_recipients(
            set(action_item.assignees.all()),
            actor,
        )
        if assignee_recipients:
            create_notification_with_recipients(
                message=[
                    f'Assigned {phrase} "{action_item.name}" was updated by '
                    f"{actor_name}. (fields: {labels})"
                ],
                content_type=ContentType.objects.get_for_model(ActionItem),
                object_id=action_item.pk,
                content_object=action_item,
                notification_type="action_item",
                action_type="update",
                recipients=assignee_recipients,
                project=project,
            )

    recipients = get_validate_recipients(
        _general_recipients(action_item),
        actor,
    )
    recipients.difference_update(action_item.assignees.all())
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ActionItem),
        object_id=action_item.pk,
        content_object=action_item,
        notification_type="action_item",
        action_type=action_type,
        recipients=recipients,
        project=project,
    )


def send_action_item_assignee_notification(
    *, action_item_id, actor_id, event
):
    action_item = _action_item_queryset().filter(pk=action_item_id).first()
    if not action_item:
        return

    actor = User.objects.filter(pk=actor_id).first()
    actor_name = get_actor_name(actor)
    phrase = "Sub Action Item" if action_item.parent_action_item_id else "Action Item"
    project = _project(action_item)
    assignee_recipients = get_validate_recipients(
        set(action_item.assignees.all()),
        actor,
    )

    if event == "create":
        if assignee_recipients:
            create_notification_with_recipients(
                message=[
                    f'You have been assigned {phrase} "{action_item.name}" by '
                    f"{actor_name}."
                ],
                content_type=ContentType.objects.get_for_model(ActionItem),
                object_id=action_item.pk,
                content_object=action_item,
                notification_type="action_item",
                action_type="create",
                recipients=assignee_recipients,
                project=project,
            )
        return

    label = "Assignees"
    if assignee_recipients:
        create_notification_with_recipients(
            message=[
                f'Assigned {phrase} "{action_item.name}" was updated by '
                f"{actor_name}. (fields: {label})"
            ],
            content_type=ContentType.objects.get_for_model(ActionItem),
            object_id=action_item.pk,
            content_object=action_item,
            notification_type="action_item",
            action_type="update",
            recipients=assignee_recipients,
            project=project,
        )

    recipients = get_validate_recipients(
        _general_recipients(action_item),
        actor,
    )
    recipients.difference_update(action_item.assignees.all())
    create_notification_with_recipients(
        message=format_notification_message(
            "update",
            f'{phrase} "{action_item.name}"',
            actor_name,
            project_name=project.project_name if project else None,
            field_changes=[label],
        ),
        content_type=ContentType.objects.get_for_model(ActionItem),
        object_id=action_item.pk,
        content_object=action_item,
        notification_type="action_item",
        action_type="update",
        recipients=recipients,
        project=project,
    )


def send_bulk_action_item_update_summary(
    *, action_item_ids, actor_id, changed_fields
):
    action_items = list(
        _action_item_queryset().filter(pk__in=action_item_ids).order_by("id")
    )
    if not action_items:
        return

    actor = User.objects.filter(pk=actor_id).first()
    eligible_recipient_ids = set()
    for action_item in action_items:
        project = _project(action_item)
        candidates = _general_recipients(action_item)
        candidates.update(action_item.assignees.all())
        for recipient in get_validate_recipients(candidates, actor):
            if should_notify_user_comprehensive(
                recipient,
                "action_item",
                action_item,
                action_type="update",
                project=project,
            ):
                eligible_recipient_ids.add(recipient.id)

    if not eligible_recipient_ids:
        return

    first_item = action_items[0]
    owner = first_item.parent_action_item or first_item
    project = _project(first_item)
    summary_target = (
        project
        or owner.company_profile
        or owner.freelancer
        or first_item
    )
    labels = ", ".join(_field_label(field) for field in changed_fields)
    create_notification_with_recipients(
        message=(
            f"{len(action_items)} Action Items were updated by "
            f"{get_actor_name(actor)} (fields: {labels})."
        ),
        content_type=ContentType.objects.get_for_model(summary_target),
        object_id=summary_target.pk,
        content_object=summary_target,
        notification_type="action_item",
        action_type="update",
        recipients=User.objects.filter(pk__in=eligible_recipient_ids),
        project=project,
        bypass_preference_check=True,
    )
