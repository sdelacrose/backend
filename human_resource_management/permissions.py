from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from ecpmpapp.permissions import BaseModulePermission
from ecpmpapp.license_queries import get_active_license_user_ids_by_product_names
from ecpmpapp.utils import get_user_active_solution
from models.constants import SHAREMUNDO_SOLUTION_TYPES
from models.models import UserRole
from project_management.utils import get_product_name_from_request

ALL_KNOWN_SOLUTIONS = [name for name, _label in SHAREMUNDO_SOLUTION_TYPES]


def has_cumulative_leave_review_permission(user):
    """LOT RH 4 closure (RH + Company Manager cumul): `BaseModulePermission.
    _get_user_role()` (and `require_any_permission`, used elsewhere in this
    codebase) resolve a SINGLE company-scope solution role per user
    (`.first()`) -- correct for every other permission in this system, but
    it means a user who already holds one role (e.g. Company Manager) can
    never be seen to ALSO hold a second role that grants only
    `leave_request.review`: whichever role `.first()` doesn't pick is
    invisible to those checks. This widens ONLY this one permission key to
    a real union across ALL of the user's company-scope solution roles, so
    the HR entitlement can be granted on top of an existing role (and
    withdrawn later) without displacing it and without changing how
    `_get_user_role`/`require_any_permission` resolve roles for any other
    module -- nothing outside this function and its leave_request call
    sites is touched. A live DB query every time: no cached or hardcoded
    notion of "who is HR", so a past holder of this permission who has
    since lost it is never treated as HR."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    queryset = UserRole.objects.filter(
        user=user,
        role__scope='company',
        role__type='solution',
        role__role_permissions__permission__key='leave_request.review',
    )
    if user.is_freelancer:
        queryset = queryset.filter(role__company__isnull=True)
    elif user.company_id:
        queryset = queryset.filter(role__company=user.company)
    else:
        return False
    return queryset.exists()


def has_leave_manager_opinion_permission(user):
    """LOT RH 5: same union-across-roles pattern as
    `has_cumulative_leave_review_permission`, for the separate, granular
    `leave_request.manager_opinion` permission -- deliberately its OWN key,
    never `leave_request.review`, so granting it can never confer any RH
    right (approval, document visibility) by construction."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    queryset = UserRole.objects.filter(
        user=user,
        role__scope='company',
        role__type='solution',
        role__role_permissions__permission__key='leave_request.manager_opinion',
    )
    if user.is_freelancer:
        queryset = queryset.filter(role__company__isnull=True)
    elif user.company_id:
        queryset = queryset.filter(role__company=user.company)
    else:
        return False
    return queryset.exists()


def has_cumulative_module_permission(user, module, actions):
    """LOT RH10 follow-up: generalizes `has_cumulative_leave_review_permission`'s
    union-across-roles pattern to an arbitrary module/action set. Scoped
    deliberately to congés-adjacent permission classes only (here:
    `StaffManagementPermission`, which gates the RH10 contract-date fields) --
    `BaseModulePermission._get_user_role()`/`ecpmpapp.utils.get_user_role()`
    still resolve a single role (`.first()`) everywhere else in this codebase,
    unchanged; this helper is not a global fix, only a reusable version of the
    same per-module union query for the handful of HR permission classes that
    need it."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    keys = [f'{module}.{action}' for action in actions]
    queryset = UserRole.objects.filter(
        user=user,
        role__scope='company',
        role__type='solution',
        role__role_permissions__permission__key__in=keys,
    )
    if user.is_freelancer:
        queryset = queryset.filter(role__company__isnull=True)
    elif user.company_id:
        queryset = queryset.filter(role__company=user.company)
    else:
        return False
    return queryset.exists()


def resolve_authorized_solution_for_request(request):
    """LOT RH13 follow-up (solution authorization, not just object-level
    isolation): the client-supplied `?solution=` query param is never
    trusted blindly here -- accepted ONLY if the requesting user actually
    holds an ACTIVE LICENSE for it (`get_active_license_user_ids_by_
    product_names`, the SAME license table `get_user_active_solution`
    itself already reads for the activation screens elsewhere in this
    module -- a real, populated, per-user mechanism, not a new model).
    An absent, unknown, or genuinely unlicensed value is never used as
    given -- falls back to this user's own actual active solution instead
    (the exact same resolution `get_user_active_solution` already performs
    for the activation endpoints), never a 403/error response: a stale or
    bookmarked URL from before a license change should degrade to "your own
    real context", not break. Deliberately NOT a new global role/license
    model -- a single, narrow validation step reused by the RH endpoints
    that call it; `get_product_name_from_request`/`_company_configs_for_
    request` themselves are UNCHANGED for every other caller in the app.

    The one case that is NEVER silently served: a user holding NO active
    license for ANY known solution. `get_user_active_solution` itself
    silently defaults to 'Enterprise Solution' when no license matches
    anything at all -- that default is a tiebreaker among licenses the user
    genuinely holds, never a real authorization, so it is not trusted here:
    such a user is refused outright instead of being handed a fabricated
    fallback context."""
    user = getattr(request, "user", None)
    user_id = getattr(user, "id", None)
    candidate = get_product_name_from_request(request)
    if user_id is None:
        return candidate
    if user_id in get_active_license_user_ids_by_product_names([candidate]):
        return candidate
    if user_id not in get_active_license_user_ids_by_product_names(ALL_KNOWN_SOLUTIONS):
        raise PermissionDenied("No active license for any solution.")
    return get_user_active_solution(user_id)


def resolve_authorized_solution_for_write(request):
    """LOT RH13 follow-up (write-path hardening): a RH WRITE (create a leave
    type/entitlement/policy, adjust a counter, activate the entitlement
    mechanism) must never silently redirect into a different solution than
    the one the client explicitly asked for -- unlike the read-path
    resolver above, where falling back to the user's own context is
    harmless (a stale bookmarked read just shows their own real data
    instead), a WRITE performed under a silently-substituted solution would
    create/mutate a row in a context the client never actually asked for.
    So here: if `solution` was supplied explicitly and it is NOT one this
    user holds an active license for, the write is refused outright
    (PermissionDenied) -- never redirected. If `solution` was NOT supplied
    at all, resolves to the user's own actually-licensed solution, exactly
    like the read path (default behavior preserved for the common case).
    Either way, a user holding NO active license for ANY known solution is
    refused outright: `get_user_active_solution`'s own silent 'Enterprise
    Solution' default (returned when no license matches anything at all) is
    deliberately never trusted here as if it were a real authorization --
    only ever used below as a priority tiebreaker AMONG licenses the user
    genuinely holds."""
    user = getattr(request, "user", None)
    user_id = getattr(user, "id", None)
    if user_id is None:
        raise PermissionDenied("Authentication required.")
    query_params = getattr(request, "query_params", getattr(request, "GET", {}))
    raw_solution = query_params.get("solution")
    if raw_solution:
        if user_id in get_active_license_user_ids_by_product_names([raw_solution]):
            return raw_solution
        raise PermissionDenied(
            "You do not hold an active license for the requested solution."
        )
    if user_id not in get_active_license_user_ids_by_product_names(ALL_KNOWN_SOLUTIONS):
        raise PermissionDenied("No active license for any solution.")
    return get_user_active_solution(user_id)


def has_staff_management_view_permission(user):
    """LOT RH13 follow-up (salary confidentiality): the exact same
    cumulative union check `StaffManagementPermission._check_view_permission`
    already uses, exposed as a plain function so a SERIALIZER (not just a
    DRF permission class on a view) can gate a field by it. A dedicated
    salary permission is deliberately NOT introduced here -- this reuses
    `staff_management` itself, the same right that already governs the
    Staff Management screen's own salary field."""
    return has_cumulative_module_permission(user, 'staff_management', ['full_access', 'view'])


def has_leave_reminder_management_permission(user):
    """AI-1685: gates configuring leave-balance reminder rules, the
    collective preview/manual trigger, and the administrative delivery
    history. A DELIBERATELY SEPARATE action from staff_management.update/
    full_access (which govern contractual-field editing) -- holding this
    permission never also grants contractual-field edit rights, and never
    exposes salary/hourly_rate (those fields are never included in any
    reminder payload in the first place, see services.leave_reminders)."""
    return has_cumulative_module_permission(user, 'staff_management', ['manage_leave_reminders'])


class LeaveReminderManagementPermission(BaseModulePermission):
    """AI-1685: same shape as StaffManagementPermission, but checks the
    dedicated `manage_leave_reminders` action instead of update/full_access."""

    def __init__(self, scope='company'):
        super().__init__('staff_management', 'manage_leave_reminders', scope)

    def has_permission(self, request, view):
        return has_leave_reminder_management_permission(request.user)

    def has_object_permission(self, request, view, obj):
        return has_leave_reminder_management_permission(request.user)


def has_workload_view_permission(user):
    """AI-2054: gates the read-only cross-project occupancy view (and being
    an eligible overload-alert recipient, see services.workload_alerts).
    Deliberately separate from staff_management.update/full_access/
    manage_leave_reminders -- holding it never grants any contractual-field
    edit or reminder-configuration right, and the view it gates never
    exposes salary/hourly_rate/absence-justification details in the first
    place (see services.workload)."""
    return has_cumulative_module_permission(user, 'staff_management', ['view_workload'])


class WorkloadViewPermission(BaseModulePermission):
    """AI-2054: same shape as LeaveReminderManagementPermission, for the
    dedicated `view_workload` action."""

    def __init__(self, scope='company'):
        super().__init__('staff_management', 'view_workload', scope)

    def has_permission(self, request, view):
        return has_workload_view_permission(request.user)

    def has_object_permission(self, request, view, obj):
        return has_workload_view_permission(request.user)


def has_workload_alert_management_permission(user):
    """AI-2054: gates configuring which RH users receive the staff-workload
    overload alert for a company/solution, and triggering a manual
    recompute. Does NOT by itself grant staff_management.view_workload --
    consulting the occupancy view still requires that separate permission
    (see D7 in PROPOSITION_AI-2054.md)."""
    return has_cumulative_module_permission(user, 'staff_management', ['manage_workload_alerts'])


class WorkloadAlertManagementPermission(BaseModulePermission):
    """AI-2054: same shape as LeaveReminderManagementPermission, for the
    dedicated `manage_workload_alerts` action."""

    def __init__(self, scope='company'):
        super().__init__('staff_management', 'manage_workload_alerts', scope)

    def has_permission(self, request, view):
        return has_workload_alert_management_permission(request.user)

    def has_object_permission(self, request, view, obj):
        return has_workload_alert_management_permission(request.user)


def is_company_admin_for_staff(user, staff):
    """L00-A: the canonical "Company Admin metier" check -- CompanyProfile.
    admin_user is the one stable, structural relation for this role (one
    designated admin user per company, a real FK, never the Role's
    DISPLAYED name, never is_staff/is_superuser, never a role held in a
    DIFFERENT company). A user is the Company Admin of `staff`'s own
    company iff they are the admin_user of that SAME CompanyProfile.
    Deliberately NOT routed through the generic ecpmpapp.utils.
    is_company_owner/resolve_company_profile dispatcher (which has no
    case for Staff) to avoid widening that shared, cross-app function for
    a single new caller -- same underlying CompanyProfile.admin_user
    relation, applied directly and narrowly here."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    company = getattr(staff.user, "company", None)
    return bool(company and getattr(company, "admin_user_id", None) == user.id)


def has_compensation_view_permission(user):
    """L00-A (DEC-02/06): a DELIBERATELY separate action from staff_
    management.view/full_access -- holding the general Staff Management
    right never implies compensation visibility. Company Admin/self access
    are granted separately (see can_view_staff_compensation), never folded
    into this permission-key check itself."""
    return has_cumulative_module_permission(user, 'staff_management', ['view_compensation'])


def has_compensation_update_permission(user):
    """L00-A (DEC-02/06): separate from view -- holding view_compensation
    never implies the right to WRITE a new value."""
    return has_cumulative_module_permission(user, 'staff_management', ['update_compensation'])


def has_compensation_export_permission(user):
    """L00-A (DEC-49): export is its own action -- holding view_compensation
    (screen/API visibility) never implies the right to include compensation
    in a generated export file."""
    return has_cumulative_module_permission(user, 'staff_management', ['export_compensation'])


def has_staff_create_permission(user):
    """L01-B1: dedicated, additive right -- creating a Staff dossier is NOT
    implied by staff_management.view/update/full_access (those only ever
    govern an ALREADY-EXISTING dossier), same separation already applied to
    the compensation rights above (DEC-02/06). Never assigned by default."""
    return has_cumulative_module_permission(user, 'staff_management', ['create'])


def can_provision_staff(viewer, target_user):
    """L01-B1: who may create a Staff dossier for `target_user` (a User who
    has none yet). Requires the dedicated staff_management.create right,
    the SAME company as the target (L01-A situation 1 only -- never
    cross-company, never a silent reattachment), and never the viewer's
    OWN record (a dossier is never self-created here, same spirit as this
    project's other DEC-04 self-mutation refusals, even though no
    compensation field is involved)."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if not has_staff_create_permission(viewer):
        return False
    if viewer.id == getattr(target_user, "id", None):
        return False
    target_company_id = getattr(target_user, "company_id", None)
    return bool(target_company_id and getattr(viewer, "company_id", None) == target_company_id)


def has_staff_document_view_permission(user):
    """L03: dedicated, additive right for HR private/shared documents --
    deliberately SEPARATE from staff_management.view/update/full_access AND
    from view_compensation/update_compensation (compensation and health/SST
    notes are explicitly never reachable through this permission, per the
    ticket's own instruction -- those stay under their own dedicated rights,
    unaffected by this lot)."""
    return has_cumulative_module_permission(user, 'staff_management', ['view_documents'])


def has_staff_document_update_permission(user):
    """L03: separate from view -- holding view_documents never implies the
    right to upload/transition status/grant access for OTHERS."""
    return has_cumulative_module_permission(user, 'staff_management', ['update_documents'])


def can_view_staff_document(viewer, staff_document):
    """L03: SHARED-scope documents (company_profile, no individual staff) are
    visible to any authenticated member of that SAME company holding
    view_documents, or the Company Admin -- never a bare "same company"
    check alone (never public, never a leak to a licensed-but-unrelated
    company member beyond this explicit right). PRIVATE-scope documents
    additionally allow the employee concerned (always, read-only) and anyone
    holding an active, non-revoked StaffDocumentGrant for this EXACT
    document -- never a grant on the whole category or company.

    LOT L04 (SST): an `is_sst` category is a COMPLETELY SEPARATE, narrower
    gate -- never has_staff_document_view_permission, never the Company
    Admin fallback, never an ordinary StaffDocumentGrant (SST confidentiality
    is closed: no ad-hoc exceptional access). Checked FIRST, before any of
    the general logic below ever runs."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if staff_document.category.is_sst:
        return staff_document.staff_id is not None and can_view_staff_sst_dossier(viewer, staff_document.staff)
    company = staff_document.company_profile
    if getattr(viewer, "company_id", None) != company.id:
        return False
    if staff_document.staff_id and viewer.id == staff_document.staff.user_id:
        return True
    if has_staff_document_view_permission(viewer):
        return True
    if getattr(company, "admin_user_id", None) == viewer.id:
        return True
    if staff_document.staff_id:
        return staff_document.grants.filter(user=viewer, revoked_at__isnull=True).exists()
    return False


def has_sst_manage_permission(user):
    """L04: RH/DRH habilitee -- declare/record/correct/close SST cases for
    staff of her own company. Deliberately separate from staff_management.
    view/update/full_access, view_documents, view_compensation, and every
    onboarding/offboarding permission -- health-and-safety data stays under
    its own dedicated right, never reachable through any general RH module."""
    return has_cumulative_module_permission(user, 'staff_management', ['manage_sst_cases'])


def has_sst_view_permission(user):
    """L04: read-only consultation of an SST dossier/documents -- narrower
    than manage_sst_cases (can consult, never record/correct/close).
    manage_sst_cases implies this (see can_view_staff_sst_dossier)."""
    return has_cumulative_module_permission(user, 'staff_management', ['view_sst_dossier'])


def has_sst_payroll_view_permission(user):
    """L04: payroll's own narrow projection (dates + administrative nature
    only, never the dossier/documents) -- deliberately SEPARATE from
    has_sst_payroll_attestation_permission per the ticket's explicit
    instruction (viewing the projection never implies the right to deposit
    an attestation, and vice versa)."""
    return has_cumulative_module_permission(user, 'staff_management', ['view_sst_payroll_projection'])


def has_sst_payroll_attestation_permission(user):
    """L04: deposit of the salary attestation document only -- never implies
    view_sst_payroll_projection nor any dossier/document access."""
    return has_cumulative_module_permission(user, 'staff_management', ['manage_sst_payroll_attestation'])


def can_manage_sst_case(viewer, staff):
    """L04: who may create/record/correct/close an SST case for `staff`.
    Same-company only, NEVER a Company Admin fallback (explicit instruction,
    a deliberate divergence from can_view_staff_compensation's own repli).

    DEC-04: the employee concerned can NEVER manage their OWN case even if
    they happen to also hold manage_sst_cases for their company (an RH
    person who is also, separately, a patient) -- checked BEFORE the
    permission check, the exact same ordering convention as every other
    DEC-04 refusal in this project (e.g. can_transition_staff_document_
    file_status). "Seule la RH habilitee enregistre" implies a genuine
    second person, never a self-administered record."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if viewer.id == staff.user_id:
        return False
    if not _same_company(viewer, staff):
        return False
    return has_sst_manage_permission(viewer)


def can_view_staff_sst_dossier(viewer, staff):
    """L04: the employee concerned (always, read-only) or an explicitly
    SST-habilitated viewer (view or manage right) of the SAME company.
    NEVER a Company Admin fallback, NEVER the general view_documents/view_
    compensation/onboarding-offboarding permissions, NEVER a manager or
    technical/solution administrator right."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if viewer == staff.user:
        return True
    if not _same_company(viewer, staff):
        return False
    return has_sst_view_permission(viewer) or has_sst_manage_permission(viewer)


def can_update_staff_document(viewer, staff_document):
    """L03: who may manage a document's metadata (rename, grant/revoke
    access, transition its file status) -- RH habilitee or Company Admin
    ONLY. Deliberately excludes the employee themselves (even for their own
    PRIVATE document) and excludes a mere StaffDocumentGrant holder (read-
    only by default, per the ticket's own instruction) -- see
    can_upload_staff_document_file below for the narrower self-upload right.

    LOT L04 (SST): an `is_sst` document uses the SAME separate, narrower
    gate as can_view_staff_document -- never has_staff_document_update_
    permission, never the Company Admin fallback."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if staff_document.category.is_sst:
        return staff_document.staff_id is not None and can_manage_sst_case(viewer, staff_document.staff)
    company = staff_document.company_profile
    if getattr(viewer, "company_id", None) != company.id:
        return False
    if has_staff_document_update_permission(viewer):
        return True
    return getattr(company, "admin_user_id", None) == viewer.id


def can_upload_staff_document_file(viewer, staff_document):
    """L03: "le collaborateur peut deposer les pieces qui lui sont demandees,
    sans les verifier ni les accepter lui-meme" -- the employee concerned may
    add a NEW VERSION to their own PRIVATE document (never a SHARED one,
    never someone else's), in addition to whoever can_update_staff_document
    already covers."""
    if staff_document.staff_id and viewer and getattr(viewer, "is_authenticated", False) and viewer.id == staff_document.staff.user_id:
        return True
    return can_update_staff_document(viewer, staff_document)


def can_transition_staff_document_file_status(viewer, staff_document):
    """L03: verifying/accepting a deposited piece is an HR-only action, and
    -- same DEC-04 spirit as compensation self-mutation -- the employee
    concerned can NEVER verify/accept their OWN document even if they
    somehow also held update_documents (defense in depth, checked BEFORE the
    permission check, exact same ordering convention as every other DEC-04
    refusal in this project)."""
    if staff_document.staff_id and viewer and getattr(viewer, "is_authenticated", False) and viewer.id == staff_document.staff.user_id:
        return False
    return can_update_staff_document(viewer, staff_document)


def has_onboarding_manage_templates_permission(user):
    """L05: a SEPARATE right from triggering/viewing an onboarding instance
    -- editing the catalog of templates never implies the right to launch
    one, and vice versa."""
    return has_cumulative_module_permission(user, 'staff_management', ['manage_onboarding_templates'])


def has_onboarding_trigger_permission(user):
    """L05: who may launch an onboarding instance for a Staff -- separate
    from managing templates and from the global tracking view."""
    return has_cumulative_module_permission(user, 'staff_management', ['onboarding_trigger'])


def has_onboarding_view_all_permission(user):
    """L05: the RH global tracking view (every onboarding instance in the
    company) -- separate from "can I see MY OWN assigned steps", which never
    requires any staff_management permission at all (see
    can_view_onboarding_step below)."""
    return has_cumulative_module_permission(user, 'staff_management', ['onboarding_view_all'])


def has_offboarding_manage_templates_permission(user):
    """L05/Offboarding: arbitrage OFF-2 -- deliberately a SEPARATE key from
    manage_onboarding_templates, never cumulated by default (managing
    departure templates is its own, more sensitive right)."""
    return has_cumulative_module_permission(user, 'staff_management', ['manage_offboarding_templates'])


def has_offboarding_trigger_permission(user):
    """L05/Offboarding: who may launch a departure for a Staff -- separate
    from onboarding_trigger (OFF-2), separate from managing templates."""
    return has_cumulative_module_permission(user, 'staff_management', ['offboarding_trigger'])


def has_offboarding_view_all_permission(user):
    """L05/Offboarding: the RH departures dashboard (every offboarding
    instance in the company) -- separate from onboarding_view_all."""
    return has_cumulative_module_permission(user, 'staff_management', ['offboarding_view_all'])


def _manage_templates_permission_for_kind(process_kind):
    from human_resource_management.models.onboarding import ProcessKind
    return has_offboarding_manage_templates_permission if process_kind == ProcessKind.OFFBOARDING else has_onboarding_manage_templates_permission


def _trigger_permission_for_kind(process_kind):
    from human_resource_management.models.onboarding import ProcessKind
    return has_offboarding_trigger_permission if process_kind == ProcessKind.OFFBOARDING else has_onboarding_trigger_permission


def _view_all_permission_for_kind(process_kind):
    from human_resource_management.models.onboarding import ProcessKind
    return has_offboarding_view_all_permission if process_kind == ProcessKind.OFFBOARDING else has_onboarding_view_all_permission


def can_manage_onboarding_templates(viewer, company_profile, process_kind="onboarding"):
    """`process_kind` selects WHICH dedicated permission gates this company's
    catalog -- manage_onboarding_templates for onboarding templates,
    manage_offboarding_templates for offboarding ones (arbitrage OFF-2:
    never cumulated, never a single shared key)."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if getattr(viewer, "company_id", None) != company_profile.id:
        return False
    if _manage_templates_permission_for_kind(process_kind)(viewer):
        return True
    return getattr(company_profile, "admin_user_id", None) == viewer.id


def can_trigger_onboarding(viewer, staff, process_kind="onboarding"):
    """L05: same company as the target Staff, dedicated trigger right for
    THIS process_kind (never implied by staff_management.create/update, and
    never implied by the OTHER kind's trigger right -- OFF-2), or Company
    Admin. Never the target's own self-trigger (consistent with every other
    DEC-04-flavoured refusal in this project, even though onboarding is not
    a compensation concept) -- this refusal applies identically to
    onboarding and offboarding, and is NEVER bypassable by a manual trigger
    call regardless of who edited the underlying contract date (see
    services.offboarding_automation for the separate, actor-less automatic
    path, which this function is never even called for)."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if viewer.id == staff.user_id:
        return False
    if not _same_company(viewer, staff):
        return False
    return _trigger_permission_for_kind(process_kind)(viewer) or is_company_admin_for_staff(viewer, staff)


def can_view_onboarding_instance(viewer, instance):
    """L05: the RH global view (onboarding_view_all/offboarding_view_all per
    instance.process_kind, or Company Admin), OR the employee themselves
    (their own instance only, further filtered to visible_to_employee steps
    at the serializer level -- this function only gates "can see the
    instance shell at all"), OR anyone who is the responsible_user of at
    least one of its steps (filtered to their own steps at the serializer
    level, same principle)."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    staff = instance.staff
    if getattr(viewer, "company_id", None) != instance.company_profile_id:
        return False
    if viewer.id == staff.user_id:
        return True
    if _view_all_permission_for_kind(instance.process_kind)(viewer) or is_company_admin_for_staff(viewer, staff):
        return True
    return instance.step_instances.filter(responsible_user=viewer).exists()


def can_act_on_onboarding_step(viewer, step_instance):
    """L05: "actions sur les taches assignees" -- being the CURRENT
    responsible_user of this exact step is, by itself, sufficient
    authorization to complete/update it. No staff_management permission is
    required for this -- assignment IS the authorization, never anything
    broader (acting on a DIFFERENT step one is not assigned to still
    requires onboarding_view_all/offboarding_view_all or Company Admin, via
    can_view_onboarding_instance).

    Offboarding-only residual guard (ticket's own instruction: "ne pas
    permettre de contourner le refus d'auto-declenchement"): the generic
    Company-Admin fallback below is refused when the viewer IS the
    departing employee of their OWN offboarding instance -- a Company Admin
    can still complete steps they are genuinely, individually ASSIGNED to
    (responsible_user_id match, first branch, unaffected), but can never
    self-certify an arbitrary step of their own departure purely through
    the admin-fallback right. This mirrors the self-trigger refusal and is
    additive/offboarding-scoped only -- onboarding's existing, already-
    shipped admin-fallback behaviour is left exactly as it was."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if viewer.id == step_instance.responsible_user_id:
        return True
    instance = step_instance.instance
    from human_resource_management.models.onboarding import ProcessKind
    if instance.process_kind == ProcessKind.OFFBOARDING and viewer.id == instance.staff.user_id:
        return False
    return _view_all_permission_for_kind(instance.process_kind)(viewer) or is_company_admin_for_staff(viewer, instance.staff)


def can_close_onboarding_instance(viewer, instance):
    """Replaces the inline check previously duplicated in views.onboarding's
    `close` action. Same residual guard as can_act_on_onboarding_step above,
    applied to closing: an offboarding instance can never be self-closed by
    its own departing employee, even if they also hold Company Admin -- the
    onboarding path (process_kind=onboarding) keeps its EXACT pre-existing
    behaviour (no self-check there; low-stakes, not touched, to avoid
    regressing already-shipped/tested behaviour for a concern this ticket
    does not raise for onboarding)."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    from human_resource_management.models.onboarding import ProcessKind
    if instance.process_kind == ProcessKind.OFFBOARDING and viewer.id == instance.staff.user_id:
        return False
    return _view_all_permission_for_kind(instance.process_kind)(viewer) or is_company_admin_for_staff(viewer, instance.staff)


def can_manage_offboarding_automation(viewer, company_profile):
    """Who may view/toggle the auto-trigger config and manage the contract-
    type -> template mapping table: the same right as managing offboarding
    templates (it is template-adjacent configuration, not a distinct
    concern worth a 4th permission key), or Company Admin."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if getattr(viewer, "company_id", None) != company_profile.id:
        return False
    if has_offboarding_manage_templates_permission(viewer):
        return True
    return getattr(company_profile, "admin_user_id", None) == viewer.id


def _same_company(viewer, staff):
    # DEC-10/11/12: a permission key held via a role scoped to the VIEWER's
    # own company is never, by itself, proof that the TARGET staff member
    # belongs to that same company -- has_cumulative_module_permission only
    # confirms "holds this key somewhere in their own company's roles", not
    # "...for THIS staff member's company". Checked explicitly here (scoped
    # to the compensation functions below) rather than widening the shared,
    # cross-app Staff-object-level gate (StaffViewSet.get_object), which is
    # pre-existing and out of this lot's narrow scope.
    target_company_id = getattr(staff.user, "company_id", None)
    return bool(target_company_id and getattr(viewer, "company_id", None) == target_company_id)


def can_view_staff_compensation(viewer, staff):
    """L00-A (DEC-03): the one combined read-access check -- the employee
    themself (always, read-only -- see can_update_staff_compensation for
    why this is never also a write right), an explicitly-habilitated RH
    user OF THE SAME COMPANY, or the canonical Company Admin of this SAME
    company. Never a manager/technical-admin bypass (DEC-04's "no automatic
    access" bullet): is_staff/is_superuser/a role held in another company
    are never consulted here."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if viewer == staff.user:
        return True
    if not _same_company(viewer, staff):
        return False
    return (
        has_compensation_view_permission(viewer)
        or is_company_admin_for_staff(viewer, staff)
    )


def can_update_staff_compensation(viewer, staff):
    """L00-A (DEC-03/04): write access -- an explicitly-habilitated RH user
    or the canonical Company Admin, WITHIN THEIR OWN company/perimeter
    (DEC-10/11/12 -- see _same_company). Deliberately EXCLUDES the
    self-mutation case: DEC-04 requires a second-person validation circuit
    this lot does not build, so a self-edit of one's OWN compensation is
    refused unconditionally at the call site (serializers.staff.
    StaffSerializer.validate), regardless of whether the editor ALSO holds
    this permission or is the Company Admin -- this function is only ever
    consulted for an edit targeting SOMEONE ELSE's record."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if not _same_company(viewer, staff):
        return False
    return (
        has_compensation_update_permission(viewer)
        or is_company_admin_for_staff(viewer, staff)
    )


def can_export_staff_compensation(viewer, company_profile):
    """L00-A (DEC-49): export-specific check, company-level (the export is
    already scoped to one company's rows) -- explicit export_compensation,
    or the Company Admin of THAT company. Never inferred from view/update
    rights alone."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if has_compensation_export_permission(viewer):
        return True
    return bool(company_profile and getattr(company_profile, "admin_user_id", None) == viewer.id)


def can_view_company_compensation_config(viewer, company_profile):
    """L00-B3.2: company-WIDE equivalent of can_view_staff_compensation --
    used for CompanyChargeRateHistory/CompanyCostItemType, which have no
    individual `staff` target to run _same_company against. Explicitly
    verifies the company matches the viewer's OWN company (same DEC-10/
    11/12 discipline) before ever consulting the permission key or Company
    Admin status -- never inferred from the permission key alone."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if not company_profile or getattr(viewer, "company_id", None) != company_profile.id:
        return False
    return (
        has_compensation_view_permission(viewer)
        or getattr(company_profile, "admin_user_id", None) == viewer.id
    )


def can_update_company_compensation_config(viewer, company_profile):
    """L00-B3.2: write equivalent of the above. No DEC-04 self-mutation
    concept is applied here -- there is no individual staff record a
    company-wide charge rate or cost-item catalog entry could be said to
    "belong" to, and changing it affects every employee identically; it is
    not a mechanism for a single person to alter their OWN pay or net
    figure (that remains governed by StaffCompensationHistory/DEC-04,
    unaffected). Recorded explicitly rather than silently assumed -- see
    CompanyChargeRateHistory's docstring and RAPPORT_L00B32.md."""
    if not viewer or not getattr(viewer, "is_authenticated", False):
        return False
    if not company_profile or getattr(viewer, "company_id", None) != company_profile.id:
        return False
    return (
        has_compensation_update_permission(viewer)
        or getattr(company_profile, "admin_user_id", None) == viewer.id
    )


def is_team_manager_for(user, employee):
    """LOT RH 5: true iff `user` may give a manager opinion on a leave
    request belonging to `employee` -- holds the granular
    `leave_request.manager_opinion` permission AND `employee` is genuinely
    one of `user`'s direct reports via the existing, already-relied-upon
    `Staff.supervisor` relationship (used elsewhere in this codebase for
    Time Card/Expense Card approval routing -- NOT a company-wide "Manager
    role" assumption, which was explicitly ruled out), same company, never
    the user's own request."""
    employee_user = getattr(employee, 'user', None)
    if employee_user is None or employee_user == user:
        return False
    if user.is_freelancer or employee_user.company_id != user.company_id:
        return False
    staff = getattr(user, 'staff', None)
    if staff is None:
        return False
    if getattr(employee, 'supervisor_id', None) != staff.id:
        return False
    return has_leave_manager_opinion_permission(user)


def is_hr_reviewer_for(user, employee):
    """LOT RH 4: the one existing mechanism this codebase has for "is this
    user HR" -- there is no separate HR flag or role-name check anywhere
    (confirmed by investigation); a company's own admin decides who holds
    `leave_request.review` via its Roles & Permissions screen, and that is
    already exactly the permission LeaveRequestPermission's status-change
    gate (`_check_update_permission` below) requires to approve/reject a
    leave request. Reused as-is for supporting-document visibility so "can
    review" and "can see the justificatif" are always the same answer --
    same-company, never the employee's own request, same permission key.
    A Company Manager/Manager/Admin-Server-only user is excluded simply
    because they don't hold this key by default; nothing here is keyed on
    a role's displayed name."""
    employee_user = getattr(employee, 'user', None)
    if employee_user is None or employee_user == user:
        return False
    if user.is_freelancer or employee_user.company_id != user.company_id:
        return False
    return has_cumulative_leave_review_permission(user)


class LeaveEntitlementPermission(BasePermission):
    """LOT RH 9: the entitlement attribution/adjustment/history/activation
    screens are reserved to real HR reviewers (`has_cumulative_leave_review_
    permission`), same union-across-roles pattern as `LeaveReportingPermission`
    -- never `staff_management`. Consultation of one's OWN entitlements is a
    separate, narrower allowance checked at the view/queryset level (never
    here), and a Company Manager/Manager/Admin-Server-only user without this
    permission can never reach another employee's droits through this class."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if getattr(user, 'is_freelancer', False) or not user.company_id:
            return False
        return has_cumulative_leave_review_permission(user)


class LeaveReportingPermission(BasePermission):
    """LOT RH 8: the detailed Leaves export and the approved-absence recap
    (view + XLSX/PDF exports) are reserved to users holding the real,
    cumulative `leave_request.review` entitlement -- never a generic
    `staff_management` permission, never a single-role check. Reuses
    `has_cumulative_leave_review_permission` as-is (the same union-across-
    roles pattern already established for the review queue/calendar), so a
    Company Manager + RH cumul is honored and a later revocation closes
    access immediately, exactly like every other use of that function. A
    freelancer (no company) can never hold a company-wide reporting right."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if getattr(user, 'is_freelancer', False) or not user.company_id:
            return False
        return has_cumulative_leave_review_permission(user)


class LeaveRequestPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('leave_request', action, scope)

    def has_permission(self, request, view):
        print(f'[DEBUG] LeaveRequestPermission: has_permission: method={request.method}, action={getattr(view, "action", "unknown")}')
        
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action in {'list', 'calendar'}:
            if view.action == 'list' and self._is_review_queue(request):
                return has_cumulative_leave_review_permission(request.user)
            if view.action == 'list' and self._is_team_view(request):
                return has_leave_manager_opinion_permission(request.user)
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None, request)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        print(f'[DEBUG] LeaveRequestPermission: has_object_permission: method={request.method}, obj={obj}')
        
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj, request)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj, request)
        return False
    
    def _check_create_permission(self, user):
        print(f'[DEBUG] LeaveRequestPermission: _check_create_permission: user={user}')
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for create')
            return True
        elif self._check_permission(user, 'leave_request', 'create', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: create granted')
            return True
        elif self._check_permission(user, 'leave_request', 'create_without_approval', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: create_without_approval granted')
            return True
        else:
            print(f'[DEBUG] LeaveRequestPermission: no create permissions found')
        
        return False
    
    def _check_update_permission(self, user, obj, request=None):
        print(f'[DEBUG] LeaveRequestPermission: _check_update_permission: user={user}, obj={obj}')

        if request is not None and 'status' in request.data:
            employee_user = getattr(getattr(obj, 'employee', None), 'user', None)
            if employee_user is None or employee_user == user:
                return False
            if user.is_freelancer or employee_user.company_id != user.company_id:
                return False
            return has_cumulative_leave_review_permission(user)
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for update')
            return True
        elif self._check_permission(user, 'leave_request', 'update', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: update granted')
            return True
        else:
            print(f'[DEBUG] LeaveRequestPermission: no update permissions found')
        
        return False

    @staticmethod
    def _is_review_queue(request):
        return str(request.query_params.get('review_queue', '')).lower() in {
            'true',
            '1',
        }

    @staticmethod
    def _is_team_view(request):
        return str(request.query_params.get('team_view', '')).lower() in {
            'true',
            '1',
        }
    
    def _check_delete_permission(self, user, obj):
        print(f'[DEBUG] LeaveRequestPermission: _check_delete_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for delete')
            return True
        elif self._check_permission(user, 'leave_request', 'view', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view granted')
            return True
        elif self._check_permission(user, 'leave_request', 'view_created', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view_created permission found')
            if hasattr(obj, 'employee') and obj.employee and obj.employee.user == user:
                print(f'[DEBUG] LeaveRequestPermission: user is employee, view granted')
                return True
        
        return False
    
    def _check_view_permission(self, user, obj, request=None):
        print(f'[DEBUG] LeaveRequestPermission: _check_view_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for view')
            return self._is_leave_in_view_scope(user, obj)
        if self._check_permission(user, 'leave_request', 'view', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view granted')
            return self._is_leave_in_view_scope(user, obj)
        if self._check_permission(user, 'leave_request', 'view_created', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view_created permission found')
            # For list operations (obj is None), allow access - filtering happens in queryset
            if obj is None:
                print(f'[DEBUG] LeaveRequestPermission: list view, access granted for view_created')
                return True
            # For object operations, check if user is the employee of this leave request
            elif hasattr(obj, 'employee') and obj.employee and obj.employee.user == user:
                print(f'[DEBUG] LeaveRequestPermission: user is employee, view granted')
                return True
        if has_cumulative_leave_review_permission(user):
            print(f'[DEBUG] LeaveRequestPermission: review permission found for view')
            if not self._is_leave_in_view_scope(user, obj):
                return False
            if (
                obj is not None
                and request is not None
                and self._is_review_queue(request)
                and obj.employee.user == user
            ):
                return False
            return True

        if has_leave_manager_opinion_permission(user):
            if obj is None:
                # List: the real team-membership filtering happens in the
                # queryset (get_queryset's team_view branch); nothing here
                # ever falls back to company-wide visibility.
                return True
            if is_team_manager_for(user, obj.employee):
                return True

        print(f'[DEBUG] LeaveRequestPermission: no view permissions found')
        
        return False

    @staticmethod
    def _is_leave_in_view_scope(user, obj):
        if obj is None:
            return True

        employee_user = getattr(getattr(obj, 'employee', None), 'user', None)
        if employee_user is None:
            return False
        if user.is_freelancer:
            return employee_user == user
        return bool(
            user.company_id
            and employee_user.company_id == user.company_id
        )


class StaffObservationPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('staff_observation', action, scope)

    def has_permission(self, request, view):
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        # For other operations, allow to proceed to object-level check
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user, None)
        
        return True
    
    def has_object_permission(self, request, view, obj):
        print("has_object_permission", request.method, obj)
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj)
        return False
    
    def _check_create_permission(self, user):
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'create', self.scope, {}):
            return True
        
        return False
    
    def _check_update_permission(self, user, obj):
        print("check update permission", self.scope, user, obj)
        print(self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}))
        print(self._check_permission(user, 'staff_observation', 'update', self.scope, {}))
        print(self._check_permission(user, 'staff_observation', 'update_created', self.scope, {}))
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'update', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'update_created', self.scope, {}):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_delete_permission(self, user, obj):
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'delete', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'delete_created', self.scope, {}):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_view_permission(self, user, obj):
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'view', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'view_created', self.scope, {}):
            if obj is None:
                return True
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False


class ResourceAllocationPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('resources.resource_allocation', action, scope)

    def has_permission(self, request, view):
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user)
        return False
    
    def _check_view_permission(self, user):
        if self._check_permission(user, 'resources.resource_allocation', 'view', self.scope, {}):
            return True
        
        return False


class StaffManagementPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('staff_management', action, scope)

    def has_permission(self, request, view):
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user)
        elif request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user)
        return True

    def has_object_permission(self, request, view, obj):
        if request.method == 'GET':
            return self._check_view_permission(request.user)
        elif request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user)
        return False
    
    def _check_view_permission(self, user):
        # LOT RH10 follow-up: union across ALL of the user's company-scope
        # roles (was `self._check_permission` -> single-role `.first()`,
        # which hid a grant made via a SECOND role on top of an existing one
        # -- e.g. a Company Manager additionally granted RH staff-management
        # rights, same cumulative scenario as leave review).
        return has_cumulative_module_permission(user, 'staff_management', ['full_access', 'view'])

    def _check_update_permission(self, user):
        return has_cumulative_module_permission(
            user, 'staff_management', ['full_access', 'update', 'default_management_update']
        )
