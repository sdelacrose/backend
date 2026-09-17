import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.utils.text import capfirst

from ecpmpapp.utils import (
    create_notification_with_recipients,
    get_actor_name,
    get_validate_recipients,
    should_notify_user_comprehensive,
)
from models.models import User
from project_management.models import Document
from project_management.services.bulk_notification_recipients import (
    get_project_bulk_notification_recipients,
)


logger = logging.getLogger(__name__)


def _field_label(field_name):
    try:
        return capfirst(Document._meta.get_field(field_name).verbose_name)
    except FieldDoesNotExist:
        return field_name.replace("_", " ").title()


def _document_recipients(document, project_recipients_by_id=None):
    recipients = {document.author}
    folder = document.folder
    if folder.project_id:
        if project_recipients_by_id is None:
            project_recipients = get_project_bulk_notification_recipients(
                folder.project
            )
        else:
            if folder.project.pk not in project_recipients_by_id:
                project_recipients_by_id[folder.project.pk] = (
                    get_project_bulk_notification_recipients(folder.project)
                )
            project_recipients = project_recipients_by_id[folder.project.pk]
        recipients.update(project_recipients)
    elif folder.company_profile_id and folder.company_profile.admin_user:
        recipients.add(folder.company_profile.admin_user)
    elif folder.freelancer_id:
        recipients.add(folder.freelancer)
    return {recipient for recipient in recipients if recipient}


def _document_queryset():
    return Document.objects.select_related(
        "author",
        "folder__project__user",
        "folder__project__created_by",
        "folder__project__company_profile",
        "folder__company_profile__admin_user",
        "folder__freelancer",
    )


def send_bulk_document_update_summary(
    *, document_ids, actor_id, changed_fields
):
    documents = list(
        _document_queryset().filter(pk__in=document_ids).order_by("id")
    )
    if not documents:
        logger.info(
            "Skipping bulk Document notification because no documents exist."
        )
        return

    actor = User.objects.filter(pk=actor_id).first()
    eligible_recipient_ids = set()
    project_recipients_by_id = {}
    for document in documents:
        project = document.folder.project
        candidates = get_validate_recipients(
            _document_recipients(document, project_recipients_by_id),
            actor,
        )
        for recipient in candidates:
            if should_notify_user_comprehensive(
                recipient,
                "documents",
                document,
                action_type="update",
                project=project,
            ):
                eligible_recipient_ids.add(recipient.id)

    if not eligible_recipient_ids:
        return

    first_document = documents[0]
    folder = first_document.folder
    summary_target = (
        folder.project
        or folder.company_profile
        or folder.freelancer
        or first_document
    )
    labels = ", ".join(_field_label(field) for field in changed_fields)
    create_notification_with_recipients(
        message=(
            f"{len(documents)} Documents were updated by "
            f"{get_actor_name(actor)} (fields: {labels})."
        ),
        content_type=ContentType.objects.get_for_model(summary_target),
        object_id=summary_target.pk,
        content_object=summary_target,
        notification_type="documents",
        action_type="update",
        recipients=User.objects.filter(pk__in=eligible_recipient_ids),
        project=folder.project,
        bypass_preference_check=True,
    )
