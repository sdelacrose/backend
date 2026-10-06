# CORR-DIST-2 section 8: RESEND is now an approved business rule -- replaces
# CorrespondenceSendEvent's "one in-flight-or-success" constraint (0208) with a narrower
# "one in-flight" constraint, so a historical SUCCESS no longer permanently blocks a later,
# independent distribution occurrence. Constraint-only change: no column added/removed, no data
# migration, no historical row touched.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0221_client_issue'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='correspondencesendevent',
            name='correspondence_send_event_one_in_flight_or_success',
        ),
        migrations.AddConstraint(
            model_name='correspondencesendevent',
            constraint=models.UniqueConstraint(
                condition=models.Q(('result', 'IN_PROGRESS')),
                fields=('correspondence',),
                name='correspondence_send_event_one_in_flight',
            ),
        ),
    ]
