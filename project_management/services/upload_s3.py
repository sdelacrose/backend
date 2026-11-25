"""GED-SEC.08.2-B0.2 / B0.3: the only place where the upload policy talks to S3 (signature, inspection, copy, read link).

No credential is read, stored or passed here: the client comes from boto3's standard credential chain, so the
identity is whatever the pod is given (today environment variables, tomorrow an IAM role for the service account)
without any change of code. The bucket comes from the server settings, never from a request. Every provider error
is turned into `StorageUnavailable`, which carries no detail: nothing of boto3 reaches a response.

IAM needed by this module (to be granted by the AWS owner, not assumed): PutObject on the pending and final prefixes,
GetObject (head, first bytes, copy source, read link) and DeleteObject on the pending prefix, ListBucket restricted to
`uploads/` (without it S3 answers 403 instead of 404 for a missing key).
"""
import logging

from django.conf import settings

from project_management.services.upload_policy import PRESIGN_SECONDS, READ_SECONDS, SNIFF_BYTES

logger = logging.getLogger(__name__)

_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}


class StorageUnavailable(Exception):
    """S3 could not do what was asked. The reason is logged by type only; it is never shown to a client."""


def bucket_name():
    name = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or ""
    if not name:
        raise StorageUnavailable("no bucket configured")
    return name


def get_s3_client():
    """boto3's default credential chain, region from the settings. Kept as one function so a test can replace it."""
    import boto3

    return boto3.client("s3", region_name=getattr(settings, "AWS_S3_REGION_NAME", None))


def _failed(exc):
    logger.warning("S3 upload operation failed (%s)", type(exc).__name__)
    return StorageUnavailable(type(exc).__name__)


def _is_missing(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) in _MISSING_CODES


def presign_post(key, content_type, disposition, max_bytes):
    """A presigned POST for exactly one object: this key, this content type, this disposition, between 1 byte and
    `max_bytes`, valid `PRESIGN_SECONDS`. No ACL is requested, so the object takes the bucket's own default: this
    never makes anything public. Returns {"url": ..., "fields": {...}} for the browser form."""
    try:
        fields = {"Content-Type": content_type, "Content-Disposition": disposition}
        conditions = [
            {"Content-Type": content_type},
            {"Content-Disposition": disposition},
            ["content-length-range", 1, int(max_bytes)],
        ]
        posted = get_s3_client().generate_presigned_post(
            Bucket=bucket_name(), Key=key, Fields=fields, Conditions=conditions, ExpiresIn=PRESIGN_SECONDS
        )
        return {"url": posted["url"], "fields": dict(posted["fields"])}
    except StorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - botocore errors, configuration errors: all one generic failure
        raise _failed(exc)


def presign_get(key, content_type=None):
    """A link that reads one private object for `READ_SECONDS`. Used only after the caller was authorised."""
    try:
        params = {"Bucket": bucket_name(), "Key": key}
        if content_type:
            params["ResponseContentType"] = content_type
        return get_s3_client().generate_presigned_url("get_object", Params=params, ExpiresIn=READ_SECONDS)
    except StorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _failed(exc)


def head_object(key):
    """(size, content type) of a stored object, or None when it is not there."""
    try:
        head = get_s3_client().head_object(Bucket=bucket_name(), Key=key)
        return int(head["ContentLength"]), str(head.get("ContentType", ""))
    except StorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            return None
        raise _failed(exc)


def inspect_object(key):
    """(size, content type, first bytes) of a stored object, or None when it is not there (yet)."""
    try:
        found = head_object(key)
        if found is None:
            return None
        body = get_s3_client().get_object(Bucket=bucket_name(), Key=key, Range=f"bytes=0-{SNIFF_BYTES - 1}")["Body"].read(SNIFF_BYTES)
        return found[0], found[1], body
    except StorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _failed(exc)


class ObjectTooLarge(Exception):
    """The stored object is larger than the caller accepts to read."""


def read_object_bytes(key, max_bytes):
    """The bytes of a stored object, read by the server with its own identity (no URL, no link), or None when it is
    not there. An object larger than `max_bytes` is refused before it is read."""
    try:
        found = head_object(key)
        if found is None:
            return None
        if found[0] > max_bytes:
            raise ObjectTooLarge()
        body = get_s3_client().get_object(Bucket=bucket_name(), Key=key)["Body"]
        try:
            data = body.read(max_bytes + 1)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        if len(data) > max_bytes:
            raise ObjectTooLarge()
        return data
    except (StorageUnavailable, ObjectTooLarge):
        raise
    except Exception as exc:  # noqa: BLE001
        raise _failed(exc)


def copy_object(source_key, destination_key, content_type, disposition):
    """Server side copy of a validated pending object to its final key. The destination takes the type and the
    disposition decided by the server; no ACL is requested."""
    try:
        bucket = bucket_name()
        get_s3_client().copy_object(
            Bucket=bucket,
            Key=destination_key,
            CopySource={"Bucket": bucket, "Key": source_key},
            ContentType=content_type,
            ContentDisposition=disposition,
            MetadataDirective="REPLACE",
        )
    except StorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _failed(exc)


def delete_object_quietly(key):
    """Best effort removal. The caller never depends on it succeeding: an object left behind under the pending prefix
    is meant to be expired by the lifecycle rule. Returns whether it worked."""
    try:
        get_s3_client().delete_object(Bucket=bucket_name(), Key=key)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("S3 cleanup of an upload object failed (%s)", type(exc).__name__)
        return False


def store_server_side(key, fileobj, content_type, disposition):
    """Store a file the server itself received (the legacy multipart endpoint). No ACL, no public flag."""
    try:
        get_s3_client().upload_fileobj(
            fileobj,
            bucket_name(),
            key,
            ExtraArgs={"ContentType": content_type, "ContentDisposition": disposition},
        )
    except StorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _failed(exc)
