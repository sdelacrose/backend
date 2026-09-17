import re
from pathlib import PurePath

from rest_framework import serializers


CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
PATH_SEPARATORS = re.compile(r"[/\\]")


def _file_extension(file_name):
    if not file_name:
        return ""
    return PurePath(str(file_name)).suffix.lstrip(".")


def validate_display_file_name(
    value,
    *,
    source_file=None,
    current_file_name=None,
    max_length=500,
):
    """Validate a user-facing filename without changing the stored file key."""
    source_name = getattr(source_file, "name", source_file) or current_file_name
    if value is None:
        if source_name:
            raise serializers.ValidationError("File name is required.")
        return value

    file_name = str(value).strip()
    if not file_name:
        raise serializers.ValidationError("File name is required.")
    if CONTROL_CHARACTERS.search(file_name) or PATH_SEPARATORS.search(file_name):
        raise serializers.ValidationError(
            "File name cannot contain path separators or control characters."
        )

    expected_extension = _file_extension(source_name)
    provided_extension = _file_extension(file_name)

    if expected_extension:
        if file_name.lower() == f".{expected_extension}".lower():
            raise serializers.ValidationError("File name is required.")
        if provided_extension and provided_extension.lower() != expected_extension.lower():
            raise serializers.ValidationError("The file extension cannot be changed.")

        if provided_extension:
            base_name = file_name[: -(len(provided_extension) + 1)]
        else:
            base_name = file_name

        if not base_name.strip(". "):
            raise serializers.ValidationError("File name is required.")
        file_name = f"{base_name}.{expected_extension}"
    elif not file_name.strip(". "):
        raise serializers.ValidationError("File name is required.")

    if len(file_name) > max_length:
        raise serializers.ValidationError(
            f"File name cannot exceed {max_length} characters."
        )

    return file_name
