from pathlib import Path

from dateutil.relativedelta import relativedelta
from django.core.files.base import ContentFile
from django.db.models import Q
from django.template.loader import render_to_string
from weasyprint import HTML

from companyprofileapis.models import Opportunity
from companyprofileapis.services.offer_line_items import (
    calculate_line_amounts,
    calculate_offer_totals,
)
from ecpmpapp.constants import (
    PLACEHOLDER_LOGO_URL,
    UPLOAD_PATH_COMPANY,
    UPLOAD_PATH_FREELANCER,
)
from project_management.models import Document, Folder
from project_management.utils import format_currency_amount, get_currency_from_record


OFFER_DOCUMENT_FOLDER_NAME = "Opportunities & Offers"
LEGACY_OFFER_DOCUMENT_FOLDER_NAME = "Opportunities and Offers"
OFFER_DOCUMENT_FOLDER_SYSTEM_KEY = "opportunities_and_offers"
OFFER_DOCUMENT_TYPE = "Offer"


def _owner_scope(opportunity):
    if opportunity.company_profile_id and not opportunity.freelancer_id:
        return (
            {"company_profile": opportunity.company_profile},
            f"{opportunity.company_profile_id}_company_root_folder",
        )
    if opportunity.freelancer_id and not opportunity.company_profile_id:
        return (
            {"freelancer": opportunity.freelancer},
            f"{opportunity.freelancer_id}_freelancer_root_folder",
        )
    raise ValueError(
        f"Offer {opportunity.id} must belong to exactly one company or freelancer."
    )


def get_or_create_offer_document_folder(opportunity):
    owner_scope, root_name = _owner_scope(opportunity)
    root_folder, _ = Folder.objects.get_or_create(
        parent=None,
        name=root_name,
        **owner_scope,
    )

    offer_folder = Folder.objects.filter(
        parent=root_folder,
        system_key=OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
        **owner_scope,
    ).first()
    if offer_folder:
        return offer_folder

    offer_folder = Folder.objects.filter(
        parent=root_folder,
        name__in=(
            OFFER_DOCUMENT_FOLDER_NAME,
            LEGACY_OFFER_DOCUMENT_FOLDER_NAME,
        ),
        **owner_scope,
    ).first()
    if offer_folder:
        if not offer_folder.system_key:
            offer_folder.system_key = OFFER_DOCUMENT_FOLDER_SYSTEM_KEY
            offer_folder.save(update_fields=["system_key", "updated_at"])
        elif offer_folder.system_key != OFFER_DOCUMENT_FOLDER_SYSTEM_KEY:
            raise ValueError(
                "The Opportunities & Offers folder is already reserved by another system feature."
            )
        return offer_folder

    offer_folder, created = Folder.objects.get_or_create(
        parent=root_folder,
        name=OFFER_DOCUMENT_FOLDER_NAME,
        defaults={
            **owner_scope,
            "system_key": OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
        },
    )
    if not created and not offer_folder.system_key:
        offer_folder.system_key = OFFER_DOCUMENT_FOLDER_SYSTEM_KEY
        offer_folder.save(update_fields=["system_key", "updated_at"])
    elif (
        not created
        and offer_folder.system_key != OFFER_DOCUMENT_FOLDER_SYSTEM_KEY
    ):
        raise ValueError(
            "The Opportunities & Offers folder is already reserved by another system feature."
        )
    return offer_folder


def _find_offer_document_folder(opportunity):
    owner_scope, root_name = _owner_scope(opportunity)
    root_folder = Folder.objects.filter(
        parent=None,
        name=root_name,
        **owner_scope,
    ).first()
    if not root_folder:
        return None
    offer_folder = Folder.objects.filter(
        parent=root_folder,
        system_key=OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
        **owner_scope,
    ).first()
    if offer_folder:
        return offer_folder
    return Folder.objects.filter(
        parent=root_folder,
        name__in=(
            OFFER_DOCUMENT_FOLDER_NAME,
            LEGACY_OFFER_DOCUMENT_FOLDER_NAME,
        ),
        **owner_scope,
    ).first()


def get_stored_offer_pdf_document(opportunity):
    if opportunity.offer_pdf_document_id:
        document = (
            Document.objects.filter(pk=opportunity.offer_pdf_document_id)
            .select_related("folder")
            .first()
        )
        if document and _document_belongs_to_offer_owner(document, opportunity):
            return document
    if not opportunity.reference:
        return None
    offer_folder = _find_offer_document_folder(opportunity)
    if not offer_folder:
        return None
    return get_offer_pdf_documents(opportunity, offer_folder).first()


def get_offer_pdf_documents(opportunity, offer_folder=None):
    if not opportunity.reference:
        return Document.objects.none()
    offer_folder = offer_folder or _find_offer_document_folder(opportunity)
    if not offer_folder:
        return Document.objects.none()
    file_name = f"{opportunity.reference}.pdf"
    return (
        Document.objects.filter(folder=offer_folder)
        .filter(
            Q(
                document_type=OFFER_DOCUMENT_TYPE,
                identification_number=opportunity.reference,
            )
            | Q(file_name=file_name)
        )
        .order_by("id")
    )


def _document_belongs_to_offer_owner(document, opportunity):
    if opportunity.company_profile_id:
        return document.folder.company_profile_id == opportunity.company_profile_id
    if opportunity.freelancer_id:
        return document.folder.freelancer_id == opportunity.freelancer_id
    return False


def _format_city(postcode, city):
    if postcode and city:
        return f"{postcode}, {city}"
    return postcode or city or ""


def _issuer_context(opportunity):
    contact = opportunity.created_by
    context = {
        "company_name": "",
        "company_address": "",
        "company_city": "",
        "company_country": "",
        "contact_name": contact.full_name if contact else "",
        "contact_email": contact.email if contact else "",
        "contact_phone": "",
        "logo_path": PLACEHOLDER_LOGO_URL,
        "footer_company_name": "",
        "footer_company_registration_number": "",
        "footer_company_tax_number": "",
    }

    if opportunity.company_profile_id:
        company = opportunity.company_profile
        context.update(
            {
                "company_name": company.company_name or "",
                "company_address": company.company_address or "",
                "company_city": _format_city(
                    company.post_code,
                    company.company_town,
                ),
                "company_country": company.company_country or "",
                "contact_phone": company.company_tell_no or "",
                "logo_path": (
                    company.company_profile_image or PLACEHOLDER_LOGO_URL
                ),
                "footer_company_name": company.company_name or "",
                "footer_company_registration_number": (
                    company.company_registration_no or ""
                ),
                "footer_company_tax_number": (
                    company.company_tax_reference or ""
                ),
            }
        )
    elif opportunity.freelancer_id:
        freelancer = opportunity.freelancer
        context.update(
            {
                "company_name": freelancer.full_name or freelancer.email,
                "company_address": freelancer.street_address or "",
                "company_city": _format_city(
                    freelancer.post_code,
                    freelancer.city,
                ),
                "company_country": freelancer.country or "",
                "contact_phone": freelancer.mobile_phone or "",
                "logo_path": freelancer.user_image or PLACEHOLDER_LOGO_URL,
                "footer_company_name": freelancer.full_name or freelancer.email,
            }
        )

    return context


def _recipient_context(opportunity):
    context = {
        "recipient_name": "",
        "recipient_company_name": "",
        "recipient_address": "",
        "recipient_city": "",
        "recipient_country": "",
        "recipient_phone": "",
        "recipient_vat_number": "",
    }
    recipient_profile = None

    if opportunity.customer_company_id:
        customer = opportunity.customer_company
        context.update(
            {
                "recipient_company_name": customer.company_name or "",
                "recipient_address": customer.company_address or "",
                "recipient_city": _format_city(
                    customer.company_postcode,
                    customer.company_town,
                ),
                "recipient_country": customer.company_country or "",
                "recipient_phone": customer.company_tel_no or "",
            }
        )
        recipient_profile = customer.company_profile
    elif opportunity.customer_user_id:
        customer = opportunity.customer_user
        context.update(
            {
                "recipient_name": " ".join(
                    part for part in (customer.name, customer.surname) if part
                ),
                "recipient_company_name": customer.company_name or "",
                "recipient_address": customer.street or "",
                "recipient_city": _format_city(
                    customer.postcode,
                    customer.city,
                ),
                "recipient_country": customer.country or "",
                "recipient_phone": customer.telephone_no or "",
            }
        )
        recipient_profile = customer.company_profile

    if recipient_profile and recipient_profile.company_tax_reference:
        context["recipient_vat_number"] = (
            recipient_profile.company_tax_reference
        )

    return context


def build_offer_pdf_context(opportunity):
    if opportunity.status not in Opportunity.OFFER_STATUSES:
        raise ValueError("An Offer PDF can only be generated for an Offer record.")
    if not opportunity.reference or not opportunity.reference.startswith("OFF-"):
        raise ValueError("An Offer PDF requires an OFF reference.")
    offer_items = list(opportunity.offer_line_items.all())
    if not offer_items:
        raise ValueError("An Offer PDF requires at least one line item.")

    currency = get_currency_from_record(opportunity)
    issue_date = opportunity.sent_date
    if opportunity.offer_expiry_period and opportunity.offer_expiry_date:
        sign_before = opportunity.offer_expiry_date
    else:
        sign_before = issue_date + relativedelta(months=1) if issue_date else None
    calculation_items = [
        {
            "rate": item.rate,
            "quantity": item.quantity,
            "vat_rate": item.vat_rate,
        }
        for item in offer_items
    ]
    subtotal, vat_amount, total = calculate_offer_totals(calculation_items)
    line_items = []
    for item in offer_items:
        item_subtotal, item_vat, item_total = calculate_line_amounts(
            item.rate,
            item.quantity,
            item.vat_rate,
        )
        quantity = format(item.quantity, "f").rstrip("0").rstrip(".")
        vat_rate = format(item.vat_rate, "f").rstrip("0").rstrip(".")
        line_items.append(
            {
                "description": item.item_name,
                "quantity": quantity or "0",
                "unit_price": format_currency_amount(item.rate, currency),
                "vat_rate": f"{vat_rate or '0'}%",
                "amount_excl_vat": format_currency_amount(
                    item_subtotal,
                    currency,
                ),
                "vat_amount": format_currency_amount(item_vat, currency),
                "amount_incl_vat": format_currency_amount(
                    item_total,
                    currency,
                ),
            }
        )

    return {
        **_issuer_context(opportunity),
        **_recipient_context(opportunity),
        "offer_reference": opportunity.reference,
        "offer_title": opportunity.title,
        "offer_description": opportunity.details or "",
        "issue_date": issue_date.strftime("%d-%m-%Y") if issue_date else "",
        "sign_before": (
            sign_before.strftime("%d-%m-%Y") if sign_before else ""
        ),
        "line_items": line_items,
        "vat_amount": format_currency_amount(vat_amount, currency),
        "subtotal": format_currency_amount(subtotal, currency),
        "total": format_currency_amount(total, currency),
    }


def render_offer_pdf(opportunity):
    html_content = render_to_string(
        "offer_pdf.html",
        build_offer_pdf_context(opportunity),
    )
    return HTML(string=html_content).write_pdf()


def _offer_upload_path(document, file_name):
    if document.folder.company_profile_id:
        return UPLOAD_PATH_COMPANY.format(
            company_profile_id=document.folder.company_profile_id,
            folder_id=document.folder_id,
            filename=file_name,
        )
    if document.folder.freelancer_id:
        return UPLOAD_PATH_FREELANCER.format(
            freelancer_id=document.folder.freelancer_id,
            folder_id=document.folder_id,
            filename=file_name,
        )
    raise ValueError("Offer PDFs require a company or freelancer document folder.")


def get_offer_pdf_storage_path(opportunity, offer_folder=None):
    offer_folder = offer_folder or get_or_create_offer_document_folder(opportunity)
    placeholder = Document(folder=offer_folder)
    return _offer_upload_path(
        placeholder,
        Path(f"{opportunity.reference}.pdf").name,
    )


def store_offer_pdf(opportunity, pdf_bytes, document_status="Draft"):
    offer_folder = get_or_create_offer_document_folder(opportunity)
    file_name = f"{opportunity.reference}.pdf"
    matching_documents = list(get_offer_pdf_documents(opportunity, offer_folder))

    authoritative_document = None
    if opportunity.offer_pdf_document_id:
        authoritative_document = (
            Document.objects.filter(
                pk=opportunity.offer_pdf_document_id,
                folder=offer_folder,
            )
            .select_related("folder")
            .first()
        )
    document = (
        authoritative_document
        or (matching_documents[0] if matching_documents else None)
        or Document(folder=offer_folder)
    )
    duplicates = [
        candidate
        for candidate in matching_documents
        if candidate.pk != document.pk
    ]
    for duplicate in duplicates:
        if duplicate.file:
            duplicate.file.delete(save=False)
        duplicate.delete()

    if document.pk and document.file:
        document.file.delete(save=False)

    document.document_type = OFFER_DOCUMENT_TYPE
    document.identification_number = opportunity.reference
    document.file_name = file_name
    document.author = opportunity.created_by
    document.created_by = opportunity.created_by
    document.status = document_status
    safe_file_name = Path(file_name).name
    upload_path = _offer_upload_path(document, safe_file_name)
    storage = document.file.storage
    if storage.exists(upload_path):
        storage.delete(upload_path)
    document.file.name = storage.save(upload_path, ContentFile(pdf_bytes))
    document.save()
    Opportunity.objects.filter(pk=opportunity.pk).update(
        offer_pdf_document=document
    )
    opportunity.offer_pdf_document = document
    opportunity.documents.add(document)
    was_existing = bool(authoritative_document or matching_documents)
    return document, "updated" if was_existing else "created"


def create_or_replace_offer_pdf(opportunity, document_status="Draft"):
    return store_offer_pdf(
        opportunity,
        render_offer_pdf(opportunity),
        document_status=document_status,
    )
