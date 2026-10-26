"""MAIL-1.3D-B: the review API of the incoming messages already stored in LINKS.

Read and triage only: list, detail, ignore, restore. No import to an IN correspondence, no reply
indicator, no synchronisation trigger, and NEVER a Microsoft Graph request -- everything here is
answered from the stored metadata (the deleted-message policy below depends on it).

Who sees what (all rules live in userapis.mailbox_messages, reused as is):
  - a message is visible only to someone who may READ its mailbox right now, in their own company,
    and -- with `?project=` -- only if the mailbox is assigned to that project;
  - a PERSONAL mailbox shows nothing but rows linked to a LINKS correspondence, and only to its
    owner; holding a right on the correspondence never opens it;
  - only the Inbox is reviewed here (sent copies are evidence of what LINKS sent, not a queue);
  - another person's message is a 404, never a 403 that would confirm it exists. A list filtered
    on a mailbox or project the user has no right to answers 403.

Deleted or moved messages: consulting never asks Outlook whether a message still exists. The sync
only removes an untouched `new`, unlinked row that Graph reports as removed; an ignored, imported
or linked row is kept as the evidence of a decision. Verifying existence for real, when an action
needs it, belongs to a later lot.
"""

from django.db import transaction
from django.db.models import F, Q
from django.http import Http404
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from userapis.mailbox_messages import can_read_stored_message, messages_visible_to, readable_mailboxes
from userapis.models import MailboxMessage
from userapis.serializers.mailbox_message import MailboxMessageReviewSerializer

REVIEWED_FOLDER = "inbox"
MAX_SEARCH_LENGTH = 100


class MailboxMessagePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class MessageAlreadyImported(APIException):
    status_code = 409
    default_detail = "This message has been imported: it can no longer be ignored or restored."
    default_code = "message_imported"


def _int_param(request, name):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "A numeric id is expected."})


class MailboxMessageViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = MailboxMessageReviewSerializer
    pagination_class = MailboxMessagePagination
    filter_backends = []
    queryset = MailboxMessage.objects.none()

    # -- scope ----------------------------------------------------------------------------------
    def _project(self):
        project_id = _int_param(self.request, "project")
        if project_id is None:
            return None
        from project_management.models import Project

        project = Project.objects.filter(
            pk=project_id, company_profile_id=self.request.user.company_id
        ).first()
        if project is None:
            raise PermissionDenied()
        return project

    def get_queryset(self):
        user = self.request.user
        if not getattr(user, "company_id", None):
            return MailboxMessage.objects.none()
        params = self.request.query_params
        queryset = messages_visible_to(user, project=self._project()).filter(
            folder=REVIEWED_FOLDER
        ).select_related("mailbox")

        mailbox_id = _int_param(self.request, "mailbox")
        if mailbox_id is not None:
            if not readable_mailboxes(user).filter(pk=mailbox_id).exists():
                raise PermissionDenied()
            queryset = queryset.filter(mailbox_id=mailbox_id)

        state = params.get("state")
        if state:
            if state not in MailboxMessage.ProcessingState.values:
                raise ValidationError({"state": "Expected one of: new, ignored, imported."})
            queryset = queryset.filter(state=state)

        search = (params.get("search") or "").strip()[:MAX_SEARCH_LENGTH]
        if search:
            # Only the metadata shown here is searchable -- there is no body to search.
            queryset = queryset.filter(
                Q(subject__icontains=search)
                | Q(sender_address__icontains=search)
                | Q(sender_name__icontains=search)
            )
        return queryset.order_by(F("received_at").desc(nulls_last=True), "-id")

    def get_object(self):
        message = super().get_object()
        # Belt and braces: the very same single-message rule, whatever the queryset did.
        if not can_read_stored_message(self.request.user, message, project=self._project()):
            raise Http404()
        return message

    # -- triage ---------------------------------------------------------------------------------
    def _set_state(self, request, target):
        message = self.get_object()
        with transaction.atomic():
            locked = MailboxMessage.objects.select_for_update().get(pk=message.pk)
            if locked.state == MailboxMessage.ProcessingState.IMPORTED:
                raise MessageAlreadyImported()
            if locked.state != target:                       # the same request twice is a no-op
                locked.state = target
                locked.save(update_fields=["state", "updated_at"])
        locked = MailboxMessage.objects.select_related("mailbox").get(pk=message.pk)
        return Response(self.get_serializer(locked).data)

    @action(detail=True, methods=["post"])
    def ignore(self, request, pk=None):
        return self._set_state(request, MailboxMessage.ProcessingState.IGNORED)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        return self._set_state(request, MailboxMessage.ProcessingState.NEW)
