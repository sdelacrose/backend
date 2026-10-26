# LOT 3C (+ amendment): Transmittal foundation (Transmittal, TransmittalItem,
# TransmittalEvent, TransmittalReferenceCounter, + django-simple-history's
# HistoricalTransmittal). Project-scoped only (no company/library scope): the amendment
# corrected the original LOT 3C draft before it was ever committed, so this single migration
# reflects the final shape directly rather than a create-then-alter chain.
#
# Written by hand rather than via `makemigrations`: this app already has unrelated,
# pre-existing migration drift (historical index-naming differences on unrelated models, and
# an unrelated `historicalobjectassessment.file` field alteration -- the same drift already
# reported in LOT 2D-12/3B) that `makemigrations` would otherwise fold into this migration.
# That drift is left untouched and reported separately -- this migration only creates the
# four new tables (+ their history table) and the external_contacts M2M through table.

import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0204_ecpmpconfig_correspondence_types'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Transmittal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transmittal_number', models.CharField(blank=True, db_index=True, default='', max_length=255)),
                ('direction', models.CharField(choices=[('IN', 'Incoming'), ('OUT', 'Outgoing')], max_length=3)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('ISSUED', 'Issued')], default='DRAFT', max_length=10)),
                ('subject', models.CharField(blank=True, max_length=500, null=True)),
                ('message', models.TextField(blank=True, null=True)),
                ('issued_at', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(blank=True, null=True)),
                ('evidence_snapshot', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='TransmittalItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('distribution_purpose', models.CharField(blank=True, choices=[('FOR_REVIEW', 'For review'), ('FOR_INFORMATION', 'For information')], max_length=20, null=True)),
                ('evidence_snapshot', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='TransmittalEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('CREATED', 'Created'), ('ITEM_ADDED', 'Item added'), ('ITEM_REMOVED', 'Item removed'), ('ISSUED', 'Issued'), ('RECEIVED', 'Received')], max_length=20)),
                ('metadata', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='TransmittalReferenceCounter',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_sequence', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='transmittal_reference_counter', to='project_management.project')),
            ],
        ),
        migrations.CreateModel(
            name='HistoricalTransmittal',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('transmittal_number', models.CharField(blank=True, db_index=True, default='', max_length=255)),
                ('direction', models.CharField(choices=[('IN', 'Incoming'), ('OUT', 'Outgoing')], max_length=3)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('ISSUED', 'Issued')], default='DRAFT', max_length=10)),
                ('subject', models.CharField(blank=True, max_length=500, null=True)),
                ('message', models.TextField(blank=True, null=True)),
                ('issued_at', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(blank=True, null=True)),
                ('evidence_snapshot', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(blank=True, editable=False)),
                ('updated_at', models.DateTimeField(blank=True, editable=False)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
            ],
            options={
                'verbose_name': 'historical transmittal',
                'verbose_name_plural': 'historical transmittals',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.AddField(
            model_name='transmittal',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_transmittals', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='transmittal',
            name='external_company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transmittals', to='project_management.contactcompany'),
        ),
        migrations.AddField(
            model_name='transmittal',
            name='external_contacts',
            field=models.ManyToManyField(blank=True, related_name='transmittals', to='project_management.contactuser'),
        ),
        migrations.AddField(
            model_name='transmittal',
            name='project',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='transmittals', to='project_management.project'),
        ),
        migrations.AddField(
            model_name='transmittalitem',
            name='document_revision',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='transmittal_items', to='project_management.documentrevision'),
        ),
        migrations.AddField(
            model_name='transmittalitem',
            name='transmittal',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='project_management.transmittal'),
        ),
        migrations.AddField(
            model_name='transmittalevent',
            name='actor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transmittal_events', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='transmittalevent',
            name='transmittal',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='project_management.transmittal'),
        ),
        migrations.AddField(
            model_name='historicaltransmittal',
            name='created_by',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicaltransmittal',
            name='external_company',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.contactcompany'),
        ),
        migrations.AddField(
            model_name='historicaltransmittal',
            name='history_user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicaltransmittal',
            name='project',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.project'),
        ),
        migrations.AddConstraint(
            model_name='transmittal',
            constraint=models.UniqueConstraint(condition=models.Q(('direction', 'OUT')), fields=('project', 'transmittal_number'), name='transmittal_out_number_unique_per_project'),
        ),
        migrations.AddConstraint(
            model_name='transmittal',
            constraint=models.UniqueConstraint(condition=models.Q(('direction', 'IN')), fields=('project', 'external_company', 'transmittal_number'), name='transmittal_in_supplier_reference_unique'),
        ),
        migrations.AddConstraint(
            model_name='transmittalitem',
            constraint=models.UniqueConstraint(fields=('transmittal', 'document_revision'), name='transmittal_item_unique_revision'),
        ),
    ]
