import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0020_quotite_models'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='leaverequest',
            name='decided_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='leaverequest',
            name='decided_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='decided_leave_requests', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='leaveaccrualcredit',
            name='absence_impact_applied',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='leaveaccrualcredit',
            name='absence_impact_breakdown',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='LeaveTypeAbsenceImpactHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('effect', models.CharField(choices=[('none', 'No effect'), ('suspend', 'Suspend accrual for the absence\'s own days'), ('reduce', 'Reduce accrual by an explicit factor')], max_length=10)),
                ('reduction_factor', models.DecimalField(blank=True, decimal_places=6, help_text='Required (0-1) for REDUCE only; must be null for NONE/SUSPEND.', max_digits=7, null=True)),
                ('effective_from', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='Null for a system-recorded change (e.g. a data migration).', null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('source_leave_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='absence_impact_as_source', to='models.leavetype')),
                ('target_leave_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='absence_impact_as_target', to='models.leavetype')),
            ],
            options={
                'ordering': ['source_leave_type_id', 'target_leave_type_id', 'effective_from', 'created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='leavetypeabsenceimpacthistory',
            constraint=models.UniqueConstraint(fields=('source_leave_type', 'target_leave_type', 'effective_from'), name='unique_absence_impact_per_pair_per_effective_date'),
        ),
        migrations.CreateModel(
            name='LeaveAccrualAbsenceSignal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('trigger_event', models.CharField(choices=[('approved', 'Leave request approved'), ('rejected', 'Leave request rejected'), ('deleted', 'Leave request deleted (was pending)')], max_length=10)),
                ('trigger_leave_request_id', models.PositiveIntegerField()),
                ('reason', models.TextField()),
                ('cycle_closed', models.BooleanField(default=False, help_text='Whether the affected entitlement\'s cycle was already closed when detected -- correction deferred.')),
                ('detected_at', models.DateTimeField(auto_now_add=True)),
                ('resolved', models.BooleanField(default=False)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('resolution_note', models.TextField(blank=True)),
                ('credit', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='absence_signals', to='human_resource_management.leaveaccrualcredit')),
                ('entitlement', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='absence_signals', to='human_resource_management.leaveentitlement')),
                ('resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='resolved_absence_signals', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-detected_at'],
            },
        ),
    ]
