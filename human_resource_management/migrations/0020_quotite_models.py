import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0019_seed_leave_request_update_permission'),
        ('models', '0139_leavetype_quotite_policy'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='leaveaccrualcredit',
            name='quotite_policy_active',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='leaveaccrualcredit',
            name='quotite_breakdown',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='LeaveTypeQuotiteProrationHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_active', models.BooleanField()),
                ('effective_from', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='Null for a system-recorded change (e.g. a data migration).', null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('leave_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='quotite_proration_history', to='models.leavetype')),
            ],
            options={
                'ordering': ['leave_type_id', 'effective_from', 'created_at'],
            },
        ),
        migrations.CreateModel(
            name='StaffQuotiteHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('contractual_hours', models.DecimalField(decimal_places=2, max_digits=5)),
                ('full_time_reference_hours', models.DecimalField(decimal_places=2, max_digits=5)),
                ('quotite', models.DecimalField(decimal_places=6, max_digits=9)),
                ('effective_from', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='Null for a system-recorded change (e.g. a data migration).', null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='quotite_history', to='models.staff')),
            ],
            options={
                'ordering': ['staff_id', 'effective_from', 'created_at'],
                'verbose_name_plural': 'Staff quotite histories',
            },
        ),
    ]
