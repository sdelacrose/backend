# LOT L04 (SST): hand-written, like 0031/0032 before it, to exclude
# pre-existing, unrelated drift makemigrations also detects (AlterField on
# created_by/id for CompanyChargeRateHistory/CompanyCostItemType/
# StaffAncillaryCostHistory/StaffCompensationHistory) -- out of this lot's
# scope, untouched here.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0032_l05_offboarding_engine'),
        ('models', '0141_leavetype_is_sst_managed'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='staffdocumentcategory',
            name='is_sst',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='SSTCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category', models.CharField(choices=[('sick_leave', 'Sick leave'), ('long_term_sick_leave', 'Long-term sick leave'), ('maternity', 'Maternity'), ('work_accident', 'Work accident'), ('incident_no_harm', 'Incident without injury')], max_length=32)),
                ('status', models.CharField(choices=[('open', 'Open'), ('closed', 'Closed')], default='open', max_length=16)),
                ('declared_at', models.DateTimeField(auto_now_add=True)),
                ('workplace_adjustment_note', models.TextField(blank=True)),
                ('external_reference', models.CharField(blank=True, max_length=255)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sst_cases', to='models.companyprofile')),
                ('declared_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sst_cases', to='models.staff')),
            ],
            options={
                'ordering': ['-declared_at'],
            },
        ),
        migrations.CreateModel(
            name='SSTCaseEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('declared', 'Declared'), ('period_recorded', 'Period recorded'), ('period_extended', 'Period extended'), ('period_corrected', 'Period corrected'), ('period_cancelled', 'Period cancelled'), ('decision_recorded', 'Decision recorded'), ('document_added', 'Document added'), ('payroll_attestation_deposited', 'Payroll attestation deposited'), ('closed', 'Closed')], max_length=32)),
                ('note', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='human_resource_management.sstcase')),
            ],
            options={
                'ordering': ['case_id', 'created_at'],
            },
        ),
        migrations.CreateModel(
            name='SSTCasePeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_initial', models.BooleanField(default=False)),
                ('cancelled_at', models.DateTimeField(blank=True, null=True)),
                ('cancellation_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cancelled_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='periods', to='human_resource_management.sstcase')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('leave_request', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='sst_period', to='human_resource_management.leaverequest')),
            ],
            options={
                'ordering': ['case_id', 'created_at'],
            },
        ),
    ]
