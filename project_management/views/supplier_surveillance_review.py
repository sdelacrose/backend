import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status, viewsets
from rest_framework.response import Response

from models.models import User
from project_management.models import SupplierSurveillance
from project_management.serializers.document_register import user_reference
from project_management.services.document_register import (
    assert_can_update_document,
    get_register_document_or_404,
)
from project_management.services.supplier_surveillance_evidence import (
    generate_supplier_surveillance_evidence,
)
from project_management.services.supplier_surveillance_review import (
    assign_reviewer,
    close_supplier_surveillance,
    complete_review,
    compute_progress,
    remove_reviewer,
    save_responses,
)
from project_management.views.supplier_surveillance import serialize_supplier_surveillance

logger = logging.getLogger(__name__)


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}


def serialize_assignment(assignment, *, include_responses=False):
    data = {
        "id": assignment.id,
        "reviewer": user_reference(assignment.reviewer),
        "status": assignment.status,
        "assigned_by": user_reference(assignment.assigned_by),
        "assigned_at": assignment.assigned_at,
        "started_at": assignment.started_at,
        "completed_at": assignment.completed_at,
        "progress": compute_progress(assignment),
    }
    if include_responses:
        data["responses"] = [
            {
                "item_snapshot_id": response.item_snapshot_id,
                "check_value": response.check_value,
                "text_value": response.text_value,
            }
            for response in assignment.responses.all()
        ]
    return data


def serialize_event(event):
    return {
        "id": event.id,
        "event_type": event.event_type,
        "actor": user_reference(event.actor),
        "assignment_id": event.assignment_id,
        "metadata": event.metadata,
        "created_at": event.created_at,
    }


class SupplierSurveillanceReviewViewSet(viewsets.GenericViewSet):
    """L3D: Reviewers + execution of ONE SupplierSurveillance.

    Reviewer MANAGEMENT (assign/remove) reuses the exact same document-update permission
    SupplierSurveillance creation already uses (L3D section 22) -- the narrowest existing
    Project Document Control write permission, not a new supplier_surveillance.* family.
    Response/completion EXECUTION is strictly self-service (L3D section 23/24): enforced in
    services.supplier_surveillance_review regardless of that permission -- a documents.update
    holder can manage reviewers but can never answer or complete on another Reviewer's behalf.
    """

    filter_backends = []

    def _surveillance(self, request, surveillance_id):
        surveillance = (
            SupplierSurveillance.objects.filter(pk=surveillance_id)
            .select_related("document_revision__document")
            .first()
        )
        if surveillance is None:
            raise Http404
        # Access to the Surveillance is access to its Document -- reuses the exact same
        # accessible-Documents check SupplierSurveillanceViewSet already uses, nothing new.
        get_register_document_or_404(
            request, document_id=surveillance.document_revision.document_id
        )
        return surveillance

    def _my_assignment_or_404(self, request, surveillance):
        assignment = surveillance.reviewer_assignments.filter(reviewer=request.user).first()
        if assignment is None:
            raise Http404
        return assignment

    def list_reviewers(self, request, surveillance_id=None, *args, **kwargs):
        surveillance = self._surveillance(request, surveillance_id)
        assignments = surveillance.reviewer_assignments.select_related("reviewer", "assigned_by")
        return Response({"reviewers": [serialize_assignment(a) for a in assignments]})

    def assign_reviewer_action(self, request, surveillance_id=None, *args, **kwargs):
        surveillance = self._surveillance(request, surveillance_id)
        assert_can_update_document(request.user, surveillance.document_revision.document)
        reviewer = User.objects.filter(pk=request.data.get("reviewer")).first()
        if reviewer is None:
            return Response(
                {"reviewer": "A valid reviewer is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            assignment = assign_reviewer(
                surveillance=surveillance, reviewer=reviewer, actor=request.user
            )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_assignment(assignment), status=status.HTTP_201_CREATED)

    def remove_reviewer_action(
        self, request, surveillance_id=None, assignment_id=None, *args, **kwargs
    ):
        surveillance = self._surveillance(request, surveillance_id)
        assert_can_update_document(request.user, surveillance.document_revision.document)
        assignment = surveillance.reviewer_assignments.filter(pk=assignment_id).first()
        if assignment is None:
            raise Http404
        try:
            remove_reviewer(assignment=assignment, actor=request.user)
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def my_review(self, request, surveillance_id=None, *args, **kwargs):
        surveillance = self._surveillance(request, surveillance_id)
        assignment = surveillance.reviewer_assignments.filter(reviewer=request.user).first()
        if assignment is None:
            return Response({"assignment": None, "snapshot": None})
        return Response(
            {
                "assignment": serialize_assignment(assignment, include_responses=True),
                "snapshot": surveillance.template_snapshot,
            }
        )

    def save_my_responses(self, request, surveillance_id=None, *args, **kwargs):
        surveillance = self._surveillance(request, surveillance_id)
        assignment = self._my_assignment_or_404(request, surveillance)
        answers = request.data.get("answers") or []
        try:
            assignment = save_responses(
                assignment=assignment, actor=request.user, answers=answers
            )
        except DjangoPermissionDenied as error:
            return Response({"detail": str(error)}, status=status.HTTP_403_FORBIDDEN)
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_assignment(assignment, include_responses=True))

    def complete_my_review(self, request, surveillance_id=None, *args, **kwargs):
        surveillance = self._surveillance(request, surveillance_id)
        assignment = self._my_assignment_or_404(request, surveillance)
        try:
            assignment = complete_review(assignment=assignment, actor=request.user)
        except DjangoPermissionDenied as error:
            return Response({"detail": str(error)}, status=status.HTTP_403_FORBIDDEN)
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_assignment(assignment, include_responses=True))

    def events(self, request, surveillance_id=None, *args, **kwargs):
        surveillance = self._surveillance(request, surveillance_id)
        events = surveillance.events.select_related("actor")
        return Response({"events": [serialize_event(event) for event in events]})

    def close_surveillance_action(self, request, surveillance_id=None, *args, **kwargs):
        """L3E: closure is a management/finalization action, not restricted to the individual
        Reviewers -- same documents.update permission as reviewer management (L3E section 6).
        status/new_revision_required/closed_at/closed_by are never accepted from the request;
        the service derives every one of them (L3E section 15).

        L3G-2 section 3/22: closure itself is already fully committed and successful by the time
        we reach this point -- evidence PDF generation is a strictly separate, best-effort
        follow-up attempted ONLY after that success, and its outcome (including any exception of
        any kind) must never turn a successful business closure into a failure response. A
        failure here is logged, never raised to the caller; L3G-3's workspace can still see
        Surveillance CLOSED with evidence_pdf null and offer a manual retry (section 23)."""
        surveillance = self._surveillance(request, surveillance_id)
        assert_can_update_document(request.user, surveillance.document_revision.document)
        result = request.data.get("result")
        closure_comment = request.data.get("closure_comment") or ""
        try:
            surveillance = close_supplier_surveillance(
                surveillance=surveillance,
                actor=request.user,
                result=result,
                closure_comment=closure_comment,
            )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        try:
            generate_supplier_surveillance_evidence(surveillance=surveillance, actor=request.user)
        except Exception:
            logger.exception(
                "Supplier Surveillance evidence generation failed after closure "
                "(surveillance_id=%s).",
                surveillance.id,
            )
        return Response(serialize_supplier_surveillance(surveillance, request=request))

    def generate_evidence_action(self, request, surveillance_id=None, *args, **kwargs):
        """L3G-2 section 23: the manual retry endpoint -- the SAME generation path as the
        automatic post-closure attempt (never a second one). Same documents.update permission as
        closure/reviewer management (section 25). Idempotent: an already-existing Evidence is
        simply returned, never regenerated (section 6)."""
        surveillance = self._surveillance(request, surveillance_id)
        assert_can_update_document(request.user, surveillance.document_revision.document)
        try:
            generate_supplier_surveillance_evidence(surveillance=surveillance, actor=request.user)
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        surveillance.refresh_from_db()
        return Response(serialize_supplier_surveillance(surveillance, request=request))
