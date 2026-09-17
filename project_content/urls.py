from django.urls import include, path

from project_management.slash_router import OptionalSlashRouter

from .views import (
    AcceptanceChecklistViewSet,
    AcceptanceCriterionCommentViewSet,
    AcceptanceCriterionViewSet,
    AcceptanceGenerateAPIView,
    AcceptancePrerequisitesAPIView,
    AcceptanceGenerationRunViewSet,
    CompanyProjectDocumentTemplateViewSet,
    GeneratedProjectDocumentRevisionViewSet,
    GeneratedProjectDocumentViewSet,
    ProjectContentAuditEventViewSet,
    ProjectDocumentGenerateAPIView,
    ProjectDocumentGenerationRunViewSet,
)


router = OptionalSlashRouter()
router.register("templates", CompanyProjectDocumentTemplateViewSet, basename="project-content-template")
router.register("documents", GeneratedProjectDocumentViewSet, basename="generated-project-document")
router.register("generation-runs", ProjectDocumentGenerationRunViewSet, basename="project-document-generation-run")
router.register("revisions", GeneratedProjectDocumentRevisionViewSet, basename="generated-project-document-revision")
router.register("acceptance-checklists", AcceptanceChecklistViewSet, basename="acceptance-checklist")
router.register("acceptance-criteria", AcceptanceCriterionViewSet, basename="acceptance-criterion")
router.register("acceptance-runs", AcceptanceGenerationRunViewSet, basename="acceptance-generation-run")
router.register("acceptance-comments", AcceptanceCriterionCommentViewSet, basename="acceptance-comment")
router.register("audit-events", ProjectContentAuditEventViewSet, basename="project-content-audit-event")


urlpatterns = [
    path("", include(router.urls)),
    path(
        "projects/<int:project_id>/acceptance/generate/",
        AcceptanceGenerateAPIView.as_view(),
        name="acceptance-generate",
    ),
    path(
        "projects/<int:project_id>/acceptance/prerequisites/",
        AcceptancePrerequisitesAPIView.as_view(),
        name="acceptance-prerequisites",
    ),
    path(
        "projects/<int:project_id>/<str:kind>/generate/",
        ProjectDocumentGenerateAPIView.as_view(),
        name="project-document-generate",
    ),
]
