import logging

from django.db import transaction
from rest_framework import serializers

from project_management.models import Document
from project_management.permissions import DocumentPermission
from project_management.serializers.document import DocumentSerializer


MAX_BULK_DOCUMENTS = 200
logger = logging.getLogger(__name__)
BULK_DOCUMENT_UPDATE_FIELDS = {
    "author",
    "category",
    "discipline",
    "discipline_group_id",
    "discipline_option_id",
    "document_type",
    "external_review_status",
    "internal_review_status",
    "status",
    "transmittal_category",
}
DISCIPLINE_UPDATE_FIELDS = {
    "discipline",
    "discipline_group_id",
    "discipline_option_id",
}


class BulkDocumentChangesSerializer(serializers.Serializer):
    author = serializers.IntegerField(min_value=1, required=False)
    category = serializers.CharField(max_length=255, required=False)
    discipline = serializers.CharField(max_length=255, required=False)
    discipline_group_id = serializers.CharField(max_length=100, required=False)
    discipline_option_id = serializers.CharField(max_length=100, required=False)
    document_type = serializers.CharField(max_length=255, required=False)
    external_review_status = serializers.CharField(
        max_length=255,
        required=False,
    )
    internal_review_status = serializers.CharField(
        max_length=255,
        required=False,
    )
    status = serializers.CharField(max_length=255, required=False)
    transmittal_category = serializers.CharField(max_length=255, required=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object of field changes.")

        unknown_fields = set(data) - BULK_DOCUMENT_UPDATE_FIELDS
        if unknown_fields:
            raise serializers.ValidationError(
                "Unsupported fields: " + ", ".join(sorted(unknown_fields))
            )
        return super().to_internal_value(data)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "At least one field must be provided."
            )

        provided_discipline_fields = DISCIPLINE_UPDATE_FIELDS.intersection(attrs)
        if provided_discipline_fields and (
            provided_discipline_fields != DISCIPLINE_UPDATE_FIELDS
        ):
            missing_fields = DISCIPLINE_UPDATE_FIELDS - provided_discipline_fields
            raise serializers.ValidationError(
                {
                    field: "This field is required when changing discipline."
                    for field in sorted(missing_fields)
                }
            )

        return attrs


class BulkDocumentUpdateRequestSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=MAX_BULK_DOCUMENTS,
    )
    changes = BulkDocumentChangesSerializer()

    def validate_ids(self, value):
        return list(dict.fromkeys(value))


def _document_scope(document):
    folder = document.folder
    if folder.project_id:
        return "project", folder.project_id
    if folder.company_profile_id:
        return "company", folder.company_profile_id
    if folder.freelancer_id:
        return "freelancer", folder.freelancer_id
    return None


def _error_text(error):
    if isinstance(error, dict):
        return "; ".join(
            f"{field}: {_error_text(value)}"
            for field, value in error.items()
        )
    if isinstance(error, (list, tuple)):
        return "; ".join(_error_text(value) for value in error)
    return str(error)


class DocumentBulkUpdateService:
    def __init__(self, *, request, serializer_context):
        self.request = request
        self.serializer_context = {
            **serializer_context,
            "user": request.user,
        }
        self.permission = DocumentPermission(action="update", scope="mixed")

    def execute(self, *, document_ids, changes):
        documents = (
            Document.objects.select_related(
                "folder__project",
                "folder__company_profile",
                "folder__freelancer",
                "author",
                "assignee",
                "created_by",
            )
            .in_bulk(document_ids)
        )
        updated_ids = []
        failures = []
        request_scope = None

        with transaction.atomic():
            for document_id in document_ids:
                document = documents.get(document_id)
                if document is None:
                    failures.append(
                        {"id": document_id, "error": "Document not found."}
                    )
                    continue

                if not self.permission.can_update_object(
                    self.request.user,
                    document,
                ):
                    failures.append(
                        {
                            "id": document_id,
                            "error": (
                                "You do not have permission to update this Document."
                            ),
                        }
                    )
                    continue

                document_scope = _document_scope(document)
                if document_scope is None:
                    failures.append(
                        {
                            "id": document_id,
                            "error": "Document does not belong to a valid scope.",
                        }
                    )
                    continue
                if request_scope is None:
                    request_scope = document_scope
                elif document_scope != request_scope:
                    failures.append(
                        {
                            "id": document_id,
                            "error": "All Documents must belong to the same scope.",
                        }
                    )
                    continue

                try:
                    with transaction.atomic():
                        serializer = DocumentSerializer(
                            document,
                            data=changes,
                            partial=True,
                            context=self.serializer_context,
                        )
                        serializer.is_valid(raise_exception=True)
                        document._suppress_document_notifications = True
                        serializer.save()
                        updated_ids.append(document_id)
                except (serializers.ValidationError, ValueError) as error:
                    detail = getattr(error, "detail", error)
                    failures.append(
                        {"id": document_id, "error": _error_text(detail)}
                    )

            if updated_ids:
                from project_management.tasks import (
                    send_bulk_document_update_notification,
                )

                transaction.on_commit(
                    lambda: self._enqueue_summary_notification(
                        send_bulk_document_update_notification,
                        updated_ids,
                        sorted(changes),
                    )
                )

        return {"updated_ids": updated_ids, "failures": failures}

    def _enqueue_summary_notification(self, task, updated_ids, changed_fields):
        try:
            task.delay(
                updated_ids,
                self.request.user.id,
                changed_fields,
            )
        except Exception:
            logger.exception(
                "Unable to enqueue bulk Document update notification."
            )
