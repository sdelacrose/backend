from functools import partial

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from ecpmpapp.utils import create_notification_with_recipients
from models.models import User
from project_management.models import Document, DocumentWorkflow


MESSAGES = {
    "English": {"CANCELLED": "Validation workflow stopped", "DRAFT": "Your signature is required", "VERIFY": "Your verification is required", "APPROVE": "Your approval is required", "APPROVED": "Internal approval completed", "REJECTED": "Document rejected — a corrected revision is required"},
    "French": {"CANCELLED": "Circuit de validation arrêté", "DRAFT": "Votre signature est attendue", "VERIFY": "Votre vérification est attendue", "APPROVE": "Votre approbation est attendue", "APPROVED": "Approbation interne terminée", "REJECTED": "Document rejeté — une révision corrigée est attendue"},
    "German": {"CANCELLED": "Freigabeworkflow gestoppt", "DRAFT": "Ihre Unterschrift ist erforderlich", "VERIFY": "Ihre Prüfung ist erforderlich", "APPROVE": "Ihre Freigabe ist erforderlich", "APPROVED": "Interne Freigabe abgeschlossen", "REJECTED": "Dokument abgelehnt — eine korrigierte Revision ist erforderlich"},
}
PENDING_ROLE = {"DRAFT": "author_id", "VERIFY": "verifier_id", "APPROVE": "approver_id"}


def schedule_workflow_notification(workflow, changed_role=None):
    """Only notify after commit. A rolled-back action never creates an alert."""
    status = workflow.status
    role = PENDING_ROLE.get(status)
    if changed_role and role != changed_role + "_id":
        return  # A replacement at a future step is notified when that step arrives.
    recipient_id = getattr(workflow, role) if role else workflow.author_id
    if recipient_id:
        transaction.on_commit(partial(
            deliver_workflow_notification, workflow.pk, status, recipient_id
        ), robust=True)


def deliver_workflow_notification(workflow_id, status, recipient_id):
    workflow = DocumentWorkflow.objects.select_related("revision__document__folder").filter(pk=workflow_id).first()
    if workflow is None:
        return
    role = PENDING_ROLE.get(status)
    if role and (workflow.status != status or getattr(workflow, role) != recipient_id):
        return  # Do not publish a stale task after a rapid transition/reassignment.
    recipient = User.objects.filter(pk=recipient_id, is_active=True, is_deleted=False, is_permanently_deleted=False).first()
    if recipient is None:
        return
    document = workflow.revision.document
    language = MESSAGES.get(recipient.preferred_language, MESSAGES["English"])
    message = f'{language[status]} : {document.full_document_number or "#" + str(document.pk)} — {document.file_name}'
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Document),
        object_id=document.pk,
        content_object=document,
        notification_type="documents",
        action_type="update",
        recipients=[recipient],
        important=True,
        project=document.folder.project if document.folder_id else None,
        # Operational assignments follow the existing mandatory "assigned to me" rule.
        # Document-level preferences alone do not recognise workflow verifier/approver.
        bypass_preference_check=True,
    )
    from project_management.services.document_workflow_email import send_workflow_email
    send_workflow_email(workflow, status, recipient, message)
