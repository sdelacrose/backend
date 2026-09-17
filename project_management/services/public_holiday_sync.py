from project_management.models import CompanyPublicHoliday, PublicHolidaySource
from models.models import GlobalPublicHoliday


def _build_synced_holiday_payload(global_holiday, acting_user):
    return {
        "name": global_holiday.name,
        "date": global_holiday.date,
        "source": PublicHolidaySource.GLOBAL_SYNC,
        "country_code": global_holiday.country_code,
        "external_id": global_holiday.external_id,
        "synced_global_public_holiday_id": global_holiday.id,
        "created_by": global_holiday.created_by or acting_user,
    }


def sync_company_public_holidays_from_global(
    *,
    company_profile,
    acting_user,
    preserve_local=False,
):
    if not company_profile:
        return {
            "synced_count": 0,
            "removed_count": 0,
        }

    global_holidays = list(
        GlobalPublicHoliday.objects.filter(company_profile=company_profile).order_by(
            "date",
            "id",
        )
    )
    existing_company_holidays = CompanyPublicHoliday.objects.filter(
        company_profile=company_profile,
    )
    if preserve_local:
        existing_company_holidays = existing_company_holidays.filter(
            source=PublicHolidaySource.GLOBAL_SYNC,
        )
    removed_count = existing_company_holidays.count()
    existing_company_holidays.delete()

    occupied_dates = set()
    if preserve_local:
        occupied_dates = set(
            CompanyPublicHoliday.objects.filter(
                company_profile=company_profile,
            ).values_list("date", flat=True)
        )

    rows_to_create = [
        CompanyPublicHoliday(
            company_profile=company_profile,
            **_build_synced_holiday_payload(global_holiday, acting_user),
        )
        for global_holiday in global_holidays
        if global_holiday.date not in occupied_dates
    ]

    if rows_to_create:
        CompanyPublicHoliday.objects.bulk_create(rows_to_create)

    return {
        "synced_count": len(rows_to_create),
        "removed_count": removed_count,
    }
