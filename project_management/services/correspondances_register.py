"""LOT 3D-2: the unified Correspondances register -- a READ-ONLY projection across
Correspondence (LOT 3B) and Transmittal (LOT 3C), never a shared database model/table.

Both models stay fully independent; this module only builds one correctly paginated/sorted/
searchable QuerySet.union() over a lean, common projection of each, reusing the exact same
tenant-scope querysets the individual APIs already use
(views.correspondence.correspondence_queryset_for_user /
views.transmittal.transmittal_queryset_for_user) so nothing here can see more than a user's
existing Correspondence/Transmittal permissions already allow.
"""

from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    DateTimeField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Concat, NullIf
from django.utils import timezone

from project_management.models import Correspondence, CorrespondenceValidationWorkflow, Transmittal
from project_management.views.correspondence import correspondence_queryset_for_user
from project_management.views.transmittal import transmittal_queryset_for_user

# The common projection's column names (post-values()); both sides must produce exactly these,
# in this order, with matching types, for QuerySet.union() to align them correctly.
PROJECTION_FIELDS = (
    "id", "entity_type", "direction", "reference_", "project_id_", "project_name_",
    "company_id_", "company_name_", "subject", "business_date_", "lifecycle_status_",
    "response_status_", "created_at",
    # LOT 3D-3A TER: the minimum added to render the Lovable register's parent/child response
    # hierarchy, Responsible column and Due date column, without redesigning the
    # Correspondence/Transmittal relationship itself. Transmittal has none of these three
    # concepts (LOT 3C amendment) -- always NULL on that side of the union.
    "parent_correspondence_id_", "responsible_id_", "responsible_name_", "response_due_date_",
    # LOT 3D-3C0B: how many responses this row already has (0 if none) -- the register's own
    # parent-side "N responses" indicator. Computed as a correlated subquery so it stays a
    # per-row SQL column (not a second, all-rows query) and survives the union/pagination
    # exactly like every other projected column. Transmittal has no response concept -- always 0.
    "responses_count_",
    # LOT 3D-3C0C section 8: the ONE direction-aware reference actually shown in the register
    # (Correspondence.display_reference, computed in SQL rather than refetched in Python --
    # same "no second, duplicated rule set" reasoning as _candidate_status_for_child). Always
    # equal to reference_ on the Transmittal side (no IN/OUT reference distinction there).
    "display_reference_",
    # LOT 3D-3C0C section 10: whether this row already has an in-progress response -- backs the
    # register's own Reply action gating. Transmittal has no response concept -- always False.
    "has_active_reply_",
    # LOT 3D-3D1 section 12: the OUT row's own internal validation state, for a compact badge
    # (never a new column, section 12) -- None for IN (validation is OUT-only, section 2) and
    # for Transmittal (no such concept at all).
    "validation_status_",
)

# Client-facing `ordering` values -> the internal projected column they sort by.
ORDERING_FIELDS = {
    "business_date": "business_date_",
    "reference": "reference_",
    "created_at": "created_at",
}
DEFAULT_ORDERING = ("-business_date_", "-created_at", "-id")


# LOT 3D-3A TER: the Lovable register's "Awaiting response" quick filter means ANY still-open
# pending state (TO_ANSWER/IN_PREPARATION/AWAITING_REPLY, excluding whichever of those has since
# become OVERDUE) -- a single alias value, resolved here (the one place response-status logic
# already lives), never re-derived client-side across several separate requests.
PENDING_STATUS_ALIAS = "PENDING"


def _correspondence_status_filter(qs, status):
    """`status` means either the lifecycle (DRAFT/ISSUED), a response state, or the PENDING
    alias, depending on its value -- never a merged enum. OVERDUE is derived (never stored):
    filtering it uses the same still-pending + elapsed-due-date rule as
    Correspondence.effective_response_status, expressed as the smallest correct annotation
    rather than a Python-side pass."""
    if status in Correspondence.Status.values:
        return qs.filter(status=status)
    is_overdue = Case(
        When(
            Q(response_status__in=Correspondence.PENDING_STATUSES)
            & Q(response_due_date__lt=timezone.localdate()),
            then=Value(True),
        ),
        default=Value(False),
        output_field=BooleanField(),
    )
    if status == PENDING_STATUS_ALIAS:
        return qs.annotate(_is_overdue=is_overdue).filter(
            response_status__in=Correspondence.PENDING_STATUSES, _is_overdue=False,
        )
    if status not in Correspondence.ResponseStatus.values:
        return qs.none()
    if status == Correspondence.ResponseStatus.OVERDUE:
        return qs.annotate(_is_overdue=is_overdue).filter(_is_overdue=True)
    # A stored pending status whose due date has since passed now DISPLAYS as OVERDUE (see
    # Correspondence.effective_response_status) -- excluded here for the same reason, so
    # filtering by e.g. TO_ANSWER matches exactly what the UI would label TO_ANSWER.
    return qs.annotate(_is_overdue=is_overdue).filter(response_status=status, _is_overdue=False)


def _transmittal_status_filter(qs, status):
    # Transmittal has no response concept (LOT 3C amendment): the PENDING/OVERDUE aliases can
    # never match a Transmittal row, same as the existing ResponseStatus values never did.
    if status == PENDING_STATUS_ALIAS or status == Correspondence.ResponseStatus.OVERDUE:
        return qs.none()
    if status not in Transmittal.Status.values:
        return qs.none()
    return qs.filter(status=status)


def _build_correspondence_queryset(
    user, *, project, company_scope, direction, company, contact, correspondence_type,
    search, status,
):
    qs = correspondence_queryset_for_user(user)
    if company_scope:
        qs = qs.filter(project__isnull=True)
    if project is not None:
        qs = qs.filter(project=project)
    if direction:
        qs = qs.filter(direction=direction)
    if correspondence_type:
        qs = qs.filter(correspondence_type=correspondence_type)
    if company is not None:
        qs = qs.filter(
            Q(direction=Correspondence.Direction.IN, sender_company_id=company)
            | Q(direction=Correspondence.Direction.OUT, recipient_company_id=company)
        )
    if contact is not None:
        # Mirrors the `company` filter above exactly: the external party sits on sender_* for
        # IN, recipient_* for OUT (LOT 3D-3A TER secondary "Contact" filter -- no new concept).
        qs = qs.filter(
            Q(direction=Correspondence.Direction.IN, sender_contact_id=contact)
            | Q(direction=Correspondence.Direction.OUT, recipient_contact_id=contact)
        )
    if search:
        # LOT 3D-3C0C section 8: external_reference is now the meaningful reference for an IN
        # row (reference itself stays blank going forward) -- included here so searching for
        # it still finds the row, same as it already could by its generated reference before.
        qs = qs.filter(
            Q(reference__icontains=search)
            | Q(external_reference__icontains=search)
            | Q(subject__icontains=search)
        )
    if status:
        qs = _correspondence_status_filter(qs, status)
    return qs


def _build_transmittal_queryset(
    user, *, project, company_scope, direction, company, contact, correspondence_type,
    search, status,
):
    if company_scope:
        # Transmittal is project-only (LOT 3C amendment): never present in company context.
        return Transmittal.objects.none()
    if correspondence_type:
        # Transmittal has no correspondence_type field (a Correspondence-only taxonomy):
        # excluded rather than silently ignored, same convention as `contact` above.
        return Transmittal.objects.none()
    qs = transmittal_queryset_for_user(user)
    if project is not None:
        qs = qs.filter(project=project)
    if direction:
        qs = qs.filter(direction=direction)
    if company is not None:
        qs = qs.filter(external_company_id=company)
    if contact is not None:
        # Transmittal has no contact-level party (only external_company): a contact filter can
        # never match a Transmittal row -- excluded rather than silently ignored.
        return Transmittal.objects.none()
    if search:
        qs = qs.filter(Q(transmittal_number__icontains=search) | Q(subject__icontains=search))
    if status:
        qs = _transmittal_status_filter(qs, status)
    return qs


def _responses_count_subquery():
    # Correlated count of children pointing at this row via parent_correspondence -- Coalesced
    # to 0 (a row with zero responses has no matching group, so the bare Subquery would be NULL).
    counted = (
        Correspondence.objects.filter(parent_correspondence_id=OuterRef("pk"))
        .order_by()
        .values("parent_correspondence_id")
        .annotate(c=Count("id"))
        .values("c")
    )
    return Coalesce(
        Subquery(counted, output_field=IntegerField()), Value(0), output_field=IntegerField(),
    )


def _has_active_reply_subquery():
    # LOT 3D-3C0C section 10: whether this row already has a response still in progress (a
    # linked child in DRAFT) -- the same rule validate_parent_correspondence enforces
    # server-side, exposed per-row here so the register's own Reply action can be disabled
    # without a second, separate fetch. Exists()-backed, so it stays a cheap per-row boolean.
    in_progress = Correspondence.objects.filter(
        parent_correspondence_id=OuterRef("pk"), status=Correspondence.Status.DRAFT,
    )
    return Exists(in_progress)


def _validation_status_subquery():
    # LOT 3D-3D1 section 12: the latest validation workflow's own status for this row -- NULL
    # (no subquery match) when no workflow was ever started, matching "Draft" being derived,
    # never stored (services.correspondence_workflow.validation_state).
    latest = CorrespondenceValidationWorkflow.objects.filter(
        correspondence_id=OuterRef("pk")
    ).order_by("-created_at", "-id").values("status")[:1]
    return Subquery(latest, output_field=CharField(max_length=25, null=True))


def _correspondence_projection(qs):
    business_date = Case(
        When(
            direction=Correspondence.Direction.OUT,
            then=Coalesce(Cast("issue_date", DateTimeField()), "created_at"),
        ),
        default=Coalesce(Cast("received_date", DateTimeField()), "created_at"),
        output_field=DateTimeField(),
    )
    company_id = Case(
        When(direction=Correspondence.Direction.IN, then=F("sender_company_id")),
        default=F("recipient_company_id"),
        output_field=IntegerField(null=True),
    )
    company_name = Case(
        When(direction=Correspondence.Direction.IN, then=F("sender_company__company_name")),
        default=F("recipient_company__company_name"),
        output_field=CharField(max_length=255, null=True),
    )
    return qs.annotate(
        entity_type=Value("CORRESPONDENCE", output_field=CharField(max_length=20)),
        reference_=F("reference"),
        project_id_=F("project_id"),
        project_name_=F("project__project_name"),
        company_id_=company_id,
        company_name_=company_name,
        business_date_=business_date,
        lifecycle_status_=F("status"),
        response_status_=Case(
            When(
                Q(response_status__in=Correspondence.PENDING_STATUSES)
                & Q(response_due_date__lt=timezone.localdate()),
                then=Value(Correspondence.ResponseStatus.OVERDUE),
            ),
            default=F("response_status"),
            output_field=CharField(max_length=20, null=True),
        ),
        parent_correspondence_id_=F("parent_correspondence_id"),
        response_due_date_=Cast("response_due_date", DateTimeField()),
        responsible_id_=F("responsible_id"),
        responsible_name_=Case(
            When(responsible__isnull=True, then=Value(None)),
            default=Concat(
                F("responsible__first_name"), Value(" "), F("responsible__last_name"),
                output_field=CharField(),
            ),
            output_field=CharField(max_length=255, null=True),
        ),
        responses_count_=_responses_count_subquery(),
        # LOT 3D-3C0C section 8: mirrors Correspondence.display_reference exactly (see that
        # property's own docstring for the historical-compatibility fallback) -- computed here
        # in SQL, not refetched per row in Python, same reasoning as responses_count_ above.
        display_reference_=Case(
            When(direction=Correspondence.Direction.OUT, then=F("reference")),
            default=Coalesce(
                NullIf(F("external_reference"), Value("")),
                NullIf(F("reference"), Value("")),
                Value(""),
            ),
            output_field=CharField(max_length=255),
        ),
        has_active_reply_=_has_active_reply_subquery(),
        # Section 12: OUT-only (IN never runs validation, section 2) -- NULL for IN rows even
        # though the subquery itself would just never match either way, so it is explicit
        # rather than relying on that as an implementation detail.
        validation_status_=Case(
            When(direction=Correspondence.Direction.OUT, then=_validation_status_subquery()),
            default=Value(None),
            output_field=CharField(max_length=25, null=True),
        ),
    ).order_by().values(*PROJECTION_FIELDS)


def _transmittal_projection(qs):
    business_date = Case(
        When(direction=Transmittal.Direction.OUT, then=Coalesce("issued_at", "created_at")),
        default=Coalesce("received_at", "created_at"),
        output_field=DateTimeField(),
    )
    return qs.annotate(
        entity_type=Value("TRANSMITTAL", output_field=CharField(max_length=20)),
        reference_=F("transmittal_number"),
        project_id_=F("project_id"),
        project_name_=F("project__project_name"),
        company_id_=F("external_company_id"),
        company_name_=F("external_company__company_name"),
        business_date_=business_date,
        lifecycle_status_=F("status"),
        # Cast(Value(None), ...), not a bare Value(None, output_field=...): an untyped SQL NULL
        # literal has no type of its own until something forces Postgres to resolve one, and a
        # plain COUNT(*) over the union happens not to need that -- but ordering the union (as
        # build_register_queryset always does) does, at which point Postgres must reconcile this
        # column's type against the Correspondence side's real, concrete type across every row,
        # and a bare, unresolved NULL fails that reconciliation ("UNION types X and Y cannot be
        # matched"). Cast() forces an explicit `::type` on the NULL so it always resolves.
        response_status_=Cast(Value(None), output_field=CharField(max_length=20, null=True)),
        # Transmittal has neither a response relationship nor a Responsible field (LOT 3C
        # amendment / LOT 3D-3A TER) -- always NULL, never a second mechanism.
        parent_correspondence_id_=Cast(Value(None), output_field=IntegerField(null=True)),
        response_due_date_=Cast(Value(None), output_field=DateTimeField(null=True)),
        responsible_id_=Cast(Value(None), output_field=IntegerField(null=True)),
        responsible_name_=Cast(Value(None), output_field=CharField(max_length=255, null=True)),
        responses_count_=Value(0, output_field=IntegerField()),
        # Transmittal has no IN/OUT reference distinction (LOT 3D-3C0C section 8 is a
        # Correspondence-only rule) -- always its own transmittal_number, same as reference_.
        display_reference_=F("transmittal_number"),
        has_active_reply_=Value(False, output_field=BooleanField()),
        # Transmittal has no validation-workflow concept at all (section 12 is Correspondence
        # OUT only).
        validation_status_=Cast(Value(None), output_field=CharField(max_length=25, null=True)),
    ).order_by().values(*PROJECTION_FIELDS)


def build_register_queryset(
    user, *, project=None, company_scope=False, entity_types=("CORRESPONDENCE", "TRANSMITTAL"),
    direction=None, company=None, contact=None, correspondence_type=None, search=None,
    status=None, ordering=None,
):
    """The unified, correctly paginated/sortable QuerySet -- callers apply DRF pagination to
    this directly (it supports .count() and slicing like any other QuerySet)."""
    kwargs = dict(
        project=project, company_scope=company_scope, direction=direction,
        company=company, contact=contact, correspondence_type=correspondence_type,
        search=search, status=status,
    )
    parts = []
    if "CORRESPONDENCE" in entity_types:
        parts.append(_correspondence_projection(_build_correspondence_queryset(user, **kwargs)))
    if "TRANSMITTAL" in entity_types:
        parts.append(_transmittal_projection(_build_transmittal_queryset(user, **kwargs)))
    if not parts:
        return Correspondence.objects.none().values(*PROJECTION_FIELDS)

    combined = parts[0]
    for part in parts[1:]:
        combined = combined.union(part, all=True)

    order_fields = [ORDERING_FIELDS[o.lstrip("-")] for o in (ordering or []) if o.lstrip("-") in ORDERING_FIELDS]
    order_by = []
    for o, mapped in zip(ordering or [], order_fields):
        order_by.append(f"-{mapped}" if o.startswith("-") else mapped)
    order_by.extend(DEFAULT_ORDERING)
    return combined.order_by(*order_by)


def serialize_row(row):
    """Row dict (from the projected QuerySet) -> the LOT 3D-2 common response shape."""
    return {
        "id": row["id"],
        "entity_type": row["entity_type"],
        "direction": row["direction"],
        # LOT 3D-3C0C section 8: the direction-aware display reference, not the raw internally
        # generated one -- see display_reference_'s own annotation comment above.
        "reference": row["display_reference_"],
        "project": (
            {"id": row["project_id_"], "name": row["project_name_"]}
            if row["project_id_"] is not None else None
        ),
        "company": (
            {"id": row["company_id_"], "name": row["company_name_"]}
            if row["company_id_"] is not None else None
        ),
        "subject": row["subject"],
        "business_date": row["business_date_"],
        "lifecycle_status": row["lifecycle_status_"],
        "response_status": row["response_status_"],
        "parent_correspondence_id": row["parent_correspondence_id_"],
        "responses_count": row["responses_count_"],
        "has_active_reply": bool(row["has_active_reply_"]),
        # LOT 3D-3D1 section 12: None for IN/Transmittal (no validation concept there).
        "validation_status": row["validation_status_"],
        "response_due_date": row["response_due_date_"],
        "responsible": (
            {"id": row["responsible_id_"], "name": row["responsible_name_"]}
            if row["responsible_id_"] is not None else None
        ),
        "created_at": row["created_at"],
    }
