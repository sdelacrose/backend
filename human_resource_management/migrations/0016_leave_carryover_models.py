import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0015_accrual_override_effective_from'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LeaveCycleClosure',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('eligible_balance', models.DecimalField(decimal_places=6, max_digits=12)),
                ('capped_quantity', models.DecimalField(decimal_places=6, default=0, max_digits=12)),
                ('carried_over_quantity', models.DecimalField(decimal_places=6, default=0, max_digits=12)),
                ('cap_applied', models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True)),
                ('expiration_months_applied', models.PositiveIntegerField(blank=True, null=True)),
                ('closed_at', models.DateTimeField(auto_now_add=True)),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('entitlement', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='closure', to='human_resource_management.leaveentitlement')),
            ],
        ),
        migrations.CreateModel(
            name='LeaveCarryOver',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity', models.DecimalField(decimal_places=6, max_digits=12)),
                ('expires_on', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('destination_entitlement', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='carry_overs_in', to='human_resource_management.leaveentitlement')),
                ('source_closure', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='carry_over', to='human_resource_management.leavecycleclosure')),
            ],
        ),
        migrations.CreateModel(
            name='LeaveExpiration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_expired', models.DecimalField(decimal_places=6, max_digits=12)),
                ('expired_at', models.DateTimeField(auto_now_add=True)),
                ('carry_over', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='expiration', to='human_resource_management.leavecarryover')),
            ],
        ),
        migrations.CreateModel(
            name='LeaveCarryOverAllocation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_allocated', models.DecimalField(decimal_places=6, max_digits=12)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('carry_over', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='allocations', to='human_resource_management.leavecarryover')),
                ('leave_request', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='carry_over_allocations', to='human_resource_management.leaverequest')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('carry_over', 'leave_request'), name='unique_carryover_allocation_per_request')],
            },
        ),
    ]
