import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0011_seed_manager_opinion_permission'),
        ('models', '0135_companyconfig_leave_entitlement_activation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LeaveCyclePeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('period_start', models.DateField()),
                ('period_end', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('leave_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cycle_periods', to='models.leavetype')),
            ],
            options={
                'ordering': ['leave_type_id', 'period_start'],
            },
        ),
        migrations.CreateModel(
            name='HistoricalLeaveEntitlement',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('mode', models.CharField(choices=[('quota', 'Quota at cycle start'), ('monthly_accrual', 'Monthly accrual (not implemented)')], default='quota', max_length=20)),
                ('quota', models.DecimalField(decimal_places=2, max_digits=6)),
                ('last_adjustment_reason', models.TextField()),
                ('created_at', models.DateTimeField(blank=True, editable=False)),
                ('updated_at', models.DateTimeField(blank=True, editable=False)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('employee', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='models.staff')),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('last_adjusted_by', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('leave_type', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='models.leavetype')),
                ('cycle_period', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='human_resource_management.leavecycleperiod')),
            ],
            options={
                'verbose_name': 'historical leave entitlement',
                'verbose_name_plural': 'historical leave entitlements',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name='LeaveEntitlement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mode', models.CharField(choices=[('quota', 'Quota at cycle start'), ('monthly_accrual', 'Monthly accrual (not implemented)')], default='quota', max_length=20)),
                ('quota', models.DecimalField(decimal_places=2, max_digits=6)),
                ('last_adjustment_reason', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('cycle_period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='entitlements', to='human_resource_management.leavecycleperiod')),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='leave_entitlements', to='models.staff')),
                ('last_adjusted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('leave_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='entitlements', to='models.leavetype')),
            ],
        ),
        migrations.AddConstraint(
            model_name='leavecycleperiod',
            constraint=models.UniqueConstraint(fields=('leave_type', 'period_start', 'period_end'), name='unique_leave_cycle_period'),
        ),
        migrations.AddConstraint(
            model_name='leaveentitlement',
            constraint=models.UniqueConstraint(fields=('employee', 'leave_type', 'cycle_period'), name='unique_leave_entitlement_per_employee_type_cycle'),
        ),
    ]
