from typing import NamedTuple

from django.db import transaction
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


@transaction.atomic
def allocate_document_sequence(folder):
    scope = resolve_document_library_scope(folder)
    counter, _ = (
        DocumentReferenceCounter.objects.select_for_update().get_or_create(
            **scope.counter_lookup,
        )
    )
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
