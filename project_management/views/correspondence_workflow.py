"""LOT 3D-3D1: internal validation workflow of an OUT Correspondence.

Access starts from the accessible Correspondence queryset (404 otherwise, same as
CorrespondenceViewSet) -- starting a workflow needs documents.correspondence_update (the same
permission that already gates editing the Correspondence itself, mirroring DocumentWorkflow's
own reuse of the Document update permission rather than a new granular key, section 13). Every
transition rule (including "only the assigned actor may act", true even for a full_access user)
is enforced in services.correspondence_workflow, not here.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from models.models import User
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from project_management.permissions import CorrespondencePermission
from project_management.views.correspondence import (
    _user_reference,
    correspondence_queryset_for_user,
)
from project_management.services.correspondence_workflow import (
    allowed_actor_queryset,
    get_latest_workflow,
    perform_action,
    ready_to_send,
    start_validation,
    validation_state,
)


def serialize_workflow(workflow):
    if workflow is None:
        return None
    return {
        "id": workflow.id,
        "correspondence": workflow.correspondence_id,
        "workflow_type": workflow.workflow_type,
        "status": workflow.status,
        "is_active": workflow.is_active,
        "author": _user_reference(workflow.author),
        "verifier": _user_reference(workflow.verifier),
        "approver": _user_reference(workflow.approver),
        "started_by": _user_reference(workflow.started_by),
        "created_at": workflow.created_at,
        "completed_at": workflow.completed_at,
        "events": [
            {
                "id": event.id,
                "step": event.step,
                "action": event.action,
                "actor": _user_reference(event.actor),
                "comment": event.comment,
                "created_at": event.created_at,
            }
            for event in workflow.events.all()
        ],
    }


def _user_or_none(value):
    if value in (None, ""):
        return None
    try:
        return User.objects.filter(pk=int(value)).first()
    except (TypeError, ValueError):
        return None


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}


class CorrespondenceValidationWorkflowViewSet(viewsets.GenericViewSet):
    filter_backends = []

    def _correspondence(self, request, correspondence_id, action):
        correspondence = correspondence_queryset_for_user(request.user).filter(
            pk=correspondence_id
        ).first()
        if correspondence is None:
            raise Http404
        if not CorrespondencePermission(action).has_object_permission(
            request, self, correspondence
        ):
            raise PermissionDenied()
        return correspondence

    def retrieve(self, request, correspondence_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "view")
        workflow = get_latest_workflow(correspondence)
        return Response(
            {
                "workflow": serialize_workflow(workflow),
                "validation_state": validation_state(correspondence),
                "ready_to_send": ready_to_send(correspondence),
            }
        )

    def actor_options(self, request, correspondence_id=None, *args, **kwargs):
        # Section 7: the exact scope the frontend's existing member picker already offers
        # (project team in Project scope, company members in Company scope) -- exposed here so
        # the workflow start dialog can populate Author/Verifier/Approver without a second,
        # independently-maintained directory.
        correspondence = self._correspondence(request, correspondence_id, "view")
        users = allowed_actor_queryset(correspondence)
        return Response(
            [_user_reference(u) for u in users.order_by("first_name", "last_name")]
        )

    def start(self, request, correspondence_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "update")
        data = request.data
        try:
            workflow = start_validation(
                correspondence,
                data.get("workflow_type"),
                _user_or_none(data.get("author")),
                _user_or_none(data.get("verifier")),
                _user_or_none(data.get("approver")),
                started_by=request.user,
            )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(
            serialize_workflow(get_latest_workflow(correspondence)),
            status=status.HTTP_201_CREATED,
        )

    def act(self, request, correspondence_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "update")
        workflow = get_latest_workflow(correspondence)
        if workflow is None:
            raise Http404
        action = request.data.get("action")
        try:
            perform_action(workflow, request.user, action, request.data.get("comment"))
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_workflow(get_latest_workflow(correspondence)))
