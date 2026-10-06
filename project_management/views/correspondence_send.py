"""LOT 3D-3D2: POST /correspondence/<id>/send and its read-only history.

Section 14: SEND is its own dedicated documents.correspondence_send permission, not
documents.correspondence_update -- an external action with real business consequences (an
actual email leaves the system) is a different authorization decision than editing a draft
record, even though today the same users likely hold both. Every lifecycle/eligibility guard in
services.correspondence_send still applies underneath regardless of permission level (section 3
"All enforced backend-side"), exactly like the validation workflow's own actor check applies
even to a full_access user (LOT 3D-3D1 section 13) -- holding the permission only means the
request is allowed to ASK; ready_to_send/idempotency/recipient/attachment-ownership decide
whether it actually happens.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from project_management.permissions import CorrespondencePermission
from project_management.services.correspondence_send import send_correspondence
from project_management.services.correspondence_workflow import can_resend, ready_to_send
from project_management.views.correspondence import _user_reference, correspondence_queryset_for_user


def serialize_send_event(event):
    return {
        "id": event.id,
        "correspondence": event.correspondence_id,
        "sender": _user_reference(event.sender),
        "from_email": event.from_email,
        # BCC is included here exactly as everywhere else send evidence is read: this endpoint
        # is already gated by documents.correspondence_send/_view on THIS Correspondence's own
        # scope (section 16 "Do not expose BCC recipients incorrectly... to unauthorized
        # users") -- there is no broader/public read path for this data.
        "to_recipients": event.to_recipients,
        "cc_recipients": event.cc_recipients,
        "bcc_recipients": event.bcc_recipients,
        "subject": event.subject,
        "body": event.body,
        "attachments_snapshot": event.attachments_snapshot,
        "provider": event.provider,
        "provider_message_id": event.provider_message_id,
        "result": event.result,
        "error_message": event.error_message,
        "sent_at": event.sent_at,
        "created_at": event.created_at,
    }


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}


class CorrespondenceSendViewSet(viewsets.GenericViewSet):
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
        # History is read-only Correspondence data: gated by the existing view permission, not
        # the new send one (matches the validation workflow endpoint's own retrieve/act split).
        correspondence = self._correspondence(request, correspondence_id, "view")
        events = correspondence.send_events.select_related("sender").all()
        return Response(
            {
                "events": [serialize_send_event(e) for e in events],
                "ready_to_send": ready_to_send(correspondence),
                # CORR-DIST-2 section 6/16: a second, independent eligibility flag -- a Resend
                # action, never conflated with the FIRST-send one above (see can_resend's own
                # docstring for why they can never both be true for the same Correspondence).
                "can_resend": can_resend(correspondence),
            }
        )

    def send(self, request, correspondence_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "send")
        data = request.data
        # Section 17: only genuinely email-composer values are ever read from the request --
        # status/sent_at/provider result/parent response/ready_to_send are exclusively backend-
        # owned consequences, never accepted here even if a client sent them.
        try:
            event = send_correspondence(
                correspondence,
                sender=request.user,
                cc=data.get("cc"),
                bcc=data.get("bcc"),
                subject=data.get("subject"),
                body=data.get("body"),
                attachment_ids=data.get("attachment_ids"),
            )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        response_status = (
            status.HTTP_201_CREATED if event.result == "SUCCESS" else status.HTTP_400_BAD_REQUEST
        )
        return Response(serialize_send_event(event), status=response_status)
