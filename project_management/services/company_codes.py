"""LOT 2D-12: the Document-aware half of the Company Code lifecycle.

CompanyProfile (and its pure derive/propose/format logic, ``models.company_codes``) lives in
the shared ``models`` package (admin-server-models), which must never depend on this repo's
Document model. The *lock* check below needs Document, so it lives here instead -- mirroring
``project_management.services.project_codes`` (``project_code_is_locked`` /
``assert_project_code_change_allowed``) as closely as possible.

NOTE: as of LOT 2D-12, no CompanyProfile write endpoint exists in the `backend` or
`admin-server-models` repositories (CompanyProfile is edited by the separate, out-of-scope
admin-server-backend application) -- see the LOT 2D-12 report for that gap. This module is
ready to be called from that write path once/if it is added here; until then it is exercised
directly (service-level) by the reference generator and by tests.
"""

from django.core.exceptions import ValidationError

from models.company_codes import validate_company_code_value


def company_code_is_locked(company_profile):
    """Locked as soon as a company-library Document has a permanent reference (derived, no
    stored flag) -- same principle as project_codes.project_code_is_locked.
    """
    from project_management.models import Document

    if not company_profile or not company_profile.pk:
        return False
    return (
        Document.objects.filter(folder__company_profile_id=company_profile.pk)
        .exclude(full_document_number__isnull=True)
        .exclude(full_document_number="")
        .exists()
    )


def assert_company_code_change_allowed(company_profile, new_code):
    """Reject a company_code change once the company has a permanent Document reference."""
    if company_code_is_locked(company_profile):
        raise ValidationError(
            "The company code cannot be changed: documents already have permanent "
            "references built with it."
        )
    validate_company_code_value(company_profile, new_code)
