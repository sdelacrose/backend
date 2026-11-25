"""GED-WF.3: manual confirmation of an incoming (IN) Transmittal, and the matching of received
documents to the canonical Supplier documents of the project.

Nothing here adds a model or a migration: the confirmation reuses the existing IN branch of
`finalize_transmittal` (RECEIVED event, received_at, frozen evidence), and the duplicate rules rely on
what already exists (unique supplier reference per project + company, one item per revision per
transmittal) completed by checks on the *normalised* reference, so "DWG-001" and "dwg 001" are never
two different documents or two different receptions.

A document may legitimately be received several times (another revision, a retransmission): the
event of reception is identified by the supplier's own transmittal reference, never by a file hash,
and there is deliberately no global document + revision uniqueness.
"""

import logging
import re

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from project_management.models import Document, DocumentOrigin, Folder, Transmittal, TransmittalReceiptLine
from project_management.services.transmittal_eligibility import validate_item_eligibility
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item

logger = logging.getLogger(__name__)


class ReceiptAnomalies(Exception):
    """The confirmation is blocked: carries the list of understandable anomalies."""

    def __init__(self, anomalies):
        super().__init__("The receipt cannot be confirmed.")
        self.anomalies = anomalies


def normalize_reference(value):
    """Formatting-insensitive form of a reference: case, spaces and punctuation are ignored."""
    return re.sub(r"[^0-9a-z]", "", str(value or "").casefold())


def project_supplier_documents(project_id):
    return Document.objects.filter(
        folder__project_id=project_id,
        document_origin=DocumentOrigin.SUPPLIER,
        withdrawn_at__isnull=True,
    )


def find_duplicate_supplier_documents(project_id, company_id, reference, exclude_id=None):
    """Canonical Supplier documents of this project and issuer company whose reference equals
    `reference` once formatting is ignored."""
    norm = normalize_reference(reference)
    if not norm or not company_id:
        return []
    candidates = project_supplier_documents(project_id).filter(
        external_issuer_company_id=company_id, identification_number__isnull=False,
    )
    if exclude_id:
        candidates = candidates.exclude(pk=exclude_id)
    return [d for d in candidates if normalize_reference(d.identification_number) == norm]


def find_reception_matches(transmittal, reference, external_revision=None):
    """Possible canonical documents for a received reference (same project, same issuer company).
    Several matches are returned as such: the caller never picks one automatically."""
    reference = (reference or "").strip()
    wanted_revision = (external_revision or "").strip().casefold()
    matches = []
    for document in find_duplicate_supplier_documents(
        transmittal.project_id, transmittal.external_company_id, reference
    ):
        revisions = list(
            document.revisions.order_by("-version_number").values(
                "id", "version_number", "external_revision", "file_name"
            )
        )
        known = next(
            (r for r in revisions
             if wanted_revision and (r["external_revision"] or "").strip().casefold() == wanted_revision),
            None,
        )
        exact = (document.identification_number or "").strip().casefold() == reference.casefold()
        matches.append({
            "document_id": document.pk,
            "reference": document.identification_number,
            "file_name": document.file_name,
            "match": "exact" if exact else "formatting",
            "latest_external_revision": revisions[0]["external_revision"] if revisions else None,
            "revisions": revisions,
            "known_revision_id": known["id"] if known else None,
        })
    return matches


def receipt_anomalies(transmittal, actor=None):
    """Blocking anomalies of an IN draft, as [{code, message, item_id?, document_id?}]."""
    anomalies = []

    def add(code, message, item=None):
        entry = {"code": code, "message": message}
        if item is not None:
            entry["item_id"] = item.pk
            entry["document_id"] = item.document_revision.document_id
        anomalies.append(entry)

    if transmittal.direction != Transmittal.Direction.IN:
        add("not_incoming", "Only an incoming transmittal can be confirmed as received.")
        return anomalies
    if transmittal.status != Transmittal.Status.DRAFT:
        add("already_received", "This transmittal has already been confirmed.")
        return anomalies
    if not transmittal.external_company_id:
        add("company_required", "The issuing company is required.")
    if not (transmittal.transmittal_number or "").strip():
        add("reference_required", "The supplier's transmittal reference is required.")

    items = list(transmittal.items.select_related("document_revision", "document_revision__document"))
    lines = list(transmittal.receipt_lines.select_related("matched_document"))
    client_lines_exist = transmittal.client_return_lines.exists()  # GED-WF.4.3.1: client returns are not supplier lines
    if not items and not lines and not client_lines_exist:
        add("no_items", "At least one received document is required.")

    # The same reception must not be recorded twice, whatever the formatting of the reference.
    norm_reference = normalize_reference(transmittal.transmittal_number)
    if norm_reference and transmittal.external_company_id:
        others = Transmittal.objects.filter(
            project_id=transmittal.project_id, direction=Transmittal.Direction.IN,
            external_company_id=transmittal.external_company_id,
            status=Transmittal.Status.ISSUED,
        ).exclude(pk=transmittal.pk)
        if any(normalize_reference(o.transmittal_number) == norm_reference for o in others):
            add("duplicate_reception", "This reception has already been recorded with the same supplier reference.")

    seen_canonical = {}
    seen_revision = set()
    for item in items:
        revision = item.document_revision
        document = revision.document
        try:
            validate_item_eligibility(transmittal, revision)
        except ValidationError as error:
            add("item_not_eligible", " ".join(error.messages), item)
            continue
        if (
            transmittal.external_company_id
            and document.external_issuer_company_id != transmittal.external_company_id
        ):
            add(
                "issuer_mismatch",
                f"{document.identification_number or document.file_name}: the document belongs to another issuing company.",
                item,
            )
        norm = normalize_reference(document.identification_number)
        if norm:
            first = seen_canonical.setdefault(norm, document.pk)
            if first != document.pk:
                add("duplicate_canonical_document",
                    f"{document.identification_number}: the same reference exists as two different documents.", item)
            for other in find_duplicate_supplier_documents(
                transmittal.project_id, document.external_issuer_company_id,
                document.identification_number, exclude_id=document.pk,
            ):
                add("duplicate_canonical_document",
                    f"{document.identification_number}: another document of the project has the same reference.", item)
                break
        key = (document.pk, (revision.external_revision or "").strip().casefold())
        if revision.external_revision and key in seen_revision:
            add("duplicate_in_transmittal",
                f"{document.identification_number or document.file_name}: the same revision is listed twice.", item)
        seen_revision.add(key)

    # GED-WF.3.1: prepared lines are re-validated now (the project may have changed since they were
    # prepared): same issuer rules, same duplicate rules, file still present.
    seen_lines = set()
    for line in lines:
        label = line.reference or line.file_name

        def add_line(code, message, _line=line):
            anomalies.append({"code": code, "message": message, "line_id": _line.pk})

        if not line.file or not line.file.storage.exists(line.file.name):
            add_line("line_file_missing", f"{label}: the prepared file is no longer available.")
        norm = normalize_reference(line.reference)
        if not norm:
            add_line("line_document_invalid", f"{label}: the supplier reference is required.")
            continue
        line_key = (norm, normalize_reference(line.external_revision))
        if line_key in seen_lines:
            add_line("duplicate_in_transmittal", f"{label}: the same revision is listed twice.")
        seen_lines.add(line_key)
        if line.decision == TransmittalReceiptLine.Decision.NEW_DOCUMENT:
            if norm in seen_canonical:
                add_line("duplicate_canonical_document",
                         f"{label}: the same reference is already listed in this transmittal.")
            seen_canonical.setdefault(norm, None)
            if not line.allow_duplicate_reference and find_duplicate_supplier_documents(
                transmittal.project_id, transmittal.external_company_id, line.reference,
            ):
                add_line("duplicate_canonical_document",
                         f"{label}: a document with this reference now exists for this supplier.")
        else:
            document = line.matched_document
            if document is None or document.withdrawn_at is not None or document.document_origin != DocumentOrigin.SUPPLIER \
                    or not document.folder_id or document.folder.project_id != transmittal.project_id:
                add_line("line_document_invalid", f"{label}: the selected document is no longer available.")
            elif transmittal.external_company_id and document.external_issuer_company_id != transmittal.external_company_id:
                add_line("issuer_mismatch", f"{label}: the document belongs to another issuing company.")
    if client_lines_exist:
        from project_management.services.transmittal_client_return import client_line_anomalies

        anomalies.extend(client_line_anomalies(transmittal, actor))
    return anomalies


def _line_document_payload(transmittal, line):
    return {
        "folder": None,  # filled by the caller (project root folder)
        "document_origin": DocumentOrigin.SUPPLIER,
        "external_issuer_company": transmittal.external_company_id,
        "identification_number": line.reference,
        "file_name": line.reference,
        **({"allow_duplicate_reference": "true"} if line.allow_duplicate_reference else {}),
    }


def _materialize_line(transmittal, line, actor, created_files):
    """Create what a prepared line stands for, with the existing services: the canonical document
    (unless the user selected an existing one), its revision, then the definitive item."""
    from project_management.serializers.document import DocumentSerializer
    from project_management.services.document_revision import add_document_revision

    if line.decision == TransmittalReceiptLine.Decision.NEW_DOCUMENT:
        folder = Folder.objects.filter(project_id=transmittal.project_id, name=f"{transmittal.project_id}_root_folder").first()
        if folder is None:
            raise ReceiptAnomalies([{"code": "line_document_invalid", "line_id": line.pk,
                                     "message": "The project document folder was not found."}])
        payload = _line_document_payload(transmittal, line)
        payload["folder"] = folder.pk
        serializer = DocumentSerializer(data=payload, context={"user": actor})
        if not serializer.is_valid():
            raise ReceiptAnomalies([{"code": "line_document_invalid", "line_id": line.pk,
                                     "message": f"{line.reference}: {serializer.errors}"}])
        document = serializer.save()
    else:
        document = Document.objects.select_for_update().get(pk=line.matched_document_id)
    with line.file.open("rb") as handle:
        content = ContentFile(handle.read(), name=line.file_name)
    revision = add_document_revision(
        document, content, actor, comment="Received via IN Transmittal reception.",
        external_revision=line.external_revision or None,
    )
    created_files.append((revision.file.storage, revision.file.name))
    add_transmittal_item(transmittal, revision, None, actor)


def _discard_created_files(created_files):
    """A database rollback does not undo storage writes: the definitive files created for this
    attempt are removed again (best effort, traced), so a failed confirmation leaves nothing."""
    for storage, name in created_files:
        try:
            storage.delete(name)
        except Exception:  # pragma: no cover - traced, the rollback itself must not fail
            logger.exception("File created by a failed receipt confirmation could not be removed: %s", name)


@transaction.atomic
def confirm_incoming_receipt(transmittal, actor):
    """Atomic: blocking anomalies -> nothing is written; otherwise the prepared lines become real
    documents / revisions / items, then the existing IN finalization records the RECEIVED event
    (actor + date), freezes the evidence and flips the status. Any failure rolls the database back
    and removes the files written meanwhile; a second confirmation is refused by the
    already-received check, without creating an event. The pending files are removed only once the
    transaction is committed (post_delete of the lines, on commit)."""
    locked = Transmittal.objects.select_for_update().get(pk=transmittal.pk)
    anomalies = receipt_anomalies(locked, actor)
    if anomalies:
        raise ReceiptAnomalies(anomalies)
    created_files = []
    try:
        # GED-WF.4.3.1: client return lines (a parallel path, the supplier lines below are untouched). Their
        # ClientIssues are locked in ascending id order before anything is written.
        client_lines = list(locked.client_return_lines.select_related("client_issue__document_revision__document__folder").order_by("client_issue_id", "id"))
        if client_lines:
            from project_management.services.transmittal_client_return import client_lines_metadata, lock_client_issues

            lock_client_issues(client_lines)
        for line in list(locked.receipt_lines.select_related("matched_document").order_by("id")):
            try:
                _materialize_line(locked, line, actor, created_files)
            except ValidationError as error:
                raise ReceiptAnomalies([{
                    "code": "line_revision_rejected", "line_id": line.pk,
                    "message": f"{line.reference}: {' '.join(error.messages)}",
                }])
        if client_lines:
            finalized = finalize_transmittal(
                locked, actor, allow_empty_items=True, event_metadata=client_lines_metadata(client_lines, actor))
        else:  # the supplier-only call is exactly the one of GED-WF.3
            finalized = finalize_transmittal(locked, actor)
        for line in list(locked.receipt_lines.all()):
            line.delete()
        if client_lines:
            from project_management.services.transmittal_client_return import confirm_client_lines

            try:
                confirm_client_lines(finalized, actor, created_files)
            except ValidationError as error:
                raise ReceiptAnomalies([{"code": "client_line_return_rejected", "message": " ".join(error.messages)}])
        return finalized
    except BaseException:
        _discard_created_files(created_files)
        raise


def _line_document_error(transmittal, document):
    if document is None or document.withdrawn_at is not None or document.document_origin != DocumentOrigin.SUPPLIER \
            or not document.folder_id or document.folder.project_id != transmittal.project_id \
            or document.external_issuer_company_id != transmittal.external_company_id:
        raise ValidationError("The selected document does not belong to this project and issuing company.")


@transaction.atomic
def add_receipt_line(transmittal, actor, reference, uploaded_file, external_revision="",
                     matched_document_id=None, allow_duplicate_reference=False):
    """Prepare a received supplier document on an incoming draft. Creates NOTHING canonical: no
    document, no revision, no item -- only the line and its pending file."""
    transmittal = Transmittal.objects.select_for_update().get(pk=transmittal.pk)
    if transmittal.direction != Transmittal.Direction.IN or transmittal.status != Transmittal.Status.DRAFT:
        raise ValidationError("Documents are only prepared on an incoming draft transmittal.")
    if not transmittal.external_company_id:
        raise ValidationError("The issuing company is required before preparing a document.")
    reference = (reference or "").strip()
    external_revision = (external_revision or "").strip()
    if not reference:
        raise ValidationError("The supplier reference is required.")
    if uploaded_file is None:
        raise ValidationError("The received file is required.")
    if matched_document_id:
        document = Document.objects.select_related("folder").filter(pk=matched_document_id).first()
        _line_document_error(transmittal, document)
        decision = TransmittalReceiptLine.Decision.NEW_REVISION
    else:
        document = None
        decision = TransmittalReceiptLine.Decision.NEW_DOCUMENT
        if not allow_duplicate_reference and find_duplicate_supplier_documents(
            transmittal.project_id, transmittal.external_company_id, reference,
        ):
            raise ValidationError(
                "A document with this reference already exists for this supplier in this project. "
                "Select it, or confirm that a new document must be created.",
                code="duplicate_supplier_reference",
            )
    norm, norm_revision = normalize_reference(reference), normalize_reference(external_revision)
    for other in transmittal.receipt_lines.all():
        if normalize_reference(other.reference) == norm and normalize_reference(other.external_revision) == norm_revision:
            raise ValidationError("This document and revision are already prepared on this transmittal.")
    line = TransmittalReceiptLine(
        transmittal=transmittal, reference=reference, external_revision=external_revision,
        file_name=getattr(uploaded_file, "name", "") or reference, matched_document=document,
        decision=decision, allow_duplicate_reference=bool(allow_duplicate_reference) and not document,
        created_by=actor,
    )
    line.file.save(line.file_name, uploaded_file, save=False)
    try:
        line.save()
    except BaseException:
        # the file is already in storage and the database cannot take it back: remove it again
        _discard_created_files([(line.file.storage, line.file.name)])
        raise
    return line
