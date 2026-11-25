"""LOT RH11: cancellation physically deletes a pending LeaveRequest (see
views.leave.LeaveRequestViewSet.destroy); a request with a frozen carry-over
allocation must still be cancellable (only its current-rights-financed
portion is released, per LOT RH11 section 5), so the allocation's own FK to
its request cannot be PROTECT. CASCADE only removes that one leaf row --
LeaveExpiration.quantity_expired (a separate, already-frozen aggregate) and
every other request's own allocation row are untouched.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("human_resource_management", "0016_leave_carryover_models"),
    ]

    operations = [
        migrations.AlterField(
            model_name="leavecarryoverallocation",
            name="leave_request",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="carry_over_allocations",
                to="human_resource_management.leaverequest",
            ),
        ),
    ]
