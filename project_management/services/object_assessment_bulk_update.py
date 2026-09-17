import logging

from django.db import transaction
from rest_framework import serializers

from project_management.models import ObjectAssessment
from project_management.permissions import ObjectAssessmentPermission
from project_management.serializers import ObjectAssessmentSerializer


MAX_BULK_OBJECT_ASSESSMENTS = 200
logger = logging.getLogger(__name__)
BULK_OBJECT_ASSESSMENT_UPDATE_FIELDS = {
    "author",
    "document_type",
    "status",
}


class BulkObjectAssessmentChangesSerializer(serializers.Serializer):
    author = serializers.IntegerField(min_value=1, required=False)
    document_type = serializers.CharField(max_length=255, required=False)
    status = serializers.CharField(max_length=255, required=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object of field changes.")

        unknown_fields = set(data) - BULK_OBJECT_ASSESSMENT_UPDATE_FIELDS
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
        return attrs


class BulkObjectAssessmentUpdateRequestSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=MAX_BULK_OBJECT_ASSESSMENTS,
    )
    changes = BulkObjectAssessmentChangesSerializer()

    def validate_ids(self, value):
        return list(dict.fromkeys(value))


def _error_text(error):
    if isinstance(error, dict):
        return "; ".join(
            f"{field}: {_error_text(value)}"
            for field, value in error.items()
        )
    if isinstance(error, (list, tuple)):
        return "; ".join(_error_text(value) for value in error)
    return str(error)


class ObjectAssessmentBulkUpdateService:
    def __init__(self, *, request, serializer_context):
        self.request = request
        self.serializer_context = {
            **serializer_context,
            "user": request.user,
        }
        self.permission = ObjectAssessmentPermission(
            action="update",
            scope="project",
        )

    def execute(self, *, object_assessment_ids, changes):
        object_assessments = (
            ObjectAssessment.objects.select_related(
                "project",
                "author",
                "assignee",
                "created_by",
            )
            .in_bulk(object_assessment_ids)
        )
        updated_ids = []
        failures = []
        project_id = None

        with transaction.atomic():
            for object_assessment_id in object_assessment_ids:
                object_assessment = object_assessments.get(object_assessment_id)
                if object_assessment is None:
                    failures.append(
                        {
                            "id": object_assessment_id,
                            "error": "Object Assessment not found.",
                        }
                    )
                    continue

                if not self.permission.can_update_object(
                    self.request.user,
                    object_assessment,
                ):
                    failures.append(
                        {
                            "id": object_assessment_id,
                            "error": (
                                "You do not have permission to update this "
                                "Object Assessment."
                            ),
                        }
                    )
                    continue

                if project_id is None:
                    project_id = object_assessment.project_id
                elif object_assessment.project_id != project_id:
                    failures.append(
                        {
                            "id": object_assessment_id,
                            "error": (
                                "All Object Assessments must belong to the same "
                                "project."
                            ),
                        }
                    )
                    continue

                try:
                    with transaction.atomic():
                        serializer = ObjectAssessmentSerializer(
                            object_assessment,
                            data=changes,
                            partial=True,
                            context=self.serializer_context,
                        )
                        serializer.is_valid(raise_exception=True)
                        object_assessment._suppress_object_assessment_notifications = True
                        serializer.save()
                        updated_ids.append(object_assessment_id)
                except (serializers.ValidationError, ValueError) as error:
                    detail = getattr(error, "detail", error)
                    failures.append(
                        {
                            "id": object_assessment_id,
                            "error": _error_text(detail),
                        }
                    )

            if updated_ids:
                from project_management.tasks import (
                    send_bulk_object_assessment_update_notification,
                )

                transaction.on_commit(
                    lambda: self._enqueue_summary_notification(
                        send_bulk_object_assessment_update_notification,
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
                "Unable to enqueue bulk Object Assessment update notification."
            )
