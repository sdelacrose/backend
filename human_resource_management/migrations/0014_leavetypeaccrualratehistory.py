import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0013_leave_accrual_credit'),
        ('models', '0137_companyconfig_accrual_timezone'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LeaveTypeAccrualRateHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rate', models.DecimalField(decimal_places=4, max_digits=8)),
                ('effective_from', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='Null for a system-recorded change (e.g. a data migration).', null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('leave_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='accrual_rate_history', to='models.leavetype')),
            ],
            options={
                'ordering': ['leave_type_id', 'effective_from', 'created_at'],
            },
        ),
    ]
