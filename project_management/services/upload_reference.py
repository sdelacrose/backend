"""GED-SEC.08.2-B0.3: what a business field may store after an upload.

A field that used to accept any string as a file URL (a logo, a project image, an avatar, the photos of an
observation, the attachments of a feedback) now accepts exactly three things:
  1. nothing (empty / null);
  2. what it already holds (a historical URL stays readable and stays savable, but cannot be copied to another record,
     because it is only accepted next to the record that holds it);
  3. the final reference of an upload that THIS user prepared and finalised for THIS usage, for this owner and this
     project, in this environment.
Proof of (3) needs no table: the final key is built by the server, it exists in S3 only after a successful
finalisation (the browser can only write under the pending prefix), and its path carries usage, owner and project.
Any other value, a foreign URL included, is refused with one generic message.

PUBLIC usages (logo, project image, avatar) are stored as the public URL of the key, so the screens that display them
keep working unchanged; PRIVATE usages (feedback, site report photos) are stored as the bare reference and read
through `uploads/read`, never through a permanent URL.
"""
from rest_framework import serializers

from project_management.services import upload_policy as policy_lib
from project_management.services import upload_s3

INVALID = "This file reference is not valid."


def _verify(user, usage, key, project_id, visibility):
    policy = policy_lib.USAGES[usage]
    if policy.visibility != visibility:  # a PRIVATE usage is never stored as a public URL, nor the contrary
        raise serializers.ValidationError(INVALID)
    reference = policy_lib.parse_final_reference(key)
    if not policy_lib.reference_belongs_to(
        reference, policy, owner=policy_lib.owner_segment(user, policy), project_id=project_id
    ):
        raise serializers.ValidationError(INVALID)
    try:
        found = upload_s3.head_object(key)
    except upload_s3.StorageUnavailable:
        raise serializers.ValidationError("The file could not be verified. Please try again later.")
    if found is None:  # never finalised (or removed): the reference proves nothing
        raise serializers.ValidationError(INVALID)


def bind_public_url(user, usage, value, *, current=None, project_id=None):
    """The value of a PUBLIC url field (a logo, an image, an avatar)."""
    if value in (None, "") or (current is not None and value == current):
        return value
    base = policy_lib.public_base_url() + "/"
    if not isinstance(value, str) or not value.startswith(base):
        raise serializers.ValidationError(INVALID)
    _verify(user, usage, value[len(base):], project_id, policy_lib.PUBLIC)
    return value


def bind_private_reference(user, usage, value, *, current=None, project_id=None):
    """The value of a PRIVATE reference field: the bare final key."""
    if value in (None, "") or (current is not None and value == current):
        return value
    if not isinstance(value, str):
        raise serializers.ValidationError(INVALID)
    _verify(user, usage, value, project_id, policy_lib.PRIVATE)
    return value


def bind_private_references(user, usage, values, *, existing=(), project_id=None):
    """A list of private references; each element is a historical one of the same record, or a valid new one."""
    if values in (None, ""):
        return values
    if not isinstance(values, (list, tuple)):
        raise serializers.ValidationError(INVALID)
    existing = set(existing or ())
    return [
        bind_private_reference(
            user, usage, value, current=value if isinstance(value, str) and value in existing else None, project_id=project_id
        )
        for value in values
    ]
