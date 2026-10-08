# MAIL-1.2A: adds CorrespondenceSendEvent.mailbox (nullable FK to userapis.MailboxConnection)
# -- additive only, no data migration, no schema change to any other field/table. Hand-written
# against the latest REAL, applied migrations in both apps (project_management 0223, userapis
# 0019) rather than accepting makemigrations' own full diff, which also pulls in a large batch
# of pre-existing, unrelated index-rename drift from elsewhere in project_management (same
# precedent as every other hand-written migration in this engagement, e.g. userapis 0016/0017).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0223_document_received_date'),
        ('userapis', '0019_mailoauthstate'),
    ]

    operations = [
        migrations.AddField(
            model_name='correspondencesendevent',
            name='mailbox',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='correspondence_send_events',
                to='userapis.mailboxconnection',
            ),
        ),
    ]
