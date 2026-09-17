from ecpmpapp.utils import require_any_permission


AAS_MODELS_PERMISSION_MODULE = "aas_models"


def has_aas_models_permission(user, *, write=False):
    """Check company-scoped access to the AAS Models module."""
    actions = ["full_access"] if write else ["view", "full_access"]
    return require_any_permission(
        user,
        AAS_MODELS_PERMISSION_MODULE,
        actions,
        "company",
        {},
    )
