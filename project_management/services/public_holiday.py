from datetime import date

import requests
from django.db import transaction
from rest_framework.exceptions import APIException

from project_management.models import CompanyPublicHoliday, PublicHolidaySource


class PublicHolidayImportException(APIException):
    status_code = 502
    default_detail = "Unable to fetch public holidays from the upstream service."
    default_code = "public_holiday_import_failed"


def fetch_national_public_holidays(country_code, year):
    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        raise PublicHolidayImportException(str(exc)) from exc

    if response.status_code == 204:
        return []

    if response.status_code != 200:
        raise PublicHolidayImportException(
            f"Upstream holiday service returned status {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise PublicHolidayImportException("Upstream holiday service returned invalid JSON.") from exc

    if not isinstance(payload, list):
        raise PublicHolidayImportException("Upstream holiday service returned an unexpected response shape.")

    return payload


@transaction.atomic
def import_company_public_holidays(*, company_profile, created_by, country_code, year):
    payload = fetch_national_public_holidays(country_code=country_code, year=year)

    existing_dates = set(
        CompanyPublicHoliday.objects.filter(
            company_profile=company_profile,
            date__year=year,
        ).values_list("date", flat=True)
    )

    rows_to_create = []
    added_dates = set()
    skipped_existing_count = 0
    skipped_non_national_count = 0
    skipped_invalid_count = 0

    for item in payload:
        if not item.get("global", False):
            skipped_non_national_count += 1
            continue

        raw_date = item.get("date")
        holiday_name = (item.get("localName") or item.get("name") or "").strip()
        if not raw_date or not holiday_name:
            skipped_invalid_count += 1
            continue

        try:
            holiday_date = date.fromisoformat(raw_date)
        except ValueError:
            skipped_invalid_count += 1
            continue

        if holiday_date in existing_dates or holiday_date in added_dates:
            skipped_existing_count += 1
            continue

        rows_to_create.append(
            CompanyPublicHoliday(
                company_profile=company_profile,
                date=holiday_date,
                name=holiday_name,
                source=PublicHolidaySource.IMPORTED,
                country_code=country_code,
                external_id=f"{country_code}:{holiday_date.isoformat()}",
                created_by=created_by,
            )
        )
        added_dates.add(holiday_date)

    if rows_to_create:
        CompanyPublicHoliday.objects.bulk_create(rows_to_create)

    return {
        "total_received": len(payload),
        "added_count": len(rows_to_create),
        "skipped_existing_count": skipped_existing_count,
        "skipped_non_national_count": skipped_non_national_count,
        "skipped_invalid_count": skipped_invalid_count,
    }
