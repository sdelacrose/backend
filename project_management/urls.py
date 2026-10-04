from django.urls import path, include, re_path

from project_management.views import ContactViewset, DocumentViewSet, FolderViewSet, \
    ProjectTimeViewSet, BillingViewSet, MilestoneViewSet, ProjectViewSet, ECPMPConfigViewSet, \
    AzureCallbackAPIView, AzureLoginAPIView, FetchSharePointSitesAPIView, AzureTokenRefreshAPIView, \
    FetchSharePointFilesAPIView, ActionItemViewSet, ObjectAssessmentViewSet, UpdateProjectOnConnectionRevoke, \
    SiteReportViewSet, RemoveRelatedObjectsOnUserDelete, CreateRelatedObjectsOnFreelancerCreate, \
    CreateRelatedObjectsOnCompanyCreate, ContactCompanyViewSet, ContactUserViewSet, TimeCardViewSet, \
    ExpenseCardViewSet, InvoiceViewSet, CreateRelatedObjectsOnUserCreate, ProjectPhaseViewSet, \
    ProjectFundingViewSet, ProjectCalendarViewSet, ActionItemCommentViewSet, UpdateTeamMembersOnRoleChange, ProjectAIJobViewSet, FundingOpportunityMatchViewSet, \
    PurchaseOrderViewSet, \
    UpdateFreelancerProjectOnConnectionRevoke, TimesheetWeekViewSet, MonthlyTimesheetViewSet, \
    CompanyPublicHolidayViewSet, ProjectMatrixConfigViewSet, SolutionDemandViewSet, RequirementViewSet, \
    TransportViewSet, PartsListConfigViewSet, SolutionSpaceConfigViewSet, MachineConfigViewSet, EquipmentConfigViewSet, AvailableProductPartViewSet, MachineViewSet, EquipmentViewSet, BillOfMaterialPartViewSet, CompanySolutionSpaceItemViewSet, ProjectSolutionSpaceItemViewSet, ProjectWorkstationViewSet, BomGenerationRunViewSet, ProcessAssemblyRecommendationRunViewSet, MatrixCommentViewSet, PartPurchaseIntentViewSet, SupplierPaymentMethodViewSet, ProductAssemblyExtractionRunViewSet, ProductAttributeExtractionRunViewSet, ProcessAssemblyExtractionRunViewSet, ProcessAttributeExtractionRunViewSet
from project_management.views.document_register import DocumentRegisterViewSet
from project_management.views.document_attachment import DocumentAttachmentViewSet
from project_management.views.correspondence import (
    CorrespondenceViewSet,
    CorrespondenceAttachmentViewSet,
)
from project_management.views.correspondence_workflow import (
    CorrespondenceValidationWorkflowViewSet,
)
from project_management.views.correspondence_send import CorrespondenceSendViewSet
from project_management.views.transmittal import (
    TransmittalEventViewSet,
    TransmittalIssueView,
    TransmittalItemViewSet,
    TransmittalViewSet,
)
from project_management.views.correspondances_register import CorrespondancesRegisterView
from project_management.views.document_revision import DocumentRevisionViewSet
from project_management.views.document_workflow import DocumentWorkflowViewSet
from project_management.views.supplier_surveillance import SupplierSurveillanceViewSet
from project_management.views.supplier_surveillance_template import SupplierSurveillanceTemplateViewSet
from project_management.views.document_search import (
    DocumentContentSearchAPIView,
    DocumentIndexBackfillAPIView,
    DocumentIndexRetryAPIView,
    DocumentIndexStatusAPIView,
    DocumentIndexSummaryAPIView,
)
from project_management.views.advanced_search import (
    AdvancedSearchBackfillAPIView,
    AdvancedSearchConfigAPIView,
    AdvancedSearchIndexSummaryAPIView,
    AdvancedSearchQueryAPIView,
    AdvancedSearchRetryAPIView,
)

from project_management.slash_router import OptionalSlashRouter
from project_management.views.catalog_recommendation import CatalogRecommendationRunViewSet

from project_management.views import GlobalConfigListView

router = OptionalSlashRouter()
router.register('catalog-recommendations', CatalogRecommendationRunViewSet, basename='catalog-recommendations')

router.register('projects', ProjectViewSet)
router.register('ecpmpconfig', ECPMPConfigViewSet)
router.register('milestone', MilestoneViewSet)
router.register('action_items', ActionItemViewSet)
router.register('object_assessment', ObjectAssessmentViewSet)
router.register('site-reporting', SiteReportViewSet)
router.register('contact-user', ContactUserViewSet)
router.register('contact-company', ContactCompanyViewSet)
router.register('time-card', TimeCardViewSet)
router.register('timesheet-weeks', TimesheetWeekViewSet, basename='timesheet-weeks')
router.register('monthly-timesheets', MonthlyTimesheetViewSet, basename='monthly-timesheets')
router.register('expense-card', ExpenseCardViewSet)
router.register('billing-invoice', InvoiceViewSet)
router.register('project-phase', ProjectPhaseViewSet)
router.register('project-funding', ProjectFundingViewSet)
router.register('project-calendar', ProjectCalendarViewSet, basename='project-calendar')
router.register('company-public-holidays', CompanyPublicHolidayViewSet, basename='company-public-holidays')
router.register('project-matrix-config', ProjectMatrixConfigViewSet, basename='project-matrix-config')
router.register('parts-list-config', PartsListConfigViewSet, basename='parts-list-config')
router.register('solution-space-config', SolutionSpaceConfigViewSet, basename='solution-space-config')
router.register('machine-config', MachineConfigViewSet, basename='machine-config')
router.register('equipment-config', EquipmentConfigViewSet, basename='equipment-config')
router.register('solution-demands', SolutionDemandViewSet, basename='solution-demands')
router.register('requirements', RequirementViewSet, basename='requirements')
router.register('transports', TransportViewSet, basename='transports')
router.register('available-product-parts', AvailableProductPartViewSet, basename='available-product-parts')
router.register('machines', MachineViewSet, basename='machines')
router.register('equipments', EquipmentViewSet, basename='equipments')
router.register('bom-parts', BillOfMaterialPartViewSet, basename='bom-parts')
router.register('company-solution-space-items', CompanySolutionSpaceItemViewSet, basename='company-solution-space-items')
router.register('project-solution-space-items', ProjectSolutionSpaceItemViewSet, basename='project-solution-space-items')
router.register('project-workstations', ProjectWorkstationViewSet, basename='project-workstations')
router.register('bom-generation-runs', BomGenerationRunViewSet, basename='bom-generation-runs')
router.register('process-assembly-recommendations', ProcessAssemblyRecommendationRunViewSet, basename='process-assembly-recommendations')
router.register('ai-jobs', ProjectAIJobViewSet, basename='ai-jobs')
router.register('funding-opportunity-matches', FundingOpportunityMatchViewSet, basename='funding-opportunity-matches')
router.register('purchase-orders', PurchaseOrderViewSet, basename='purchase-orders')
router.register('part-purchase-intents', PartPurchaseIntentViewSet, basename='part-purchase-intents')
router.register('part-purchase-payment-methods', SupplierPaymentMethodViewSet, basename='part-purchase-payment-methods')
router.register(
    'product-assembly-extractions',
    ProductAssemblyExtractionRunViewSet,
    basename='product-assembly-extractions',
)
router.register(
    'product-attribute-extractions',
    ProductAttributeExtractionRunViewSet,
    basename='product-attribute-extractions',
)
router.register(
    'process-assembly-extractions',
    ProcessAssemblyExtractionRunViewSet,
    basename='process-assembly-extractions',
)
router.register(
    'process-attribute-extractions',
    ProcessAttributeExtractionRunViewSet,
    basename='process-attribute-extractions',
)

urlpatterns = [
    path('', include(router.urls)),
    re_path(r'^advanced-search/query/?$', AdvancedSearchQueryAPIView.as_view(), name='advanced-search-query'),
    re_path(r'^advanced-search/config/?$', AdvancedSearchConfigAPIView.as_view(), name='advanced-search-config'),
    re_path(r'^advanced-search/index-summary/?$', AdvancedSearchIndexSummaryAPIView.as_view(), name='advanced-search-index-summary'),
    re_path(r'^advanced-search/backfill/?$', AdvancedSearchBackfillAPIView.as_view(), name='advanced-search-backfill'),
    re_path(
        r'^advanced-search/(?P<source_type>[a-z_]+)/(?P<source_id>\d+)/retry/?$',
        AdvancedSearchRetryAPIView.as_view(),
        name='advanced-search-retry',
    ),
    path('document/content-search', DocumentContentSearchAPIView.as_view(), name='document-content-search'),
    path('document/<int:pk>/index-status', DocumentIndexStatusAPIView.as_view(), name='document-index-status'),
    path('document/<int:pk>/retry-indexing', DocumentIndexRetryAPIView.as_view(), name='document-retry-indexing'),
    path('document-index/summary', DocumentIndexSummaryAPIView.as_view(), name='document-index-summary'),
    path('document-index/backfill', DocumentIndexBackfillAPIView.as_view(), name='document-index-backfill'),
    path(
        'solution-demands/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        {'target_type': 'solution_demand'},
        name='solution-demand-comments',
    ),
    path(
        'solution-demands/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        {'target_type': 'solution_demand'},
        name='solution-demand-comment-attachments',
    ),
    path(
        'solution-demands/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        {'target_type': 'solution_demand'},
        name='solution-demand-comment-mention-candidates',
    ),
    path(
        'solution-demands/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        {'target_type': 'solution_demand'},
        name='solution-demand-comment-detail',
    ),
    path(
        'solution-demands/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        {'target_type': 'solution_demand'},
        name='solution-demand-comment-toggle-reaction',
    ),
    path(
        'requirements/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        {'target_type': 'requirement'},
        name='requirement-comments',
    ),
    path(
        'requirements/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        {'target_type': 'requirement'},
        name='requirement-comment-attachments',
    ),
    path(
        'requirements/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        {'target_type': 'requirement'},
        name='requirement-comment-mention-candidates',
    ),
    path(
        'requirements/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        {'target_type': 'requirement'},
        name='requirement-comment-detail',
    ),
    path(
        'requirements/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        {'target_type': 'requirement'},
        name='requirement-comment-toggle-reaction',
    ),
    path(
        'transports/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        {'target_type': 'transport'},
        name='transport-comments',
    ),
    path(
        'transports/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        {'target_type': 'transport'},
        name='transport-comment-attachments',
    ),
    path(
        'transports/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        {'target_type': 'transport'},
        name='transport-comment-mention-candidates',
    ),
    path(
        'transports/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        {'target_type': 'transport'},
        name='transport-comment-detail',
    ),
    path(
        'transports/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        {'target_type': 'transport'},
        name='transport-comment-toggle-reaction',
    ),
    path(
        'available-product-parts/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        {'target_type': 'available_product_part'},
        name='available-product-part-comments',
    ),
    path(
        'available-product-parts/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        {'target_type': 'available_product_part'},
        name='available-product-part-comment-attachments',
    ),
    path(
        'available-product-parts/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        {'target_type': 'available_product_part'},
        name='available-product-part-comment-mention-candidates',
    ),
    path(
        'available-product-parts/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        {'target_type': 'available_product_part'},
        name='available-product-part-comment-detail',
    ),
    path(
        'available-product-parts/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        {'target_type': 'available_product_part'},
        name='available-product-part-comment-toggle-reaction',
    ),
    path(
        'machines/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        {'target_type': 'machine'},
        name='machine-comments',
    ),
    path(
        'machines/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        {'target_type': 'machine'},
        name='machine-comment-attachments',
    ),
    path(
        'machines/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        {'target_type': 'machine'},
        name='machine-comment-mention-candidates',
    ),
    path(
        'machines/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        {'target_type': 'machine'},
        name='machine-comment-detail',
    ),
    path(
        'machines/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        {'target_type': 'machine'},
        name='machine-comment-toggle-reaction',
    ),
    path(
        'equipments/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        {'target_type': 'equipment'},
        name='equipment-comments',
    ),
    path(
        'equipments/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        {'target_type': 'equipment'},
        name='equipment-comment-attachments',
    ),
    path(
        'equipments/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        {'target_type': 'equipment'},
        name='equipment-comment-mention-candidates',
    ),
    path(
        'equipments/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        {'target_type': 'equipment'},
        name='equipment-comment-detail',
    ),
    path(
        'equipments/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        {'target_type': 'equipment'},
        name='equipment-comment-toggle-reaction',
    ),
    path(
        'bom-parts/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        {'target_type': 'bom_part'},
        name='bom-part-comments',
    ),
    path(
        'bom-parts/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        {'target_type': 'bom_part'},
        name='bom-part-comment-attachments',
    ),
    path(
        'bom-parts/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        {'target_type': 'bom_part'},
        name='bom-part-comment-mention-candidates',
    ),
    path(
        'bom-parts/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        {'target_type': 'bom_part'},
        name='bom-part-comment-detail',
    ),
    path(
        'bom-parts/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        {'target_type': 'bom_part'},
        name='bom-part-comment-toggle-reaction',
    ),
    path(
        'company-solution-space-items/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({'get': 'list', 'post': 'create'}),
        {'target_type': 'company_solution_space'},
        name='company-solution-space-comments',
    ),
    path(
        'company-solution-space-items/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({'post': 'upload_attachments'}),
        {'target_type': 'company_solution_space'},
        name='company-solution-space-comment-attachments',
    ),
    path(
        'company-solution-space-items/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({'get': 'mention_candidates'}),
        {'target_type': 'company_solution_space'},
        name='company-solution-space-comment-mention-candidates',
    ),
    path(
        'company-solution-space-items/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({'patch': 'partial_update', 'delete': 'destroy'}),
        {'target_type': 'company_solution_space'},
        name='company-solution-space-comment-detail',
    ),
    path(
        'company-solution-space-items/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({'post': 'toggle_reaction'}),
        {'target_type': 'company_solution_space'},
        name='company-solution-space-comment-toggle-reaction',
    ),
    path(
        'project-solution-space-items/<int:item_id>/comments/',
        MatrixCommentViewSet.as_view({'get': 'list', 'post': 'create'}),
        {'target_type': 'project_solution_space'},
        name='project-solution-space-comments',
    ),
    path(
        'project-solution-space-items/<int:item_id>/comments/attachments/',
        MatrixCommentViewSet.as_view({'post': 'upload_attachments'}),
        {'target_type': 'project_solution_space'},
        name='project-solution-space-comment-attachments',
    ),
    path(
        'project-solution-space-items/<int:item_id>/comments/mention-candidates/',
        MatrixCommentViewSet.as_view({'get': 'mention_candidates'}),
        {'target_type': 'project_solution_space'},
        name='project-solution-space-comment-mention-candidates',
    ),
    path(
        'project-solution-space-items/<int:item_id>/comments/<int:comment_id>/',
        MatrixCommentViewSet.as_view({'patch': 'partial_update', 'delete': 'destroy'}),
        {'target_type': 'project_solution_space'},
        name='project-solution-space-comment-detail',
    ),
    path(
        'project-solution-space-items/<int:item_id>/comments/<int:comment_id>/reactions/toggle/',
        MatrixCommentViewSet.as_view({'post': 'toggle_reaction'}),
        {'target_type': 'project_solution_space'},
        name='project-solution-space-comment-toggle-reaction',
    ),
    path(
        'action_items/<int:action_item_id>/comments/',
        ActionItemCommentViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        name='action-item-comments',
    ),
    path(
        'action_items/<int:action_item_id>/comments/attachments/',
        ActionItemCommentViewSet.as_view({
            'post': 'upload_attachments',
        }),
        name='action-item-comment-attachments',
    ),
    path(
        'action_items/<int:action_item_id>/comments/mention-candidates/',
        ActionItemCommentViewSet.as_view({
            'get': 'mention_candidates',
        }),
        name='action-item-comment-mention-candidates',
    ),
    path(
        'action_items/<int:action_item_id>/comments/<int:comment_id>/',
        ActionItemCommentViewSet.as_view({
            'get': 'retrieve',
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        name='action-item-comment-detail',
    ),
    path(
        'action_items/<int:action_item_id>/comments/<int:comment_id>/reactions/toggle/',
        ActionItemCommentViewSet.as_view({
            'post': 'toggle_reaction',
        }),
        name='action-item-comment-toggle-reaction',
    ),
    path('global-configurations', GlobalConfigListView.as_view(), name='global_configurations'),

    # path('contacts', ContactViewset.as_view({
    #     'get': 'list',    
    #     'post': 'create'
    # }), name='contacts'),

    # path('contacts/<int:pk>', ContactViewset.as_view({
    #     'get': 'retrieve',         
    #     'put': 'update',           
    #     'patch': 'partial_update', 
    #     'delete': 'destroy'       
    # }), name='contacts-detail'),

    # path('contacts/import-csv', ContactViewset.as_view({
    #     'post': 'import_csv'
    # }), name='contacts-import-csv'),

    # path('contacts/export-csv', ContactViewset.as_view({
    #     'get': 'export_csv'
    # }), name='contacts-export-csv'),

    path('document', DocumentViewSet.as_view({
        'get': 'list',    
        'post': 'create'  
    }), name='documents'),

    path('document/bulk-update', DocumentViewSet.as_view({
        'patch': 'bulk_update',
    }), name='document-bulk-update'),
    path('document/issuer-companies', DocumentViewSet.as_view({
        'get': 'issuer_companies',
    }), name='document-issuer-companies'),
    path('document-register', DocumentRegisterViewSet.as_view({'get': 'list'}), name='document-register'),
    path('document-register/filter-options', DocumentRegisterViewSet.as_view({'get': 'filter_options'}), name='document-register-filter-options'),
    path('document-register/<int:document_id>', DocumentRegisterViewSet.as_view({
        'get': 'retrieve',
        'patch': 'partial_update',
    }), name='document-register-detail'),
    path('document/<int:document_id>/revisions', DocumentRevisionViewSet.as_view({'get': 'list', 'post': 'create'}), name='document-revisions'),
    path('document/<int:document_id>/revisions/<int:revision_id>/download', DocumentRevisionViewSet.as_view({'get': 'download'}), name='document-revision-download'),
    path('document/<int:document_id>/attachments', DocumentAttachmentViewSet.as_view({'get': 'list', 'post': 'create'}), name='document-attachments'),
    path('document/<int:document_id>/attachments/<int:attachment_id>', DocumentAttachmentViewSet.as_view({'delete': 'destroy'}), name='document-attachment-detail'),
    path('document/<int:document_id>/attachments/<int:attachment_id>/download', DocumentAttachmentViewSet.as_view({'get': 'download'}), name='document-attachment-download'),

    # LOT 3B: Correspondence -- a formal external communication, not a Document/Livrable.
    path('correspondence', CorrespondenceViewSet.as_view({'get': 'list', 'post': 'create'}), name='correspondences'),
    path('correspondence/<int:pk>', CorrespondenceViewSet.as_view({
        'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy',
    }), name='correspondence-detail'),
    path('correspondence/<int:correspondence_id>/attachments', CorrespondenceAttachmentViewSet.as_view({'get': 'list', 'post': 'create'}), name='correspondence-attachments'),
    path('correspondence/<int:correspondence_id>/attachments/<int:attachment_id>', CorrespondenceAttachmentViewSet.as_view({'delete': 'destroy'}), name='correspondence-attachment-detail'),
    path('correspondence/<int:correspondence_id>/attachments/<int:attachment_id>/download', CorrespondenceAttachmentViewSet.as_view({'get': 'download'}), name='correspondence-attachment-download'),
    # LOT 3D-3D1: internal validation workflow of an OUT Correspondence (NOT DocumentWorkflow --
    # see models/correspondence_workflow.py for why).
    path('correspondence/<int:correspondence_id>/workflow', CorrespondenceValidationWorkflowViewSet.as_view({'get': 'retrieve', 'post': 'start'}), name='correspondence-workflow'),
    path('correspondence/<int:correspondence_id>/workflow/actions', CorrespondenceValidationWorkflowViewSet.as_view({'post': 'act'}), name='correspondence-workflow-action'),
    path('correspondence/<int:correspondence_id>/workflow/actors', CorrespondenceValidationWorkflowViewSet.as_view({'get': 'actor_options'}), name='correspondence-workflow-actors'),
    # LOT 3D-3D2: the SEND step for an approved OUT Correspondence (see
    # services/correspondence_send.py).
    path('correspondence/<int:correspondence_id>/send', CorrespondenceSendViewSet.as_view({'get': 'retrieve', 'post': 'send'}), name='correspondence-send'),

    # LOT 3C: Transmittal -- a formal transmission record/bordereau, not a Correspondence.
    path('transmittal', TransmittalViewSet.as_view({'get': 'list', 'post': 'create'}), name='transmittals'),
    path('transmittal/<int:pk>', TransmittalViewSet.as_view({
        'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy',
    }), name='transmittal-detail'),
    path('transmittal/<int:transmittal_id>/items', TransmittalItemViewSet.as_view({'get': 'list', 'post': 'create'}), name='transmittal-items'),
    path('transmittal/<int:transmittal_id>/items/<int:item_id>', TransmittalItemViewSet.as_view({'delete': 'destroy'}), name='transmittal-item-detail'),
    path('transmittal/<int:transmittal_id>/issue', TransmittalIssueView.as_view({'post': 'create'}), name='transmittal-issue'),
    path('transmittal/<int:transmittal_id>/events', TransmittalEventViewSet.as_view({'get': 'list'}), name='transmittal-events'),

    # LOT 3D-2: unified read-only Correspondances register (Correspondence + Transmittal
    # projection). No new model/table -- see services.correspondances_register.
    path('correspondances', CorrespondancesRegisterView.as_view(), name='correspondances-register'),

    path('document/<int:document_id>/workflow', DocumentWorkflowViewSet.as_view({'post': 'start_current'}), name='document-workflow-start'),
    path('document/<int:document_id>/revisions/<int:revision_id>/workflow', DocumentWorkflowViewSet.as_view({'get': 'retrieve', 'post': 'start'}), name='document-revision-workflow'),
    path('document/<int:document_id>/revisions/<int:revision_id>/workflow/actions', DocumentWorkflowViewSet.as_view({'post': 'act'}), name='document-revision-workflow-action'),
    # L3B: Supplier Surveillance foundation -- on-demand creation only, no update/delete/close.
    path('document/<int:document_id>/revisions/<int:revision_id>/supplier-surveillance', SupplierSurveillanceViewSet.as_view({'get': 'retrieve', 'post': 'create'}), name='document-revision-supplier-surveillance'),
    # L3C: active Surveillance Templates available for this exact revision's Project/Company.
    path('document/<int:document_id>/revisions/<int:revision_id>/supplier-surveillance-templates', SupplierSurveillanceViewSet.as_view({'get': 'templates'}), name='document-revision-supplier-surveillance-templates'),
    # L3C: Surveillance Template library editor (Company-scoped, Project-configuration-permission-gated).
    path('supplier-surveillance-templates', SupplierSurveillanceTemplateViewSet.as_view({'get': 'list', 'post': 'create'}), name='supplier-surveillance-templates'),
    path('supplier-surveillance-templates/<int:pk>', SupplierSurveillanceTemplateViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update'}), name='supplier-surveillance-template-detail'),
    path('document-control/<int:control_id>', DocumentRegisterViewSet.as_view({
        'get': 'retrieve',
        'patch': 'partial_update',
    }), name='document-control-detail'),

    path('document/<int:pk>', DocumentViewSet.as_view({
        'get': 'retrieve',         
        'put': 'update',           
        'patch': 'partial_update', 
        'delete': 'destroy'        
    }), name='document-detail'),
    path('folder', FolderViewSet.as_view({
        'get': 'list',    
        'post': 'create'
    }), name='folder'),

    path('folder/<int:pk>', FolderViewSet.as_view({
        'get': 'retrieve',         
        'put': 'update',           
        'patch': 'partial_update',
        'delete': 'destroy'        
    }), name='folder-detail'),

    path('folder/search', FolderViewSet.as_view({
        'get': 'search_folders'
    })),
    # path('project_time', ProjectTimeViewSet.as_view({
    #     'get': 'list',    
    #     'post': 'create'
    # }), name='project_time'),

    # path('project_time/<int:pk>', ProjectTimeViewSet.as_view({
    #     'get': 'retrieve',         
    #     'put': 'update',           
    #     'patch': 'partial_update',
    #     'delete': 'destroy'        
    # }), name='project_time-detail'),
    
    # path('project_billing', BillingViewSet.as_view({
    #     'get': 'list',    
    #     'post': 'create'
    # }), name='project_billing'),

    # path('project_billing/<int:pk>', BillingViewSet.as_view({
    #     'get': 'retrieve',         
    #     'put': 'update',           
    #     'patch': 'partial_update',
    #     'delete': 'destroy'        
    # }), name='project_billing-detail'),
    path('azure/login', AzureLoginAPIView.as_view(), name='azure-login'),
    path('azure/callback', AzureCallbackAPIView.as_view(), name='azure-callback'),
    path('azure/refresh-token', AzureTokenRefreshAPIView.as_view(), name='azure-refresh-token'),
    path('azure/list-sites', FetchSharePointSitesAPIView.as_view(), name='azure-list-sites'),
    path('azure/update-sites-data', FetchSharePointFilesAPIView.as_view(), name='azure-update-sites-data'),
    path('update-project-on-connection-revoke', UpdateProjectOnConnectionRevoke.as_view(), name='update-project-on-connection-revoke'),
    path('update-freelancer-project-on-connection-revoke', UpdateFreelancerProjectOnConnectionRevoke.as_view(), name='update-freelancer-project-on-connection-revoke'),
    path('remove-related-objects-on-user-delete', RemoveRelatedObjectsOnUserDelete.as_view(), name='remove-related-objects-on-user-delete'),
    path('create-related-objects-on-freelancer-create', CreateRelatedObjectsOnFreelancerCreate.as_view(), name='create-related-objects-on-freelancer-create'),
    path('create-related-objects-on-company-create', CreateRelatedObjectsOnCompanyCreate.as_view(), name='create-related-objects-on-company-create'),
    path('create-related-objects-on-user-create', CreateRelatedObjectsOnUserCreate.as_view(), name='create-related-objects-on-user-create'),
    path('update-team-members-on-role-change', UpdateTeamMembersOnRoleChange.as_view(), name='update-team-members-on-role-change'),
]
