"""LOT RH 8: XLSX/PDF rendering for the Leaves-tab detailed export and the
approved-absence recap. Takes already-computed rows (services/
absence_reporting.py) and only ever builds the file -- no permission check,
no data filtering happens here. Mirrors the existing openpyxl pattern already
used by human_resource_management/views/staff.py's export_staff_to_csv (same
HttpResponse + EXCEL_MIME_TYPE + Content-Disposition idiom); PDF mirrors the
render_to_string(...) -> weasyprint.HTML(...).write_pdf() pattern already used
by companyprofileapis/services/offer_pdf.py.
"""
import openpyxl
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from weasyprint import HTML

from human_resource_management.constants import EXCEL_MIME_TYPE

HALF_DAY_LABELS = {"morning": "Matin", "afternoon": "Après-midi"}


def _safe_xlsx_text(value):
    """Neutralize XLSX formula injection: a cell value starting with
    =, +, - or @ is interpreted as a formula by Excel/LibreOffice if written
    as-is. Prefixing with a straight apostrophe forces text, same mitigation
    used for CSV formula injection generally (OWASP)."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def _half_day_label(is_half_day, half_day_period):
    if not is_half_day:
        return "Journée complète"
    return HALF_DAY_LABELS.get(half_day_period, "Demi-journée")


def build_detailed_leave_xlsx(rows, *, company_name, period_start, period_end):
    header = [
        "Collaborateur",
        "Type",
        "Sous-type",
        "Début",
        "Fin",
        "Journée",
        "Statut",
        "Régularisation",
        "Durée (jours, période filtrée)",
        "Déduction effective (approved uniquement)",
    ]
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Congés"
    bold = Font(bold=True)

    sheet.cell(row=1, column=1, value=f"{company_name} — Export détaillé des congés")
    sheet.cell(
        row=2, column=1,
        value=f"Période : {period_start.isoformat()} au {period_end.isoformat()}",
    )
    sheet.cell(row=3, column=1, value=f"Généré le : {timezone.now().strftime('%Y-%m-%d %H:%M')}")

    header_row = 5
    for col, title in enumerate(header, start=1):
        cell = sheet.cell(row=header_row, column=col, value=title)
        cell.font = bold
    sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(len(header))}{header_row}"
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1).coordinate

    r = header_row + 1
    for row in rows:
        values = [
            _safe_xlsx_text(row.employee_name),
            _safe_xlsx_text(row.type_label),
            _safe_xlsx_text(row.sub_type_label),
            row.start_date,
            row.end_date,
            _half_day_label(row.is_half_day, row.half_day_period),
            row.status,
            "Oui" if row.is_regularization else "Non",
            float(row.duration),
            # LOT RH 9, section 6: never presented as an already-imputed day
            # for a pending/rejected row -- only an APPROVED row has actually
            # been deducted; a pending row is a reservation (shown elsewhere,
            # never here), a rejected one was never deducted at all.
            float(row.deductible) if row.status == "approved" else "",
        ]
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row=r, column=col, value=value)
            if col in (4, 5):
                cell.number_format = "YYYY-MM-DD"
            if col in (9, 10):
                cell.number_format = "0.0"
        r += 1
    if not rows:
        sheet.cell(row=r, column=1, value="Aucune donnée pour cette période/sélection.")

    for col in range(1, len(header) + 1):
        sheet.column_dimensions[get_column_letter(col)].width = 24

    response = HttpResponse(content_type=EXCEL_MIME_TYPE)
    response["Content-Disposition"] = 'attachment; filename="export_conges.xlsx"'
    workbook.save(response)
    return response


def _format_entitlement_balances_text(balances):
    if not balances:
        return ""
    parts = []
    for b in balances:
        available = b["available"] if b["available"] is not None else "?"
        # LOT RH10: a monthly-accrual row shows "acquis" (acquired) rather than
        # "quota" -- `quota` is repurposed as a manual adjustment for that mode and
        # would read as a near-always-zero, meaningless figure here otherwise.
        if b.get("mode") == "monthly_accrual":
            acquired = b.get("acquired") if b.get("acquired") is not None else "?"
            basis = f"acquis {acquired}"
        else:
            basis = f"quota {b['quota']}"
        parts.append(
            f"{b['leave_type_name']}: disponible {available} "
            f"({basis}, cycle {b['cycle_start']}→{b['cycle_end']})"
        )
    return " ; ".join(parts)


def build_absence_recap_xlsx(data):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Récapitulatif"
    bold = Font(bold=True)

    sheet.cell(
        row=1, column=1,
        value=f"{data['company_name']} — Récapitulatif des absences approuvées",
    )
    sheet.cell(
        row=2, column=1,
        value=f"Période : {data['period_start'].isoformat()} au {data['period_end'].isoformat()}",
    )
    sheet.cell(
        row=3, column=1,
        value=f"Généré le : {data['generated_at'].strftime('%Y-%m-%d %H:%M')}",
    )

    has_entitlement_balances = any(row.entitlement_balances for row in data["rows"])
    header = [
        "Collaborateur",
        "Types pris",
        "Total jours (période filtrée)",
        "Solde congés (année civile en cours)",
        "Solde télétravail (année civile en cours)",
    ]
    if has_entitlement_balances:
        header.append("Soldes par type (cycle en cours, LOT RH 9)")
    header_row = 5
    for col, title in enumerate(header, start=1):
        cell = sheet.cell(row=header_row, column=col, value=title)
        cell.font = bold

    r = header_row + 1
    reference_year = None
    reference_as_of = None
    for row in data["rows"]:
        global_balance = row.global_balance
        if global_balance and reference_year is None:
            reference_year = global_balance["year"]
            reference_as_of = global_balance["as_of"]
        values = [
            _safe_xlsx_text(row.employee_name),
            _safe_xlsx_text(", ".join(row.types)),
            float(row.total_days),
            (
                float(global_balance["leave_balance"])
                if global_balance and global_balance["leave_balance"] is not None
                else ""
            ),
            (
                float(global_balance["wfh_balance"])
                if global_balance and global_balance["wfh_balance"] is not None
                else ""
            ),
        ]
        if has_entitlement_balances:
            values.append(_safe_xlsx_text(_format_entitlement_balances_text(row.entitlement_balances)))
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row=r, column=col, value=value)
            if col == 3:
                cell.number_format = "0.0"
        r += 1

    if not data["rows"]:
        sheet.cell(row=r, column=1, value="Aucune absence approuvée sur cette période.")
    else:
        r += 1
        sheet.cell(
            row=r, column=1,
            value=(
                f"Solde global de référence : année civile {reference_year}, "
                f"calculé le {reference_as_of} -- n'est pas le solde restant "
                f"à la fin de la période filtrée."
            ),
        )
        if has_entitlement_balances:
            r += 1
            sheet.cell(
                row=r, column=1,
                value=(
                    "Soldes par type : cycle en cours de chaque type, calculés à la date de "
                    "génération ci-dessus -- distincts des jours pris sur la période filtrée."
                ),
            )

    for col in range(1, len(header) + 1):
        sheet.column_dimensions[get_column_letter(col)].width = 28

    response = HttpResponse(content_type=EXCEL_MIME_TYPE)
    response["Content-Disposition"] = 'attachment; filename="recapitulatif_absences.xlsx"'
    workbook.save(response)
    return response


def build_absence_recap_pdf(data):
    has_entitlement_balances = any(row.entitlement_balances for row in data["rows"])
    rows_with_text = []
    for row in data["rows"]:
        rows_with_text.append({
            "employee_name": row.employee_name,
            "types": row.types,
            "total_days": row.total_days,
            "global_balance": row.global_balance,
            "entitlement_balances_text": _format_entitlement_balances_text(row.entitlement_balances),
        })
    html_content = render_to_string(
        "absence_recap_pdf.html",
        {
            "company_name": data["company_name"],
            "period_start": data["period_start"],
            "period_end": data["period_end"],
            "generated_at": data["generated_at"],
            "rows": rows_with_text,
            "has_entitlement_balances": has_entitlement_balances,
        },
    )
    pdf_bytes = HTML(string=html_content).write_pdf()
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="recapitulatif_absences.pdf"'
    return response
