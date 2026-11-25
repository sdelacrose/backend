"""LOT RH11 completion: cancellation must keep the allocation's audit
trail, not just avoid crashing. `leave_request` becomes nullable/SET_NULL
(instead of 0017's CASCADE, which silently dropped the historical row) and
`original_request_id` permanently remembers which request this was, even
once `leave_request` is nulled out by a later cancellation. Table is empty
in every environment this migration has run against (fixtures only, always
cleaned up) -- no backfill needed.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("human_resource_management", "0017_carryover_allocation_cascade_on_request_delete"),
    ]

    operations = [
        migrations.AlterField(
            model_name="leavecarryoverallocation",
            name="leave_request",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="carry_over_allocations",
                to="human_resource_management.leaverequest",
            ),
        ),
        migrations.AddField(
            model_name="leavecarryoverallocation",
            name="original_request_id",
            field=models.PositiveIntegerField(default=0),
            preserve_default=False,
        ),
    ]
