from rest_framework import status
from rest_framework.exceptions import APIException


class AIToolClarificationRequired(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "clarification_required"

    def __init__(self, *, field, message, choices=None):
        self.field = field
        self.message = message
        self.choices = choices or []
        super().__init__(detail=message, code=self.default_code)

    def as_payload(self):
        return {
            "code": self.default_code,
            "field": self.field,
            "message": self.message,
            "choices": self.choices,
        }
