# LOT RH 5: seeds the `leave_request.manager_opinion` Permission row so it
# exists everywhere this app is deployed, the same way admin-server-backend's
# setup_roles_permissions command seeds the rest of the leave_request.* keys
# -- but as a data migration here instead, since that command only runs for
# NEW companies/on demand and this key needs to exist unconditionally for
# any existing company's Roles & Permissions screen to offer it at all. A
# granular, additive permission: distinct from leave_request.review, and
# never assigned to any role by default -- no company gets this delegated to
# anyone automatically.
from django.db import migrations


def seed_permission(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    Permission.objects.get_or_create(
        key='leave_request.manager_opinion',
        defaults={
            'name': 'Give Manager Opinion on Leave Request',
            'description': (
                "Can give a non-binding operational opinion (favorable/"
                "unfavorable, with an optional comment) on a leave request "
                "from a direct report. Never grants approval/rejection "
                "rights or access to supporting documents."
            ),
            'module': 'leave_request',
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0010_leaverequest_is_regularization_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_permission, noop),
    ]
