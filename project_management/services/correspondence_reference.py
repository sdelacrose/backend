"""LOT 3B: Correspondence permanent reference generation.

Mirrors project_management.services.document_reference's proven pattern (transaction.atomic +
select_for_update + last_sequence) exactly, but against a dedicated counter
(CorrespondenceReferenceCounter): Correspondence numbering never shares Document's counter
rows/state.

V2 format (5-digit sequence) -- LOT 3D-3C0 section 9: aligned with the canonical Project
Document reference's own default segment order (PROJECT-DISCIPLINE-DOCUMENT_TYPE-SEQUENCE,
see document_reference.py) -- the owner code leads, the type-identifying code (here, the fixed
"COR" trigram, since Correspondence has no discipline/document_type of its own) comes next,
the sequence is always last:
    Project-scoped:  <PROJECT_CODE>-COR-00001
    Company-scoped:  <COMPANY_CODE>-COR-00001
    Freelancer-scoped: COR-00001 (no permanent freelancer code exists yet -- same lenient
    behaviour as the Document reference generator for that scope; unchanged from V1).

V1 (COR-<CODE>-00001, COR leading) was the original LOT 3B format -- changed here only for
NEW references going forward; existing stored `reference` values are never rewritten (no
migration touches them), matching the same "permanent once generated" rule Document references
already follow.
"""

from django.db import transaction

from models.company_codes import propose_unique_company_code

from project_management.models import Correspondence, CorrespondenceReferenceCounter
from project_management.services.project_codes import propose_unique_project_code

SEQUENCE_WIDTH = 5


def _owner_code_and_scope(*, project, company_profile, freelancer):
    if project is not None:
        if not project.project_code:
            project.project_code = propose_unique_project_code(project)
            project.save(update_fields=["project_code"])
        return project.project_code, {"project": project, "company_profile": None, "freelancer": None}
    if company_profile is not None:
        if not company_profile.company_code:
            company_profile.company_code = propose_unique_company_code(company_profile)
            company_profile.save(update_fields=["company_code"])
        return company_profile.company_code, {"project": None, "company_profile": company_profile, "freelancer": None}
    return "", {"project": None, "company_profile": None, "freelancer": freelancer}


@transaction.atomic
def generate_correspondence_reference(*, project=None, company_profile=None, freelancer=None):
    """The next permanent reference for a NEW Correspondence in this owner scope.

    Exactly one of project/company_profile/freelancer must be given (mirrors Correspondence's
    own CheckConstraint). Called once, at creation; the caller holds the transaction the same
    way Document's generator does.
    """
    owner_code, scope_kwargs = _owner_code_and_scope(
        project=project, company_profile=company_profile, freelancer=freelancer
    )
    counter, _ = CorrespondenceReferenceCounter.objects.select_for_update().get_or_create(
        **scope_kwargs
    )
    counter.last_sequence += 1
    counter.save(update_fields=["last_sequence", "updated_at"])

    # Owner code leads (matching Document's PROJECT/COMPANY-first convention); COR fills the
    # position Document uses for its DOCUMENT_TYPE segment; sequence is always last.
    parts = [owner_code] if owner_code else []
    parts.append("COR")
    parts.append(str(counter.last_sequence).zfill(SEQUENCE_WIDTH))
    reference = "-".join(parts)

    # Runtime protection on top of the locked counter, mirroring Document's own check.
    if Correspondence.objects.filter(reference=reference).exists():
        raise RuntimeError("Could not allocate a unique correspondence reference.")
    return reference
