# L01-B1: seeds the dedicated, additive staff_management.create permission
# -- never assigned to any role by default, mirroring 0027's exact pattern
# for the compensation permissions. Schema-free: no model change, this
# lot's real-time Staff-creation path reuses the existing Staff model
# as-is (see services/staff_provisioning.py, already shipped in L00-B2B).
from django.db import migrations


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    Permission.objects.get_or_create(
        key='staff_management.create',
        defaults={
            'name': 'Create Staff Dossier',
            'description': (
                "Can create a Staff dossier for a User already attached to "
                "this company who does not yet have one -- a DELIBERATELY "
                "separate right from staff_management.view/update/"
                "full_access, which only ever govern an ALREADY-EXISTING "
                "dossier. Never implies compensation, role, license or "
                "company-assignment rights. Never assigned by default."
            ),
            'module': 'staff_management',
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0029_charges_and_ancillary_costs'),
    ]

    operations = [
        migrations.RunPython(seed_permissions, noop),
    ]
