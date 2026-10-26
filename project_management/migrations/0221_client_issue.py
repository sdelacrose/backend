# HISTORY-1: ClientIssue foundation -- the stable identity of one source DocumentRevision as
# distributed to one exact Client company (document_revision, client_company), plus the nullable
# TransmittalItem.client_issue FK that lets multiple TransmittalItems (retransmissions) share the
# same ClientIssue.
#
# Written by hand rather than via `makemigrations`: this app already has the same unrelated,
# pre-existing migration drift noted in every migration since 0207_correspondence_validation_
# workflow (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold in here -- none of
# it belongs to this lot and none of it is included. The operations below are otherwise exactly
# what Django generated (dry-run, confirmed clean of any other diff), with the FKs inlined into
# CreateModel rather than added as separate AddField steps (the same convention already used by
# 0218_document_type_review_duration.py).
#
# Fully additive and backward-compatible: TransmittalItem.client_issue is nullable, so every
# existing issued TransmittalItem keeps client_issue=NULL (section 10/25) -- no backfill, no
# fabricated ClientIssue rows for historical records.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0220_transmittal_item_review_due_date'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ClientIssue',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client_revision_label', models.CharField(blank=True, max_length=20, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('client_company', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='client_issues', to='project_management.contactcompany')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_client_issues', to=settings.AUTH_USER_MODEL)),
                ('document_revision', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='client_issues', to='project_management.documentrevision')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.AddField(
            model_name='transmittalitem',
            name='client_issue',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='transmittal_items', to='project_management.clientissue'),
        ),
        migrations.AddConstraint(
            model_name='clientissue',
            constraint=models.UniqueConstraint(fields=('document_revision', 'client_company'), name='client_issue_unique_per_revision_and_client'),
        ),
    ]
