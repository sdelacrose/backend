import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.utils.text import capfirst

from ecpmpapp.constants import CUSTOM_LABELS_OBJECT_ASSESSMENT
from ecpmpapp.utils import (
    create_notification_with_recipients,
    get_actor_name,
    get_validate_recipients,
    should_notify_user_comprehensive,
)
from models.models import User
from project_management.models import ObjectAssessment
from project_management.services.bulk_notification_recipients import (
    get_project_bulk_notification_recipients,
)


logger = logging.getLogger(__name__)


def _field_label(field_name):
    if field_name in CUSTOM_LABELS_OBJECT_ASSESSMENT:
        return CUSTOM_LABELS_OBJECT_ASSESSMENT[field_name]
    try:
        return capfirst(ObjectAssessment._meta.get_field(field_name).verbose_name)
    except FieldDoesNotExist:
        return field_name.replace("_", " ").title()


def send_bulk_object_assessment_update_summary(
    *, object_assessment_ids, actor_id, changed_fields
):
    object_assessments = list(
        ObjectAssessment.objects.select_related(
            "project__user",
            "project__created_by",
            "project__company_profile",
        )
        .filter(pk__in=object_assessment_ids)
        .order_by("id")
    )
    if not object_assessments:
        logger.info(
            "Skipping bulk Object Assessment notification because no records exist."
        )
        return

    actor = User.objects.filter(pk=actor_id).first()
    eligible_recipient_ids = set()
    project_recipients_by_id = {}
    for object_assessment in object_assessments:
        project = object_assessment.project
        if project.pk not in project_recipients_by_id:
            project_recipients_by_id[project.pk] = (
                get_project_bulk_notification_recipients(project)
            )
        candidates = get_validate_recipients(
            project_recipients_by_id[project.pk],
            actor,
        )
        for recipient in candidates:
            if should_notify_user_comprehensive(
                recipient,
                "object_assessment",
                object_assessment,
                action_type="update",
                project=project,
            ):
                eligible_recipient_ids.add(recipient.id)

    if not eligible_recipient_ids:
        return

    project = object_assessments[0].project
    labels = ", ".join(_field_label(field) for field in changed_fields)
    create_notification_with_recipients(
        message=(
            f"{len(object_assessments)} Object Assessments were updated by "
            f"{get_actor_name(actor)} (fields: {labels})."
        ),
        content_type=ContentType.objects.get_for_model(project),
        object_id=project.pk,
        content_object=project,
        notification_type="object_assessment",
        action_type="update",
        recipients=User.objects.filter(pk__in=eligible_recipient_ids),
        project=project,
        bypass_preference_check=True,
    )
