"""GED-SEC.08.2-B0.2 / B0.3: authenticated, server-controlled uploads (S3 presigned POST) and the private read link.

POST uploads/prepare   : the browser declares what it wants to send (usage, name, type, size, and the project when the
                         usage has one). The server checks the user, the licence, the scope and the permission of
                         the usage, builds the PENDING destination itself and returns a short-lived presigned POST.
POST uploads/finalize  : after the browser has posted the file, the server checks the pending object (it exists, its
                         size, its content type, its first bytes, the rights again), copies it to its FINAL key, checks
                         the copy and removes the pending object. Only then does a reference exist.
GET  uploads/read      : a PRIVATE final reference becomes a link valid a few minutes, for a user who may read it.

All views keep the default permission classes of the API (authenticated + active licence). Every refusal of scope
(unknown or foreign project, other company, other user) is the same 404, so nothing tells whether it exists.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.models import Project, ProjectTeamMembers
from project_management.permissions import (
    ContactPermission,
    ObjectAssessmentPermission,
    ProjectPermission,
    ProjectsPermission,
    SiteReportingPermission,
)
from project_management.services import upload_policy as policy_lib
from project_management.services import upload_s3
from project_management.services.upload_policy import UploadRejected

NOT_FOUND = "Not found."
FORBIDDEN = "You do not have permission to upload this file."


def _optional_id(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) < 1:
        raise UploadRejected("Invalid request.")
    return int(value)


def _accessible_project(user, project_id):
    """The project, only when the user belongs to its team or to its company; otherwise the one uniform 404."""
    project = Project.objects.filter(pk=project_id).first()
    allowed = project is not None and (
        ProjectTeamMembers.objects.filter(project=project, team_member=user).exists()
        or (user.company_id is not None and project.company_profile_id == user.company_id)
    )
    if not allowed:
        raise UploadRejected(NOT_FOUND, status.HTTP_404_NOT_FOUND)
    return project


def _may_upload(user, usage, project):
    """The existing module permissions of the usage: no new permission exists, none is universal. The project-scope
    checks are the ones the corresponding create/update endpoints already use."""
    context = {"project": project}
    if usage == "project_image":
        if project is None:  # the project does not exist yet: the right to create one
            return ProjectsPermission("create", "company")._check_create_permission(user)
        return ProjectPermission("update", "project")._check_update_permission(user, context)
    if usage == "site_report_photo":
        perm = SiteReportingPermission("create", "project")
        return perm._check_create_permission(user, context) or perm._check_update_permission_project(user, None, context)
    if usage == "object_assessment_file":
        perm = ObjectAssessmentPermission("create", "project")
        return perm._check_create_permission(user, context) or perm._check_update_permission_project(user, None, context)
    if usage == "contact_company_logo":
        perm = ContactPermission("create", "company")
        return perm._check_create_permission(user) or perm._check_permission(
            user, "contact_management", "update", "company", {}
        )
    return usage in ("user_avatar", "feedback_attachment")  # own avatar / the same rule as creating a feedback


def authorize_usage(user, policy, *, company_id=None, project_id=None, target_user_id=None):
    """Scope and permission of one prepared upload. Returns the validated project id (or None)."""
    company_id, project_id, target_user_id = _optional_id(company_id), _optional_id(project_id), _optional_id(target_user_id)
    scope = policy.scope
    if (project_id is not None and scope not in ("project_required", "project_optional")) or (
        target_user_id is not None and scope != "self"
    ):
        raise UploadRejected("Invalid request.")
    if scope == "project_required" and project_id is None:
        raise UploadRejected("Invalid request.")
    if company_id is not None and company_id != user.company_id:
        raise UploadRejected(NOT_FOUND, status.HTTP_404_NOT_FOUND)
    if scope == "self" and target_user_id not in (None, user.pk):
        raise UploadRejected(NOT_FOUND, status.HTTP_404_NOT_FOUND)
    project = _accessible_project(user, project_id) if project_id is not None else None
    if not _may_upload(user, policy.name, project):
        raise UploadRejected(FORBIDDEN, status.HTTP_403_FORBIDDEN)
    return project.pk if project is not None else None


def _refused(exc):
    return Response({"detail": exc.public_message}, status=exc.status_code)


def _storage_unavailable():
    return Response({"detail": "The file could not be stored. Please try again later."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class UploadPrepareView(APIView):
    parser_classes = [JSONParser]

    def post(self, request):
        try:
            data = request.data
            policy_lib.reject_unexpected_fields(data)
            policy = policy_lib.usage_policy(data.get("usage"))
            extension, content_type, disposition, size = policy_lib.validate_upload_request(
                policy, data.get("filename"), data.get("content_type"), data.get("size_bytes")
            )
            project_id = authorize_usage(
                request.user, policy, company_id=data.get("company_id"), project_id=data.get("project_id"),
                target_user_id=data.get("user_id"),
            )
            pending_key = policy_lib.build_pending_key(policy.name, extension)
            final_key = policy_lib.build_final_key(policy, policy_lib.owner_segment(request.user, policy), project_id, extension)
            posted = upload_s3.presign_post(pending_key, content_type, disposition, size)
            ticket = policy_lib.issue_ticket(
                user=request.user, usage=policy.name, pending_key=pending_key, final_key=final_key,
                content_type=content_type, extension=extension, declared_size=size, project_id=project_id,
                original_name=data.get("filename"),
            )
        except UploadRejected as exc:
            return _refused(exc)
        except upload_s3.StorageUnavailable:
            return _storage_unavailable()
        expires_at = timezone.now() + timedelta(seconds=policy_lib.PRESIGN_SECONDS)
        return Response(
            {
                "upload_id": ticket,
                "method": "POST",
                "url": posted["url"],
                "fields": posted["fields"],
                "expires_at": expires_at.isoformat(),
                "max_bytes": size,
            },
            status=status.HTTP_200_OK,
        )


def _object_is_valid(ticket, policy, size, content_type, head):
    return (
        1 <= size <= min(ticket["size"], policy_lib.max_bytes_for(policy))
        and content_type.split(";")[0].strip().lower() == ticket["ct"]
        and policy_lib.content_matches_extension(ticket["ext"], head)
    )


def finalize_upload(ticket, policy):
    """Pending object -> checked -> copied to its final key -> copy checked -> pending object removed. Repeatable: a
    second call (or the retry of an interrupted one) never copies twice and never loses the object. Returns the size."""
    pending, final = ticket["pending"], ticket["key"]
    already = upload_s3.inspect_object(final)
    if already is not None:  # an earlier call copied it: check it again, finish the clean-up, do not copy again
        if not _object_is_valid(ticket, policy, *already):
            upload_s3.delete_object_quietly(final)
            raise UploadRejected("The uploaded file was refused.")
        upload_s3.delete_object_quietly(pending)
        return already[0]
    stored = upload_s3.inspect_object(pending)
    if stored is None:
        raise UploadRejected("The upload is not complete.", status.HTTP_409_CONFLICT)
    if not _object_is_valid(ticket, policy, *stored):
        upload_s3.delete_object_quietly(pending)
        raise UploadRejected("The uploaded file was refused.")
    canonical, _aliases, inline_safe = policy_lib.FORMATS[ticket["ext"]]
    upload_s3.copy_object(pending, final, canonical, "inline" if inline_safe else "attachment")
    copied = upload_s3.head_object(final)
    if copied is None or copied[0] != stored[0]:  # the copy is not what was checked: nothing is attached to it
        upload_s3.delete_object_quietly(final)
        raise upload_s3.StorageUnavailable("copy not confirmed")
    upload_s3.delete_object_quietly(pending)  # only now, after the copy was confirmed
    return stored[0]


class UploadFinalizeView(APIView):
    parser_classes = [JSONParser]

    def post(self, request):
        try:
            data = request.data
            if not hasattr(data, "keys") or set(data.keys()) != {"upload_id"}:
                raise UploadRejected("Invalid request.")
            ticket = policy_lib.read_ticket(data.get("upload_id"), request.user)
            policy = policy_lib.usage_policy(ticket["usage"])
            # the rights are checked again: they may have been withdrawn since the preparation
            authorize_usage(request.user, policy, project_id=ticket["project"])
            size = finalize_upload(ticket, policy)
        except UploadRejected as exc:
            return _refused(exc)
        except upload_s3.StorageUnavailable:
            return _storage_unavailable()
        return Response(
            {
                "reference": ticket["key"],
                "usage": ticket["usage"],
                "visibility": policy.visibility,
                # a PUBLIC usage (logo, project image, avatar) is stored as its public URL; a PRIVATE one as its
                # reference only, never as a permanent URL
                "public_url": policy_lib.public_url_for(ticket["key"]) if policy.visibility == policy_lib.PUBLIC else None,
                "size_bytes": size,
                "content_type": ticket["ct"],
                "original_name": ticket["name"],
            },
            status=status.HTTP_200_OK,
        )


def _may_read(user, reference):
    """The same uniform 404 for everything that is not readable by this user."""
    refusal = UploadRejected(NOT_FOUND, status.HTTP_404_NOT_FOUND)
    if reference is None or reference.visibility != policy_lib.PRIVATE or reference.usage not in policy_lib.USAGES:
        raise refusal
    policy = policy_lib.USAGES[reference.usage]
    # B0.4-FIX: a site report photo belongs to its PROJECT, not to the uploader's company: on a project shared by several
    # companies any team member who may view the reports reads it, provided the uploader is a legitimate party of that
    # project (checked below). Every other private usage keeps its own owner rule.
    shared = reference.usage == "site_report_photo"
    if reference.environment != policy_lib._environment_segment():
        raise refusal
    if not shared and reference.owner != policy_lib.owner_segment(user, policy):
        raise refusal
    if reference.project_id is None:
        if shared:
            raise refusal
        return policy
    try:
        project = _accessible_project(user, reference.project_id)
    except UploadRejected:
        raise refusal
    context = {"project": project}
    if reference.usage == "site_report_photo":
        allowed = SiteReportingPermission("view", "project")._check_view_permission_project(user, None, context)
    elif reference.usage == "object_assessment_file":
        allowed = ObjectAssessmentPermission("view", "project")._check_view_permission_project(user, None, context)
    else:
        allowed = False
    if not allowed or (shared and reference.owner not in policy_lib.project_team_owners(project)):
        raise refusal
    return policy


class UploadReadView(APIView):
    """GET uploads/read?reference=<private final key>: a link that works for a few minutes, never a stored URL."""

    def get(self, request):
        try:
            reference = request.query_params.get("reference")
            parsed = policy_lib.parse_final_reference(reference)
            _may_read(request.user, parsed)
            if upload_s3.head_object(reference) is None:
                raise UploadRejected(NOT_FOUND, status.HTTP_404_NOT_FOUND)
            url = upload_s3.presign_get(reference)
        except UploadRejected as exc:
            return _refused(exc)
        except upload_s3.StorageUnavailable:
            return _storage_unavailable()
        expires_at = timezone.now() + timedelta(seconds=policy_lib.READ_SECONDS)
        return Response({"url": url, "expires_at": expires_at.isoformat()}, status=status.HTTP_200_OK)


def store_legacy_multipart_upload(user, uploaded_file):
    """`projects/upload_file_to_s3`: the server receives the file, so it can check the real bytes. The object is only
    a preview helper of an unsaved form: it goes under the PENDING prefix (it expires), under a server-built name, with
    the type of its extension. Returns (key, read link valid a few minutes). The caller has already applied the
    endpoint's own permission, which stays the minimum."""
    policy = policy_lib.usage_policy("legacy_multipart", for_prepare=False)
    extension = policy_lib.validate_filename(getattr(uploaded_file, "name", None))
    if extension not in policy.extensions:
        raise UploadRejected("This file type is not allowed.")
    if uploaded_file.size < 1:
        raise UploadRejected("Invalid file size.")
    if uploaded_file.size > policy_lib.max_bytes_for(policy):
        raise UploadRejected("The file is too large.", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    head = uploaded_file.read(policy_lib.SNIFF_BYTES)
    uploaded_file.seek(0)
    if not policy_lib.content_matches_extension(extension, head):
        raise UploadRejected("The file content does not match its type.")
    canonical, _aliases, inline_safe = policy_lib.FORMATS[extension]
    key = policy_lib.build_pending_key(policy.name, extension)
    upload_s3.store_server_side(key, uploaded_file, canonical, "inline" if inline_safe else "attachment")
    return key, upload_s3.presign_get(key, canonical)
