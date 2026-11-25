"""L05 -- configurable onboarding ("parcours d'accueil") templates and their
triggered instances for an EXISTING Staff record (L01-B1 already guarantees
the Staff exists; this app never creates one). Two versioning precedents
already built elsewhere in this codebase are combined here:

- The immutable JSON snapshot pattern of
  project_management.models.supplier_surveillance_template (editing a
  template NEVER alters an already-triggered instance -- the instance only
  ever reads its OWN frozen `template_snapshot`, never the live template
  again).
- The "one ACTIVE instance at a time, not one forever" partial
  UniqueConstraint pattern of project_management.models.document_workflow
  (DocumentWorkflow) -- a strict OneToOneField would wrongly forbid
  re-triggering onboarding for the same Staff after a prior run was
  cancelled or completed.

Deliberately NOT built on `project_management.ActionItem` -- a new, narrow,
RH-dedicated model family instead, consistent with this project's own
convention (StaffQuotiteHistory/StaffCompensationHistory/CompanyChargeRate
History/StaffAncillaryCostHistory are all narrow, dedicated models rather
than extensions of a shared, heavily-depended-on one).

L05 (Offboarding) extends this SAME engine with `process_kind` rather than
building a parallel one -- the spec's own section 6.9 asks for "un moteur
commun Onboarding/Offboarding", and the shape (versioned template, immutable
snapshot, one-active-at-a-time, idempotent/concurrent trigger, reassignment,
cancellation, due-date recompute) is structurally identical for a departure
as for an arrival. `arrival_date` is renamed `anchor_date` (migration
0032): it anchors due-date computation either way -- the employee's first
day for onboarding, their departure date for offboarding -- `due_offset_days`
already being signed (see OnboardingTemplateStep) already supported J-5/J+10
BEFORE this lot, nothing to change there. Model/URL names keep the
`Onboarding*`/`onboarding-*` spelling deliberately (a rename would touch
already-shipped, already-tested L03 code and frontend routes for a purely
cosmetic gain) -- read as "the accueil/depart engine", not literally
"onboarding-only", wherever `process_kind` appears."""
from django.db import models
from django.db.models import Q
from models.models import CompanyProfile, Staff, User


class ProcessKind(models.TextChoices):
    ONBOARDING = "onboarding", "Onboarding"
    OFFBOARDING = "offboarding", "Offboarding"


class StepKind(models.TextChoices):
    CHECKLIST = "checklist", "Checklist"
    INTERVIEW = "interview", "Interview"
    DOCUMENT = "document", "Document"


class OnboardingTemplate(models.Model):
    """A company's named onboarding/offboarding template -- several per
    company, selected explicitly at trigger time (never a single implicit
    default). `version` is informational/display only; the real "never
    silently alter a launched instance" guarantee comes from the immutable
    OnboardingInstance.template_snapshot, not from this integer.
    `process_kind` is fixed at creation (never changed afterwards -- a
    template is either an onboarding template or an offboarding template,
    never both; switching would silently reinterpret every instance already
    snapshotted from it, so no endpoint allows mutating it)."""

    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="onboarding_templates")
    process_kind = models.CharField(max_length=16, choices=ProcessKind.choices, default=ProcessKind.ONBOARDING)
    name = models.CharField(max_length=255)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company_profile_id", "name"]

    def __str__(self):
        return f"{self.name} v{self.version} ({self.company_profile_id}, {self.process_kind})"


class OnboardingTemplateStep(models.Model):
    """One ordered step of a template. Fields here are copied verbatim into
    `OnboardingInstance.template_snapshot` AND onto each
    OnboardingStepInstance at trigger time -- never re-read from this row
    again afterwards."""

    ROLE_SUPERVISOR = "supervisor"
    ROLE_COMPANY_ADMIN = "company_admin"
    ROLE_NAMED = "named"
    ROLE_CHOICES = (
        (ROLE_SUPERVISOR, "Employee's supervisor (Staff.supervisor)"),
        (ROLE_COMPANY_ADMIN, "Company Admin (canonical)"),
        (ROLE_NAMED, "A specifically named person"),
    )

    template = models.ForeignKey(OnboardingTemplate, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveIntegerField()
    title = models.CharField(max_length=255)
    # L05: "trois natures d'etapes" -- a plain classification for the UI
    # (which action a step offers) and for the interview-scheduling action
    # below; it never changes who may act on the step (that stays
    # responsible_role/responsible_user, identity-based, unchanged) nor
    # bypasses the document-acceptance gate (`step_kind=document` steps still
    # go through the exact same `expected_category`/accepted-status check as
    # before this lot).
    step_kind = models.CharField(max_length=16, choices=StepKind.choices, default=StepKind.CHECKLIST)
    responsible_role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    # Only meaningful (and required) when responsible_role == ROLE_NAMED --
    # a fixed default; RH may still override it per-instance at trigger time
    # (see services.onboarding.preview_trigger/trigger_onboarding).
    default_responsible_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Signed -- a negative offset lets a step be due BEFORE the anchor date
    # (employee's arrival for onboarding, departure for offboarding) --
    # e.g. "prepare workstation" (onboarding, -5) or "revoke VPN access"
    # (offboarding, -2), "exit interview" (offboarding, +10 after last day).
    due_offset_days = models.IntegerField()
    expected_category = models.ForeignKey(
        "human_resource_management.StaffDocumentCategory", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    # Opt-IN, never opt-out -- "ne jamais donner accès automatiquement".
    visible_to_employee = models.BooleanField(default=False)
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ["template_id", "order"]
        constraints = [
            models.UniqueConstraint(fields=["template", "order"], name="unique_onboarding_template_step_order"),
        ]

    def __str__(self):
        return f"{self.template_id}#{self.order}: {self.title}"


class OnboardingInstance(models.Model):
    """One triggered onboarding OR offboarding run for one Staff.
    `template_snapshot` is the ONLY source of truth for what was launched --
    `template` is kept purely for reporting ("which template was this based
    on"), never re-read for structure. `anchor_date` is explicit (never
    inferred from `triggered_at`) so a step can be prepared BEFORE the
    employee's actual first day (onboarding) or departure (offboarding).
    `process_kind` is copied from `template.process_kind` at creation time
    (never passed independently by a caller -- a trigger call's kind is
    entirely determined by which template it names) and never changes
    afterwards.

    `source`/`trigger_key` (L05/Offboarding): `source` records whether THIS
    instance came from an explicit RH click or from the automatic
    attribution job (`services.offboarding_automation`), for audit only --
    it changes no permission or behaviour. `trigger_key` is the mechanism
    behind "apres annulation, aucune recreation automatique a chaque passage
    du job" WITHOUT a separate suppression ledger: it is set to the anchor
    date's isoformat at creation, and the automatic job's own eligibility
    query is simply "does an instance of this process_kind already exist for
    this staff with this exact trigger_key (whatever its status -- active,
    cancelled or completed)?" -- since instances are never deleted, a
    cancelled instance's row IS the durable trace of "already handled for
    this departure event", with no extra model needed. Changing the
    underlying contract date (a NEW trigger_key) makes the staff eligible
    again automatically; an explicit manual relance for the SAME date is
    unaffected (it is a distinct user action, not gated by this check at
    all -- only the automatic job consults it)."""

    STATUS_ACTIVE = "active"
    STATUS_CANCELLED = "cancelled"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = (
        (STATUS_ACTIVE, "Active"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_COMPLETED, "Completed"),
    )

    SOURCE_MANUAL = "manual"
    SOURCE_AUTOMATIC = "automatic"
    SOURCE_CHOICES = (
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_AUTOMATIC, "Automatic"),
    )

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="onboarding_instances")
    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="onboarding_instances")
    process_kind = models.CharField(max_length=16, choices=ProcessKind.choices, default=ProcessKind.ONBOARDING)
    template = models.ForeignKey(OnboardingTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="instances")
    template_snapshot = models.JSONField()
    anchor_date = models.DateField()
    # Only ever set for process_kind=offboarding, triggered automatically --
    # see the class docstring. Left null for every manual trigger (onboarding
    # or offboarding): a manual relance is never suppressed by this field.
    trigger_key = models.CharField(max_length=32, null=True, blank=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    triggered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    triggered_at = models.DateTimeField(auto_now_add=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["staff_id", "-triggered_at"]
        constraints = [
            # "Un seul actif a la fois PAR TYPE DE PROCESSUS, jamais un seul
            # pour toujours" -- permet un nouveau declenchement apres
            # annulation/cloture, ET permet un accueil actif et un depart
            # actif de coexister pour le meme collaborateur (compteurs
            # independants, cas limite reel : affectation administrative
            # tardive pendant qu'un depart anticipe doit deja etre prepare).
            models.UniqueConstraint(
                fields=["staff", "process_kind"], condition=Q(status="active"),
                name="unique_active_process_instance_per_staff_and_kind",
            ),
        ]

    def __str__(self):
        return f"{self.staff_id}: {self.process_kind}/{self.status} ({self.triggered_at})"


class OnboardingStepInstance(models.Model):
    """One step of a triggered instance -- fields copied from the template
    snapshot at creation, never re-read from OnboardingTemplateStep again.
    `responsible_user` IS mutable (reassignment) -- access is always
    computed LIVE from its CURRENT value, so reassigning a step revokes the
    previous responsible's access to it immediately, with no separate
    "access revocation" step needed. `due_date` is a plain field, computed
    once at trigger time (anchor_date + due_offset_days) and only ever
    recomputed through the explicit anchor-date-change preview/apply flow
    (services.onboarding.preview_arrival_date_change/apply_arrival_date_
    change) -- never silently on every read. `calendar_event` (L05) is only
    ever populated through `services.offboarding_interview.schedule_interview`
    for a `step_kind=interview` step -- a plain link to a real
    `userapis.CalendarEvent` row (the existing, already-built calendar
    capability, reused as-is), never a second calendar system."""

    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_DONE, "Done"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    instance = models.ForeignKey(OnboardingInstance, on_delete=models.CASCADE, related_name="step_instances")
    order = models.PositiveIntegerField()
    title = models.CharField(max_length=255)
    step_kind = models.CharField(max_length=16, choices=StepKind.choices, default=StepKind.CHECKLIST)
    responsible_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="onboarding_step_responsibilities")
    due_date = models.DateField()
    expected_category = models.ForeignKey(
        "human_resource_management.StaffDocumentCategory", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    calendar_event = models.ForeignKey("userapis.CalendarEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    visible_to_employee = models.BooleanField(default=False)
    is_required = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["instance_id", "order"]

    def __str__(self):
        return f"{self.instance_id}#{self.order}: {self.title} [{self.status}]"


class OnboardingInstanceEvent(models.Model):
    """Append-only audit trail for an OnboardingInstance -- triggering,
    step completion, reassignment, cancellation, arrival-date changes.
    `template_snapshot` on the instance is NEVER altered by any of these --
    this event log is the ONLY place a change is ever recorded."""

    EVENT_TRIGGERED = "triggered"
    EVENT_STEP_COMPLETED = "step_completed"
    EVENT_STEP_REASSIGNED = "step_reassigned"
    EVENT_STEP_CANCELLED = "step_cancelled"
    EVENT_ARRIVAL_DATE_CHANGED = "arrival_date_changed"
    EVENT_CANCELLED = "cancelled"
    EVENT_COMPLETED = "completed"
    EVENT_INTERVIEW_SCHEDULED = "interview_scheduled"
    EVENT_CHOICES = (
        (EVENT_TRIGGERED, "Triggered"),
        (EVENT_STEP_COMPLETED, "Step completed"),
        (EVENT_STEP_REASSIGNED, "Step reassigned"),
        (EVENT_STEP_CANCELLED, "Step cancelled"),
        (EVENT_ARRIVAL_DATE_CHANGED, "Arrival date changed"),
        (EVENT_CANCELLED, "Cancelled"),
        (EVENT_COMPLETED, "Completed"),
        (EVENT_INTERVIEW_SCHEDULED, "Interview scheduled"),
    )

    instance = models.ForeignKey(OnboardingInstance, on_delete=models.CASCADE, related_name="events")
    step_instance = models.ForeignKey(OnboardingStepInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    event_type = models.CharField(max_length=24, choices=EVENT_CHOICES)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["instance_id", "created_at"]

    def __str__(self):
        return f"{self.instance_id}: {self.event_type}"
