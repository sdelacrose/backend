"""Import date of an intervention and the "Import date" period filter of the Interventions views.

Import date = the moment the CSV import batch that CREATED the request was successfully confirmed
(`InterventionRequest.import_batch.confirmed_at`, filled by the server in `confirm_import`). Nothing new
is stored: this trace already exists and is reliable.
  * a preview, a cancelled or a failed import never has a `confirmed_at`, so it creates no import date;
  * a re-import never touches an existing request (`import_batch` is set at creation only), so the date
    of the first successful import is kept;
  * sub-actions added afterwards are not an import: they do not change it;
  * requests that have no confirmed batch (older data, requests created by hand) have NO import date:
    it is never invented (`None`).

Reference time zone: the application's `TIME_ZONE` (UTC, settings.common). The filter bounds, the "today"
of the shortcuts and the date shown in the list are all the calendar date in that zone, so a request
imported at 23:30 UTC belongs to that UTC day for both the display and the filter.

Periods (all inclusive, `today` = the current date in the reference zone):
  all     no filter (requests with an unknown import date stay visible);
  today   today;
  week    from the Monday of the current week to today;
  last7   today and the six previous days;
  month   from the 1st of the current month to today;
  custom  `import_from` .. `import_to`, both bounds inclusive (one bound alone is an open range).
Every period except `all` excludes the requests whose import date is unknown.
"""
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from project_management.models import InterventionImportBatch

PERIODS = ("all", "today", "week", "last7", "month", "custom")


def import_date_of(request):
    """Calendar date (reference zone) of the first successful import, or None when unknown."""
    batch = getattr(request, "import_batch", None) if request is not None else None
    if batch is None or batch.status != InterventionImportBatch.Status.CONFIRMED or batch.confirmed_at is None:
        return None
    return timezone.localtime(batch.confirmed_at).date()


def _parse(value, name):
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise serializers.ValidationError({name: "Must be a date (YYYY-MM-DD)."})


def period_bounds(period, *, start=None, end=None, today=None):
    """(first_day, last_day) inclusive; a bound may be None for a custom open range; None for `all`."""
    today = today or timezone.localdate()
    if period in (None, "", "all"):
        return None
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=today.weekday()), today  # Monday .. today
    if period == "last7":
        return today - timedelta(days=6), today
    if period == "month":
        return today.replace(day=1), today
    if period == "custom":
        first = _parse(start, "import_from") if start not in (None, "") else None
        last = _parse(end, "import_to") if end not in (None, "") else None
        if first is None and last is None:
            raise serializers.ValidationError({"import_period": "A custom period needs at least one date."})
        if first and last and first > last:
            raise serializers.ValidationError({"import_to": "The end date is before the start date."})
        return first, last
    raise serializers.ValidationError({"import_period": f"Unknown period. Allowed: {', '.join(PERIODS)}."})


def apply_import_period(queryset, params, *, today=None):
    """Keep the actions of the interventions imported in the period: the mother or flat action itself
    and its sub-actions (also those added by hand), never anything else."""
    period = str(params.get("import_period") or "").strip().lower()
    bounds = period_bounds(period, start=params.get("import_from"), end=params.get("import_to"), today=today)
    if bounds is None:
        return queryset
    first, last = bounds
    from project_management.models import InterventionRequest

    requests = InterventionRequest.objects.filter(
        import_batch__status=InterventionImportBatch.Status.CONFIRMED, import_batch__confirmed_at__isnull=False
    )
    if first is not None:
        requests = requests.filter(import_batch__confirmed_at__date__gte=first)
    if last is not None:
        requests = requests.filter(import_batch__confirmed_at__date__lte=last)
    ids = requests.values("pk")
    return queryset.filter(Q(intervention_request__in=ids) | Q(parent_action_item__intervention_request__in=ids))
