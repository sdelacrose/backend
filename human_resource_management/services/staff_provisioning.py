"""L00-B2B: resolves DEC-52's second half -- "defaults apply to FUTURE
recruitments" -- at the one real Staff-provisioning path confirmed in
L00-B1/L00-B2A (human_resource_management/management/commands/
create_staff_profiles.py; an exhaustive repo-wide search found no other
code instantiating Staff(...) outside tests). This module never touches an
existing Staff row -- that command's own query (`staff__isnull=True`)
already guarantees it only ever processes users genuinely lacking one.
"""
from models.models import Staff, StaffConfiguration

UNSET = object()

_FIELD_MAP = {
    "salary": "base_salary",
    "hourly_rate": "hourly_rate",
    "weekly_hours": "number_of_hours",
    "annual_leaves_allowed": "number_of_leaves",
    "wfh_leaves_allowed": "number_of_wfh_leaves",
}

# L00-B2A point 6: the pre-existing "no configuration" convention, preserved
# EXACTLY as create_staff_profiles.py already hardcoded it -- never
# reinvented. hourly_rate/wfh_leaves_allowed are deliberately absent here:
# that command never set them explicitly either, relying on Staff's own
# model-level field default (75 / 12) -- omitting them from the resolved
# dict lets that same model default keep applying, unchanged.
_NO_CONFIG_FALLBACKS = {
    "salary": 0.00,
    "weekly_hours": 40,
    "annual_leaves_allowed": 12,
}


def resolve_staff_provisioning_config(user):
    """The StaffConfiguration genuinely applicable to `user` -- their OWN
    (freelancer=user) if they are a freelancer, else their company's
    (company_profile=user.company_id). Exactly the same branching already
    used by StaffConfigurationViewSet.get_queryset -- never a cross-company
    or cross-freelancer fallback."""
    if getattr(user, "is_freelancer", False):
        return StaffConfiguration.objects.filter(freelancer=user).first()
    if user.company_id:
        return StaffConfiguration.objects.filter(company_profile_id=user.company_id).first()
    return None


def build_staff_defaults(
    user, *, salary=UNSET, hourly_rate=UNSET, weekly_hours=UNSET,
    annual_leaves_allowed=UNSET, wfh_leaves_allowed=UNSET,
):
    """Field values for a BRAND NEW `Staff(user=user, ...)`.

    Three distinct states per field, matching each field's real nullability:
    - UNSET (the default): not explicitly provided -- resolved from `user`'s
      own StaffConfiguration if one exists, else the pre-existing
      "no configuration" convention (see _NO_CONFIG_FALLBACKS).
    - an explicit value, INCLUDING `None` or `0`: always wins outright, never
      reinterpreted as "absent" -- a real zero (e.g. an unpaid/unconfirmed
      salary) or an explicit `None` (on a nullable field) is not a gap to
      fill.
    Never reapplied to an existing Staff -- this only ever runs at the one
    real creation path, before any row is persisted."""
    explicit = {
        "salary": salary, "hourly_rate": hourly_rate, "weekly_hours": weekly_hours,
        "annual_leaves_allowed": annual_leaves_allowed, "wfh_leaves_allowed": wfh_leaves_allowed,
    }
    config = resolve_staff_provisioning_config(user)
    resolved = {}
    for field, value in explicit.items():
        if value is not UNSET:
            resolved[field] = value
        elif config is not None:
            resolved[field] = getattr(config, _FIELD_MAP[field])
        elif field in _NO_CONFIG_FALLBACKS:
            resolved[field] = _NO_CONFIG_FALLBACKS[field]
        # else: left out entirely so Staff's own model-level field default
        # applies, exactly as it already does today.
    return resolved


def provision_staff_for_user(user, **explicit_overrides):
    """An UNSAVED `Staff` instance for `user`, ready to append to a
    `bulk_create` list -- the exact per-user construction used by
    create_staff_profiles.py, extracted so it is independently testable
    without exercising that command's own, deliberately global,
    `User.objects.filter(staff__isnull=True)` query."""
    defaults = build_staff_defaults(user, **explicit_overrides)
    return Staff(
        user=user,
        supervisor=None,
        productivity_percentage=0,
        **defaults,
    )
