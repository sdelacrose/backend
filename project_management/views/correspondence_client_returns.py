"""SURVEILLANCE IN LOT B: process the documents concerned by ONE existing IN correspondence (the single proof).

GET  correspondence/<id>/client-returns/candidates?reference=&revision=  -> candidate distributions (never a choice)
POST correspondence/<id>/client-returns                                    -> one A/B/C/D return per selected document,
                                                                              in ONE transaction

No new right: reading the correspondence (documents.correspondence_view), then per document the same checks as the
single-document path (read access, documents.update, distribution of that document). An unreadable correspondence is a
404, like a missing one; an unavailable document is a generic per-line answer."""
from django.utils import timezone
from django.http import Http404
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.core.exceptions import ValidationError as DjangoValidationError

from ecpmpapp.permissions import LicensePermission
from project_management.models import ClientSurveillanceReturn, Correspondence
from project_management.permissions import CorrespondencePermission
from project_management.services.client_surveillance_code import list_effective_codes
from project_management.services.correspondence_client_returns import default_received_at, proof_candidates, record_proof_returns
from project_management.services.document_register import get_register_queryset


def _proof_or_404(request, correspondence_id):
    correspondence = Correspondence.objects.select_related("sender_company", "project").prefetch_related("attachments").filter(pk=correspondence_id).first()
    if correspondence is None or not CorrespondencePermission("view").has_object_permission(request, None, correspondence):
        raise Http404
    if correspondence.direction != "IN" or correspondence.status != "ISSUED" or correspondence.project_id is None:
        raise ValidationError({"detail": "Only an issued incoming correspondence of a project can be the proof of client returns.", "code": "proof_not_eligible"})
    return correspondence


def serialize_proof(correspondence):
    names = [a.file_name or "" for a in correspondence.attachments.all()]
    eml = sum(1 for n in names if n.lower().endswith(".eml"))
    return {"id": correspondence.pk, "reference": correspondence.display_reference or correspondence.reference or correspondence.external_reference or "",
            "subject": correspondence.subject, "correspondence_type": correspondence.correspondence_type,
            "sender_company_id": correspondence.sender_company_id,
            "sender_company_name": correspondence.sender_company.company_name if correspondence.sender_company_id else None,
            "received_date": correspondence.received_date, "has_original_email": eml > 0, "attachment_count": len(names) - eml,
            "is_surveillance": (correspondence.correspondence_type or "").lower() == "surveillance"}


class CorrespondenceClientReturnCandidatesView(APIView):
    permission_classes = [IsAuthenticated, LicensePermission]

    def get(self, request, correspondence_id):
        correspondence = _proof_or_404(request, correspondence_id)
        params = request.query_params
        results = proof_candidates(correspondence, request.user, get_register_queryset(request, project_id=correspondence.project_id),
                                   params.get("reference", ""), params.get("revision", ""))
        return Response({"proof": serialize_proof(correspondence), "results": results,
                         "codes": list_effective_codes(correspondence.project), "default_received_at": default_received_at(correspondence),
                         "default_received_estimated": bool(correspondence.received_date and correspondence.received_date < timezone.localdate())})


class CorrespondenceClientReturnsView(APIView):
    permission_classes = [IsAuthenticated, LicensePermission]

    def post(self, request, correspondence_id):
        correspondence = _proof_or_404(request, correspondence_id)

        class Line(serializers.Serializer):
            client_issue_id = serializers.IntegerField()
            category = serializers.ChoiceField(choices=ClientSurveillanceReturn.Category.choices, allow_null=True, allow_blank=True, required=False)
            comment = serializers.CharField(allow_blank=True, required=False, max_length=10000)
            send_event = serializers.IntegerField(required=False, allow_null=True)

        class Input(serializers.Serializer):
            lines = Line(many=True)
            received_at = serializers.DateTimeField(required=False)
            source_channel = serializers.ChoiceField(choices=ClientSurveillanceReturn.Channel.choices, required=False)
            confirm_other_company = serializers.BooleanField(required=False)
            confirm_other_type = serializers.BooleanField(required=False)

        payload = Input(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        can_view = lambda c: CorrespondencePermission("view").has_object_permission(request, None, c)
        try:
            results, errors = record_proof_returns(
                correspondence, request.user, [dict(line) for line in values["lines"]],
                received_at=values.get("received_at") or default_received_at(correspondence),
                source_channel=values.get("source_channel") or ClientSurveillanceReturn.Channel.EMAIL,
                accessible_documents=get_register_queryset(request, project_id=correspondence.project_id), can_view_correspondence=can_view,
                confirm_other_company=bool(values.get("confirm_other_company")), confirm_other_type=bool(values.get("confirm_other_type")))
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages)
        if errors:
            return Response({"detail": "Some documents cannot be processed: nothing was saved.", "code": "lines_invalid", "errors": errors},
                            status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"results": results, "created": sum(1 for r in results if r["created"])},
                        status=http_status.HTTP_201_CREATED if any(r["created"] for r in results) else http_status.HTTP_200_OK)
