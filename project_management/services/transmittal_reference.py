"""LOT 3C: OUT Transmittal permanent reference generation.

LOT 3C amendment section 1/2: every Transmittal is project-scoped -- there is no
company/library numbering scope. The project's own canonical `project_code` is reused as-is
(never a newly-derived trigram); `generate_transmittal_number` only proposes one via the same
shared helper Document/Correspondence already use, when the project has none yet.

V1 format (5-digit sequence, PER PROJECT):
    TR-<PROJECT_CODE>-00001

This is the OUT path only. An IN Transmittal's business reference is the Supplier's own
reference, entered manually (services.transmittal_items / serializers.transmittal) -- it never
calls this function and never consumes this counter (LOT 3C amendment section 3/5).
"""

from django.db import transaction

from project_management.models import Transmittal, TransmittalReferenceCounter
from project_management.services.project_codes import propose_unique_project_code

SEQUENCE_WIDTH = 5


@transaction.atomic
def generate_transmittal_number(*, project):
    """The next permanent OUT reference for `project`. Called once, at creation."""
    if not project.project_code:
        project.project_code = propose_unique_project_code(project)
        project.save(update_fields=["project_code"])

    counter, _ = TransmittalReferenceCounter.objects.select_for_update().get_or_create(
        project=project
    )
    counter.last_sequence += 1
    counter.save(update_fields=["last_sequence", "updated_at"])

    number = f"TR-{project.project_code}-{str(counter.last_sequence).zfill(SEQUENCE_WIDTH)}"

    # Runtime protection on top of the locked counter, mirroring Document's/Correspondence's
    # own check.
    if Transmittal.objects.filter(
        project=project, transmittal_number=number, direction=Transmittal.Direction.OUT
    ).exists():
        raise RuntimeError("Could not allocate a unique transmittal number.")
    return number
