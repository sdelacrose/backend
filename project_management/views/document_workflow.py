from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.db import transaction
from models.models import User
from rest_framework import status, viewsets
from rest_framework.response import Response

from project_management.models import Document, DocumentRevision
from project_management.services.document_cycle import assert_can_manage_document_cycle, can_stop_workflow
from project_management.serializers.document_register import user_reference
from project_management.services.document_register import (
    assert_can_update_document,
    get_allowed_owner_queryset,
    get_register_document_or_404,
)
from project_management.services.document_workflow import (
    change_actor,
    get_workflow_for_revision,
    perform_action,
    stop_workflow,
    start_workflow,
    start_workflow_for_document,
)


def serialize_workflow(workflow, user=None):
    return {
        "id": workflow.id,
        "revision": workflow.revision_id,
        "workflow_type": workflow.workflow_type,
        "status": workflow.status,
        "is_active": workflow.is_active,
        "can_stop": can_stop_workflow(user, workflow),
        "author": user_reference(workflow.author),
        "verifier": user_reference(workflow.verifier),
        "approver": user_reference(workflow.approver),
        "created_by": user_reference(workflow.created_by),
        "created_at": workflow.created_at,
        "started_at": workflow.started_at,
        "completed_at": workflow.completed_at,
        "events": [
            {
                "id": event.id,
                "step": event.step,
                "action": event.action,
                "actor": user_reference(event.actor),
                "comment": event.comment,
                # Only set for CHANGE_ACTOR events (actor = who made the change).
                "previous_actor": user_reference(event.previous_actor),
                "new_actor": user_reference(event.new_actor),
                "created_at": event.created_at,
            }
            for event in workflow.events.all()
        ],
    }


# Replacing the verifier / approver goes through the same action endpoint.
CHANGE_ACTIONS = {"CHANGE_VERIFIER": "verifier", "CHANGE_APPROVER": "approver"}


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}


def _user_or_none(value):
    if value in (None, ""):
        return None
    try:
        return User.objects.filter(pk=int(value)).first()
    except (TypeError, ValueError):
        return None


class DocumentWorkflowViewSet(viewsets.GenericViewSet):
    """Internal workflow of one DocumentRevision.

    Access starts from the accessible Documents queryset (404 otherwise); the
    revision is only ever fetched through its accessible Document. Starting a
    workflow needs the permission to update the document. Every transition rule is
    enforced in services.document_workflow.
    """

    filter_backends = []

    def _revision(self, request, document_id, revision_id):
        document = get_register_document_or_404(request, document_id=document_id)
        revision = DocumentRevision.objects.filter(
            document=document, pk=revision_id
        ).first()
        if revision is None:
            raise Http404
        return document, revision

    def retrieve(self, request, document_id=None, revision_id=None, *args, **kwargs):
        _document, revision = self._revision(request, document_id, revision_id)
        workflow = get_workflow_for_revision(revision)
        return Response(
            {"workflow": serialize_workflow(workflow, request.user) if workflow else None}
        )

    def _start(self, request, document, revision):
        assert_can_update_document(request.user, document)
        assert_can_manage_document_cycle(request.user, document)
        data = request.data
        allowed = get_allowed_owner_queryset(request.user)
        try:
            with transaction.atomic():
                # Serialize author assignment, launch and optional signature as one operation.
                locked_document = Document.objects.select_for_update().get(pk=document.pk)
                assert_can_manage_document_cycle(request.user, locked_document)
                if revision is not None:
                    from project_management.services.document_workflow import get_current_revision
                    current = get_current_revision(locked_document)
                    if current is None or current.pk != revision.pk:
                        raise DjangoValidationError({"revision": "Only the current document revision can start a workflow."})
                author = _user_or_none(data.get("author"))
                if locked_document.author_id:
                    if author is not None and author.pk != locked_document.author_id:
                        raise DjangoValidationError({"author": "The workflow must use the document's assigned author."})
                    author = locked_document.author
                args = (
                    data.get("workflow_type"), author,
                    _user_or_none(data.get("verifier")),
                    _user_or_none(data.get("approver")), request.user,
                )
                # Company actors are also available in the launch form. Do not start
                # an inaccessible step: selection never grants project/document access.
                if locked_document.folder.project_id:
                    from types import SimpleNamespace
                    from project_management.services.document_access import get_accessible_document_queryset
                    for field, actor in zip(('author', 'verifier', 'approver'), args[1:4]):
                        if actor is None or not actor.company_id:
                            continue
                        actor_request = SimpleNamespace(user=actor, query_params=request.query_params)
                        if not get_accessible_document_queryset(actor_request,
                            project_id=locked_document.folder.project_id).filter(pk=locked_document.pk).exists():
                            raise DjangoValidationError({field: 'This actor cannot access the document. Assign the appropriate project role before starting the workflow.'})
                if revision is None:
                    workflow = start_workflow_for_document(
                        locked_document, *args, allowed_users=allowed
                    )
                else:
                    workflow = start_workflow(revision, *args, allowed_users=allowed)
                # START records the launcher. SIGN is only recorded for the assigned author.
                if workflow.author_id == request.user.pk:
                    perform_action(workflow, request.user, "SIGN", "")
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(
            serialize_workflow(get_workflow_for_revision(workflow.revision), request.user),
            status=status.HTTP_201_CREATED,
        )

    def start(self, request, document_id=None, revision_id=None, *args, **kwargs):
        document, revision = self._revision(request, document_id, revision_id)
        return self._start(request, document, revision)

    def start_current(self, request, document_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        return self._start(request, document, None)

    def act(self, request, document_id=None, revision_id=None, *args, **kwargs):
        _document, revision = self._revision(request, document_id, revision_id)
        workflow = get_workflow_for_revision(revision)
        if workflow is None:
            raise Http404
        action = request.data.get("action")
        try:
            if action == "STOP":
                stop_workflow(workflow, request.user, request.data.get("comment"))
            elif action in CHANGE_ACTIONS:
                change_actor(
                    workflow,
                    request.user,
                    CHANGE_ACTIONS[action],
                    _user_or_none(request.data.get("new_actor")),
                    request.data.get("comment"),
                    allowed_users=get_allowed_owner_queryset(request.user),
                )
            else:
                perform_action(
                    workflow, request.user, action, request.data.get("comment")
                )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_workflow(get_workflow_for_revision(revision), request.user))
