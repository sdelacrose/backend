# MAIL-1.3D-C2-B3: provenance and duplicate protection for the files kept from an imported Outlook
# message (the original .eml and the selected attachments). One new table, nothing else touched.
# Written by hand with dependencies pinned to older, already-committed migrations, like 0020/0021.
# NOT applied automatically: it needs an explicit go-ahead before `migrate`.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0130_companyconfig_correspondence_types'),
        ('project_management', '0224_correspondence_send_event_mailbox'),
        ('userapis', '0021_mailboxmessage_imported_correspondence'),
    ]

    operations = [
        migrations.CreateModel(
            name='MailboxImportedFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('original_eml', 'Original message (.eml)'), ('attachment', 'Attachment')], max_length=20)),
                ('graph_attachment_id', models.CharField(blank=True, default='', max_length=512)),
                ('file_name', models.CharField(max_length=500)),
                ('file_size', models.BigIntegerField()),
                ('sha256', models.CharField(max_length=64)),
                ('content_type', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('correspondence', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mailbox_imported_files', to='project_management.correspondence')),
                ('correspondence_attachment', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='mailbox_source', to='project_management.correspondenceattachment')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='mailbox_imported_files', to=settings.AUTH_USER_MODEL)),
                ('mailbox_message', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='imported_files', to='userapis.mailboxmessage')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.AddConstraint(
            model_name='mailboximportedfile',
            constraint=models.UniqueConstraint(
                condition=models.Q(('kind', 'original_eml')), fields=('mailbox_message',),
                name='mailbox_imported_file_one_eml_per_message',
            ),
        ),
        migrations.AddConstraint(
            model_name='mailboximportedfile',
            constraint=models.UniqueConstraint(
                condition=models.Q(('kind', 'attachment')), fields=('mailbox_message', 'graph_attachment_id'),
                name='mailbox_imported_file_one_row_per_graph_attachment',
            ),
        ),
        migrations.AddConstraint(
            model_name='mailboximportedfile',
            constraint=models.UniqueConstraint(
                fields=('correspondence', 'sha256'),
                name='mailbox_imported_file_unique_content_per_correspondence',
            ),
        ),
    ]
