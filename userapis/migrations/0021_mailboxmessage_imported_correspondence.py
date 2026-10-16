# MAIL-1.3D-C2-A: where a stored message was imported/attached to a LINKS correspondence by a
# person. Three nullable columns and one partial unique constraint on MailboxMessage only (the
# rows already there are untouched). Written by hand, with dependencies pinned to older,
# already-committed migrations, so it does not depend on migrations still uncommitted in parallel
# work -- exactly like 0020.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0130_companyconfig_correspondence_types'),
        ('project_management', '0224_correspondence_send_event_mailbox'),
        ('userapis', '0020_mailboxmessage_mailboxsyncstate'),
    ]

    operations = [
        migrations.AddField(
            model_name='mailboxmessage',
            name='imported_correspondence',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='imported_from_messages', to='project_management.correspondence',
            ),
        ),
        migrations.AddField(
            model_name='mailboxmessage',
            name='imported_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='imported_mailbox_messages', to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='mailboxmessage',
            name='imported_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='mailboxmessage',
            constraint=models.UniqueConstraint(
                condition=models.Q(('imported_correspondence__isnull', False)),
                fields=('imported_correspondence',),
                name='mailbox_message_unique_imported_correspondence',
            ),
        ),
    ]
