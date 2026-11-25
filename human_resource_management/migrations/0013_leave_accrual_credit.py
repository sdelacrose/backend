import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0012_leave_entitlement_models'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalleaveentitlement',
            name='accrual_rate_override',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name='leaveentitlement',
            name='accrual_rate_override',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=8, null=True),
        ),
        migrations.AlterField(
            model_name='historicalleaveentitlement',
            name='mode',
            field=models.CharField(choices=[('quota', 'Quota at cycle start'), ('monthly_accrual', 'Monthly accrual')], default='quota', max_length=20),
        ),
        migrations.AlterField(
            model_name='leaveentitlement',
            name='mode',
            field=models.CharField(choices=[('quota', 'Quota at cycle start'), ('monthly_accrual', 'Monthly accrual')], default='quota', max_length=20),
        ),
        migrations.CreateModel(
            name='LeaveAccrualCredit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('period_start', models.DateField()),
                ('period_end', models.DateField()),
                ('rate_applied', models.DecimalField(decimal_places=6, max_digits=12)),
                ('proration_fraction', models.DecimalField(decimal_places=6, max_digits=12)),
                ('quantity', models.DecimalField(decimal_places=6, max_digits=12)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='Null for system/batch-generated credits.', null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('entitlement', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='accrual_credits', to='human_resource_management.leaveentitlement')),
            ],
            options={
                'ordering': ['entitlement_id', 'period_start'],
                'constraints': [models.UniqueConstraint(fields=('entitlement', 'period_start', 'period_end'), name='unique_accrual_credit_per_entitlement_period')],
            },
        ),
    ]
