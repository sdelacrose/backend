# LOT L04 (SST): seeds 4 dedicated, additive staff_management permissions,
# never assigned to any role by default, mirroring 0031/0032's exact
# pattern. All SEPARATE keys -- no general RH/documents/compensation/
# onboarding-offboarding permission implies any of these (ticket's explicit
# instruction), and the payroll projection/attestation-deposit rights are
# themselves two separate keys (also explicit).
from django.db import migrations


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    permissions = [
        (
            'staff_management.manage_sst_cases',
            'Manage SST Cases',
            (
                "RH/DRH habilitee: declare/record/correct/cancel/close an SST "
                "case (maladie/maladie longue duree/maternite/accident du "
                "travail/incident) for any staff of her own company. "
                "Deliberately separate from staff_management.view/update/"
                "full_access, view_documents, view_compensation, and every "
                "onboarding/offboarding permission -- never has a Company "
                "Admin fallback either."
            ),
        ),
        (
            'staff_management.view_sst_dossier',
            'View SST Dossier',
            (
                "Read-only consultation of an SST dossier/documents -- "
                "narrower than manage_sst_cases (can consult, never record/"
                "correct/close). manage_sst_cases implies this."
            ),
        ),
        (
            'staff_management.view_sst_payroll_projection',
            'View SST Payroll Projection',
            (
                "Paie: the narrow administrative projection (dates + nature "
                "only, never reason/notes/documents). Deliberately SEPARATE "
                "from manage_sst_payroll_attestation -- viewing the "
                "projection never implies the right to deposit an "
                "attestation, and vice versa."
            ),
        ),
        (
            'staff_management.manage_sst_payroll_attestation',
            'Deposit SST Payroll Attestation',
            (
                "Paie: deposit the salary attestation document for an SST "
                "case only -- never implies view_sst_payroll_projection nor "
                "any dossier/document read access."
            ),
        ),
    ]
    for key, name, description in permissions:
        Permission.objects.get_or_create(
            key=key, defaults={'name': name, 'description': description, 'module': 'staff_management'},
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0034_staffdocument_sst_case'),
        ('models', '0141_leavetype_is_sst_managed'),
    ]

    operations = [
        migrations.RunPython(seed_permissions, noop),
    ]
