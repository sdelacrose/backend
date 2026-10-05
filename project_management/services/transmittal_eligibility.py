"""LOT 3C: eligibility rules for a DocumentRevision joining a Transmittal.

OUT (client delivery): eligible origins are INTERNAL and SUPPLIER, each subject to its own gate
(L3F.1 section 6 -- explicit INTERNAL-or-SUPPLIER, never "anything except CLIENT"). CLIENT-origin
Documents are never eligible for outgoing Client distribution.

INTERNAL gate (unchanged since LOT 3C/3E-1C1, L3F.1 section 7 -- preserved exactly): a Revision
that is internally APPROVED -- proven exclusively by the canonical workflow/revision approval
evidence (DocumentRevision.revision_label, assigned once by
services.document_workflow.perform_action the moment a DocumentWorkflow reaches APPROVED).
Document.status is never used as proof (LOT 3C section 6).

SUPPLIER gate (new, L3F.1 sections 8/9/10): the exact DocumentRevision's own SupplierSurveillance
(OneToOne, L3B -- never "the Document's most recent Surveillance") must be CLOSED with
result=REVIEWED, UNLESS the Document's supplier_transmittal_category is FOR_INFORMATION, which
carries no mandatory Surveillance at all (the established L3B/L3D/L3F semantic, reused as-is).
COMMENTED/REJECTED (new_revision_required=True) and every pre-closure Surveillance status
(DRAFT/UNDER_REVIEW/READY_FOR_CLOSURE) are deliberately NOT eligible -- read-only here, this
module never writes to SupplierSurveillance (L3B-E stay closed, untouched).

Both OUT gates additionally require the Document's own transmission classification
(Document.transmittal_category -- the existing, already-used "Transmittal Category" metadata
field, configured per project/company as one of "Not sent" / "Available" / "For review" /
"For information", see project_management/models/constants.py GC_TRANSMITTAL_CATEGORIES) to be
"For review" or "For information". A Document classified "Not sent" (or "Available", or left
unset) must never be distributable through an outgoing transmittal -- there is deliberately no
"Available" condition here, only this exact whitelist (LOT 3E-1C1, unchanged).

IN (supplier receipt): the minimal safe rule the current architecture supports without
inventing a Supplier review workflow -- the Document must be SUPPLIER-origin. No internal
approval is required for an incoming Revision (LOT 3C section 7). The transmittal_category
rule above is OUT-only and never applied to IN.
"""

from django.core.exceptions import ValidationError

from project_management.models.document import DocumentOrigin
from project_management.models.supplier_surveillance import SupplierSurveillance

# Case-insensitive on purpose: transmittal_category is a free-text field whose options are
# configured per project/company (GC_TRANSMITTAL_CATEGORIES is only the seeded default), so the
# stored value's exact casing is never guaranteed beyond matching one of the configured labels.
OUT_ELIGIBLE_TRANSMITTAL_CATEGORIES = {"for review", "for information"}


def document_transmittal_purpose(document):
    """The distribution purpose implied by the Document's own transmittal_category, or None if
    it is not classified for transmission ("Not sent", "Available", unset, or any other value).
    Shared by the OUT eligibility check and the frontend's Purpose-prefill (LOT 3E-1C1 section
    19) -- kept here as the single canonical mapping rather than duplicated ad hoc."""
    category = (document.transmittal_category or "").strip().lower()
    if category not in OUT_ELIGIBLE_TRANSMITTAL_CATEGORIES:
        return None
    return "FOR_REVIEW" if category == "for review" else "FOR_INFORMATION"


def owner_scope_of_document(document):
    """project_id of a Document, via its Folder (None for a company/freelancer-library
    document, which can never be transmitted now that Transmittal is project-only)."""
    return document.folder.project_id


def validate_item_scope(transmittal, document_revision):
    """Reject a DocumentRevision whose owning Folder is a different project (or no project at
    all -- a company/library document) than the Transmittal's own project (LOT 3C amendment
    section 1/8)."""
    project_id = owner_scope_of_document(document_revision.document)
    if transmittal.project_id != project_id:
        raise ValidationError(
            "This document revision does not belong to the transmittal's own project."
        )


def supplier_out_eligible(document_revision):
    """L3F.1 sections 8/9/10: the Supplier gate for OUT distribution, evaluated against THIS
    exact DocumentRevision's own SupplierSurveillance (OneToOne, never "most recent").

    FOR_INFORMATION carries no mandatory Surveillance (established L3B/L3D/L3F semantic) -- the
    gate is satisfied without one. Otherwise: CLOSED + REVIEWED only; every other Surveillance
    state (none yet, DRAFT, UNDER_REVIEW, READY_FOR_CLOSURE, or CLOSED with COMMENTED/REJECTED,
    i.e. new_revision_required=True) is NOT eligible. Read-only: never writes to
    SupplierSurveillance.
    """
    document = document_revision.document
    if document.supplier_transmittal_category == document.SupplierTransmittalCategory.FOR_INFORMATION:
        return True
    surveillance = SupplierSurveillance.objects.filter(
        document_revision=document_revision
    ).first()
    if surveillance is None:
        return False
    return (
        surveillance.status == SupplierSurveillance.Status.CLOSED
        and surveillance.result == SupplierSurveillance.Result.REVIEWED
    )


def validate_item_eligibility(transmittal, document_revision):
    """Full eligibility check for adding (or re-validating at finalization) one item."""
    validate_item_scope(transmittal, document_revision)
    document = document_revision.document

    if transmittal.direction == transmittal.Direction.OUT:
        if document.document_origin == DocumentOrigin.INTERNAL:
            # L3F.1 section 7: preserved exactly, unchanged since LOT 3C/3E-1C1.
            if not document_revision.revision_label:
                raise ValidationError(
                    "This document revision is not internally approved and cannot be "
                    "distributed."
                )
            if document_transmittal_purpose(document) is None:
                raise ValidationError(
                    "This document is not classified for transmission (For review / For "
                    "information) and cannot be distributed."
                )
        elif document.document_origin == DocumentOrigin.SUPPLIER:
            if document_transmittal_purpose(document) is None:
                raise ValidationError(
                    "This document is not classified for transmission (For review / For "
                    "information) and cannot be distributed."
                )
            if not supplier_out_eligible(document_revision):
                raise ValidationError(
                    "This supplier document revision's review is not complete (or resulted "
                    "in Commented/Rejected) and cannot be distributed to the Client."
                )
        else:
            raise ValidationError(
                "Only internal-origin or eligible supplier-origin documents may be "
                "distributed through an outgoing transmittal."
            )
    else:
        if document.document_origin != DocumentOrigin.SUPPLIER:
            raise ValidationError(
                "Only supplier-origin documents may be recorded through an incoming "
                "transmittal."
            )


def build_item_evidence_snapshot(document_revision, distribution_purpose):
    """Frozen per-item evidence (LOT 3C section 12): document permanent reference, revision
    label, file name, file SHA-256, distribution purpose. Read-only: never writes to
    DocumentRevision, which stays the single source of that same evidence."""
    document = document_revision.document
    if (
        transmittal_direction_requires_hash(document)
        and not document_revision.file_sha256
    ):
        # Unreachable under the current architecture: services.document_revision.
        # record_approval_evidence always computes and freezes file_sha256 in the SAME
        # transaction as revision_label, for every approved revision (including legacy ones
        # with no hash at approval time). Kept as an explicit, clean failure rather than
        # fabricating cryptographic evidence that was never actually computed (LOT 3C
        # section 13) -- see the LOT 3C report for this finding.
        raise ValidationError(
            {
                "file_sha256": (
                    "This approved revision has no recorded file hash. This should not "
                    "happen under the current approval evidence architecture; the legacy "
                    "revision must be remediated before it can be transmitted."
                )
            }
        )
    return {
        "schema_version": 1,
        "document": {
            "id": document.pk,
            "reference": document.full_document_number,
            "file_name": document.file_name,
        },
        "revision": {
            "id": document_revision.pk,
            "version_number": document_revision.version_number,
            "revision_label": document_revision.revision_label,
            # D13: the external issuer's own revision designation (IN items, primarily).
            "external_revision": document_revision.external_revision,
            "file_name": document_revision.file_name,
            "file_sha256": document_revision.file_sha256,
        },
        "distribution_purpose": distribution_purpose,
    }


def transmittal_direction_requires_hash(document):
    """Only an OUT item's evidence needs a cryptographic hash (an approved internal
    revision); an IN item has no internal-approval evidence to freeze."""
    return document.document_origin == DocumentOrigin.INTERNAL
