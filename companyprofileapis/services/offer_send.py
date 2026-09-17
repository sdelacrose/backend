import zipfile
from io import BytesIO
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction

from companyprofileapis.services.offer_pdf import (
    get_offer_pdf_documents,
    get_offer_pdf_storage_path,
    get_or_create_offer_document_folder,
    render_offer_pdf,
    store_offer_pdf,
)
from companyprofileapis.services.opportunity_lifecycle import (
    get_offer_send_state,
)
from companyprofileapis.services.opportunity_history import (
    OpportunityHistoryReason,
    prepare_opportunity_history,
)
from project_management.models import Document


OFFER_INCLUDE_MODE_OFFER_ONLY = "offer_only"
OFFER_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS = (
    "offer_with_supporting_documents"
)
OFFER_INCLUDE_MODES = {
    OFFER_INCLUDE_MODE_OFFER_ONLY,
    OFFER_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
}


def _safe_archive_name(name, default_name):
    normalized = Path(str(name or "").replace("\\", "/")).name.strip()
    return normalized or default_name


def _unique_archive_name(file_name, used_names):
    path = Path(file_name)
    candidate = file_name
    suffix = 2
    while candidate.lower() in used_names:
        candidate = f"{path.stem}_{suffix}{path.suffix}"
        suffix += 1
    used_names.add(candidate.lower())
    return candidate


def _read_document(document):
    if not document.file:
        raise ValueError(f"Linked document {document.id} has no file.")
    with document.file.open("rb") as document_file:
        return document_file.read()


def _document_belongs_to_offer_owner(document, opportunity):
    if opportunity.company_profile_id:
        return document.folder.company_profile_id == opportunity.company_profile_id
    if opportunity.freelancer_id:
        return document.folder.freelancer_id == opportunity.freelancer_id
    return False


def _supporting_documents(opportunity, generated_document_ids):
    documents = list(
        opportunity.documents.select_related(
            "folder",
            "folder__company_profile",
            "folder__freelancer",
        ).order_by("id")
    )
    supporting_documents = []
    for document in documents:
        if document.id in generated_document_ids:
            continue
        if (
            document.document_type == "Offer"
            and document.identification_number == opportunity.reference
        ):
            continue
        if not _document_belongs_to_offer_owner(document, opportunity):
            raise ValueError(
                f"Linked document {document.id} does not belong to the Offer owner."
            )
        supporting_documents.append(document)
    return supporting_documents


def _build_offer_package_bytes(
    opportunity,
    offer_pdf_bytes,
    include_mode,
    generated_document_ids,
):
    used_names = set()
    package_buffer = BytesIO()
    with zipfile.ZipFile(
        package_buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        offer_file_name = f"{opportunity.reference}.pdf"
        archive.writestr(
            _unique_archive_name(offer_file_name, used_names),
            offer_pdf_bytes,
        )

        if include_mode == OFFER_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS:
            for document in _supporting_documents(
                opportunity,
                generated_document_ids,
            ):
                file_name = _safe_archive_name(
                    document.file_name or document.file.name,
                    f"document-{document.id}",
                )
                archive.writestr(
                    _unique_archive_name(file_name, used_names),
                    _read_document(document),
                )

    package_buffer.seek(0)
    package_bytes = package_buffer.read()
    package_buffer.close()
    return package_bytes


def _package_storage_path(opportunity):
    file_name = f"offer-package-{opportunity.reference}.zip"
    if opportunity.company_profile_id:
        return (
            f"documents/company_{opportunity.company_profile_id}/"
            f"offer_packages/{file_name}"
        )
    if opportunity.freelancer_id:
        return (
            f"documents/freelancer_{opportunity.freelancer_id}/"
            f"offer_packages/{file_name}"
        )
    raise ValueError("An Offer package requires a company or freelancer owner.")


def _snapshot_storage_file(storage, path):
    if not path or not storage.exists(path):
        return None
    with storage.open(path, "rb") as stored_file:
        return stored_file.read()


def _restore_storage_file(storage, path, previous_bytes):
    if storage.exists(path):
        storage.delete(path)
    if previous_bytes is not None:
        storage.save(path, ContentFile(previous_bytes))


def _recipient_email(opportunity):
    if opportunity.customer_company_id:
        return opportunity.customer_company.contact_email or ""
    if opportunity.customer_user_id:
        return opportunity.customer_user.email or ""
    return ""


@transaction.atomic
def create_offer_send_package(opportunity, include_mode, actor=None):
    if include_mode not in OFFER_INCLUDE_MODES:
        raise ValueError(f"Unsupported Offer package mode: {include_mode}")

    target_status, sent_date = get_offer_send_state(opportunity)
    previous_status = opportunity.status
    previous_sent_date = opportunity.sent_date
    opportunity.status = target_status
    opportunity.sent_date = sent_date

    offer_folder = get_or_create_offer_document_folder(opportunity)
    existing_pdf_documents = list(
        get_offer_pdf_documents(opportunity, offer_folder)
    )
    generated_document_ids = {
        document.id for document in existing_pdf_documents
    }
    if opportunity.offer_pdf_document_id:
        generated_document_ids.add(opportunity.offer_pdf_document_id)
    pdf_snapshots = []
    for document in existing_pdf_documents:
        if document.file:
            pdf_snapshots.append(
                (
                    document.file.storage,
                    document.file.name,
                    _read_document(document),
                )
            )

    pdf_storage_path = get_offer_pdf_storage_path(opportunity, offer_folder)
    storage = (
        existing_pdf_documents[0].file.storage
        if existing_pdf_documents
        else Document(folder=offer_folder).file.storage
    )
    package_storage_path = _package_storage_path(opportunity)
    previous_package_bytes = _snapshot_storage_file(
        storage,
        package_storage_path,
    )

    stored_pdf_path = pdf_storage_path
    stored_package_path = package_storage_path
    try:
        offer_pdf_bytes = render_offer_pdf(opportunity)
        package_bytes = _build_offer_package_bytes(
            opportunity,
            offer_pdf_bytes,
            include_mode,
            generated_document_ids,
        )
        offer_pdf_document, _ = store_offer_pdf(
            opportunity,
            offer_pdf_bytes,
            document_status="Issued",
        )
        stored_pdf_path = offer_pdf_document.file.name

        if storage.exists(package_storage_path):
            storage.delete(package_storage_path)
        stored_package_path = storage.save(
            package_storage_path,
            ContentFile(package_bytes),
        )

        prepare_opportunity_history(
            opportunity,
            OpportunityHistoryReason.OFFER_ISSUED,
            actor or opportunity.created_by,
        )
        opportunity.save(update_fields=["status", "sent_date", "updated_at"])
    except Exception:
        paths_to_clear = {
            pdf_storage_path,
            stored_pdf_path,
            *(path for _, path, _ in pdf_snapshots),
        }
        for path in paths_to_clear:
            if path and storage.exists(path):
                storage.delete(path)
        for snapshot_storage, path, previous_bytes in pdf_snapshots:
            snapshot_storage.save(path, ContentFile(previous_bytes))
        _restore_storage_file(
            storage,
            package_storage_path,
            previous_package_bytes,
        )
        if (
            stored_package_path != package_storage_path
            and storage.exists(stored_package_path)
        ):
            storage.delete(stored_package_path)
        opportunity.status = previous_status
        opportunity.sent_date = previous_sent_date
        raise

    package_file_name = f"offer-package-{opportunity.reference}.zip"
    return {
        "file_name": package_file_name,
        "file_url": storage.url(stored_package_path),
        "include_mode": include_mode,
        "offer_reference": opportunity.reference,
        "offer_pdf_document_id": offer_pdf_document.id,
        "recipient_email": _recipient_email(opportunity),
        "email_subject": f"Offer: {opportunity.reference}",
        "status": opportunity.status,
        "sent_date": opportunity.sent_date,
    }
