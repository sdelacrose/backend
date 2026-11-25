# L00-A: seeds the three dedicated, additive staff_management permissions
# (view_compensation, update_compensation, export_compensation) -- never
# assigned to any role by default, mirroring 0024/0026's exact pattern.
# Also records StaffContractAuditLog.field gaining two new choices (salary,
# hourly_rate) -- reusing that existing append-only audit model as-is for
# compensation changes (see models.staff.StaffContractAuditLog docstring),
# never a new audit model.
import django.db.models.deletion
from django.db import migrations, models


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    Permission.objects.get_or_create(
        key='staff_management.view_compensation',
        defaults={
            'name': 'View Staff Compensation',
            'description': (
                "Can view salary/hourly_rate for staff of this company -- a "
                "DELIBERATELY separate right from staff_management.view/"
                "full_access, which no longer implies compensation "
                "visibility (DEC-02/06). Never assigned by default."
            ),
            'module': 'staff_management',
        },
    )
    Permission.objects.get_or_create(
        key='staff_management.update_compensation',
        defaults={
            'name': 'Update Staff Compensation',
            'description': (
                "Can modify salary/hourly_rate for OTHER staff of this "
                "company (never one's own -- see DEC-04, a self-edit of "
                "one's own compensation is always refused regardless of "
                "this permission). Separate from view_compensation."
            ),
            'module': 'staff_management',
        },
    )
    Permission.objects.get_or_create(
        key='staff_management.export_compensation',
        defaults={
            'name': 'Export Staff Compensation',
            'description': (
                "Can include salary in a generated Staff export file "
                "(DEC-49: export is its own action, never implied by view/"
                "update compensation rights)."
            ),
            'module': 'staff_management',
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0026_ai2054_workload'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffcontractauditlog',
            name='field',
            field=models.CharField(
                choices=[
                    ('contract_type', 'Contract type'),
                    ('contract_end_date', 'Contract end date'),
                    ('salary', 'Salary'),
                    ('hourly_rate', 'Hourly rate'),
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(seed_permissions, noop),
    ]
