"""CRM-1B: one place that says which tenant a Network/CRM record belongs to.

A tenant is either a company (CompanyProfile) or a freelancer (User), matching the existing
dual-ownership pattern of ContactUser/ContactCompany (company_profile | user), Opportunity and
ActionItem (company_profile | freelancer). Links between records of different tenants are
refused server-side with these helpers.
"""

from django.core.exceptions import ValidationError


def tenant_of_user(user):
    if getattr(user, "is_freelancer", False):
        return ("freelancer", user.pk)
    company_id = getattr(user, "company_id", None)
    return ("company", company_id) if company_id else None


def tenant_of(obj):
    """Tenant key of a ContactUser, ContactCompany, Opportunity, ActionItem or CrmInteraction."""
    if obj is None:
        return None
    company_id = getattr(obj, "company_profile_id", None)
    if company_id:
        return ("company", company_id)
    # ContactUser / ContactCompany keep their freelancer owner in `user`; the others in `freelancer`.
    for field in ("freelancer_id", "user_id"):
        owner_id = getattr(obj, field, None)
        if owner_id:
            return ("freelancer", owner_id)
    return None


def tenant_filter_kwargs(tenant, *, owner_field="freelancer"):
    """Queryset kwargs restricting a model to a tenant (owner_field: 'freelancer' or 'user')."""
    kind, pk = tenant
    if kind == "company":
        return {"company_profile_id": pk}
    return {"company_profile__isnull": True, f"{owner_field}_id": pk}


def ensure_same_tenant(tenant, objects, *, field="links"):
    for obj in objects:
        if obj is not None and tenant_of(obj) != tenant:
            raise ValidationError({field: "Linked records must belong to the same company or freelancer."})


def user_belongs_to_tenant(user, tenant):
    if user is None:
        return True
    kind, pk = tenant
    if kind == "company":
        return getattr(user, "company_id", None) == pk
    return user.pk == pk
