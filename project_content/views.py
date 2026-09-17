from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.models import Milestone, Project
from project_management.services.document_access import get_accessible_document_queryset

from .audit import record_project_content_event
from .models import (
    AcceptanceChecklist,
    AcceptanceCriterion,
    AcceptanceCriterionComment,
    AcceptanceCriterionCommentReaction,
    AcceptanceGenerationRun,
    CompanyProjectDocumentTemplate,
    GeneratedProjectDocument,
    GeneratedProjectDocumentRevision,
    ProjectContentAuditEvent,
    ProjectDocumentGenerationRun,
)
from .permissions import (
    ACCEPTANCE_TESTS_MODULE,
    PROJECT_CONTENT_MODULE,
    require_acceptance_generation_access,
    require_project_content_access,
    require_template_access,
)
from .rendering import DocumentRenderingError, system_template_bytes
from .serializers import (
    AcceptanceApplySerializer,
    AcceptanceBulkUpdateSerializer,
    AcceptanceChecklistSerializer,
    AcceptanceCriterionCommentReactionSerializer,
    AcceptanceCriterionCommentSerializer,
    AcceptanceCriterionSerializer,
    AcceptanceDraftSerializer,
    AcceptanceGenerationRunSerializer,
    AcceptanceGenerateSerializer,
    AcceptanceReorderSerializer,
    CompanyProjectDocumentTemplateSerializer,
    GeneratedProjectDocumentRevisionSerializer,
    GeneratedProjectDocumentSerializer,
    ProjectContentGenerateSerializer,
    ProjectContentAuditEventSerializer,
    ProjectDocumentDraftSerializer,
    ProjectDocumentGenerationRunSerializer,
)
from .services import (
    ProjectContentError,
    apply_acceptance_generation_run,
    approve_document_revision,
    create_acceptance_generation_run,
    create_document_generation_run,
    dispatch_acceptance_generation_run,
    dispatch_document_generation_run,
    queue_document_generation_run,
    retry_generation_run,
    validate_acceptance_review_draft,
)
from .snapshots import acceptance_prerequisite_state
from .template_validation import TemplateValidationError, validate_template_file


def _accessible_project_filter(user):
    if user.is_freelancer:
        return Q(user=user) | Q(team_members__team_member=user)
    return (
        Q(company_profile=user.company)
        | Q(user__company=user.company)
        | Q(team_members__team_member=user)
    )


def _project(
    request,
    project_id,
    *,
    module,
    write=False,
    approve=False,
    action=None,
):
    project = get_object_or_404(
        Project.objects.filter(_accessible_project_filter(request.user)).distinct(),
        pk=project_id,
    )
    require_project_content_access(
        request,
        project,
        module=module,
        write=write,
        approve=approve,
        action=action,
    )
    return project


def _error_response(exc):
    conflict_codes = {
        "generation_in_progress",
        "run_not_review_ready",
        "run_not_retryable",
        "source_changed",
        "stale_acceptance_target",
        "suggestion_selection_required",
    }
    error = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        error["details"] = exc.details
    if exc.code == "inaccessible_source":
        response_status = status.HTTP_403_FORBIDDEN
    elif exc.code in conflict_codes:
        response_status = status.HTTP_409_CONFLICT
    else:
        response_status = status.HTTP_400_BAD_REQUEST
    return Response(
        {"error": error},
        status=response_status,
    )


def _validate_document_access(request, project, document_ids):
    if document_ids is None:
        return
    visible_ids = set(
        get_accessible_document_queryset(request, project_id=project.id)
        .filter(id__in=document_ids)
        .values_list("id", flat=True)
    )
    if visible_ids != set(document_ids):
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("One or more selected documents are not accessible.")


class ProjectContentObjectPermissionMixin:
    access_module = None

    def project_for_object(self, instance):
        raise NotImplementedError

    def get_object(self):
        instance = super().get_object()
        require_project_content_access(
            self.request,
            self.project_for_object(instance),
            module=self.access_module,
        )
        return instance


class CompanyProjectDocumentTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyProjectDocumentTemplateSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        require_template_access(self.request, write=self.action in {"create", "destroy"})
        return CompanyProjectDocumentTemplate.objects.filter(
            company=self.request.user.company
        ).select_related("created_by")

    def perform_destroy(self, instance):
        if instance.is_active:
            from rest_framework.exceptions import ValidationError

            raise ValidationError("Activate another template version before deleting this one.")
        instance.delete()

    def partial_update(self, request, *args, **kwargs):
        require_template_access(request, write=True)
        if set(request.data) - {"name"}:
            return Response(
                {
                    "detail": (
                        "Only the template name can be changed. Upload a new version "
                        "to replace its file, kind, or locale."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().partial_update(request, *args, **kwargs)

    @action(detail=False, methods=["get"])
    def sample(self, request):
        require_template_access(request)
        kind = request.query_params.get("kind", "offer")
        locale = request.query_params.get("locale", "en")
        try:
            content = system_template_bytes(kind, locale)
        except DocumentRenderingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        label = "Proposal" if kind == "offer" else "Requirements-Specification"
        response = HttpResponse(
            content,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="ShareMundo-{label}-Template.docx"'
        )
        return response

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        require_template_access(request, write=True)
        template = self.get_object()
        try:
            report = validate_template_file(template.template_file, template.original_file_name)
            template.validation_status = template.ValidationStatus.VALID
            template.validation_report = report
            template.file_hash = report["sha256"]
        except TemplateValidationError as exc:
            template.validation_status = template.ValidationStatus.INVALID
            template.validation_report = {
                "valid": False,
                "code": exc.code,
                "message": exc.message,
            }
        template.save(
            update_fields=["validation_status", "validation_report", "file_hash", "updated_at"]
        )
        return Response(self.get_serializer(template).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def activate(self, request, pk=None):
        require_template_access(request, write=True)
        template = self.get_object()
        if template.validation_status != template.ValidationStatus.VALID:
            return Response(
                {"detail": "Only a valid template can be activated."},
                status=status.HTTP_409_CONFLICT,
            )
        CompanyProjectDocumentTemplate.objects.filter(
            company=template.company,
            kind=template.kind,
            locale=template.locale,
            is_active=True,
        ).exclude(pk=template.pk).update(is_active=False)
        template.is_active = True
        template.save(update_fields=["is_active", "updated_at"])
        GeneratedProjectDocument.objects.filter(
            project__company_profile=template.company,
            kind=template.kind,
            status=GeneratedProjectDocument.Status.CURRENT,
        ).update(status=GeneratedProjectDocument.Status.OUT_OF_DATE)
        return Response(self.get_serializer(template).data)


class ProjectDocumentGenerateAPIView(APIView):
    def post(self, request, project_id, kind):
        project = _project(
            request,
            project_id,
            module=PROJECT_CONTENT_MODULE,
            action="generate",
        )
        serializer = ProjectContentGenerateSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        document_ids = data.get("document_ids")
        _validate_document_access(request, project, document_ids)
        try:
            run, created = create_document_generation_run(
                project=project,
                user=request.user,
                kind=kind,
                locale=data["locale"],
                document_ids=document_ids,
                request_options=data.get("options"),
            )
            if created:
                dispatch_document_generation_run(run.id)
                run.refresh_from_db()
        except ProjectContentError as exc:
            return _error_response(exc)
        return Response(
            ProjectDocumentGenerationRunSerializer(run).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class AcceptanceGenerateAPIView(APIView):
    def post(self, request, project_id):
        phase_bound = str((request.data or {}).get("schema_version") or "") == "2" or any(
            key in (request.data or {})
            for key in ("fat_phase_id", "cat_phase_id")
        )
        if phase_bound:
            project = get_object_or_404(
                Project.objects.filter(
                    _accessible_project_filter(request.user)
                ).distinct(),
                pk=project_id,
            )
            require_acceptance_generation_access(request, project)
            serializer = AcceptanceGenerateSerializer(data=request.data or {})
        else:
            if settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
                return Response(
                    {
                        "error": {
                            "code": "feature_disabled",
                            "message": "The standalone acceptance generation flow is retired.",
                        }
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            project = _project(
                request,
                project_id,
                module=ACCEPTANCE_TESTS_MODULE,
                action="generate",
            )
            serializer = ProjectContentGenerateSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not phase_bound:
            _validate_document_access(request, project, data.get("document_ids"))
        try:
            run, created = create_acceptance_generation_run(
                project=project,
                user=request.user,
                locale=data["locale"],
                document_ids=data.get("document_ids"),
                fat_phase_id=data.get("fat_phase_id"),
                cat_phase_id=data.get("cat_phase_id"),
                request_options=data.get("options"),
                request=request if phase_bound else None,
            )
            if created:
                dispatch_acceptance_generation_run(run.id)
                run.refresh_from_db()
        except ProjectContentError as exc:
            return _error_response(exc)
        return Response(
            AcceptanceGenerationRunSerializer(run).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class AcceptancePrerequisitesAPIView(APIView):
    def get(self, request, project_id):
        project = get_object_or_404(
            Project.objects.filter(_accessible_project_filter(request.user)).distinct(),
            pk=project_id,
        )
        require_acceptance_generation_access(request, project)
        if not settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
            return Response(
                {
                    "error": {
                        "code": "feature_disabled",
                        "message": "AI acceptance Action Item generation is disabled.",
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        state = acceptance_prerequisite_state(project)
        phases = []
        for phase in Milestone.objects.filter(project=project).order_by(
            "sort_order", "id"
        ):
            duration_days = (
                (phase.due_date - phase.start_date).days + 1
                if phase.start_date and phase.due_date
                else None
            )
            phases.append(
                {
                    "id": phase.id,
                    "name": phase.name or phase.phase_title or "",
                    "start_date": phase.start_date,
                    "due_date": phase.due_date,
                    "duration_days": duration_days,
                    "standard_duration": duration_days == 7,
                }
            )
        return Response(
            {
                "ready": state["ready"],
                "missing": state["missing"],
                "checks": {
                    key: state[key]
                    for key in (
                        "products",
                        "product_attributes",
                        "processes",
                        "machinery",
                        "stations",
                        "process_attributes",
                    )
                },
                "phases": phases,
            }
        )


class GeneratedProjectDocumentViewSet(
    ProjectContentObjectPermissionMixin, viewsets.ReadOnlyModelViewSet
):
    serializer_class = GeneratedProjectDocumentSerializer
    access_module = PROJECT_CONTENT_MODULE

    def project_for_object(self, instance):
        return instance.project

    def get_queryset(self):
        queryset = GeneratedProjectDocument.objects.filter(
            project__in=Project.objects.filter(
                _accessible_project_filter(self.request.user)
            )
        ).select_related("project", "current_revision")
        project_id = self.request.query_params.get("project")
        kind = self.request.query_params.get("kind")
        if project_id:
            project = _project(
                self.request,
                project_id,
                module=PROJECT_CONTENT_MODULE,
            )
            queryset = queryset.filter(project=project)
        elif self.action == "list":
            return queryset.none()
        if kind:
            queryset = queryset.filter(kind=kind)
        return queryset.distinct()


class ProjectContentAuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ProjectContentAuditEventSerializer

    def get_queryset(self):
        project_id = self.request.query_params.get("project")
        if not project_id:
            return ProjectContentAuditEvent.objects.none()
        scope = self.request.query_params.get("scope", "documents")
        module = ACCEPTANCE_TESTS_MODULE if scope == "acceptance" else PROJECT_CONTENT_MODULE
        project = _project(self.request, project_id, module=module)
        queryset = ProjectContentAuditEvent.objects.filter(project=project).select_related(
            "actor"
        )
        object_type = self.request.query_params.get("object_type")
        object_id = self.request.query_params.get("object_id")
        if object_type:
            queryset = queryset.filter(object_type=object_type)
        if object_id:
            queryset = queryset.filter(object_id=object_id)
        return queryset


class ProjectDocumentGenerationRunViewSet(
    ProjectContentObjectPermissionMixin, viewsets.ReadOnlyModelViewSet
):
    serializer_class = ProjectDocumentGenerationRunSerializer
    access_module = PROJECT_CONTENT_MODULE

    def project_for_object(self, instance):
        return instance.project

    def get_queryset(self):
        queryset = ProjectDocumentGenerationRun.objects.filter(
            project__in=Project.objects.filter(_accessible_project_filter(self.request.user))
        ).select_related("project", "generated_document", "applied_revision")
        project_id = self.request.query_params.get("project")
        kind = self.request.query_params.get("kind")
        if project_id:
            project = _project(self.request, project_id, module=PROJECT_CONTENT_MODULE)
            queryset = queryset.filter(project=project)
        elif self.action == "list":
            return queryset.none()
        if kind:
            queryset = queryset.filter(kind=kind)
        return queryset.distinct()

    def partial_update(self, request, pk=None):
        run = self.get_object()
        require_project_content_access(
            request,
            run.project,
            module=PROJECT_CONTENT_MODULE,
            action="update",
        )
        if run.status != run.Status.REVIEW_READY:
            return Response(
                {"detail": "Only a review-ready draft can be edited."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ProjectDocumentDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run.draft_content = serializer.validated_data["content"]
        run.modified_by = request.user
        run.save(update_fields=["draft_content", "modified_by", "updated_at"])
        record_project_content_event(
            project=run.project,
            event_type="document_generation_review_updated",
            instance=run,
            actor=request.user,
        )
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        run = self.get_object()
        require_project_content_access(
            request, run.project, module=PROJECT_CONTENT_MODULE, action="update"
        )
        content = request.data.get("content") if request.data else None
        try:
            run = queue_document_generation_run(run, request.user, content)
        except ProjectContentError as exc:
            return _error_response(exc)
        return Response(
            self.get_serializer(run).data,
            status=(
                status.HTTP_200_OK
                if run.status == run.Status.APPLIED
                else status.HTTP_202_ACCEPTED
            ),
        )

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        run = self.get_object()
        require_project_content_access(
            request, run.project, module=PROJECT_CONTENT_MODULE, action="generate"
        )
        try:
            replacement = retry_generation_run(run, request.user, request=request)
        except ProjectContentError as exc:
            return _error_response(exc)
        record_project_content_event(
            project=run.project,
            event_type="document_generation_retried",
            instance=run,
            actor=request.user,
            metadata={"replacement_run_id": str(replacement.id)},
        )
        return Response(self.get_serializer(replacement).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        require_project_content_access(
            request, run.project, module=PROJECT_CONTENT_MODULE, action="generate"
        )
        if run.status not in {run.Status.QUEUED, run.Status.GENERATING, run.Status.REVIEW_READY}:
            return Response({"detail": "This generation can no longer be cancelled."}, status=409)
        run.status = run.Status.CANCELLED
        run.modified_by = request.user
        run.save(update_fields=["status", "modified_by", "updated_at"])
        record_project_content_event(
            project=run.project,
            event_type="document_generation_cancelled",
            instance=run,
            actor=request.user,
        )
        return Response(self.get_serializer(run).data)


class GeneratedProjectDocumentRevisionViewSet(
    ProjectContentObjectPermissionMixin, viewsets.ReadOnlyModelViewSet
):
    serializer_class = GeneratedProjectDocumentRevisionSerializer
    access_module = PROJECT_CONTENT_MODULE

    def project_for_object(self, instance):
        return instance.generated_document.project

    def get_queryset(self):
        queryset = GeneratedProjectDocumentRevision.objects.filter(
            generated_document__project__in=Project.objects.filter(
                _accessible_project_filter(self.request.user)
            )
        ).select_related("generated_document", "generated_document__project")
        project_id = self.request.query_params.get("project")
        kind = self.request.query_params.get("kind")
        if project_id:
            project = _project(self.request, project_id, module=PROJECT_CONTENT_MODULE)
            queryset = queryset.filter(generated_document__project=project)
        elif self.action == "list":
            return queryset.none()
        if kind:
            queryset = queryset.filter(generated_document__kind=kind)
        return queryset.distinct()

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        revision = self.get_object()
        require_project_content_access(
            request,
            revision.generated_document.project,
            module=PROJECT_CONTENT_MODULE,
            action="approve",
        )
        try:
            revision = approve_document_revision(revision, request.user)
        except ProjectContentError as exc:
            return _error_response(exc)
        return Response(self.get_serializer(revision).data)


class RetiredAcceptanceWriteMixin:
    def initial(self, request, *args, **kwargs):
        if settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            raise MethodNotAllowed(request.method)
        return super().initial(request, *args, **kwargs)


class AcceptanceChecklistViewSet(
    RetiredAcceptanceWriteMixin,
    ProjectContentObjectPermissionMixin, viewsets.ModelViewSet
):
    serializer_class = AcceptanceChecklistSerializer
    access_module = ACCEPTANCE_TESTS_MODULE
    http_method_names = ["get", "post", "patch", "head", "options"]

    def project_for_object(self, instance):
        return instance.project

    def get_queryset(self):
        queryset = AcceptanceChecklist.objects.filter(
            project__in=Project.objects.filter(_accessible_project_filter(self.request.user))
        ).annotate(criteria_count=Count("criteria"))
        project_id = self.request.query_params.get("project")
        if project_id:
            project = _project(self.request, project_id, module=ACCEPTANCE_TESTS_MODULE)
            queryset = queryset.filter(project=project)
        elif self.action == "list":
            return queryset.none()
        phase = self.request.query_params.get("phase")
        if phase:
            queryset = queryset.filter(phase=phase)
        return queryset.distinct()

    def create(self, request, *args, **kwargs):
        project = _project(
            request,
            request.data.get("project"),
            module=ACCEPTANCE_TESTS_MODULE,
            action="create",
        )
        phase = request.data.get("phase")
        if phase not in dict(AcceptanceChecklist.Phase.choices):
            return Response({"phase": ["Invalid acceptance phase."]}, status=400)
        default_names = {
            AcceptanceChecklist.Phase.FACTORY: "Factory Acceptance Test (FAT)",
            AcceptanceChecklist.Phase.SITE: "Customer Acceptance Test (CAT)",
        }
        checklist, created = AcceptanceChecklist.objects.get_or_create(
            project=project,
            phase=phase,
            defaults={
                "display_name": request.data.get("display_name") or default_names[phase],
                "created_by": request.user,
            },
        )
        if created:
            record_project_content_event(
                project=project,
                event_type="acceptance_checklist_created",
                instance=checklist,
                actor=request.user,
                metadata={"phase": phase},
            )
        return Response(
            self.get_serializer(checklist).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def partial_update(self, request, *args, **kwargs):
        checklist = self.get_object()
        require_project_content_access(
            request, checklist.project, module=ACCEPTANCE_TESTS_MODULE, action="update"
        )
        response = super().partial_update(request, *args, **kwargs)
        record_project_content_event(
            project=checklist.project,
            event_type="acceptance_checklist_updated",
            instance=checklist,
            actor=request.user,
        )
        return response


class AcceptanceCriterionViewSet(
    RetiredAcceptanceWriteMixin,
    ProjectContentObjectPermissionMixin, viewsets.ModelViewSet
):
    serializer_class = AcceptanceCriterionSerializer
    access_module = ACCEPTANCE_TESTS_MODULE

    def project_for_object(self, instance):
        return instance.checklist.project

    def get_queryset(self):
        queryset = AcceptanceCriterion.objects.filter(
            checklist__project__in=Project.objects.filter(
                _accessible_project_filter(self.request.user)
            )
        ).select_related("checklist", "checklist__project", "parent").prefetch_related(
            "assignees", "requirements", "documents", "linked_folders", "evidence"
        ).annotate(comments_count=Count("comments", distinct=True))
        project_id = self.request.query_params.get("project")
        if project_id:
            project = _project(self.request, project_id, module=ACCEPTANCE_TESTS_MODULE)
            queryset = queryset.filter(checklist__project=project)
        elif self.action == "list":
            return queryset.none()
        for field in ("checklist", "parent", "status", "priority"):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        assignee = self.request.query_params.get("assignee")
        if assignee:
            queryset = queryset.filter(assignees__id=assignee)
        label = self.request.query_params.get("label")
        if label:
            queryset = queryset.filter(labels__contains=[label])
        date_field = self.request.query_params.get("date_field", "start_date")
        if date_field not in {"start_date", "end_date", "forecast_date"}:
            date_field = "start_date"
        date_from = parse_date(self.request.query_params.get("date_from", ""))
        date_to = parse_date(self.request.query_params.get("date_to", ""))
        if date_from:
            queryset = queryset.filter(**{f"{date_field}__gte": date_from})
        if date_to:
            queryset = queryset.filter(**{f"{date_field}__lte": date_to})
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(acceptance_criteria__icontains=search)
            )
        return queryset.distinct().order_by("sort_order", "created_at", "id")

    def perform_create(self, serializer):
        checklist = serializer.validated_data["checklist"]
        require_project_content_access(
            self.request,
            checklist.project,
            module=ACCEPTANCE_TESTS_MODULE,
            action="create",
        )
        criterion = serializer.save()
        record_project_content_event(
            project=checklist.project,
            event_type="acceptance_criterion_created",
            instance=criterion,
            actor=self.request.user,
        )

    def perform_update(self, serializer):
        require_project_content_access(
            self.request,
            serializer.instance.checklist.project,
            module=ACCEPTANCE_TESTS_MODULE,
            action="update",
        )
        criterion = serializer.save()
        record_project_content_event(
            project=criterion.checklist.project,
            event_type="acceptance_criterion_updated",
            instance=criterion,
            actor=self.request.user,
            metadata={"fields": sorted(serializer.validated_data)},
        )

    def perform_destroy(self, instance):
        require_project_content_access(
            self.request,
            instance.checklist.project,
            module=ACCEPTANCE_TESTS_MODULE,
            action="delete",
        )
        record_project_content_event(
            project=instance.checklist.project,
            event_type="acceptance_criterion_deleted",
            instance=instance,
            actor=self.request.user,
            metadata={"name": instance.name},
        )
        instance.delete()

    @action(detail=False, methods=["get"])
    def options(self, request):
        rows = self.get_queryset().order_by("sort_order", "created_at").values(
            "id",
            "name",
            "parent_id",
        )[:1000]
        return Response(
            [
                {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "parent": str(row["parent_id"]) if row["parent_id"] else None,
                }
                for row in rows
            ]
        )

    @action(detail=False, methods=["post"], url_path="bulk-update")
    @transaction.atomic
    def bulk_update(self, request):
        serializer = AcceptanceBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        criteria = list(self.get_queryset().filter(id__in=serializer.validated_data["ids"]))
        if len(criteria) != len(set(serializer.validated_data["ids"])):
            return Response({"detail": "One or more criteria are not accessible."}, status=403)
        allowed = {
            "status",
            "priority",
            "start_date",
            "end_date",
            "forecast_date",
            "labels",
            "assignees",
        }
        changes = serializer.validated_data["changes"]
        if not set(changes).issubset(allowed):
            return Response({"detail": "Unsupported bulk update field."}, status=400)
        updated = []
        for criterion in criteria:
            require_project_content_access(
                request,
                criterion.checklist.project,
                module=ACCEPTANCE_TESTS_MODULE,
                action="update",
            )
            criterion_serializer = self.get_serializer(
                criterion,
                data=changes,
                partial=True,
            )
            criterion_serializer.is_valid(raise_exception=True)
            criterion = criterion_serializer.save()
            updated.append(criterion)
            record_project_content_event(
                project=criterion.checklist.project,
                event_type="acceptance_criterion_bulk_updated",
                instance=criterion,
                actor=request.user,
                metadata={"fields": sorted(changes)},
            )
        return Response(self.get_serializer(updated, many=True).data)

    @action(detail=False, methods=["post"])
    @transaction.atomic
    def reorder(self, request):
        serializer = AcceptanceReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        for item in serializer.validated_data["items"]:
            criterion = get_object_or_404(self.get_queryset(), pk=item.get("id"))
            require_project_content_access(
                request,
                criterion.checklist.project,
                module=ACCEPTANCE_TESTS_MODULE,
                action="update",
            )
            parent_id = item.get("parent")
            if parent_id:
                parent = get_object_or_404(
                    AcceptanceCriterion,
                    pk=parent_id,
                    checklist=criterion.checklist,
                )
                if parent.pk == criterion.pk:
                    return Response({"detail": "A criterion cannot be its own parent."}, status=400)
                ancestor = parent
                seen_ids = set()
                while ancestor and ancestor.id not in seen_ids:
                    if ancestor.id == criterion.id:
                        return Response(
                            {"detail": "A criterion cannot be nested below its descendant."},
                            status=400,
                        )
                    seen_ids.add(ancestor.id)
                    ancestor = ancestor.parent
                criterion.parent = parent
            else:
                criterion.parent = None
            criterion.sort_order = max(0, int(item.get("sort_order", 0)))
            criterion.modified_by = request.user
            criterion.manual_fields = sorted(
                set(criterion.manual_fields or []) | {"parent", "sort_order"}
            )
            criterion.save()
            record_project_content_event(
                project=criterion.checklist.project,
                event_type="acceptance_criterion_reordered",
                instance=criterion,
                actor=request.user,
                metadata={
                    "parent_id": str(criterion.parent_id) if criterion.parent_id else None,
                    "sort_order": criterion.sort_order,
                },
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AcceptanceGenerationRunViewSet(
    ProjectContentObjectPermissionMixin, viewsets.ReadOnlyModelViewSet
):
    serializer_class = AcceptanceGenerationRunSerializer
    access_module = ACCEPTANCE_TESTS_MODULE

    def get_object(self):
        instance = super(ProjectContentObjectPermissionMixin, self).get_object()
        if instance.schema_version == "2" and instance.fat_phase_id:
            if not settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
                raise NotFound()
            require_acceptance_generation_access(self.request, instance.project)
        else:
            require_project_content_access(
                self.request,
                instance.project,
                module=ACCEPTANCE_TESTS_MODULE,
            )
        return instance

    def project_for_object(self, instance):
        return instance.project

    def get_queryset(self):
        queryset = AcceptanceGenerationRun.objects.filter(
            project__in=Project.objects.filter(_accessible_project_filter(self.request.user))
        ).select_related("project").prefetch_related("action_item_provenance")
        project_id = self.request.query_params.get("project")
        if project_id:
            project = get_object_or_404(
                Project.objects.filter(
                    _accessible_project_filter(self.request.user)
                ).distinct(),
                pk=project_id,
            )
            allowed_runs = Q(pk__in=[])
            has_run_access = False
            if settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
                try:
                    require_acceptance_generation_access(self.request, project)
                    allowed_runs |= Q(
                        schema_version="2",
                        fat_phase__isnull=False,
                    )
                    has_run_access = True
                except PermissionDenied:
                    pass
            try:
                require_project_content_access(
                    self.request,
                    project,
                    module=ACCEPTANCE_TESTS_MODULE,
                )
                allowed_runs |= ~Q(
                    schema_version="2",
                    fat_phase__isnull=False,
                )
                has_run_access = True
            except PermissionDenied:
                pass
            if not has_run_access:
                raise PermissionDenied()
            queryset = queryset.filter(project=project).filter(allowed_runs)
        elif self.action == "list":
            return queryset.none()
        return queryset.distinct()

    def partial_update(self, request, pk=None):
        run = self.get_object()
        if not (run.schema_version == "2" and run.fat_phase_id):
            if settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
                raise MethodNotAllowed(request.method)
            require_project_content_access(
                request, run.project, module=ACCEPTANCE_TESTS_MODULE, action="update"
            )
        if run.status != run.Status.REVIEW_READY:
            return Response({"detail": "Only a review-ready draft can be edited."}, status=409)
        serializer = AcceptanceDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if run.schema_version == "2" and run.fat_phase_id:
            try:
                run.draft_content = validate_acceptance_review_draft(
                    run,
                    serializer.validated_data.get("proposals"),
                )
            except ProjectContentError as exc:
                return _error_response(exc)
        else:
            run.draft_content = serializer.validated_data
        run.modified_by = request.user
        run.save(update_fields=["draft_content", "modified_by", "updated_at"])
        record_project_content_event(
            project=run.project,
            event_type="acceptance_generation_review_updated",
            instance=run,
            actor=request.user,
        )
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        run = self.get_object()
        if not (run.schema_version == "2" and run.fat_phase_id):
            if settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
                raise MethodNotAllowed(request.method)
            require_project_content_access(
                request, run.project, module=ACCEPTANCE_TESTS_MODULE, action="update"
            )
        serializer = AcceptanceApplySerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        try:
            run = apply_acceptance_generation_run(
                run,
                request.user,
                selected_keys=serializer.validated_data.get("selected_keys"),
                rows=serializer.validated_data.get("criteria"),
                request=request,
            )
        except ProjectContentError as exc:
            return _error_response(exc)
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        run = self.get_object()
        if not (run.schema_version == "2" and run.fat_phase_id):
            if settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
                raise MethodNotAllowed(request.method)
            require_project_content_access(
                request, run.project, module=ACCEPTANCE_TESTS_MODULE, action="generate"
            )
        try:
            replacement = retry_generation_run(run, request.user, request=request)
        except ProjectContentError as exc:
            return _error_response(exc)
        record_project_content_event(
            project=run.project,
            event_type="acceptance_generation_retried",
            instance=run,
            actor=request.user,
            metadata={"replacement_run_id": str(replacement.id)},
        )
        return Response(self.get_serializer(replacement).data, status=202)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        if not (run.schema_version == "2" and run.fat_phase_id):
            if settings.AI_ACCEPTANCE_ACTION_ITEMS_ENABLED:
                raise MethodNotAllowed(request.method)
            require_project_content_access(
                request, run.project, module=ACCEPTANCE_TESTS_MODULE, action="generate"
            )
        if run.status not in {run.Status.QUEUED, run.Status.GENERATING, run.Status.REVIEW_READY}:
            return Response({"detail": "This generation can no longer be cancelled."}, status=409)
        run.status = run.Status.CANCELLED
        run.modified_by = request.user
        run.save(update_fields=["status", "modified_by", "updated_at"])
        record_project_content_event(
            project=run.project,
            event_type="acceptance_generation_cancelled",
            instance=run,
            actor=request.user,
        )
        return Response(self.get_serializer(run).data)


class AcceptanceCriterionCommentViewSet(
    RetiredAcceptanceWriteMixin,
    ProjectContentObjectPermissionMixin, viewsets.ModelViewSet
):
    serializer_class = AcceptanceCriterionCommentSerializer
    access_module = ACCEPTANCE_TESTS_MODULE

    def project_for_object(self, instance):
        return instance.criterion.checklist.project

    def get_queryset(self):
        queryset = AcceptanceCriterionComment.objects.filter(
            criterion__checklist__project__in=Project.objects.filter(
                _accessible_project_filter(self.request.user)
            )
        ).select_related("criterion", "criterion__checklist", "criterion__checklist__project")
        criterion_id = self.request.query_params.get("criterion")
        if criterion_id:
            criterion = get_object_or_404(
                AcceptanceCriterion.objects.select_related("checklist__project"),
                pk=criterion_id,
            )
            require_project_content_access(
                self.request,
                criterion.checklist.project,
                module=ACCEPTANCE_TESTS_MODULE,
            )
            queryset = queryset.filter(criterion=criterion)
        elif self.action == "list":
            return queryset.none()
        return queryset.distinct()

    def perform_create(self, serializer):
        criterion = serializer.validated_data["criterion"]
        require_project_content_access(
            self.request,
            criterion.checklist.project,
            module=ACCEPTANCE_TESTS_MODULE,
            action="create",
        )
        comment = serializer.save(sender=self.request.user)
        record_project_content_event(
            project=criterion.checklist.project,
            event_type="acceptance_comment_created",
            instance=comment,
            actor=self.request.user,
            metadata={"criterion_id": str(criterion.id)},
        )

    def perform_update(self, serializer):
        require_project_content_access(
            self.request,
            serializer.instance.criterion.checklist.project,
            module=ACCEPTANCE_TESTS_MODULE,
            action="update",
        )
        comment = serializer.save()
        record_project_content_event(
            project=comment.criterion.checklist.project,
            event_type="acceptance_comment_updated",
            instance=comment,
            actor=self.request.user,
            metadata={"criterion_id": str(comment.criterion_id)},
        )

    def perform_destroy(self, instance):
        require_project_content_access(
            self.request,
            instance.criterion.checklist.project,
            module=ACCEPTANCE_TESTS_MODULE,
            action="delete",
        )
        record_project_content_event(
            project=instance.criterion.checklist.project,
            event_type="acceptance_comment_deleted",
            instance=instance,
            actor=self.request.user,
            metadata={"criterion_id": str(instance.criterion_id)},
        )
        instance.delete()

    @action(detail=True, methods=["post"], url_path="reactions/toggle")
    def toggle_reaction(self, request, pk=None):
        comment = self.get_object()
        require_project_content_access(
            request,
            comment.criterion.checklist.project,
            module=ACCEPTANCE_TESTS_MODULE,
            action="update",
        )
        emoji = str(request.data.get("emoji", ""))[:32]
        if not emoji:
            return Response({"emoji": ["This field is required."]}, status=400)
        reaction, created = AcceptanceCriterionCommentReaction.objects.get_or_create(
            comment=comment,
            user=request.user,
            emoji=emoji,
        )
        if not created:
            reaction.delete()
            record_project_content_event(
                project=comment.criterion.checklist.project,
                event_type="acceptance_comment_reaction_removed",
                instance=comment,
                actor=request.user,
                metadata={"reaction": emoji},
            )
            return Response(status=status.HTTP_204_NO_CONTENT)
        record_project_content_event(
            project=comment.criterion.checklist.project,
            event_type="acceptance_comment_reaction_added",
            instance=comment,
            actor=request.user,
            metadata={"reaction": emoji},
        )
        return Response(
            AcceptanceCriterionCommentReactionSerializer(reaction).data,
            status=status.HTTP_201_CREATED,
        )
