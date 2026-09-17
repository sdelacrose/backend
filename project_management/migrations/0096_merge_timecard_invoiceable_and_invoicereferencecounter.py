# Merge migration: parallel 0094 branches (timecard invoiceable vs invoice reference counter).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        (
            "project_management",
            "0094_timecard_is_invoiceable_from_timesheet",
        ),
        (
            "project_management",
            "0095_fix_invoicereferencecounter_unique_constraints",
        ),
    ]

    operations = []
