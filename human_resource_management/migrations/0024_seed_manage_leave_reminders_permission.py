# AI-1685: seeds the `staff_management.manage_leave_reminders` Permission
# row -- a granular, additive permission, never assigned to any role by
# default (mirrors 0019_seed_leave_request_update_permission.py's exact
# pattern). It controls configuring reminder rules, the collective
# preview/manual trigger, and the administrative delivery history --
# deliberately separate from staff_management.update/full_access (which
# govern contractual-field EDITING, unchanged by this permission).
from django.db import migrations


def seed_permission(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    Permission.objects.get_or_create(
        key='staff_management.manage_leave_reminders',
        defaults={
            'name': 'Manage Leave Balance Reminders',
            'description': (
                "Can configure leave-balance reminder rules, preview/manually "
                "trigger reminders for the company's staff, and view the "
                "administrative delivery history. Does not grant staff_"
                "management.update/full_access (contractual-field editing) and "
                "never exposes salary/hourly_rate."
            ),
            'module': 'staff_management',
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0023_ai1685_contract_and_reminders'),
    ]

    operations = [
        migrations.RunPython(seed_permission, noop),
    ]
