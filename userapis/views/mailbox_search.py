"""MAIL-1.3D-C1: POST /api/user/mailbox-search/ -- the user's explicit search of one mailbox.

POST (a body, never a URL) so the criteria -- personal data -- stay out of access logs and browser
history; nothing is cached and nothing is stored (see userapis.mailbox_search). Errors carry a
stable `code` and a fixed generic message, never a token, URL or Graph body.
"""

from datetime import datetime, time, timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from userapis.mailbox_read import ERROR_MESSAGES, MailboxReadError
from userapis.mailbox_search import (
    DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MAX_PAGES, MAX_PERIOD, MAX_TEXT_LENGTH, MIN_TEXT_LENGTH,
    SEARCH_FOLDERS, search_messages,
)
from userapis.models import MailboxConnection

# Reading problems are the user's connection/rights, never an authentication failure of LINKS
# itself (a 401 would log the user out of the app).
STATUS_BY_CODE = {
    "not_authorized": 403, "access_denied": 403,
    "mailbox_inactive": 409, "token_unavailable": 409, "token_refresh_failed": 409,
    "token_rejected": 409, "identity_mismatch": 409,
    "unsupported_provider": 400, "invalid_request": 400, "request_rejected": 400,
    "not_found": 404, "rate_limited": 429, "temporary": 503, "invalid_response": 502,
}
ERROR_MESSAGES = {
    **ERROR_MESSAGES,
    "request_rejected": "Microsoft 365 could not run this search; narrow the criteria and retry.",
}


def _error(exc):
    response = Response(
        {"detail": ERROR_MESSAGES.get(exc.code, str(exc)), "code": exc.code},
        status=STATUS_BY_CODE.get(exc.code, 502),
    )
    if exc.code == "rate_limited" and exc.retry_after:
        response["Retry-After"] = str(int(exc.retry_after))
    return response


def _moment(value, *, end_of_day):
    """An ISO date-time, or an ISO date (the start, or with end_of_day the end, of that day, UTC)."""
    value = value.strip()
    if "T" in value or " " in value:
        parsed = parse_datetime(value)
    else:
        day = parse_date(value)
        parsed = (
            datetime.combine(day, time(23, 59, 59) if end_of_day else time(0, 0, 0))
            if day is not None else None
        )
    if parsed is None:
        raise serializers.ValidationError("Expected an ISO date or date-time.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


class MailboxSearchRequestSerializer(serializers.Serializer):
    mailbox = serializers.IntegerField(min_value=1)
    project = serializers.IntegerField(min_value=1, required=False)
    folder = serializers.ChoiceField(choices=SEARCH_FOLDERS, default="inbox")
    sender = serializers.EmailField(required=False, allow_blank=True, max_length=254)
    subject = serializers.CharField(
        required=False, allow_blank=True, max_length=MAX_TEXT_LENGTH, trim_whitespace=True
    )
    date_from = serializers.CharField(required=False, allow_blank=True, max_length=40)
    date_to = serializers.CharField(required=False, allow_blank=True, max_length=40)
    page = serializers.IntegerField(min_value=1, max_value=MAX_PAGES, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=MAX_PAGE_SIZE, default=DEFAULT_PAGE_SIZE)

    def validate_subject(self, value):
        if value and len(value) < MIN_TEXT_LENGTH:
            raise serializers.ValidationError(f"At least {MIN_TEXT_LENGTH} characters are needed.")
        return value

    def validate(self, attrs):
        errors = {}
        until = since = None
        try:
            if attrs.get("date_to"):
                until = _moment(attrs["date_to"], end_of_day=True)
        except serializers.ValidationError as exc:
            errors["date_to"] = exc.detail
        try:
            if attrs.get("date_from"):
                since = _moment(attrs["date_from"], end_of_day=False)
        except serializers.ValidationError as exc:
            errors["date_from"] = exc.detail
        if errors:
            raise serializers.ValidationError(errors)
        now = timezone.now()
        until = min(until or now, now)
        if since is not None and since > until:
            raise serializers.ValidationError({"date_from": "The start must not be after the end."})
        if since is not None and until - since > MAX_PERIOD:
            raise serializers.ValidationError(
                {"date_from": "The period is too wide: search one year at most."}
            )
        attrs["until"], attrs["since"] = until, since      # None = the default recent period
        if attrs.get("sender"):
            attrs["sender"] = attrs["sender"].strip().lower()
        return attrs


class MailboxSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MailboxSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user

        # An unknown mailbox, another company's, or one the user has no right to are one and the
        # same answer: the search never reveals what exists.
        mailbox = None
        company_id = getattr(user, "company_id", None)
        if company_id:
            mailbox = MailboxConnection.objects.filter(
                pk=data["mailbox"], company_profile_id=company_id
            ).first()
        project = None
        if mailbox is not None and data.get("project"):
            from project_management.models import Project

            project = Project.objects.filter(pk=data["project"], company_profile_id=company_id).first()
            if project is None:
                mailbox = None
        if mailbox is None:
            return _error(MailboxReadError("not_authorized"))

        try:
            found = search_messages(
                user, mailbox, folder=data["folder"], sender=data.get("sender"),
                subject=data.get("subject"), since=data["since"], until=data["until"],
                page=data["page"], page_size=data["page_size"], project=project,
            )
        except MailboxReadError as exc:
            return _error(exc)

        return Response({
            "mailbox": {
                "id": mailbox.pk, "email_address": mailbox.email_address,
                "display_name": mailbox.display_name, "connection_type": mailbox.connection_type,
            },
            "folder": found.folder,
            "criteria": {
                "sender": data.get("sender") or None, "subject": data.get("subject") or None,
                "date_from": found.since, "date_to": found.until,
            },
            "page": found.page, "page_size": found.page_size, "max_pages": MAX_PAGES,
            "has_next": found.has_next and found.page < MAX_PAGES,
            "results": [
                {
                    "reference": reference,
                    "sender_address": meta.sender_address, "sender_name": meta.sender_name,
                    "to_addresses": list(meta.to_addresses), "cc_addresses": list(meta.cc_addresses),
                    "subject": meta.subject, "received_at": meta.received_at,
                    "has_attachments": meta.has_attachments,
                }
                for reference, meta in found.results
            ],
        })
