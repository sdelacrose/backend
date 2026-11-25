from django.urls import path
from .feedback_views import PortalFeedback, InternalSurveillanceFeedback, InternalSupplierPreview
from .views import (PortalProjects, PortalDashboard, PortalTemplate, PortalChoices, PortalBatches, PortalBatchDetail, PortalBulkFiles,
    InternalProjects, InternalProjectAccess, InternalBatches, InternalBatchDetail, InternalTasks, InternalDocuments)

from .shared import InternalShared, PortalShared, PortalSharedFile

urlpatterns = [
    path("documents/<int:document_id>/feedback/<int:publication_id>", PortalFeedback.as_view()),
    path("internal/surveillances/<int:surveillance_id>/feedback", InternalSurveillanceFeedback.as_view()),
    path("internal/documents/<int:document_id>/supplier-preview", InternalSupplierPreview.as_view()),
    path('internal/projects/<int:project_id>/shared', InternalShared.as_view()),
    path('access/<int:access_id>/shared', PortalShared.as_view()),
    path('access/<int:access_id>/shared/<int:exchange_id>/files/<int:file_id>', PortalSharedFile.as_view()),
    path('internal/projects/<int:project_id>/documents', InternalDocuments.as_view()),
    path('internal/projects/<int:project_id>/tasks', InternalTasks.as_view()),
    path('projects', PortalProjects.as_view()),
    path('projects/<int:project_id>/dashboard', PortalDashboard.as_view()),
    path('access/<int:access_id>/template', PortalTemplate.as_view()),
    path('access/<int:access_id>/choices', PortalChoices.as_view()),
    path('access/<int:access_id>/batches', PortalBatches.as_view()),
    path('access/<int:access_id>/batches/<int:batch_id>', PortalBatchDetail.as_view()),
    path('projects/<int:project_id>/files', PortalBulkFiles.as_view()),
    path('internal/projects', InternalProjects.as_view()),
    path('internal/projects/<int:project_id>/access', InternalProjectAccess.as_view()),
    path('internal/projects/<int:project_id>/batches', InternalBatches.as_view()),
    path('internal/projects/<int:project_id>/batches/<int:batch_id>', InternalBatchDetail.as_view()),
]
