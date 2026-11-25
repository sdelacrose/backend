from rest_framework.routers import DefaultRouter
from human_resource_management.views import (
    LeaveEntitlementActivationViewSet,
    LeaveEntitlementViewSet,
    LeaveManagerOpinionViewSet,
    LeaveRequestAttachmentViewSet,
    LeaveRequestViewSet,
    LeaveSubTypeViewSet,
    LeaveTypeViewSet,
    StaffViewSet,
    StaffObservationViewSet,
    StaffConfigurationViewSet,
    StaffQuotiteHistoryViewSet,
    StaffCompensationHistoryViewSet,
    CompanyChargeRateHistoryViewSet,
    CompanyCostItemTypeViewSet,
    StaffAncillaryCostHistoryViewSet,
    LeaveTypeAbsenceImpactViewSet,
    LeaveAccrualAbsenceSignalViewSet,
    LeaveBalanceReminderRuleViewSet,
    LeaveReminderDeliveryViewSet,
    StaffWorkloadViewSet,
    StaffWorkloadAlertConfigViewSet,
    StaffWorkloadAlertEpisodeViewSet,
    StaffDocumentCategoryViewSet,
    StaffDocumentViewSet,
    StaffDocumentFileViewSet,
    StaffDocumentFileStatusViewSet,
    StaffDocumentGrantViewSet,
    OnboardingTemplateViewSet,
    OnboardingInstanceViewSet,
    OnboardingStepInstanceViewSet,
    OffboardingAutoTriggerConfigViewSet,
    OffboardingTemplateMappingViewSet,
    SSTCaseViewSet,
    SSTCasePeriodViewSet,
    SSTPayrollViewSet,
)
from django.urls import path, include

router = DefaultRouter()

router.register('leave-requests', LeaveRequestViewSet, basename='leave-request')
router.register('leave-types', LeaveTypeViewSet, basename='leave-type')
router.register('leave-sub-types', LeaveSubTypeViewSet, basename='leave-sub-type')
router.register('leave-entitlements', LeaveEntitlementViewSet, basename='leave-entitlement')
router.register(
    'leave-entitlement-activation', LeaveEntitlementActivationViewSet, basename='leave-entitlement-activation'
)
router.register('staff', StaffViewSet, basename='staff')
router.register('staff-observation', StaffObservationViewSet, basename='staff_observation')
router.register('staff-configuration', StaffConfigurationViewSet, basename='staff_configuration')
router.register('staff-quotite-history', StaffQuotiteHistoryViewSet, basename='staff-quotite-history')
router.register('staff-compensation-history', StaffCompensationHistoryViewSet, basename='staff-compensation-history')
router.register('company-charge-rate-history', CompanyChargeRateHistoryViewSet, basename='company-charge-rate-history')
router.register('company-cost-item-types', CompanyCostItemTypeViewSet, basename='company-cost-item-type')
router.register('staff-ancillary-cost-history', StaffAncillaryCostHistoryViewSet, basename='staff-ancillary-cost-history')
router.register('leave-absence-impact', LeaveTypeAbsenceImpactViewSet, basename='leave-absence-impact')
router.register('leave-accrual-absence-signals', LeaveAccrualAbsenceSignalViewSet, basename='leave-accrual-absence-signal')
router.register('leave-balance-reminder-rules', LeaveBalanceReminderRuleViewSet, basename='leave-balance-reminder-rule')
router.register('leave-reminder-deliveries', LeaveReminderDeliveryViewSet, basename='leave-reminder-delivery')
router.register('staff-workload', StaffWorkloadViewSet, basename='staff-workload')
router.register('staff-workload-alert-configs', StaffWorkloadAlertConfigViewSet, basename='staff-workload-alert-config')
router.register('staff-workload-alert-episodes', StaffWorkloadAlertEpisodeViewSet, basename='staff-workload-alert-episode')
urlpatterns = [
    path('', include(router.urls)),
    path(
        'leave-requests/<int:leave_request_id>/attachments',
        LeaveRequestAttachmentViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='leave-request-attachments',
    ),
    path(
        'leave-requests/<int:leave_request_id>/attachments/<int:attachment_id>',
        LeaveRequestAttachmentViewSet.as_view({'delete': 'destroy'}),
        name='leave-request-attachment-detail',
    ),
    path(
        'leave-requests/<int:leave_request_id>/attachments/<int:attachment_id>/download',
        LeaveRequestAttachmentViewSet.as_view({'get': 'download'}),
        name='leave-request-attachment-download',
    ),
    path(
        'leave-requests/<int:leave_request_id>/manager-opinion',
        LeaveManagerOpinionViewSet.as_view({'post': 'create'}),
        name='leave-request-manager-opinion',
    ),

    # L03: private/shared HR documents.
    path('staff-document-categories', StaffDocumentCategoryViewSet.as_view({'get': 'list', 'post': 'create'}), name='staff-document-categories'),
    path('staff-document-categories/<int:pk>', StaffDocumentCategoryViewSet.as_view({'patch': 'partial_update'}), name='staff-document-category-detail'),
    path('staff-documents', StaffDocumentViewSet.as_view({'get': 'list', 'post': 'create'}), name='staff-documents'),
    path('staff-documents/<int:pk>', StaffDocumentViewSet.as_view({'patch': 'partial_update'}), name='staff-document-detail'),
    path('staff-documents/<int:staff_document_id>/files', StaffDocumentFileViewSet.as_view({'post': 'create'}), name='staff-document-files'),
    path('staff-documents/<int:staff_document_id>/files/<int:file_id>/download', StaffDocumentFileViewSet.as_view({'get': 'download'}), name='staff-document-file-download'),
    path('staff-documents/<int:staff_document_id>/files/<int:file_id>/status', StaffDocumentFileStatusViewSet.as_view({'post': 'create'}), name='staff-document-file-status'),
    path('staff-documents/<int:staff_document_id>/grants', StaffDocumentGrantViewSet.as_view({'post': 'create'}), name='staff-document-grants'),
    path('staff-documents/<int:staff_document_id>/grants/<int:pk>', StaffDocumentGrantViewSet.as_view({'delete': 'destroy'}), name='staff-document-grant-detail'),

    # L05: onboarding templates and instances.
    path('onboarding-templates', OnboardingTemplateViewSet.as_view({'get': 'list', 'post': 'create'}), name='onboarding-templates'),
    path('onboarding-templates/<int:pk>', OnboardingTemplateViewSet.as_view({'patch': 'partial_update'}), name='onboarding-template-detail'),
    path('staff/<int:staff_id>/onboarding/preview', OnboardingInstanceViewSet.as_view({'post': 'preview'}), name='onboarding-preview'),
    path('staff/<int:staff_id>/onboarding/trigger', OnboardingInstanceViewSet.as_view({'post': 'trigger'}), name='onboarding-trigger'),
    path('onboarding-instances', OnboardingInstanceViewSet.as_view({'get': 'list'}), name='onboarding-instances'),
    path('onboarding-instances/<int:pk>', OnboardingInstanceViewSet.as_view({'get': 'retrieve'}), name='onboarding-instance-detail'),
    path('onboarding-instances/<int:pk>/events', OnboardingInstanceViewSet.as_view({'get': 'events'}), name='onboarding-instance-events'),
    path('onboarding-instances/<int:pk>/cancel', OnboardingInstanceViewSet.as_view({'post': 'cancel'}), name='onboarding-instance-cancel'),
    path('onboarding-instances/<int:pk>/close', OnboardingInstanceViewSet.as_view({'post': 'close'}), name='onboarding-instance-close'),
    path('onboarding-instances/<int:pk>/arrival-date/preview', OnboardingInstanceViewSet.as_view({'post': 'arrival_date_preview'}), name='onboarding-instance-arrival-date-preview'),
    path('onboarding-instances/<int:pk>/arrival-date/apply', OnboardingInstanceViewSet.as_view({'post': 'arrival_date_apply'}), name='onboarding-instance-arrival-date-apply'),
    path('onboarding-steps/<int:pk>/complete', OnboardingStepInstanceViewSet.as_view({'post': 'complete'}), name='onboarding-step-complete'),
    path('onboarding-steps/<int:pk>/reassign', OnboardingStepInstanceViewSet.as_view({'post': 'reassign'}), name='onboarding-step-reassign'),
    path('onboarding-steps/<int:pk>/interview', OnboardingStepInstanceViewSet.as_view({'post': 'interview'}), name='onboarding-step-interview'),

    # L05: offboarding automatic-attribution configuration (opt-in, per company).
    path('offboarding-auto-trigger-config', OffboardingAutoTriggerConfigViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update'}), name='offboarding-auto-trigger-config'),
    path('offboarding-auto-trigger-config/preview', OffboardingAutoTriggerConfigViewSet.as_view({'get': 'preview'}), name='offboarding-auto-trigger-preview'),
    path('offboarding-template-mappings', OffboardingTemplateMappingViewSet.as_view({'get': 'list', 'post': 'create'}), name='offboarding-template-mappings'),
    path('offboarding-template-mappings/<int:pk>', OffboardingTemplateMappingViewSet.as_view({'delete': 'destroy'}), name='offboarding-template-mapping-detail'),

    # L04: SST (sante et securite au travail).
    path('sst-cases', SSTCaseViewSet.as_view({'get': 'list', 'post': 'create'}), name='sst-cases'),
    path('sst-cases/<int:pk>', SSTCaseViewSet.as_view({'get': 'retrieve'}), name='sst-case-detail'),
    path('sst-cases/<int:pk>/periods', SSTCaseViewSet.as_view({'post': 'periods'}), name='sst-case-periods'),
    path('sst-cases/<int:pk>/decision', SSTCaseViewSet.as_view({'post': 'decision'}), name='sst-case-decision'),
    path('sst-cases/<int:pk>/close', SSTCaseViewSet.as_view({'post': 'close'}), name='sst-case-close'),
    path('sst-cases/<int:pk>/documents', SSTCaseViewSet.as_view({'post': 'documents'}), name='sst-case-documents'),
    path('sst-case-periods/<int:pk>/correct', SSTCasePeriodViewSet.as_view({'post': 'correct'}), name='sst-case-period-correct'),
    path('sst-case-periods/<int:pk>/cancel', SSTCasePeriodViewSet.as_view({'post': 'cancel'}), name='sst-case-period-cancel'),
    path('sst-payroll-projection', SSTPayrollViewSet.as_view({'get': 'list'}), name='sst-payroll-projection'),
    path('sst-cases/<int:pk>/payroll-attestation', SSTPayrollViewSet.as_view({'post': 'deposit_attestation'}), name='sst-case-payroll-attestation'),
]
