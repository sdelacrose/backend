import requests
import zipfile
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from django.db import connection
from django.utils import timezone
from django.db.models import Prefetch, Q
from django.core.files.base import ContentFile
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.template import loader

from django.template.loader import render_to_string
from weasyprint import HTML
from io import BytesIO
from pathlib import Path
from django.conf import settings
from django.utils.text import slugify

from project_management.models import (
    Folder,
    Document,
    Project,
    PurchaseOrder,
    PurchaseOrderLineItem,
)
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION, CUSTOMER_DEMAND_DOCUMENT_TYPE
from models.models import Connection, ConnectionUser, User, CompanyProfile, Preference
from project_management.models import ProjectTeamMembers
from ecpmpapp.alt_messages import (
    plain_messages_for_send_approval_email,
    plain_messages_for_send_pending_approval_reminder_email,
    plain_message_for_send_project_welcome_email,
    plain_message_for_send_invitation_email_to_external_user,
    plain_messages_for_expiring_po_reminder
)
from ecpmpapp.constants import (
    UserType, StatusType,
    MS_GRAPH_ITEM_CHILDREN_URL,
    MS_GRAPH_DRIVE_ITEM_CONTENT_URL,
    PLACEHOLDER_LOGO_URL,
    PO_PDF_LOGO_URL,
)
from project_management.currency_converter import ECBCurrencyConverter

from ecpmpapp.utils import (
    get_frontend_url_for_solution, 
    get_user_active_solution,
)

INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY = "invoice_only"
INVOICE_EXPORT_INCLUDE_MODE_WITH_TIMESHEETS = "invoice_with_timesheets"
INVOICE_EXPORT_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS = (
    "invoice_with_timesheets_and_supporting_documents"
)
INVOICE_EXPORT_INCLUDE_MODES = {
    INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
    INVOICE_EXPORT_INCLUDE_MODE_WITH_TIMESHEETS,
    INVOICE_EXPORT_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
}

OBJECT_ASSESSMENT_RECORD_TITLE_PREFIXES = {
    "Photos": "Photo",
    "Notes": "Note",
    "Sketch": "Sketch",
    "Audio": "Audio",
    "Documents": "Document",
}


def get_project_id_from_request(request):
    """Return the canonical or legacy project identifier from a DRF request."""
    request_data = getattr(request, "data", {}) or {}
    query_params = getattr(request, "query_params", {}) or {}
    return (
        request_data.get("project")
        or query_params.get("project")
        or query_params.get("projectId")
    )


def generate_object_assessment_record_title(queryset_or_model, project, record_type):
    prefix = OBJECT_ASSESSMENT_RECORD_TITLE_PREFIXES.get(
        record_type,
        record_type or "Record",
    )
    count = queryset_or_model.filter(
        project=project,
        record_type=record_type,
    ).count()
    return f"{prefix} {count + 1}"


def build_structure(item, parent_folder, site_id, headers, request_user, project=None, company=None, freelancer=None):
    if 'folder' in item:
        folder, created = Folder.objects.get_or_create(
            project=project, company_profile=company, freelancer=freelancer,
            name=item['name'],
            parent=parent_folder
        )
        # Fetch child items of the folder
        children_url = MS_GRAPH_ITEM_CHILDREN_URL.format(site_id=site_id, item_id=item["id"])
        children_response = requests.get(children_url, headers=headers)
        if children_response.status_code != 200:
            return {"error": f"Failed to fetch items for folder {item['id']}"}
        children_data = children_response.json().get('value', [])
        for child in children_data:
            build_structure(child, folder, site_id, headers, request_user, project=project, company=company,
                            freelancer=freelancer)
    else:
        # Create document
        file_url = MS_GRAPH_DRIVE_ITEM_CONTENT_URL.format(site_id=site_id, item_id=item["id"])
        file_response = requests.get(file_url, headers=headers)
        if file_response.status_code == 200:
            file_name = item['name']
            file_content = file_response.content
            document, created = Document.objects.get_or_create(
                folder=parent_folder,
                file=ContentFile(file_content, name=file_name),
                file_name=file_name,
                defaults={
                    'uploaded_at': timezone.now()
                },
                author=request_user
            )
            if not created:
                document.uploaded_at = timezone.now()
                document.save()
        else:
            return {"error": f"Failed to fetch file content for {item['id']}"}


def get_contact_project_ids(contact):
    # Get project ids where the contact is a client or a project team member
    client_projects = Project.objects.filter(client=contact).values_list('project_name', flat=True)
    team_projects = Project.objects.filter(external_users__member=contact).values_list('project_name', flat=True)

    # Combine and remove duplicates using `set()`
    all_project_names = set(client_projects) | set(team_projects)

    return list(all_project_names)


def get_root_folder_name_from_bill(bill):
    root_folder_name = None
    if bill.project:
        if bill.project.user.is_freelancer:
            root_folder_name = f"{bill.project.user.id}_freelancer_root_folder"
        else:
            root_folder_name = f"{bill.project.user.company.id}_company_root_folder"
    elif bill.company_profile:
        root_folder_name = f"{bill.company_profile.id}_company_root_folder"
    elif bill.freelancer:
        root_folder_name = f"{bill.freelancer.id}_freelancer_root_folder"
    return root_folder_name


def get_invoices_folder_from_bill(bill):
    root_folder_name = get_root_folder_name_from_bill(bill)
    root_folder = Folder.objects.filter(name=root_folder_name).first()
    if not root_folder:
        raise ValueError(f"Company level root folder for bill ID {bill.id} not found.")

    invoices_folder = Folder.objects.filter(parent=root_folder, name="invoices").first()
    if not invoices_folder:
        raise ValueError(f"'Invoices' folder under root folder for bill ID {bill.id} not found.")

    return invoices_folder, root_folder_name


def get_root_folder_name_from_po(po):
    """Resolve document root folder name for a purchase order (mirrors bill scope)."""
    if po.project_id:
        if po.project.user.is_freelancer:
            return f"{po.project.user.id}_freelancer_root_folder"
        return f"{po.project.user.company.id}_company_root_folder"
    if po.company_profile_id:
        return f"{po.company_profile.id}_company_root_folder"
    if po.freelancer_id:
        return f"{po.freelancer.id}_freelancer_root_folder"
    return None


def _get_or_create_purchase_orders_subfolder(root_folder):
    owner_kwargs = {}
    if root_folder.project_id:
        owner_kwargs["project"] = root_folder.project
    elif root_folder.company_profile_id:
        owner_kwargs["company_profile"] = root_folder.company_profile
    elif root_folder.freelancer_id:
        owner_kwargs["freelancer"] = root_folder.freelancer
    folder, _ = Folder.objects.get_or_create(
        parent=root_folder,
        name="purchase_orders",
        defaults=owner_kwargs,
    )
    return folder


def get_purchase_order_pdf_base_name(po):
    root_folder_name = get_root_folder_name_from_po(po)
    if not root_folder_name:
        raise ValueError(f"No root folder scope for purchase order {po.id}")
    return f"purchase_order_{root_folder_name}_po_{po.id}", root_folder_name


def get_change_notice_pdf_base_name(cn):
    """File base name for a standalone change-notice PDF (original PO + this CN only)."""
    if not cn.source_po_id:
        raise ValueError(f"No source PO for change notice {cn.id}")
    src = cn.source_po
    root_folder_name = get_root_folder_name_from_po(src)
    if not root_folder_name:
        raise ValueError(f"No root folder scope for change notice {cn.id}")
    return f"purchase_order_{root_folder_name}_po_{src.id}_cn_{cn.id}", root_folder_name


def get_stored_purchase_order_pdf_document(po):
    """Return the auto-generated PO PDF document if it exists (same lookup as create)."""
    root_folder_name = get_root_folder_name_from_po(po)
    if not root_folder_name:
        return None
    root_folder = Folder.objects.filter(name=root_folder_name).first()
    if not root_folder:
        return None
    po_folder = Folder.objects.filter(parent=root_folder, name="purchase_orders").first()
    if not po_folder:
        return None
    base_name, _ = get_purchase_order_pdf_base_name(po)
    slug = base_name.split(".")[0]
    return (
        Document.objects.filter(folder=po_folder, file__icontains=slug)
        .exclude(file__icontains="_cn_")
        .first()
    )


def get_stored_change_notice_pdf_document(cn):
    """Return the auto-generated PDF for this change notice only (not the root PO bundle)."""
    if not cn.source_po_id:
        return None
    root_folder_name = get_root_folder_name_from_po(cn.source_po)
    if not root_folder_name:
        return None
    root_folder = Folder.objects.filter(name=root_folder_name).first()
    if not root_folder:
        return None
    po_folder = Folder.objects.filter(parent=root_folder, name="purchase_orders").first()
    if not po_folder:
        return None
    base_name, _ = get_change_notice_pdf_base_name(cn)
    return Document.objects.filter(
        folder=po_folder,
        file__icontains=base_name.split(".")[0],
    ).first()


def _get_logo_path_for_po(po):
    if po.company_profile_id:
        cp = po.company_profile
        return cp.company_profile_image if cp.company_profile_image else PLACEHOLDER_LOGO_URL
    if po.freelancer_id:
        u = po.freelancer
        return u.user_image if u.user_image else PLACEHOLDER_LOGO_URL
    if po.project_id:
        u = po.project.user
        if getattr(u, "is_freelancer", False):
            return u.user_image if u.user_image else PLACEHOLDER_LOGO_URL
        comp = getattr(u, "company", None)
        if comp:
            return comp.company_profile_image if comp.company_profile_image else PLACEHOLDER_LOGO_URL
    return PLACEHOLDER_LOGO_URL


def _po_pdf_issuer_context(po, request_user):
    """Issuer block + bank details for PO PDF (aligned with invoice PDF issuer logic)."""
    context = {
        "company_name": "",
        "address": "",
        "city": "",
        "country": "",
        "contact_number": "",
        "company_contact_person": getattr(request_user, "full_name", None) if request_user else None,
        "logo_path": _get_logo_path_for_po(po),
        "bank_details": {},
    }
    if po.project_id:
        proj_user = po.project.user
        if getattr(proj_user, "is_freelancer", False):
            context["company_name"] = proj_user.full_name
            context["address"] = proj_user.street_address
            post_code = proj_user.post_code
            city = f"{post_code}, {proj_user.city}" if post_code else proj_user.city
            context["city"] = city
            context["country"] = proj_user.country
            context["contact_number"] = proj_user.mobile_phone
            fp = getattr(proj_user, "freelancer_profile", None)
            if fp:
                context["bank_details"] = {
                    "bank_name": fp.bank_name,
                    "bank_iban": fp.iban,
                    "bank_bic": fp.bic,
                    "bank_address": getattr(fp, "bank_address", None) or "",
                    "bank_city": "",
                }
        elif proj_user.company:
            comp = proj_user.company
            context["company_name"] = comp.company_name
            context["address"] = comp.company_address
            post_code = comp.post_code
            city = f"{post_code}, {comp.company_town}" if post_code else comp.company_town
            context["city"] = city
            context["country"] = comp.company_country
            context["contact_number"] = comp.company_tell_no
            bank_obj = comp.bank_details.first()
            if bank_obj:
                context["bank_details"] = get_bank_details(bank_obj)
    elif po.company_profile_id:
        cp = po.company_profile
        context["company_name"] = cp.company_name
        context["address"] = cp.company_address
        post_code = cp.post_code
        city = f"{post_code}, {cp.company_town}" if post_code else cp.company_town
        context["city"] = city
        context["country"] = cp.company_country
        context["contact_number"] = cp.company_tell_no
        bank_obj = cp.bank_details.first()
        if bank_obj:
            context["bank_details"] = get_bank_details(bank_obj)
    elif po.freelancer_id:
        u = po.freelancer
        context["company_name"] = u.full_name
        context["address"] = u.street_address
        post_code = u.post_code
        city = f"{post_code}, {u.city}" if post_code else u.city
        context["city"] = city
        context["country"] = u.country
        context["contact_number"] = u.mobile_phone
        fp = getattr(u, "freelancer_profile", None)
        if fp:
            context["bank_details"] = {
                "bank_name": fp.bank_name,
                "bank_iban": fp.iban,
                "bank_bic": fp.bic,
                "bank_address": getattr(fp, "bank_address", None) or "",
                "bank_city": "",
            }
    if request_user:
        context["company_contact_email"] = getattr(request_user, "email", None)
    if "contact_number" in context:
        context["company_contact_phone"] = context.get("contact_number")
    return context


def _po_pdf_footer_company_context(root_po):
    """Footer registration / tax lines (same idea as invoice PDF)."""
    issuer_company_profile = None
    if getattr(root_po, "company_profile", None):
        issuer_company_profile = root_po.company_profile
    elif (
        getattr(root_po, "project", None)
        and getattr(root_po.project, "user", None)
        and getattr(root_po.project.user, "company", None)
    ):
        issuer_company_profile = root_po.project.user.company

    out = {
        "footer_company_name": None,
        "footer_company_type": None,
        "footer_company_capital": None,
        "footer_company_reg_no": None,
        "footer_company_tax_no": None,
    }
    if issuer_company_profile:
        out["footer_company_name"] = issuer_company_profile.company_name
        out["footer_company_reg_no"] = getattr(
            issuer_company_profile, "company_registration_no", None
        )
        out["footer_company_tax_no"] = getattr(
            issuer_company_profile, "company_tax_reference", None
        )
    return out


def _po_pdf_quantize_money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _po_pdf_format_quantity(quantity) -> str:
    text = format(Decimal(str(quantity or 0)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _po_pdf_line_item_vat_amount(amount_excl_vat, vat_rate) -> Decimal:
    excl = _po_pdf_quantize_money(amount_excl_vat)
    rate = _po_pdf_quantize_money(vat_rate)
    return (excl * rate / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _po_pdf_build_line_item_rows(po, currency):
    """Build PDF rows from PO/CN line items (unit price, qty, per-item VAT)."""
    rows = []
    for item in po.line_items.all():
        excl = _po_pdf_quantize_money(item.total)
        vat_rate = item.vat_rate
        vat_amt = _po_pdf_line_item_vat_amount(excl, vat_rate)
        incl = _po_pdf_quantize_money(excl + vat_amt)
        description = (item.item_name or "").strip()
        if not description and getattr(item, "available_product", None):
            description = item.available_product.name
        if not description:
            description = f"Item {item.line_number}"
        rows.append(
            {
                "description": description,
                "quantity": _po_pdf_format_quantity(item.quantity),
                "unit_price": format_currency_amount(float(item.rate or 0), currency),
                "vat_rate": vat_rate,
                "amount_excl_vat": format_currency_amount(float(excl), currency),
                "vat_amount": format_currency_amount(float(vat_amt), currency),
                "amount_incl_vat": format_currency_amount(float(incl), currency),
            }
        )
    return rows


def _po_pdf_descendants_change_notices(root):
    """BFS of change notices under a root PO (same order as serializer)."""
    out = []
    seen = {root.id}
    queue = list(root.change_notices.all().order_by("po_date", "id"))
    while queue:
        child = queue.pop(0)
        if child.id in seen:
            continue
        seen.add(child.id)
        out.append(child)
        queue.extend(
            ch
            for ch in child.change_notices.all().order_by("po_date", "id")
            if ch.id not in seen
        )
    return out


def _po_pdf_ancestor_chain_root_first(instance):
    """Ancestors from root toward parent of ``instance`` (excludes self)."""
    chain_rev = []
    cur_id = instance.source_po_id
    guard = 0
    while cur_id and guard < 64:
        src = PurchaseOrder.objects.filter(pk=cur_id).first()
        if src is None:
            break
        chain_rev.append(src)
        cur_id = src.source_po_id
        guard += 1
    return list(reversed(chain_rev))


def _po_pdf_history_relation_label(relation_type):
    if relation_type == "source_purchase_order":
        return "Original PO"
    if relation_type == "prior_change_notice":
        return "Prior change notice"
    if relation_type == "change_notice":
        return "Change notice"
    return "Linked document"


def _po_pdf_build_history_full(root_po, currency):
    """
    Outgoing root PO history with CN deltas — matches the PO view form table.
    """
    if root_po.po_type != PurchaseOrder.POType.OUTGOING:
        return None
    cns = sorted(
        _po_pdf_descendants_change_notices(root_po),
        key=lambda p: (p.po_date or date.min, p.id),
    )
    if not cns:
        return None

    prev_excl = float(root_po.amount_excl_vat or 0)
    prev_incl = float(root_po.amount_incl_vat or 0)
    rows = []
    sum_cn_delta_incl = Decimal("0")

    for cn in cns:
        tip_excl = float(cn.amount_excl_vat or 0)
        tip_incl = float(cn.amount_incl_vat or 0)
        if cn.change_amount_excl_vat is not None:
            change_excl = float(cn.change_amount_excl_vat)
        else:
            change_excl = tip_excl - prev_excl
        change_incl = tip_incl - prev_incl
        change_vat = change_incl - change_excl
        sum_cn_delta_incl += _po_pdf_quantize_money(change_incl)
        rows.append(
            {
                "relation": _po_pdf_history_relation_label("change_notice"),
                "reference": cn.reference or str(cn.id),
                "status": cn.status or "",
                "change_excl": format_currency_amount(change_excl, currency),
                "change_vat": format_currency_amount(change_vat, currency),
                "change_incl": format_currency_amount(change_incl, currency),
                "revised_incl": format_currency_amount(tip_incl, currency),
            }
        )
        prev_excl = tip_excl
        prev_incl = tip_incl

    return {
        "mode": "full",
        "title": f"PO history ({len(rows)})",
        "rows": rows,
        "sum_cn_incl": format_currency_amount(float(sum_cn_delta_incl), currency),
        "revised_total": format_currency_amount(prev_incl, currency),
        "show_sum_cn": True,
    }


def _po_pdf_build_history_source(cn, currency):
    """
    Change-notice history showing source / ancestors — matches CN view form.
    """
    ancestors = _po_pdf_ancestor_chain_root_first(cn)
    if not ancestors:
        return None

    rows = []
    for anc in ancestors:
        if anc.document_type == PurchaseOrder.DocumentType.PURCHASE_ORDER:
            relation = "source_purchase_order"
        else:
            relation = "prior_change_notice"
        amount = float(anc.amount_incl_vat or 0)
        rows.append(
            {
                "relation": _po_pdf_history_relation_label(relation),
                "reference": anc.reference or str(anc.id),
                "status": anc.status or "",
                "original_po_amount": format_currency_amount(amount, currency),
            }
        )

    return {
        "mode": "source",
        "title": f"PO history ({len(rows)})",
        "rows": rows,
        "sum_cn_incl": None,
        "revised_total": None,
        "show_sum_cn": False,
    }


def create_pdf_purchase_order(po, request_user=None):
    """
    PDF for a root purchase order (incoming or outgoing) showing that PO's
    line items and items-based totals. Change notices use
    ``create_pdf_change_notice_standalone``.
    """
    if po.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
        return None, "skipped"

    root_po = (
        PurchaseOrder.objects.select_related(
            "project__user__company",
            "project__user__freelancer_profile",
            "company_profile",
            "freelancer__freelancer_profile",
        )
        .prefetch_related(
            "linked_payment_project_phases",
            Prefetch(
                "line_items",
                queryset=PurchaseOrderLineItem.objects.select_related(
                    "available_product"
                ).order_by("line_number", "id"),
            ),
            Prefetch(
                "change_notices",
                queryset=PurchaseOrder.objects.filter(
                    document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE
                ).order_by("po_date", "id"),
            ),
        )
        .get(pk=po.pk)
    )

    currency = (root_po.currency or "").strip() or "€"
    issuer = _po_pdf_issuer_context(root_po, request_user)
    po_date_str = root_po.po_date.strftime("%d-%m-%Y") if root_po.po_date else ""
    issue_date = timezone.now().strftime("%d-%m-%Y")

    phases = []
    for ph in root_po.linked_payment_project_phases.all().order_by("date", "id"):
        due = ph.date.date().strftime("%d-%m-%Y") if getattr(ph, "date", None) else ""
        phases.append(
            {
                "name": ph.name,
                "due_date": due,
                "amount": format_currency_amount(float(ph.amount or 0), currency),
            }
        )

    line_items = _po_pdf_build_line_item_rows(root_po, currency)
    po_history = _po_pdf_build_history_full(root_po, currency)
    subtotal_raw = float(root_po.amount_excl_vat or 0)
    vat_amount_total_raw = float(root_po.vat_amount or 0)
    grand_total_raw = float(root_po.amount_incl_vat or 0)

    footer = _po_pdf_footer_company_context(root_po)

    ctx = {
        "document_label": "Purchase order",
        "po_reference": root_po.reference or str(root_po.id),
        "po_date": po_date_str,
        "issue_date": issue_date,
        "status": root_po.status or "",
        "project_name": root_po.project.project_name if root_po.project_id else "",
        "line_items": line_items,
        "po_history": po_history,
        "subtotal": format_currency_amount(subtotal_raw, currency),
        # Do not show a single global VAT % — rates are per line item.
        "vat_rate": None,
        "vat_amount": format_currency_amount(vat_amount_total_raw, currency),
        "grand_total": format_currency_amount(grand_total_raw, currency),
        "grand_total_label": "Total (incl. VAT)",
        "source_po_reference": None,
        "notes": (root_po.notes or "").strip() or "-",
        "payment_milestones": phases,
        "recipient_name": "",
        "surname": "",
        "recipient_company_name": "",
        "recipient_address": "",
        "recipient_town": "",
        "recipient_country": "",
        "recipient_number": "",
        **issuer,
        **footer,
        "logo_path": PO_PDF_LOGO_URL,
    }

    html_content = render_to_string("purchase_order_pdf.html", ctx)
    pdf_file = BytesIO()
    HTML(string=html_content).write_pdf(pdf_file)
    pdf_file.seek(0)

    root_folder_name = get_root_folder_name_from_po(root_po)
    root_folder = Folder.objects.filter(name=root_folder_name).first()
    if not root_folder:
        raise ValueError(f"Root folder not found for purchase order ID {root_po.id}.")

    po_folder = _get_or_create_purchase_orders_subfolder(root_folder)
    base_name, _ = get_purchase_order_pdf_base_name(root_po)
    slug = base_name.split(".")[0]
    existing_document = (
        Document.objects.filter(folder=po_folder, file__icontains=slug)
        .exclude(file__icontains="_cn_")
        .first()
    )

    file_name = f"{base_name}.pdf"
    if existing_document:
        existing_document.file.save(file_name, ContentFile(pdf_file.read()), save=True)
        existing_document.status = "Draft"
        existing_document.save()
        action = "updated"
        out = existing_document
    else:
        document = Document(
            folder=po_folder,
            document_type="Purchase Order",
            file_name=file_name,
            author=request_user,
            status="Draft",
        )
        document.file.save(file_name, ContentFile(pdf_file.read()))
        document.save()
        out = document
        action = "created"

    pdf_file.close()
    return out, action


def create_pdf_change_notice_standalone(cn, request_user=None):
    """
    PDF for a single change notice: that CN's line items and items-based
    revised totals. Linked source PO is shown in the header only.
    """
    if cn.document_type != PurchaseOrder.DocumentType.CHANGE_NOTICE:
        return None, "skipped"
    if not cn.source_po_id:
        return None, "skipped"

    cn = (
        PurchaseOrder.objects.select_related(
            "source_po__project__user__company",
            "source_po__project__user__freelancer_profile",
            "source_po__company_profile",
            "source_po__freelancer__freelancer_profile",
            "project__user__company",
            "company_profile",
            "freelancer__freelancer_profile",
        )
        .prefetch_related(
            "source_po__linked_payment_project_phases",
            Prefetch(
                "line_items",
                queryset=PurchaseOrderLineItem.objects.select_related(
                    "available_product"
                ).order_by("line_number", "id"),
            ),
        )
        .get(pk=cn.pk)
    )
    source = cn.source_po

    currency = (cn.currency or source.currency or "").strip() or "€"
    issuer = _po_pdf_issuer_context(source, request_user)
    issue_date = timezone.now().strftime("%d-%m-%Y")
    cn_date_str = cn.po_date.strftime("%d-%m-%Y") if cn.po_date else ""

    phases = []
    for ph in source.linked_payment_project_phases.all().order_by("date", "id"):
        due = ph.date.date().strftime("%d-%m-%Y") if getattr(ph, "date", None) else ""
        phases.append(
            {
                "name": ph.name,
                "due_date": due,
                "amount": format_currency_amount(float(ph.amount or 0), currency),
            }
        )

    line_items = _po_pdf_build_line_item_rows(cn, currency)
    po_history = _po_pdf_build_history_source(cn, currency)

    subtotal_raw = float(cn.amount_excl_vat or 0)
    vat_amount_total_raw = float(cn.vat_amount or 0)
    grand_total_raw = float(cn.amount_incl_vat or 0)
    footer = _po_pdf_footer_company_context(source)

    ctx = {
        "document_label": "Change notice",
        "po_reference": cn.reference or str(cn.id),
        "po_date": cn_date_str,
        "issue_date": issue_date,
        "status": cn.status or "",
        "project_name": source.project.project_name if source.project_id else "",
        "line_items": line_items,
        "po_history": po_history,
        "subtotal": format_currency_amount(subtotal_raw, currency),
        # Do not show a single global VAT % — rates are per line item.
        "vat_rate": None,
        "vat_amount": format_currency_amount(vat_amount_total_raw, currency),
        "grand_total": format_currency_amount(grand_total_raw, currency),
        "grand_total_label": "Revised total (incl. VAT)",
        "source_po_reference": source.reference or str(source.id),
        "notes": (cn.notes or source.notes or "").strip() or "-",
        "payment_milestones": phases,
        "recipient_name": "",
        "surname": "",
        "recipient_company_name": "",
        "recipient_address": "",
        "recipient_town": "",
        "recipient_country": "",
        "recipient_number": "",
        **issuer,
        **footer,
        "logo_path": PO_PDF_LOGO_URL,
    }

    html_content = render_to_string("purchase_order_pdf.html", ctx)
    pdf_file = BytesIO()
    HTML(string=html_content).write_pdf(pdf_file)
    pdf_file.seek(0)

    root_folder_name = get_root_folder_name_from_po(source)
    root_folder = Folder.objects.filter(name=root_folder_name).first()
    if not root_folder:
        raise ValueError(f"Root folder not found for change notice ID {cn.id}.")

    po_folder = _get_or_create_purchase_orders_subfolder(root_folder)
    base_name, _ = get_change_notice_pdf_base_name(cn)

    existing_document = Document.objects.filter(
        folder=po_folder,
        file__icontains=base_name.split(".")[0],
    ).first()

    file_name = f"{base_name}.pdf"
    if existing_document:
        existing_document.file.save(file_name, ContentFile(pdf_file.read()), save=True)
        existing_document.status = "Draft"
        existing_document.save()
        action = "updated"
        out = existing_document
    else:
        document = Document(
            folder=po_folder,
            document_type="Change Notice",
            file_name=file_name,
            author=request_user,
            status="Draft",
        )
        document.file.save(file_name, ContentFile(pdf_file.read()))
        document.save()
        out = document
        action = "created"

    pdf_file.close()
    return out, action


def get_logo_path(bill):
    if bill.company_profile:
        return bill.company_profile.company_profile_image if bill.company_profile.company_profile_image else PLACEHOLDER_LOGO_URL
    if bill.freelancer:
        return bill.freelancer.user_image if bill.freelancer.user_image else PLACEHOLDER_LOGO_URL
    if bill.project:
        if bill.project.user.is_freelancer:
            return bill.project.user.user_image if bill.project.user.user_image else PLACEHOLDER_LOGO_URL
        else:
            return bill.project.user.company.company_profile_image if bill.project.user.company.company_profile_image else PLACEHOLDER_LOGO_URL
    return ""

def get_abbreviation(value, config, key):
    if value and config:
        config_values = getattr(config, key, [])
        if isinstance(config_values, list):
            for config_value in config_values:
                if ":" in config_value:
                    parts = config_value.split(":")
                    if len(parts) == 2 and parts[0] == value:
                        return parts[1]
    return value


def ensure_customer_demand_document_type(document_types):
    types = list(document_types or [])
    if any(doc_type.split(":")[0] == "Customer Demand" for doc_type in types):
        return types, False

    other_index = next(
        (index for index, doc_type in enumerate(types) if doc_type.split(":")[0] == "Other"),
        len(types),
    )
    types.insert(other_index, CUSTOMER_DEMAND_DOCUMENT_TYPE)
    return types, True


def get_invoice_reference_display(invoice):
    return invoice.reference_number or str(invoice.id)


def get_billing_document_label(invoice):
    return "Credit Note" if getattr(invoice, "document_type", "invoice") == "credit_note" else "Invoice"


def _round_export_hours(value):
    return round(float(value or 0), 2)


def _sanitize_export_filename(name, default_name):
    normalized = str(name or "").strip() or default_name
    normalized = normalized.replace("\\", "_").replace("/", "_")
    return normalized


def _ensure_unique_export_filename(file_name, used_names):
    candidate = file_name
    path = Path(file_name)
    counter = 2

    while candidate.lower() in used_names:
        candidate = f"{path.stem}_{counter}{path.suffix}"
        counter += 1

    used_names.add(candidate.lower())
    return candidate


def _read_document_bytes(document):
    with document.file.open("rb") as document_file:
        return document_file.read()


def _get_monthly_timesheet_logo_for_user(user):
    company = getattr(user, "company", None)
    if company and company.company_profile_image:
        return company.company_profile_image
    if getattr(user, "user_image", None):
        return user.user_image
    return None


def _filter_monthly_preview_for_invoice_scope(
    preview,
    project_id=None,
    purchase_order_id=None,
):
    next_preview = deepcopy(preview)
    next_weeks = []
    month_total_hours = 0.0

    for week in preview.get("weeks", []):
        filtered_activities = []
        activity_hours_by_date = {}

        for activity in week.get("activities", []):
            if activity.get("is_invoiceable", True) is False:
                continue
            if project_id and (activity.get("project") or {}).get("id") != project_id:
                continue
            if (
                purchase_order_id
                and (activity.get("purchase_order") or {}).get("id")
                != purchase_order_id
            ):
                continue

            next_entries = []
            row_total_hours = 0.0
            for entry in activity.get("entries", []):
                entry_hours = _round_export_hours(entry.get("hours"))
                next_entries.append({**entry, "hours": entry_hours})
                row_total_hours += entry_hours
                entry_date = entry.get("entry_date")
                if entry_date:
                    activity_hours_by_date[entry_date] = _round_export_hours(
                        activity_hours_by_date.get(entry_date, 0) + entry_hours
                    )

            row_total_hours = _round_export_hours(row_total_hours)
            if row_total_hours <= 0:
                continue

            filtered_activities.append(
                {
                    **activity,
                    "entries": next_entries,
                    "row_total_hours": row_total_hours,
                }
            )

        if not filtered_activities:
            continue

        next_days = [
            {
                **day,
                "is_locked": False,
                "locked_day_detail": None,
            }
            for day in week.get("days", [])
        ]
        next_daily_totals = []
        for day in next_days:
            if not day.get("is_in_month"):
                continue
            next_daily_totals.append(
                {
                    "date": day.get("date"),
                    "hours": _round_export_hours(
                        activity_hours_by_date.get(day.get("date"), 0)
                    ),
                }
            )

        week_total_hours = _round_export_hours(
            sum(float(day.get("hours") or 0) for day in next_daily_totals)
        )
        month_total_hours += week_total_hours
        next_weeks.append(
            {
                **week,
                "days": next_days,
                "activities": filtered_activities,
                "daily_warning_map": {},
                "daily_totals": next_daily_totals,
                "week_total_hours": week_total_hours,
            }
        )

    next_preview["weeks"] = next_weeks
    next_preview["month_total_hours"] = _round_export_hours(month_total_hours)
    return next_preview


def _build_monthly_timesheet_summary_rows(preview):
    month_label = timezone.datetime(
        int(preview["year"]),
        int(preview["month"]),
        1,
    ).strftime("%B %Y")
    summary_map = {}

    for week in preview.get("weeks", []):
        for activity in week.get("activities", []):
            row_signature = activity.get("row_signature")
            source_type = str(activity.get("source_type") or "activity")
            target_id = (activity.get("target") or {}).get("id")
            invoiceable_state = str(activity.get("is_invoiceable", True))
            key = (
                row_signature
                or f"{source_type}:{target_id}:{invoiceable_state}:{activity.get('activity_code')}:{activity.get('activity_name')}"
            )
            if key not in summary_map:
                summary_map[key] = {
                    "activity_name": activity.get("activity_name") or "-",
                    "activity_code": activity.get("activity_code") or "-",
                    "total_hours": 0.0,
                    "month_label": month_label,
                }
            summary_map[key]["total_hours"] = _round_export_hours(
                summary_map[key]["total_hours"] + float(activity.get("row_total_hours") or 0)
            )

    return sorted(
        summary_map.values(),
        key=lambda row: str(row["activity_name"]).lower(),
    )


def build_monthly_timesheet_pdf_html(preview, locale="en", logo=None, user_name=None):
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    month_label = timezone.datetime(
        int(preview["year"]),
        int(preview["month"]),
        1,
    ).strftime("%B %Y")
    summary_rows = _build_monthly_timesheet_summary_rows(preview)
    week_sections = []

    def _normalize_preview_date(value):
        if not value:
            return None
        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
            return value
        return timezone.datetime.strptime(str(value), "%Y-%m-%d").date()

    for week in preview.get("weeks", []):
        visible_days = [day for day in week.get("days", []) if day.get("is_in_month")]
        if not visible_days:
            continue

        first_visible_day = visible_days[0].get("date")
        last_visible_day = visible_days[-1].get("date")
        if first_visible_day and last_visible_day:
            first_visible_date = _normalize_preview_date(first_visible_day)
            last_visible_date = _normalize_preview_date(last_visible_day)
            if first_visible_day == last_visible_day:
                detail_range_label = first_visible_date.strftime("%d %b %Y")
            else:
                detail_range_label = (
                    f"{first_visible_date.strftime('%d %b %Y')} - "
                    f"{last_visible_date.strftime('%d %b %Y')}"
                )
        else:
            detail_range_label = (
                f"{week.get('week_start_date', '')} - {week.get('week_end_date', '')}"
            )

        day_headers = []
        for day in visible_days:
            current_date = _normalize_preview_date(day["date"])
            day_headers.append(
                {
                    "label": f"{day_labels[current_date.weekday()]} {current_date.day}",
                }
            )

        activity_rows = []
        for activity in week.get("activities", []):
            entry_map = {
                entry.get("entry_date"): _round_export_hours(entry.get("hours"))
                for entry in activity.get("entries", [])
            }
            activity_rows.append(
                {
                    "activity_name": activity.get("activity_name") or "-",
                    "activity_code": activity.get("activity_code") or "-",
                    "cells": [
                        entry_map.get(day.get("date"), 0) for day in visible_days
                    ],
                    "row_total_hours": _round_export_hours(
                        activity.get("row_total_hours")
                    ),
                }
            )

        daily_totals_map = {
            total.get("date"): _round_export_hours(total.get("hours"))
            for total in week.get("daily_totals", [])
        }
        totals_row = [daily_totals_map.get(day.get("date"), 0) for day in visible_days]
        week_sections.append(
            {
                "detail_range_label": detail_range_label,
                "day_headers": day_headers,
                "activity_rows": activity_rows,
                "totals_row": totals_row,
                "week_total_hours": _round_export_hours(week.get("week_total_hours")),
                "empty_colspan": len(visible_days) + 2,
            }
        )

    return render_to_string(
        "monthly_timesheet_pdf.html",
        {
            "preview": preview,
            "month_label": month_label,
            "summary_rows": summary_rows,
            "week_sections": week_sections,
            "logo": logo,
            "user_name": user_name or "-",
            "generated_at": timezone.now().strftime("%Y-%m-%d %H:%M"),
        },
    )


def create_monthly_timesheet_pdf_bytes(
    submission,
    preview,
    locale="en",
):
    html_content = build_monthly_timesheet_pdf_html(
        preview,
        locale=locale,
        logo=_get_monthly_timesheet_logo_for_user(submission.user),
        user_name=submission.user.full_name or submission.user.email,
    )
    pdf_file = BytesIO()
    HTML(string=html_content).write_pdf(pdf_file)
    pdf_file.seek(0)
    pdf_bytes = pdf_file.read()
    pdf_file.close()
    return pdf_bytes


def _get_invoice_scope_filtered_preview(invoice, submission, preview):
    return _filter_monthly_preview_for_invoice_scope(
        preview,
        project_id=invoice.project_id,
        purchase_order_id=invoice.purchase_order_id,
    )


def _get_invoice_purchase_order_monthly_line_item_values(invoice, line_item):
    if not getattr(invoice, "purchase_order_id", None):
        return None

    submission = getattr(line_item, "monthly_timesheet_submission", None)
    if not submission:
        return None

    from project_management.serializers.time_expense import _build_monthly_timesheet_preview

    preview = _build_monthly_timesheet_preview(
        submission.user,
        submission.month,
        submission.year,
    )
    filtered_preview = _get_invoice_scope_filtered_preview(
        invoice,
        submission,
        preview,
    )
    quantity = _round_export_hours(filtered_preview.get("month_total_hours"))
    if float(quantity or 0) <= 0:
        return None

    collaborator_name = submission.user.full_name or submission.user.email
    po_reference = getattr(invoice.purchase_order, "reference", None)
    description = (
        f"Monthly timesheet {submission.month:02d}/{submission.year} - "
        f"{collaborator_name}"
    )
    if po_reference:
        description = f"{description} - {po_reference}"

    return {
        "description": description,
        "quantity": quantity,
        "date_range": (
            f"{submission.period_start.strftime('%d-%m-%Y')} - "
            f"{submission.period_end.strftime('%d-%m-%Y')}"
        ),
        "collaborator_name": collaborator_name,
    }


def _get_invoice_supporting_documents_from_preview(preview):
    document_map = {}
    for week in preview.get("weeks", []):
        for activity in week.get("activities", []):
            if activity.get("is_invoiceable", True) is False:
                continue
            for document in activity.get("linked_documents", []):
                document_id = int(document["id"])
                document_map[document_id] = document
    if not document_map:
        return []
    return list(Document.objects.filter(id__in=document_map.keys()).order_by("id"))


def _build_monthly_timesheet_export_filename(submission, purchase_order=None):
    month_label = f"{int(submission.year):04d}-{int(submission.month):02d}"
    user_slug = slugify(submission.user.full_name or submission.user.email or "user")
    user_slug = user_slug or f"user-{submission.user_id}"
    po_slug = ""
    if purchase_order:
        po_label = purchase_order.reference or f"po-{purchase_order.id}"
        po_slug = f"-{slugify(po_label) or f'po-{purchase_order.id}'}"
    return f"monthly-timesheet-{month_label}-{user_slug}{po_slug}.pdf"


def create_invoice_export_package(
    invoice,
    include_mode=INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
    request_user=None,
):
    from project_management.serializers.time_expense import _build_monthly_timesheet_preview
    from project_management.serializers.billing import get_effective_invoice_documents

    if include_mode not in INVOICE_EXPORT_INCLUDE_MODES:
        raise ValueError(f"Unsupported invoice export include mode: {include_mode}")

    invoice_pdf_document, _ = create_pdf_invoice_by_bill(invoice, request_user)
    invoices_folder, root_folder_name = get_invoices_folder_from_bill(invoice)
    used_package_names = set()
    package_buffer = BytesIO()

    with zipfile.ZipFile(
        package_buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        invoice_pdf_name = _sanitize_export_filename(
            invoice_pdf_document.file_name or Path(invoice_pdf_document.file.name).name,
            f"{slugify(get_invoice_reference_display(invoice)) or f'invoice-{invoice.id}'}.pdf",
        )
        archive.writestr(
            _ensure_unique_export_filename(invoice_pdf_name, used_package_names),
            _read_document_bytes(invoice_pdf_document),
        )

        if include_mode != INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY:
            submission_line_items = list(
                invoice.line_items.filter(
                    monthly_timesheet_submission__isnull=False
                )
                .select_related("monthly_timesheet_submission__user", "monthly_timesheet_submission__user__company")
                .order_by("id")
            )
            seen_submission_ids = set()
            supporting_documents_by_id = {}

            for line_item in submission_line_items:
                submission = line_item.monthly_timesheet_submission
                if not submission or submission.id in seen_submission_ids:
                    continue
                seen_submission_ids.add(submission.id)

                preview = _build_monthly_timesheet_preview(
                    submission.user,
                    submission.month,
                    submission.year,
                )
                filtered_preview = _get_invoice_scope_filtered_preview(
                    invoice,
                    submission,
                    preview,
                )
                filtered_preview["submission"] = {
                    "status": submission.status,
                }
                if (
                    not filtered_preview.get("weeks")
                    or _round_export_hours(filtered_preview.get("month_total_hours")) <= 0
                ):
                    continue

                timesheet_pdf_bytes = create_monthly_timesheet_pdf_bytes(
                    submission,
                    filtered_preview,
                )
                archive.writestr(
                    _ensure_unique_export_filename(
                        _build_monthly_timesheet_export_filename(
                            submission,
                            invoice.purchase_order,
                        ),
                        used_package_names,
                    ),
                    timesheet_pdf_bytes,
                )

                if include_mode == INVOICE_EXPORT_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS:
                    for document in _get_invoice_supporting_documents_from_preview(
                        filtered_preview
                    ):
                        supporting_documents_by_id[document.id] = document

            if include_mode == INVOICE_EXPORT_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS:
                for document in get_effective_invoice_documents(invoice):
                    supporting_documents_by_id[document.id] = document
                for document in supporting_documents_by_id.values():
                    document_file_name = _sanitize_export_filename(
                        document.file_name or Path(document.file.name).name,
                        f"document-{document.id}",
                    )
                    archive.writestr(
                        _ensure_unique_export_filename(
                            document_file_name,
                            used_package_names,
                        ),
                        _read_document_bytes(document),
                    )

    package_buffer.seek(0)
    package_bytes = package_buffer.read()
    package_buffer.close()

    package_slug = (
        slugify(get_invoice_reference_display(invoice))
        or f"invoice-{invoice.id}"
    )
    document_label_slug = (
        "credit-note"
        if getattr(invoice, "document_type", "invoice") == "credit_note"
        else "invoice"
    )
    document_display_label = (
        "Credit Note"
        if getattr(invoice, "document_type", "invoice") == "credit_note"
        else "Invoice"
    )
    package_base_name = (
        f"invoice_package_{root_folder_name}_bill_{invoice.id}_{include_mode}"
    )
    package_file_name = f"{document_label_slug}-package-{package_slug}.zip"
    existing_document = Document.objects.filter(
        folder=invoices_folder,
        file__icontains=package_base_name,
    ).first()

    if existing_document:
        existing_document.file.save(
            f"{package_base_name}.zip",
            ContentFile(package_bytes),
            save=True,
        )
        existing_document.document_type = f"{document_display_label} Package"
        existing_document.file_name = package_file_name
        existing_document.status = "Draft"
        existing_document.author = request_user
        existing_document.save()
        return existing_document, "updated"

    document = Document(
        folder=invoices_folder,
        document_type=f"{document_display_label} Package",
        file_name=package_file_name,
        author=request_user,
        status="Draft",
    )
    document.file.save(
        f"{package_base_name}.zip",
        ContentFile(package_bytes),
    )
    document.save()
    return document, "created"


def create_pdf_invoice_by_bill(bill, request_user=None):
    """
    Generate a PDF for an invoice/bill and store it in the invoices folder.
    Supports both `Invoice` and `Billing` models and enriches the template
    context with VAT breakdown, offer reference, and footer company details.
    """
    line_items = []
    subtotal_raw = 0.0
    vat_total_raw = 0.0
    has_po_scoped_timesheet_line_items = False
    line_item_collaborator_names = []

    currency = get_currency_from_record(bill)
    is_invoice_model = hasattr(bill, "document_type")
    is_credit_note = getattr(bill, "document_type", "invoice") == "credit_note"
    source_invoice = getattr(bill, "source_invoice", None)

    for line_item in bill.line_items.all():
        # Unit price: support both InvoiceLineItem.price and Billing LineItem.rate
        if hasattr(line_item, "price") and line_item.price is not None:
            unit_price = line_item.price
        else:
            unit_price = getattr(line_item, "rate", 0) or 0

        quantity = getattr(line_item, "quantity", 0) or 0
        scoped_monthly_values = _get_invoice_purchase_order_monthly_line_item_values(
            bill,
            line_item,
        )
        if scoped_monthly_values:
            quantity = scoped_monthly_values["quantity"]
            has_po_scoped_timesheet_line_items = True
            collaborator_name = scoped_monthly_values.get("collaborator_name")
            if collaborator_name:
                line_item_collaborator_names.append(collaborator_name)
        amount = (unit_price or 0) * quantity

        # Description: prefer time_card/expense_card, fall back to description
        description = None
        if scoped_monthly_values:
            description = scoped_monthly_values["description"]
        elif hasattr(line_item, "time_card") and line_item.time_card:
            description = line_item.time_card.name
        elif hasattr(line_item, "expense_card") and line_item.expense_card:
            description = line_item.expense_card.name
        else:
            description = getattr(line_item, "description", "") or ""

        date_range = (
            scoped_monthly_values.get("date_range")
            if scoped_monthly_values
            else getattr(line_item, "date_range", None)
        )

        # VAT rate per line: prefer explicit rate, fall back to bill.vat_rate
        line_vat_rate = None
        if hasattr(line_item, "vat_rate") and getattr(line_item, "vat_rate") is not None:
            line_vat_rate = line_item.vat_rate
        elif getattr(bill, "vat_rate", None) is not None:
            line_vat_rate = bill.vat_rate

        # VAT amount per line: compute from the displayed rate so it stays
        # consistent with the VAT % column and invoice totals. Stored
        # vat_amount can go stale when the invoice VAT rate is changed later.
        if line_vat_rate is not None:
            line_vat_amount_raw = round(
                float(amount) * (float(line_vat_rate) / 100.0),
                2,
            )
        elif (
            hasattr(line_item, "vat_amount")
            and getattr(line_item, "vat_amount") is not None
        ):
            line_vat_amount_raw = line_item.vat_amount
        else:
            line_vat_amount_raw = 0.0

        subtotal_raw += amount
        vat_total_raw += line_vat_amount_raw or 0.0

        line_items.append(
            {
                "description": description,
                "rate": format_currency_amount(unit_price, currency),
                "quantity": quantity,
                "total": format_currency_amount(amount, currency),
                "date_range": date_range,
                "vat_rate": line_vat_rate,
                "vat_amount": format_currency_amount(line_vat_amount_raw, currency)
                if line_vat_amount_raw
                else None,
            }
        )

    # Invoice-level VAT amount and totals
    vat_amount_total_raw = None
    bill_vat_amount = getattr(bill, "vat_amount", None)
    bill_vat_rate = getattr(bill, "vat_rate", None)
    bill_grand_total = getattr(bill, "grand_total", None)

    if has_po_scoped_timesheet_line_items:
        vat_amount_total_raw = vat_total_raw
    elif bill_vat_amount is not None:
        vat_amount_total_raw = bill_vat_amount
    elif bill_grand_total is not None and subtotal_raw and bill_grand_total > subtotal_raw:
        vat_amount_total_raw = bill_grand_total - subtotal_raw
    elif bill_vat_rate is not None:
        vat_amount_total_raw = subtotal_raw * (bill_vat_rate / 100.0)
    else:
        vat_amount_total_raw = vat_total_raw

    vat_rate_for_display = bill_vat_rate
    grand_total_raw = bill_grand_total

    if has_po_scoped_timesheet_line_items:
        grand_total_raw = subtotal_raw + (vat_amount_total_raw or 0.0)
    elif grand_total_raw is None:
        grand_total_raw = subtotal_raw + (vat_amount_total_raw or 0.0)

    linked_credit_notes = []
    total_credit_notes_amount_raw = 0.0
    net_total_after_credit_notes_raw = grand_total_raw

    if is_invoice_model and not is_credit_note:
        linked_credit_note_qs = bill.credit_notes.all().order_by("invoice_date", "id")
        total_credit_notes_amount_raw = sum((credit_note.grand_total or 0.0) for credit_note in linked_credit_note_qs)
        net_total_after_credit_notes_raw = (grand_total_raw or 0.0) + total_credit_notes_amount_raw
        linked_credit_notes = [
            {
                "reference_number": get_invoice_reference_display(credit_note),
                "invoice_date": credit_note.invoice_date.strftime("%d-%m-%Y"),
                "grand_total": format_currency_amount(credit_note.grand_total or 0.0, currency),
                "notes": credit_note.notes or "",
            }
            for credit_note in linked_credit_note_qs
        ]

    # Offer / invoice reference for naming and header
    offer_reference = None
    if hasattr(bill, "invoice_reference") and getattr(bill, "invoice_reference"):
        offer_reference = bill.invoice_reference
    elif hasattr(bill, "invoice_no") and getattr(bill, "invoice_no"):
        offer_reference = bill.invoice_no

    context = {
        "id": bill.id,
        "reference_number": get_invoice_reference_display(bill),
        "document_label": get_billing_document_label(bill),
        "invoice_date": bill.invoice_date.strftime("%d-%m-%Y"),
        "due_date": bill.due_date.strftime("%d-%m-%Y") if getattr(bill, "due_date", None) else "",
        "status": bill.status if hasattr(bill, "status") else "",
        "offer_reference": offer_reference,
        # Date of issue should be the date the PDF is generated (today)
        "issue_date": timezone.now().strftime("%d-%m-%Y"),
        "line_items": line_items,
        "subtotal": format_currency_amount(subtotal_raw, currency),
        "vat_rate": vat_rate_for_display,
        "vat_amount": format_currency_amount(vat_amount_total_raw or 0.0, currency),
        "grand_total": format_currency_amount(grand_total_raw, currency),
        "total_credit_notes_amount": format_currency_amount(total_credit_notes_amount_raw, currency),
        "net_total_after_credit_notes": format_currency_amount(net_total_after_credit_notes_raw, currency),
        "linked_credit_notes": linked_credit_notes,
        "source_invoice_reference": get_invoice_reference_display(source_invoice) if source_invoice else None,
        "logo_path": get_logo_path(bill),
        "notes": bill.notes or "-",
        "bank_details": {},
        "footer_company_name": None,
        "footer_company_type": None,
        "footer_company_capital": None,
        "footer_company_reg_no": None,
        "footer_company_tax_no": None,
        "purchase_order_reference": getattr(
            getattr(bill, "purchase_order", None),
            "reference",
            None,
        ),
        "collaborator_names": [],
    }
    if hasattr(bill, "collaborators"):
        collaborator_names = [
            collaborator.full_name or collaborator.email
            for collaborator in bill.collaborators.all().order_by(
                "first_name",
                "last_name",
                "email",
            )
        ]
        context["collaborator_names"] = list(
            dict.fromkeys(collaborator_names + line_item_collaborator_names)
        )
    if bill.reciepient_user:
        recipient = bill.reciepient_user
        context["recipient_name"] = recipient.name
        context["recipient_email"] = recipient.email
        context["surname"] = recipient.surname
        context["recipient_company_name"] = recipient.company_name
        context["recipient_address"] = recipient.street

        postcode = recipient.postcode
        city = recipient.city

        if postcode and city:
            context["recipient_town"] = f"{postcode}, {city}"
        elif postcode:
            context["recipient_town"] = postcode
        elif city:
            context["recipient_town"] = city
        else:
            context["recipient_town"] = ""

        context["recipient_country"] = recipient.country
        context["recipient_number"] = recipient.telephone_no

    elif bill.reciepient_company:
        context["recipient_name"] = bill.reciepient_company.contact_name
        context["recipient_email"] = bill.reciepient_company.contact_email
        context["surname"] = bill.reciepient_company.contact_surname
        context["recipient_company_name"] = bill.reciepient_company.company_name
        context["recipient_address"] = bill.reciepient_company.company_address

        postcode = bill.reciepient_company.company_postcode
        city = bill.reciepient_company.company_town

        if postcode and city:
            context["recipient_town"] = f"{postcode}, {city}"
        elif postcode:
            context["recipient_town"] = postcode
        elif city:
            context["recipient_town"] = city
        else:
            context["recipient_town"] = ""


        context["recipient_country"] = bill.reciepient_company.company_country
        context["recipient_number"] = bill.reciepient_company.company_tel_no


    if bill.project:
        context["project_name"] = bill.project.project_name
        context[
            "company_name"] = bill.project.user.company.company_name if bill.project.user.company else bill.project.user.full_name
        context["company_contact_person"] = request_user.full_name if request_user else None
        post_code = bill.project.user.company.post_code if bill.project.user.company else bill.project.user.post_code
        city = f"{post_code}, {bill.project.user.company.company_town}" if post_code else bill.project.user.company.company_town
        context["city"] = city
        context[
            "address"] = bill.project.user.company.company_address if bill.project.user.company else bill.project.user.street_address
        context[
            "country"] = bill.project.user.company.company_country if bill.project.user.company else bill.project.user.country
        context[
            "contact_number"] = bill.project.user.company.company_tell_no if bill.project.user.company else bill.project.user.mobile_phone
        if bill.project.user.company:
            bank_obj = bill.project.user.company.bank_details.first()
            if bank_obj:
                context["bank_details"] = get_bank_details(bank_obj)
            context["company_reg_no"] = bill.project.user.company.company_registration_no or "-"
            context["company_tax_no"] = bill.project.user.company.company_tax_reference or "-"
        else:
            bank_details = {}
            bank_details["bank_name"] = bill.project.user.freelancer_profile.bank_name
            bank_details["bank_iban"] = bill.project.user.freelancer_profile.iban if bill.project.user.freelancer_profile.iban else None
            bank_details["bank_bic"] = bill.project.user.freelancer_profile.bic
            context["bank_details"] = bank_details
    elif bill.freelancer:
        context["company_name"] = bill.freelancer.full_name
        context["address"] = bill.freelancer.street_address
        post_code = bill.freelancer.post_code
        city = f"{post_code}, {bill.freelancer.city}" if post_code else bill.freelancer.city
        context["city"] = city
        context["country"] = bill.freelancer.country
        context["contact_number"] = bill.freelancer.mobile_phone
        bank_details = {}
        bank_details["bank_name"] = bill.freelancer.freelancer_profile.bank_name
        bank_details["bank_iban"] = bill.freelancer.freelancer_profile.iban if bill.freelancer.freelancer_profile.iban else None
        bank_details["bank_bic"] = bill.freelancer.freelancer_profile.bic
        bank_details["bank_address"] = bill.freelancer.freelancer_profile.bank_address
        context["bank_details"] = bank_details
    elif bill.company_profile:
        context["company_contact_person"] = request_user.full_name if request_user else None
        context["company_name"] = bill.company_profile.company_name
        context["address"] = bill.company_profile.company_address
        post_code = bill.company_profile.post_code
        city = f"{post_code}, {bill.company_profile.company_town}" if post_code else bill.company_profile.company_town
        context["city"] = city
        context["country"] = bill.company_profile.company_country
        context["contact_number"] = bill.company_profile.company_tell_no
        bank_obj = bill.company_profile.bank_details.first()
        if bank_obj:
            context["bank_details"] = get_bank_details(bank_obj)
        context["company_reg_no"] = bill.company_profile.company_registration_no or "-"
        context["company_tax_no"] = bill.company_profile.company_tax_reference or "-"

    # Company contact email / phone for header
    if request_user:
        context["company_contact_email"] = getattr(request_user, "email", None)
    if "contact_number" in context:
        context["company_contact_phone"] = context.get("contact_number")

    # Customer VAT number (from related company profile when available)
    recipient_company_profile = None
    if bill.reciepient_company and bill.reciepient_company.company_profile:
        recipient_company_profile = bill.reciepient_company.company_profile
    elif bill.reciepient_user and bill.reciepient_user.company_profile:
        recipient_company_profile = bill.reciepient_user.company_profile

    if recipient_company_profile and getattr(recipient_company_profile, "company_tax_reference", None):
        context["recipient_vat_no"] = recipient_company_profile.company_tax_reference

    # Footer company information (issuer side)
    issuer_company_profile = None
    if getattr(bill, "company_profile", None):
        issuer_company_profile = bill.company_profile
    elif getattr(bill, "project", None) and getattr(bill.project, "user", None) and getattr(
        bill.project.user, "company", None
    ):
        issuer_company_profile = bill.project.user.company

    if issuer_company_profile:
        context["footer_company_name"] = issuer_company_profile.company_name
        context["footer_company_reg_no"] = getattr(issuer_company_profile, "company_registration_no", None)
        context["footer_company_tax_no"] = getattr(issuer_company_profile, "company_tax_reference", None)

    html_content = render_to_string("billing_invoice.html", context)

    # Generate PDF using WeasyPrint
    pdf_file = BytesIO()
    HTML(string=html_content).write_pdf(pdf_file)
    pdf_file.seek(0)

    invoices_folder, root_folder_name = get_invoices_folder_from_bill(bill)

    # Determine base file name, using OFF- reference for offers
    reference = offer_reference
    is_offer = bool(reference and reference.startswith("OFF-"))

    if is_offer:
        base_name = reference
    else:
        base_name = f"invoice_project_{root_folder_name}_bill_{bill.id}"

    # Keep the legacy storage key for locating and updating generated documents,
    # but expose a meaningful filename whenever the PDF is downloaded or added
    # to an export package.
    public_reference = get_invoice_reference_display(bill)
    public_file_name = _sanitize_export_filename(
        (
            f"credit-note-{public_reference}.pdf"
            if is_credit_note
            else f"{public_reference}.pdf"
        ),
        f"invoice-{bill.id}.pdf",
    )

    # Check if a document for this bill already exists
    existing_document = Document.objects.filter(
        folder=invoices_folder,
        file__icontains=base_name.split(".")[0]
    ).first()

    if existing_document:
        # Update the existing document
        file_name = f"{base_name}.pdf"
        existing_document.file.save(file_name, ContentFile(pdf_file.read()), save=True)
        existing_document.file_name = public_file_name
        existing_document.status = "Draft"
        existing_document.save()
        action = "updated"
    else:
        # Create a new document
        document = Document(
            folder=invoices_folder,
            document_type="Customer Invoice",
            file_name=public_file_name,
            author=request_user,
            status="Draft",
        )
        file_name = f"{base_name}.pdf"
        document.file.save(file_name, ContentFile(pdf_file.read()))
        document.save()
        existing_document = document
        action = "created"

    pdf_file.close()
    return existing_document, action


def create_pdf_invoice_by_site_report(site_report, request_user):
    project = site_report.project
    project_name = project.project_name

    company_logo = ""
    if project.project_image:
        company_logo = project.project_image
    else:
        if project.user and project.user.company and project.user.company.company_profile_image:
            company_logo = project.user.company.company_profile_image

    observations = []

    allowed_extensions = ('.jpg', '.heic', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff')
    for observation in site_report.observations.all():
        image_photos = []
        other_files = []
        for i, photo in enumerate(observation.photos):
            if photo.lower().endswith(allowed_extensions):
                image_photos.append((i + 1, photo))
            else:
                other_files.append((i + 1, photo))

        observations.append(
            {
                "observation": observation.observation,
                "rectification": observation.rectification,
                "contractor": observation.contractor.team_member.full_name,
                "deadline": observation.deadline.strftime("%d-%m-%Y"),
                "agreed": "Yes" if observation.agreed else "No",
                "photos": image_photos,
                "other_files": other_files,
                "role": observation.role
            }
        )

    context = {
        "date": site_report.date_time.strftime("%d-%m-%Y"),
        "observer": site_report.observer.team_member.full_name,
        "weather_condition": site_report.weather_condition,
        "observations": observations,
        "location": site_report.location,
        "distributions": [d.team_member.full_name for d in site_report.distribution.all()],
        "presents": [p.team_member.full_name for p in site_report.present.all()],
        "created_at": site_report.created_at.strftime("%d-%m-%Y"),
        "updated_at": site_report.updated_at.strftime("%d-%m-%Y"),
        "project_name": project_name,
        "company_logo": company_logo,
    }

    html_content = render_to_string("site_reporting_template.html", context)

    # Generate PDF using WeasyPrint
    pdf_file = BytesIO()
    HTML(string=html_content).write_pdf(pdf_file)
    pdf_file.seek(0)

    root_folder_name = f"{site_report.project.id}_root_folder"
    root_folder = Folder.objects.filter(name=root_folder_name).first()
    if not root_folder:
        raise ValueError(f"Company level root folder for site_report ID {site_report.id} not found.")

    # Find the 'site_reports' subfolder within the root folder
    site_reports_folder = Folder.objects.filter(parent=root_folder, name="site_reports").first()
    if not site_reports_folder:
        raise ValueError(f"'site_reports' folder under root folder for site_report ID {site_report.id} not found.")

    # Check if a document for this site_report already exists
    file_name = f"site_reports_project_{site_report.project.id}_site_report_{site_report.id}.pdf"
    existing_document = Document.objects.filter(folder=site_reports_folder,
                                                file__icontains=file_name.split(".")[0]).first()

    if existing_document:
        # Update the existing document
        existing_document.file.save(file_name, ContentFile(pdf_file.read()), save=True)
        existing_document.status = "Draft"
        existing_document.file_name = site_report.name + ".pdf" if site_report.name else None
        existing_document.document_type = "Site Report"
        existing_document.save()
        action = "updated"
    else:
        # Create a new document
        document = Document(
            folder=site_reports_folder,
            document_type="Site Report",
            file_name=site_report.name + ".pdf" if site_report.name else None,
            author=request_user,
            status="Draft",
        )
        document.file.save(file_name, ContentFile(pdf_file.read()))
        document.save()
        existing_document = document
        action = "created"

    pdf_file.close()
    return existing_document, action


def has_accepted_connection(initiator_id, target_id):
    return Connection.objects.filter(
        initiator_id__in=[initiator_id, target_id],
        target_id__in=[initiator_id, target_id],
        status=StatusType.ACCEPTED.value
    ).exists()


def has_accepted_internal_user_connection(initiator_id, target_id, user_id):
    return ConnectionUser.objects.filter(
        connection__initiator_id__in=[initiator_id, target_id],
        connection__target_id__in=[initiator_id, target_id],
        connection__status=StatusType.ACCEPTED.value,
        user_id=user_id,
        status=StatusType.ACCEPTED.value
    ).exists()

def send_project_member_notification(project, new_project_team_member, requester, created=False):
    """
    Send notification email (HTML + plain text) when a user is added to a project.
    """
    subject = f"Welcome to the project '{project.project_name}'"
    date_of_request = timezone.now().strftime('%d-%m-%Y')

    if created==False:
        requested_membership = ProjectTeamMembers.objects.filter(
            project=project,
            team_member=requester
        ).first()

        if requested_membership:
            requester_role = requested_membership.project_role.name
        else:
            requester_role   = 'Unknown'

    else:
        requester_role = 'Project owner'
    context = {
        'user_name':        new_project_team_member.team_member.full_name,
        'project_name':     project.project_name,
        'requested_by':     requester.full_name,
        'requester_role':   requester_role.capitalize() if requester_role else "Unknown",
        'date_of_request':  date_of_request,
        'access_level':     new_project_team_member.role if new_project_team_member.role else "Unknown",
        'user_solution': get_user_active_solution(new_project_team_member.team_member.id),
        'user_solution_link': get_frontend_url_for_solution(get_user_active_solution(new_project_team_member.team_member.id)),
    }
    html_message = render_to_string('project_welcome_email.html', context)
    plain_message = plain_message_for_send_project_welcome_email(
        project,
        new_project_team_member,
        requester
    )
    email_check = get_user_email_preferences(new_project_team_member.team_member.id)
    if email_check:
        try:
            send_mail(
                subject,     
                plain_message,         
                settings.DEFAULT_FROM_EMAIL,
                [new_project_team_member.team_member.email],                
                html_message=html_message
            )
        except Exception as e:
            raise Exception(
                f"Failed to send project welcome notification reminder email to {new_project_team_member.team_member.email}, "
                f"Reason: {repr(e)}"
            )

def send_invitation_email_to_external_user(new_project_team_member, requester):
    """
    Send invitation email (HTML + plain text) to an external user,
    using only user_name and invitation_sender_name in the template context.
    """
    subject = f"Invitation to join sharemundo links®"
    context = {
        'user_name':               new_project_team_member.team_member.full_name,
        'invitation_sender_name':  requester.full_name,
    }

    html_message  = render_to_string('project_welcome_email_for_external_user.html', context)
    plain_message = plain_message_for_send_invitation_email_to_external_user(new_project_team_member, requester)
    email_check = get_user_email_preferences(new_project_team_member.team_member.id)
    if email_check:
        try:
            send_mail(
                subject,
                plain_message,
                settings.DEFAULT_FROM_EMAIL,
                [new_project_team_member.team_member.email],
                html_message=html_message
            )
        except Exception as e:
            raise Exception(
                f"Failed to send invitation email to {new_project_team_member.team_member.email}. Reason: {e!r}"
            )

def get_product_name_from_request(request):
    if not request:
        return "Energy Consultant Solution"
    query_params = getattr(request, "query_params", getattr(request, "GET", {}))
    return query_params.get("solution", "Energy Consultant Solution")


def has_project_permission(request_method, request_user, project, product_name, is_sub_module=True):
    if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
        products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
    else:
        products = [product_name]
    if project.solution not in products:
        raise PermissionDenied("You do not have permission to access this project.")

    # Check if the user is the project owner
    if project.user == request_user:
        return True

    if request_user.groups.filter(name=UserType.ADMIN.value).exists() and not request_user.is_freelancer:
        if project.user.company == request_user.company:
            return True

    # Check if the request_user is a team member of the project
    team_member = project.team_members.filter(team_member=request_user).first()
    if not team_member:
        raise PermissionDenied("You do not have permission to access this project.")

    # If the request method is "PATCH" or "PUT", ensure the request_user has RW access
    if request_method in ("PATCH", "PUT") and team_member.project_access != "RW":
        raise PermissionDenied("You do not have permission to modify this project.")
    if is_sub_module:
        if request_method in ("DELETE") and team_member.project_access != "RW":
            raise PermissionDenied("You do not have permission to delete this object.")
    else:
        if request_method in ("DELETE") and project.user != request_user:
            raise PermissionDenied("You do not have permission to modify this project.")

    return True


def calculate_project_progress(project):
    """
    Calculate the overall project progress based on milestones, action items, and subtasks.
    
    :param project: Project instance
    :return: Overall project progress as a percentage (float)
    """
    milestones = project.milestones.all()
    if not milestones:
        return 0.0

    milestone_progress_list = []

    for milestone in milestones:
        action_items = milestone.action_items.all()
        if not action_items:
            milestone_progress_list.append(0.0)
            continue

        action_item_progress_list = []

        for action_item in action_items:
            subtasks = action_item.sub_action_items.all()
            if not subtasks:
                # If no subtasks, treat action item as a single unit (closed or not)
                action_item_progress = 1.0 if action_item.status == "closed" else 0.0
            else:
                closed_subtasks = subtasks.filter(status="closed").count()
                total_subtasks = subtasks.count()
                action_item_progress = closed_subtasks / total_subtasks

            action_item_progress_list.append(action_item_progress)

        # Calculate milestone progress as the average progress of its action items
        milestone_progress = sum(action_item_progress_list) / len(action_item_progress_list)
        milestone_progress_list.append(milestone_progress)

    # Calculate overall project progress as the average progress of its milestones
    overall_project_progress = sum(milestone_progress_list) / len(milestone_progress_list)

    return overall_project_progress * 100  # Return progress as a percentage


def send_approval_email(manager, card_type, card, url):
    """
    Sends an email notification to the manager when a Time Card or Expense Card is created.
    
    :param manager: User instance (the manager who needs to approve)
    :param card_type: str ('Time Card' or 'Expense Card')
    :param card: Time Card or Expense Card instance
    """
    subject = f"Approval Required for a {card_type}"
    frontend_url = url
    # Determine the frontend URL based on the card type
    if card_type == "Expense Card":
        frontend_url = url + f"/expense-booking"
    else:
        frontend_url = url + f"/time-booking"

    context = {
        'assignee': manager.full_name,
        'card_type': card_type,
        'card_name': card.name,
        'submitted_by': card.created_by.full_name if card.created_by else "Unknown",
        'created_at': card.created_at.strftime('%Y-%m-%d') if card.created_at else "N/A",
        'description': card.description if card.description else "N/A",

        'milestone_name': card.milestone.name if hasattr(card, 'milestone') and card.milestone else None,
        'action_item_name': card.action_item.name if hasattr(card, 'action_item') and card.action_item else None,
        # Add extra fields conditionally based on card type
        'hours': card.hours if card_type == "Time Card" and hasattr(card, 'hours') else None,
        'category': card.category if card_type == "Expense Card" and hasattr(card, 'category') else None,
        'rate': card.rate if card_type == "Expense Card" and hasattr(card, 'rate') else None,
        'quantity': card.quantity if card_type == "Expense Card" and hasattr(card, 'quantity') else None,
        'total': format_currency_amount(card.total, get_currency_from_record(card)) if card_type == "Expense Card" and hasattr(card, 'total') else None,
        'frontend_url': frontend_url
    }

    html_body = loader.render_to_string('approval_email_for_card.html', context)
    plain_message = plain_messages_for_send_approval_email(
        assignee=manager.full_name,
        card_type=card_type,
        card_name=card.name,
        submitted_by=card.created_by.full_name if card.created_by else "Unknown",
        created_at=card.created_at.strftime('%Y-%m-%d') if card.created_at else "N/A",
        description=card.description if card.description else "N/A",
        milestone_name=card.milestone.name if hasattr(card, 'milestone') and card.milestone else None,
        action_item_name=card.action_item.name if hasattr(card, 'action_item') and card.action_item else None,
        hours=card.hours if card_type == "Time Card" and hasattr(card, 'hours') else None,
        category=card.category if card_type == "Expense Card" and hasattr(card, 'category') else None,
        rate=card.rate if card_type == "Expense Card" and hasattr(card, 'rate') else None,
        quantity=card.quantity if card_type == "Expense Card" and hasattr(card, 'quantity') else None,
        total=format_currency_amount(card.total, get_currency_from_record(card)) if card_type == "Expense Card" and hasattr(card, 'total') else None,
        frontend_url=frontend_url
    )
    email_check = get_user_email_preferences(manager.id)
    if email_check:
        try:
            send_mail(
                subject=subject,
                message=plain_message,
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[manager.email],
                fail_silently=False
            )
        except Exception as e:
            raise Exception(f'Failed to send Approval Required for a {card_type} notification email to {manager.email}, Reason: {repr(e)}')

def send_pending_approval_reminder_email(manager, card_type, card, url):
    """
    Sends a reminder email notification to the manager for a pending Time Card or Expense Card
    using an HTML template.
    
    :param manager: User instance (the manager who needs to approve)
    :param card_type: str ('Time Card' or 'Expense Card')
    :param card: Time Card or Expense Card instance
    """
    subject = f"Reminder: Pending {card_type} Requires Your Approval"
    frontend_url = url
    if card_type == "Expense Card":
        frontend_url = url + f"/expense-booking"
    else:
        frontend_url = url + f"/time-booking"
    context = {
        'assignee': manager.full_name,
        'card_type': card_type,
        'card_name': card.name,
        'submitted_by': card.created_by.full_name if card.created_by else "Unknown",
        'created_at': card.created_at.strftime('%Y-%m-%d') if card.created_at else "N/A",
        'description': card.description if card.description else "N/A",
        'milestone_name': card.milestone.name if hasattr(card, 'milestone') and card.milestone else None,
        'action_item_name': card.action_item.name if hasattr(card, 'action_item') and card.action_item else None,
        # Additional fields conditionally
        'hours': card.hours if card_type == "Time Card" and hasattr(card, 'hours') else None,
        'category': card.category if card_type == "Expense Card" and hasattr(card, 'category') else None,
        'rate': card.rate if card_type == "Expense Card" and hasattr(card, 'rate') else None,
        'quantity': card.quantity if card_type == "Expense Card" and hasattr(card, 'quantity') else None,
        'total': format_currency_amount(card.total, get_currency_from_record(card)) if card_type == "Expense Card" and hasattr(card, 'total') else None,
        'frontend_url': frontend_url
    }

    # Render the HTML body for the pending approval reminder email using the template
    html_body = loader.render_to_string('approval_reminder_email_for_card.html', context)
    plain_messages = plain_messages_for_send_pending_approval_reminder_email(
        assignee=manager.full_name,
        card_type=card_type,
        card_name=card.name,
        submitted_by=card.created_by.full_name if card.created_by else "Unknown",
        created_at=card.created_at.strftime('%Y-%m-%d') if card.created_at else "N/A",
        description=card.description if card.description else "N/A",
        milestone_name=card.milestone.name if hasattr(card, 'milestone') and card.milestone else None,
        action_item_name=card.action_item.name if hasattr(card, 'action_item') and card.action_item else None,
        hours=card.hours if card_type == "Time Card" and hasattr(card, 'hours') else None,
        category=card.category if card_type == "Expense Card" and hasattr(card, 'category') else None,
        rate=card.rate if card_type == "Expense Card" and hasattr(card, 'rate') else None,
        quantity=card.quantity if card_type == "Expense Card" and hasattr(card, 'quantity') else None,
        total=format_currency_amount(card.total, get_currency_from_record(card)) if card_type == "Expense Card" and hasattr(card, 'total') else None,
        frontend_url=frontend_url
    )
    email_check = get_user_email_preferences(manager.id)
    if email_check:
        try:
            send_mail(
                subject=subject,
                message=plain_messages,
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[manager.email],
                fail_silently=False
            )
        except Exception as e:
            raise Exception(
                f"Failed to send Reminder: Pending {card_type} notification email to {manager.email}, "
                f"Reason: {repr(e)}"
            )

def get_sharemundo_user(contact_email):
    """Check and return if a user exists with this email address in Sharemundo"""
    return User.objects.filter(email__iexact=contact_email).first()


def get_sharemundo_company(contact_email):
    """Check and return if a company/company's admin user exists with this email address in Sharemundo"""
    return CompanyProfile.objects.filter(
        Q(company_email_address__iexact=contact_email) |
        Q(admin_user__email__iexact=contact_email)
    ).first()


def get_bank_details(bank_obj):
    bank_details = {
        "bank_name": bank_obj.company_bank_name,
        "bank_iban": bank_obj.company_iban if bank_obj.company_iban else None,
        "bank_bic": bank_obj.company_bic,
        "bank_address": bank_obj.company_bank_address,
    }

    postcode = bank_obj.company_postcode
    town = bank_obj.company_town

    if postcode and town:
        bank_details["bank_city"] = f"{postcode}, {town}"
    elif postcode:
        bank_details["bank_city"] = postcode
    elif town:
        bank_details["bank_city"] = town
    else:
        bank_details["bank_city"] = ""

    return bank_details


def format_currency_amount(amount, currency="€", decimal_places=2):
    """
    Formats a number as currency string with thousand separator and decimal places
    Example: 4451.38 -> "4,451.38 €"
    """
    if amount is None:
        return None
    if currency is None:
        currency = "€"
    
    try:
        # Format with thousand separator and fixed decimal places
        formatted_number = "{:,.{}f}".format(float(amount), decimal_places)
        return f"{currency}{formatted_number}"
    except (ValueError, TypeError):
        return None


def get_currency_from_record(record):
    """
    Get currency from a record (bill/invoice) based on its relationships.
    Checks project, company_profile, and freelancer in that order.
    
    Args:
        record: A bill or invoice object that may have project, company_profile, or freelancer
        
    Returns:
        str: Currency symbol, defaults to "€" if no currency is found
    """
    currency = "€"  # default
    
    if hasattr(record, 'project') and record.project and record.project.currency:
        return record.project.currency

    # Try direct company_profile
    if hasattr(record, 'company_profile') and record.company_profile and record.company_profile.currency:
        return record.company_profile.currency

    # Try freelancer → freelancer_profile
    if hasattr(record, 'freelancer') and record.freelancer:
        freelancer_profile = getattr(record.freelancer, 'freelancer_profile', None)
        if freelancer_profile and freelancer_profile.currency:
            return freelancer_profile.currency

    return "€"  # default


def symbol_to_currency_code(currency_symbol):
    """
    Convert currency symbol to ISO 4217 currency code.
    
    Args:
        currency_symbol (str): Currency symbol like "€", "$", "£"
        
    Returns:
        str: ISO 4217 currency code like "EUR", "USD", "GBP"
    """
    symbol_to_code = {
        '€': 'EUR',
        '$': 'USD',
        '£': 'GBP',
        '¥': 'JPY',
        '₹': 'INR',
        '₽': 'RUB',
        '₩': 'KRW',
        '₪': 'ILS',
        '₨': 'PKR',
        '₦': 'NGN',
        '₡': 'CRC',
        '₫': 'VND',
        '₱': 'PHP',
        '₲': 'PYG',
        '₴': 'UAH',
        '₵': 'GHS',
        '₸': 'KZT',
        '₺': 'TRY',
        '₼': 'AZN',
        '₾': 'GEL',
        '₿': 'BTC',  # Bitcoin (though ECB doesn't support crypto)
        'CHF': 'CHF',  # Swiss Franc
        'SEK': 'SEK',  # Swedish Krona
        'NOK': 'NOK',  # Norwegian Krone
        'DKK': 'DKK',  # Danish Krone
        'PLN': 'PLN',  # Polish Złoty
        'CZK': 'CZK',  # Czech Koruna
        'HUF': 'HUF',  # Hungarian Forint
        'RON': 'RON',  # Romanian Leu
        'BGN': 'BGN',  # Bulgarian Lev
        'HRK': 'HRK',  # Croatian Kuna
        'RSD': 'RSD',  # Serbian Dinar
        'UAH': 'UAH',  # Ukrainian Hryvnia
        'BYN': 'BYN',  # Belarusian Ruble
        'MDL': 'MDL',  # Moldovan Leu
        'GEL': 'GEL',  # Georgian Lari
        'AMD': 'AMD',  # Armenian Dram
        'AZN': 'AZN',  # Azerbaijani Manat
        'KZT': 'KZT',  # Kazakhstani Tenge
        'KGS': 'KGS',  # Kyrgyzstani Som
        'TJS': 'TJS',  # Tajikistani Somoni
        'TMT': 'TMT',  # Turkmenistani Manat
        'UZS': 'UZS',  # Uzbekistani Som
        'MNT': 'MNT',  # Mongolian Tögrög
        'CNY': 'CNY',  # Chinese Yuan
        'HKD': 'HKD',  # Hong Kong Dollar
        'TWD': 'TWD',  # Taiwan Dollar
        'SGD': 'SGD',  # Singapore Dollar
        'MYR': 'MYR',  # Malaysian Ringgit
        '฿': 'THB',  # Thai Baht
        'IDR': 'IDR',  # Indonesian Rupiah
        'PHP': 'PHP',  # Philippine Peso
        'VND': 'VND',  # Vietnamese Dong
        'KRW': 'KRW',  # South Korean Won
        'INR': 'INR',  # Indian Rupee
        'PKR': 'PKR',  # Pakistani Rupee
        'BDT': 'BDT',  # Bangladeshi Taka
        'LKR': 'LKR',  # Sri Lankan Rupee
        'NPR': 'NPR',  # Nepalese Rupee
        'MMK': 'MMK',  # Myanmar Kyat
        'KHR': 'KHR',  # Cambodian Riel
        'LAK': 'LAK',  # Lao Kip
        'MNT': 'MNT',  # Mongolian Tögrög
        'AUD': 'AUD',  # Australian Dollar
        'NZD': 'NZD',  # New Zealand Dollar
        'CAD': 'CAD',  # Canadian Dollar
        'MXN': 'MXN',  # Mexican Peso
        'BRL': 'BRL',  # Brazilian Real
        'ARS': 'ARS',  # Argentine Peso
        'CLP': 'CLP',  # Chilean Peso
        'COP': 'COP',  # Colombian Peso
        'PEN': 'PEN',  # Peruvian Sol
        'UYU': 'UYU',  # Uruguayan Peso
        'PYG': 'PYG',  # Paraguayan Guaraní
        'BOB': 'BOB',  # Bolivian Boliviano
        'VEF': 'VEF',  # Venezuelan Bolívar
        'ZAR': 'ZAR',  # South African Rand
        'EGP': 'EGP',  # Egyptian Pound
        'NGN': 'NGN',  # Nigerian Naira
        'KES': 'KES',  # Kenyan Shilling
        'GHS': 'GHS',  # Ghanaian Cedi
        'UGX': 'UGX',  # Ugandan Shilling
        'TZS': 'TZS',  # Tanzanian Shilling
        'MAD': 'MAD',  # Moroccan Dirham
        'TND': 'TND',  # Tunisian Dinar
        'DZD': 'DZD',  # Algerian Dinar
        'LYD': 'LYD',  # Libyan Dinar
        'SDG': 'SDG',  # Sudanese Pound
        'ETB': 'ETB',  # Ethiopian Birr
        'SOS': 'SOS',  # Somali Shilling
        'DJF': 'DJF',  # Djiboutian Franc
        'KMF': 'KMF',  # Comorian Franc
        'MUR': 'MUR',  # Mauritian Rupee
        'SCR': 'SCR',  # Seychellois Rupee
        'BIF': 'BIF',  # Burundian Franc
        'RWF': 'RWF',  # Rwandan Franc
        'CDF': 'CDF',  # Congolese Franc
        'XAF': 'XAF',  # Central African CFA Franc
        'XOF': 'XOF',  # West African CFA Franc
        'XPF': 'XPF',  # CFP Franc
        'XCD': 'XCD',  # East Caribbean Dollar
        'JMD': 'JMD',  # Jamaican Dollar
        'TTD': 'TTD',  # Trinidad and Tobago Dollar
        'BBD': 'BBD',  # Barbadian Dollar
        'BZD': 'BZD',  # Belize Dollar
        'GTQ': 'GTQ',  # Guatemalan Quetzal
        'HNL': 'HNL',  # Honduran Lempira
        'NIO': 'NIO',  # Nicaraguan Córdoba
        'CRC': 'CRC',  # Costa Rican Colón
        'PAB': 'PAB',  # Panamanian Balboa
        'DOP': 'DOP',  # Dominican Peso
        'HTG': 'HTG',  # Haitian Gourde
        'CUP': 'CUP',  # Cuban Peso
        'BMD': 'BMD',  # Bermudian Dollar
        'KYD': 'KYD',  # Cayman Islands Dollar
        'AWG': 'AWG',  # Aruban Florin
        'ANG': 'ANG',  # Netherlands Antillean Guilder
        'SRD': 'SRD',  # Surinamese Dollar
        'GYD': 'GYD',  # Guyanese Dollar
        'BWP': 'BWP',  # Botswana Pula
        'NAD': 'NAD',  # Namibian Dollar
        'SZL': 'SZL',  # Swazi Lilangeni
        'LSL': 'LSL',  # Lesotho Loti
        'MWK': 'MWK',  # Malawian Kwacha
        'ZMW': 'ZMW',  # Zambian Kwacha
        'ZWL': 'ZWL',  # Zimbabwean Dollar
        'MOP': 'MOP',  # Macanese Pataca
        'BND': 'BND',  # Brunei Dollar
        'KHR': 'KHR',  # Cambodian Riel
        'LAK': 'LAK',  # Lao Kip
        'MNT': 'MNT',  # Mongolian Tögrög
        'NPR': 'NPR',  # Nepalese Rupee
        'BTN': 'BTN',  # Bhutanese Ngultrum
        'MVR': 'MVR',  # Maldivian Rufiyaa
        'LKR': 'LKR',  # Sri Lankan Rupee
        'BDT': 'BDT',  # Bangladeshi Taka
        'MMK': 'MMK',  # Myanmar Kyat
    }
    
    # If the symbol is already a code, return it as is
    if currency_symbol in symbol_to_code.values():
        return currency_symbol
    
    # Convert symbol to code
    return symbol_to_code.get(currency_symbol, 'EUR')  # Default to EUR if not found

def send_reminder_emails_for_expiring_po(po, user):
    """Send reminder email for expiring purchase order"""
    subject = f"Reminder: Expiring Purchase Order"
    context = {
        'po': po,
        'contact_name': user.full_name
    }
    html_body = loader.render_to_string('expiring_po_reminder_email.html', context)
    plain_message = plain_messages_for_expiring_po_reminder(user.full_name, po)
    email_check = get_user_email_preferences(user.id)
    if email_check:
        try:
            send_mail(
                subject=subject,
                message=plain_message,
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False
            )
        except Exception as e:
            raise Exception(    
                f"Failed to send Reminder: Expiring Purchase Order {po.po_number} notification email to {user.email}, "
                f"Reason: {repr(e)}"
            )


def get_user_notification_preferences(user_id):
    try:
        preference = Preference.objects.get(user_id=user_id)
        return {
            "notify_created": preference.notify_item_created_by_me,
            "notify_own": preference.notify_item_i_own,
            "notify_part": preference.notify_item_am_part,
            "own_types": preference.specific_notify_item_i_own or [],
            "part_types": preference.specific_notify_item_am_part_of or [],
            "company_own_types": preference.specific_notify_company_item_i_own or [],
            "company_part_types": preference.specific_notify_company_item_am_part_of or [],
        }
    except Preference.DoesNotExist:
        return {
            "notify_created": None,
            "notify_own": None,
            "notify_part": None,
            "own_types": [],
            "part_types": [],
            "company_own_types": [],
            "company_part_types": [],
        }

def get_user_email_preferences(user_id):
    try:
        preference = Preference.objects.get(user_id=user_id)
        return preference.email_check
    except Preference.DoesNotExist:
            return None

def get_user_currency(user):
    if not user:
        return '€'
    if not user.is_freelancer and user.company:
        return user.company.currency or '€'
    elif user.is_freelancer and hasattr(user, 'freelancer_profile'):
        return user.freelancer_profile.currency or '€'
    return '€'
