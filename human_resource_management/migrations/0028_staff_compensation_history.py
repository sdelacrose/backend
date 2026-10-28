# L00-B3.1: creates StaffCompensationHistory (the dedicated, append-only
# source of truth for an employee's compensation, see models.staff) and
# backfills ONE "initial_capture" / "legacy_unqualified" row per EXISTING
# Staff -- a prudent data takeover, never an invented historical fact.
#
# The reprise row is explicitly dated the day this migration actually runs
# (NOT contract_start_date, NOT antidated in any way) -- it only records
# "this is what was known about this employee's compensation as of this
# migration", with its brut/net nature left explicitly unqualified (basis=
# legacy_unqualified). Qualifying it into a confirmed gross figure is a
# separate, later, auditable RH action (see services.staff_compensation.
# create_compensation_event, event_type=qualification) -- never done here
# automatically. Idempotent: a Staff that already has a compensation_
# history row (e.g. a second run of this migration, or a row created by a
# disposable test fixture before this migration ever applies to it) is
# skipped, never duplicated.
import datetime
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_legacy_compensation_history(apps, schema_editor):
    Staff = apps.get_model('models', 'Staff')
    StaffCompensationHistory = apps.get_model('human_resource_management', 'StaffCompensationHistory')

    today = datetime.date.today()
    already_covered = set(
        StaffCompensationHistory.objects.values_list('staff_id', flat=True).distinct()
    )
    rows = []
    for staff in Staff.objects.select_related('user__company').all():
        if staff.id in already_covered:
            continue
        currency = "€"
        company = getattr(staff.user, 'company', None)
        if company is not None and getattr(company, 'currency', None):
            currency = company.currency
        rows.append(StaffCompensationHistory(
            staff_id=staff.id,
            gross_monthly_salary=Decimal(str(staff.salary)),
            hourly_rate=Decimal(str(staff.hourly_rate)) if staff.hourly_rate is not None else None,
            currency=currency,
            basis='legacy_unqualified',
            event_type='initial_capture',
            supersedes=None,
            effective_from=today,
            note='Automatic data takeover -- brut/net nature not confirmed.',
            created_by=None,
        ))
    if rows:
        StaffCompensationHistory.objects.bulk_create(rows)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0140_staff_contract_type'),
        ('human_resource_management', '0027_l00a_compensation_permissions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StaffCompensationHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('gross_monthly_salary', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('hourly_rate', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                ('currency', models.CharField(max_length=10)),
                ('basis', models.CharField(choices=[
                    ('confirmed_gross', 'Confirmed gross monthly'),
                    ('legacy_unqualified', 'Legacy amount, basis unknown'),
                ], max_length=24)),
                ('event_type', models.CharField(choices=[
                    ('initial_capture', 'Initial capture (data takeover)'),
                    ('new_situation', 'New situation'),
                    ('qualification', 'Qualification of a captured amount'),
                    ('correction', 'Retroactive correction'),
                    ('cancellation', 'Cancellation of a scheduled change'),
                ], max_length=20)),
                ('effective_from', models.DateField()),
                ('note', models.CharField(blank=True, max_length=255, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='compensation_history', to='models.staff')),
                ('supersedes', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='superseded_by', to='human_resource_management.staffcompensationhistory')),
            ],
            options={
                'verbose_name_plural': 'Staff compensation histories',
                'ordering': ['staff_id', 'effective_from', 'created_at'],
            },
        ),
        migrations.RunPython(backfill_legacy_compensation_history, noop),
    ]
