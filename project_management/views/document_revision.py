import posixpath

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.reverse import reverse

from project_management.models import DocumentWorkflow
from project_management.serializers.document_register import user_reference
from project_management.services.document_register import (
    assert_can_update_document,
    get_register_document_or_404,
)
from project_management.services.document_control_settings import (
    get_document_control_settings,
)
from project_management.services.document_revision import (
    create_document_revision,
    get_next_versions,
    get_revision_history,
)


def _absolute_url(request, url):
    return url if str(url).startswith("http") else request.build_absolute_uri(url)


def serialize_workflow_summary(workflow):
    """Workflow of a version, as needed by the Revision History (details: workflow endpoint).

    The approval date exists only for an APPROVED workflow (completed_at is also set when a
    workflow is rejected, so it is never exposed as such). TWO_STEP has no reviewer.
    """
    if workflow is None:
        return None
    approved = workflow.status == DocumentWorkflow.Status.APPROVED
    return {
        "id": workflow.id,
        "status": workflow.status,
        "workflow_type": workflow.workflow_type,
        "verifier": user_reference(workflow.verifier),
        "approver": user_reference(workflow.approver),
        "approved_at": workflow.completed_at if approved else None,
    }


def serialize_revision(revision, request, document, workflow=None):
    return {
        # Latest workflow of this revision (summary only; events: the workflow endpoint).
        "workflow": serialize_workflow_summary(workflow),
        "id": revision.id,
        "version_number": revision.version_number,
        "revision_label": revision.revision_label,
        "major_revision": revision.major_revision,
        "minor_revision": revision.minor_revision,
        "label": revision.label,
        "file_name": revision.file_name,
        "file_size": revision.file_size,
        # Approval evidence (read-only): exact-bytes hash and the approval-time metadata.
        "file_sha256": revision.file_sha256,
        "approval_snapshot": revision.approval_snapshot,
        "file_url": _absolute_url(request, revision.file.url),
        "comment": revision.comment,
        "created_at": revision.created_at,
        "created_by": user_reference(revision.created_by),
        "download_url": reverse(
            "document-revision-download",
            kwargs={"document_id": document.id, "revision_id": revision.id},
            request=request,
        ),
    }


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}


class DocumentRevisionViewSet(viewsets.GenericViewSet):
    """Stored versions of one Document.

    Access is exactly the Documents access: the Document is resolved from the
    accessible queryset first (404 otherwise), and a revision is only ever
    fetched through its accessible Document. Creating a version additionally
    requires the permission to update the document.
    """

    filter_backends = []
    parser_classes = [MultiPartParser, FormParser]

    def list(self, request, document_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        revisions = list(get_revision_history(document))
        workflows = {}
        for workflow in DocumentWorkflow.objects.filter(
            revision__in=revisions
        ).select_related("verifier", "approver").order_by("created_at", "id"):
            workflows[workflow.revision_id] = workflow  # latest wins
        results = [
            serialize_revision(r, request, document, workflows.get(r.id))
            for r in revisions
        ]

        if not results and document.file:
            # Compatibility: no real revision yet, the current Document.file is
            # the only stored version. Nothing is created or backfilled.
            file_url = _absolute_url(request, document.file.url)
            major = str(document.revision if document.revision is not None else 0)
            results = [
                {
                    "id": None,
                    "version_number": 1,
                    "revision_label": None,
                    "major_revision": major,
                    "minor_revision": 0,
                    "label": f"{major}-00",
                    "file_name": document.file_name
                    or posixpath.basename(document.file.name),
                    "file_size": None,
                    "file_sha256": None,
                    "approval_snapshot": None,
                    "file_url": file_url,
                    "comment": "",
                    "workflow": None,
                    "created_at": document.uploaded_at,
                    "created_by": user_reference(document.created_by),
                    "download_url": file_url,
                }
            ]

        for index, item in enumerate(results):
            item["is_current"] = index == 0
        return Response(
            {
                "count": len(results),
                "results": results,
                "next_versions": get_next_versions(document),
                # Project default of the internal workflow (preselected when launching it).
                "workflow_default": get_document_control_settings(document)["workflow"][
                    "default"
                ],
            }
        )

    def create(self, request, document_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_can_update_document(request.user, document)
        try:
            revision = create_document_revision(
                document,
                request.FILES.get("file"),
                request.user,
                request.data.get("revision_type"),
                request.data.get("comment"),
            )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(
            serialize_revision(revision, request, document),
            status=status.HTTP_201_CREATED,
        )

    def download(self, request, document_id=None, revision_id=None, *args, **kwargs):
        document = get_register_document_or_404(request, document_id=document_id)
        revision = (
            get_revision_history(document).filter(pk=revision_id).first()
        )
        if revision is None or not revision.file:
            raise Http404
        return FileResponse(
            revision.file.open("rb"),
            as_attachment=True,
            filename=revision.file_name or posixpath.basename(revision.file.name),
        )
