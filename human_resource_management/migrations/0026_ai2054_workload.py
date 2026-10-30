# AI-2054: workload alert models (StaffWorkloadAlertConfig/Episode/Delivery)
# + seeds the two dedicated, additive staff_management permissions
# (view_workload, manage_workload_alerts) -- never assigned to any role by
# default, mirroring 0024_seed_manage_leave_reminders_permission.py exactly.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    Permission.objects.get_or_create(
        key='staff_management.view_workload',
        defaults={
            'name': 'View Staff Workload',
            'description': (
                "Can view the read-only cross-project staff occupancy view "
                "(capacity/charge/rate from Project Management action items, "
                "estimated and clearly flagged as such). Grants no assignment "
                "or budget-editing right anywhere -- Project Management "
                "permissions are unchanged and still govern that. Never "
                "exposes salary/hourly_rate or absence justification details."
            ),
            'module': 'staff_management',
        },
    )
    Permission.objects.get_or_create(
        key='staff_management.manage_workload_alerts',
        defaults={
            'name': 'Manage Staff Workload Alerts',
            'description': (
                "Can configure which RH users receive the staff-workload "
                "overload alert for a company/solution, and trigger a manual "
                "recompute (shares the exact same anti-duplicate claim as the "
                "scheduled evaluation). Does not by itself grant staff_"
                "management.view_workload -- consulting the occupancy view "
                "still requires that separate, dedicated permission."
            ),
            'module': 'staff_management',
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0025_leave_reminder_delivery_recipient'),
        ('models', '0140_staff_contract_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StaffWorkloadAlertConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_active', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company_config', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='workload_alert_config', to='models.companyconfig')),
                ('created_by', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('recipients', models.ManyToManyField(blank=True, related_name='workload_alert_configs', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='StaffWorkloadAlertEpisode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('period_key', models.CharField(help_text="Calendar period, e.g. '2026-10'.", max_length=16)),
                ('period_start', models.DateField()),
                ('period_end', models.DateField()),
                ('status', models.CharField(choices=[('open', 'Open'), ('closed', 'Closed'), ('indeterminate', 'Indeterminate')], default='open', max_length=16)),
                ('started_on', models.DateField()),
                ('closed_on', models.DateField(blank=True, null=True)),
                ('last_evaluated_at', models.DateTimeField(auto_now_add=True)),
                ('last_known_rate', models.DecimalField(blank=True, decimal_places=4, max_digits=9, null=True)),
                ('had_no_eligible_recipients', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company_config', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='workload_alert_episodes', to='models.companyconfig')),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='workload_alert_episodes', to='models.staff')),
            ],
        ),
        migrations.AddIndex(
            model_name='staffworkloadalertepisode',
            index=models.Index(fields=['staff', 'period_key'], name='wl_episode_staff_period_idx'),
        ),
        migrations.AddConstraint(
            model_name='staffworkloadalertepisode',
            constraint=models.UniqueConstraint(condition=~models.Q(('status', 'closed')), fields=('staff', 'period_key'), name='unique_open_workload_episode'),
        ),
        migrations.CreateModel(
            name='StaffWorkloadAlertDelivery',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel', models.CharField(choices=[('internal', 'Internal notification')], default='internal', max_length=16)),
                ('is_cc', models.BooleanField(default=False, help_text='Reserved for parity with AI-1685\'s shape; always False in this delivery (there is no \'subject\' recipient here distinct from RH recipients -- every recipient is an RH destinataire).')),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('episode', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deliveries', to='human_resource_management.staffworkloadalertepisode')),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='workload_alert_deliveries_received', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name='staffworkloadalertdelivery',
            constraint=models.UniqueConstraint(fields=('episode', 'channel', 'recipient'), name='unique_workload_alert_delivery'),
        ),
        migrations.RunPython(seed_permissions, noop),
    ]
