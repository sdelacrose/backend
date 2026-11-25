"""LOT L04 -- Sante et securite au travail (SST). A confidential dossier
family, deliberately its OWN model family rather than an extension of
StaffDocument/OnboardingInstance: the data (maladie/maladie longue duree/
maternite/accident du travail/incident sans dommage) needs a visibility
boundary strictly narrower than every existing HR permission (view_documents,
view_compensation, onboarding/offboarding, Company Admin) -- see
permissions.can_view_staff_sst_dossier/can_manage_sst_case, which never fall
back to any of those.

The administrative absence PERIOD itself is never duplicated here: a
SSTCasePeriod POINTS to the one real `LeaveRequest` row (the SAME source of
truth every other absence/calendar/availability/RH13 mechanism already
reads), it never stores its own start/end dates. See services.sst for the
one write path that creates/corrects/cancels these LeaveRequest rows
directly (never through LeaveRequestSerializer -- see that service module's
own docstring for why)."""
from django.db import models
from models.models import CompanyProfile, Staff, User

from human_resource_management.models.leave import LeaveRequest


class SSTCase(models.Model):
    CATEGORY_SICK_LEAVE = "sick_leave"
    CATEGORY_LONG_TERM_SICK_LEAVE = "long_term_sick_leave"
    CATEGORY_MATERNITY = "maternity"
    CATEGORY_WORK_ACCIDENT = "work_accident"
    CATEGORY_INCIDENT_NO_HARM = "incident_no_harm"
    CATEGORY_CHOICES = (
        (CATEGORY_SICK_LEAVE, "Sick leave"),
        (CATEGORY_LONG_TERM_SICK_LEAVE, "Long-term sick leave"),
        (CATEGORY_MATERNITY, "Maternity"),
        (CATEGORY_WORK_ACCIDENT, "Work accident"),
        (CATEGORY_INCIDENT_NO_HARM, "Incident without injury"),
    )

    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = (
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    )

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="sst_cases")
    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="sst_cases")
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    # The employee concerned OR an SST-habilitated RH -- both are an explicit
    # ticket requirement ("declaration par le salarie OU par la RH"), never
    # inferred from who happens to be logged in.
    declared_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    declared_at = models.DateTimeField(auto_now_add=True)
    # "Suites donnees, notamment une adaptation de poste" -- a plain
    # administrative note, never a diagnosis/medical detail field. Updated by
    # services.sst.record_decision, each change also logged as an SSTCaseEvent.
    workplace_adjustment_note = models.TextField(blank=True)
    # SST-8 (cadrage arbitrage, validated): a purely administrative tracking
    # reference (e.g. a CPAM declaration number RH already has), optional,
    # free text -- NEVER a real CPAM telltransmission and never presented as
    # one. The actual CPAM proces-verbal is an ordinary SST StaffDocument
    # (is_sst=True), downloadable by RH and the employee like any other.
    external_reference = models.CharField(max_length=255, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-declared_at"]

    def __str__(self):
        return f"SSTCase({self.staff_id}, {self.category}, {self.status})"


class SSTCasePeriod(models.Model):
    """One administrative absence period of a case -- the very first
    (`is_initial=True`) or a later prolongation. Each is its OWN
    `LeaveRequest` row, NEVER a rewrite of an earlier one's dates (same
    append-only convention as every other lot in this codebase): a
    correction changes an EXISTING period's own LeaveRequest dates in place
    (see services.sst.correct_period); a prolongation always creates a NEW,
    contiguous period/LeaveRequest."""

    case = models.ForeignKey(SSTCase, on_delete=models.CASCADE, related_name="periods")
    leave_request = models.OneToOneField(LeaveRequest, on_delete=models.PROTECT, related_name="sst_period")
    is_initial = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancellation_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["case_id", "created_at"]

    def __str__(self):
        return f"SSTCasePeriod(case={self.case_id}, leave_request={self.leave_request_id})"


class SSTCaseEvent(models.Model):
    """Append-only audit trail -- same convention as OnboardingInstanceEvent/
    StaffDocumentEvent. Never deleted, never rewritten."""

    EVENT_DECLARED = "declared"
    EVENT_PERIOD_RECORDED = "period_recorded"
    EVENT_PERIOD_EXTENDED = "period_extended"
    EVENT_PERIOD_CORRECTED = "period_corrected"
    EVENT_PERIOD_CANCELLED = "period_cancelled"
    EVENT_DECISION_RECORDED = "decision_recorded"
    EVENT_DOCUMENT_ADDED = "document_added"
    EVENT_PAYROLL_ATTESTATION_DEPOSITED = "payroll_attestation_deposited"
    EVENT_CLOSED = "closed"
    EVENT_CHOICES = (
        (EVENT_DECLARED, "Declared"),
        (EVENT_PERIOD_RECORDED, "Period recorded"),
        (EVENT_PERIOD_EXTENDED, "Period extended"),
        (EVENT_PERIOD_CORRECTED, "Period corrected"),
        (EVENT_PERIOD_CANCELLED, "Period cancelled"),
        (EVENT_DECISION_RECORDED, "Decision recorded"),
        (EVENT_DOCUMENT_ADDED, "Document added"),
        (EVENT_PAYROLL_ATTESTATION_DEPOSITED, "Payroll attestation deposited"),
        (EVENT_CLOSED, "Closed"),
    )

    case = models.ForeignKey(SSTCase, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=32, choices=EVENT_CHOICES)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["case_id", "created_at"]

    def __str__(self):
        return f"SSTCaseEvent({self.case_id}, {self.event_type})"
