# L00-B3.2: CompanyChargeRateHistory (company-wide, historized, aggregate
# employee/employer charge rates), CompanyCostItemType (per-company
# extensible ancillary-cost catalog) and StaffAncillaryCostHistory
# (per-employee, append-only assignment history). No data migration here --
# unlike compensation (L00-B3.1), there is nothing existing to take over:
# no charge rate or ancillary cost ever existed anywhere before this lot,
# so no row is activated automatically (an absent rate stays genuinely
# "unknown", never defaulted to a guessed percentage).
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0140_staff_contract_type'),
        ('human_resource_management', '0028_staff_compensation_history'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CompanyChargeRateHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('employee_rate_percent', models.DecimalField(decimal_places=2, max_digits=5)),
                ('employer_rate_percent', models.DecimalField(decimal_places=2, max_digits=5)),
                ('effective_from', models.DateField()),
                ('event_type', models.CharField(choices=[
                    ('new_situation', 'New situation'),
                    ('correction', 'Retroactive correction'),
                    ('cancellation', 'Cancellation of a scheduled change'),
                ], max_length=20)),
                ('note', models.CharField(blank=True, max_length=255, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='charge_rate_history', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('supersedes', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='superseded_by', to='human_resource_management.companychargeratehistory')),
            ],
            options={
                'verbose_name_plural': 'Company charge rate histories',
                'ordering': ['company_profile_id', 'effective_from', 'created_at'],
            },
        ),
        migrations.CreateModel(
            name='CompanyCostItemType',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('default_frequency', models.CharField(choices=[
                    ('monthly', 'Monthly'), ('annual', 'Annual'), ('one_off', 'One-off'),
                ], max_length=16)),
                ('default_included_in_consolidated_cost', models.BooleanField(default=False)),
                ('social_tax_treatment', models.CharField(default='to_qualify', max_length=24)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cost_item_types', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['company_profile_id', 'name'],
            },
        ),
        migrations.CreateModel(
            name='StaffAncillaryCostHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('currency', models.CharField(blank=True, max_length=10, null=True)),
                ('frequency', models.CharField(blank=True, choices=[
                    ('monthly', 'Monthly'), ('annual', 'Annual'), ('one_off', 'One-off'),
                ], max_length=16, null=True)),
                ('included_in_consolidated_cost', models.BooleanField(blank=True, null=True)),
                ('nature', models.CharField(choices=[('estimated', 'Estimated'), ('actual', 'Actual')], default='estimated', max_length=16)),
                ('event_type', models.CharField(choices=[
                    ('new_assignment', 'New assignment'), ('correction', 'Correction'),
                    ('termination', 'Termination (benefit ends)'), ('cancellation', 'Cancellation of a scheduled change'),
                ], max_length=20)),
                ('effective_from', models.DateField()),
                ('note', models.CharField(blank=True, max_length=255, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cost_item_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='assignments', to='human_resource_management.companycostitemtype')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ancillary_cost_history', to='models.staff')),
                ('supersedes', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='superseded_by', to='human_resource_management.staffancillarycosthistory')),
            ],
            options={
                'verbose_name_plural': 'Staff ancillary cost histories',
                'ordering': ['staff_id', 'cost_item_type_id', 'effective_from', 'created_at'],
            },
        ),
    ]
