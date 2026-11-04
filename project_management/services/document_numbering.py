from typing import NamedTuple

from django.db import connection, transaction
from django.db.models import Max
from rest_framework import serializers

from project_management.models import Document, DocumentReferenceCounter


class DocumentLibraryScope(NamedTuple):
    owner_type: str
    owner_id: int

    @property
    def counter_lookup(self):
        lookup = {
            "project_id": None,
            "company_profile_id": None,
            "freelancer_id": None,
        }
        lookup[f"{self.owner_type}_id"] = self.owner_id
        return lookup

    @property
    def document_filter(self):
        return {f"folder__{self.owner_type}_id": self.owner_id}


def resolve_document_library_scope(folder):
    owners = [
        DocumentLibraryScope("project", folder.project_id)
        if folder.project_id
        else None,
        DocumentLibraryScope("company_profile", folder.company_profile_id)
        if folder.company_profile_id
        else None,
        DocumentLibraryScope("freelancer", folder.freelancer_id)
        if folder.freelancer_id
        else None,
    ]
    owners = [owner for owner in owners if owner is not None]

    if len(owners) != 1:
        raise serializers.ValidationError(
            {"folder": "The folder must belong to one document library."}
        )
    return owners[0]


def _company_scope(folder):
    """Company (or freelancer) owning the library of a folder: COMPANY sequence scope."""
    if folder.project_id:
        project = folder.project
        company_id = project.company_profile_id or getattr(
            project.user, "company_id", None
        )
        if company_id:
            return DocumentLibraryScope("company_profile", company_id)
        return DocumentLibraryScope("freelancer", project.user_id)
    return resolve_document_library_scope(folder)


_REFERENCE_LOCK_OWNER_TYPES = {"project": 1, "company_profile": 2, "freelancer": 3}


def lock_reference_perimeter(folder, sequence_scope="PROJECT"):
    """GED-L1.3: serialise the generation of official references inside ONE numbering perimeter.

    The sequence counters only serialise creations that share a counter row. With the DISCIPLINE
    or DOCUMENT_TYPE sequence scopes two creations of the same library use different counters, and
    a reference format without those segments can then produce the same reference twice: the
    "reference already exists" check cannot see a document that is not committed yet. A
    transaction-level advisory lock per owner (project / company / freelancer library, or the
    company for the COMPANY sequence scope) closes that gap without any schema change: the second
    creation waits for the first to commit and then sees its reference. Other perimeters are not
    blocked. Released automatically at commit/rollback, so the caller must hold a transaction
    (generate_document_reference does); a no-op outside PostgreSQL."""
    if connection.vendor != "postgresql":
        return
    scope = (
        _company_scope(folder)
        if sequence_scope == "COMPANY"
        else resolve_document_library_scope(folder)
    )
    key = (_REFERENCE_LOCK_OWNER_TYPES[scope.owner_type] << 40) | int(scope.owner_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


@transaction.atomic
def allocate_document_sequence(
    folder, sequence_scope="PROJECT", discipline_code="", type_code=""
):
    """Next sequence number, serialized on a counter row (select_for_update).

    PROJECT (default): the existing library counter (project / company / freelancer
    library), unchanged behaviour. The other scopes use their own counter rows, told apart
    by scope_key: COMPANY (continuous across the projects of a company), DISCIPLINE and
    DOCUMENT_TYPE.
    """
    scope = resolve_document_library_scope(folder)
    scope_key = ""
    if sequence_scope == "COMPANY":
        scope, scope_key = _company_scope(folder), "company"
    elif sequence_scope == "DISCIPLINE":
        scope_key = f"discipline:{discipline_code or ''}"
    elif sequence_scope == "DOCUMENT_TYPE":
        scope_key = f"type:{type_code or ''}"

    counter, _ = DocumentReferenceCounter.objects.select_for_update().get_or_create(
        **scope.counter_lookup,
        scope_key=scope_key,
    )
    existing_max = 0
    if scope_key == "":
        existing_max = (
            Document.objects.filter(**scope.document_filter).aggregate(
                max_sequence=Max("sequence_number")
            )["max_sequence"]
            or 0
        )
    next_sequence = max(counter.last_sequence, existing_max) + 1
    counter.last_sequence = next_sequence
    counter.save(update_fields=["last_sequence", "updated_at"])
    return next_sequence
