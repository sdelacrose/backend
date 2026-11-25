# LOT RH11 completion: seeds the `leave_request.update` Permission row --
# the non-status PATCH permission check (views.leave.LeaveRequestPermission.
# _check_update_permission, pre-existing code) has referenced this key
# since before this lot, but the row itself was never seeded, so it could
# never exist on any role anywhere. Mirrors 0011_seed_manager_opinion_
# permission.py's exact pattern: a granular, additive permission, never
# assigned to any role by default -- no company gets this delegated to
# anyone automatically. This alone does NOT make it self-service-assignable
# via the Roles & Permissions screen: RoleSerializer.validate_permissions_
# json (companyprofileapis/serializers/role.py) only recognizes a
# permission as "known" once at least one role of that scope/type already
# holds it -- a pre-existing, cross-cutting bootstrap gate unrelated to
# RH11 and out of this lot's scope to change. Granting it today still
# requires a one-off, direct RolePermission row (exactly how this lot's
# own tests exercise it, disposable and reverted after each run).
from django.db import migrations


def seed_permission(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    Permission.objects.get_or_create(
        key='leave_request.update',
        defaults={
            'name': 'Update Leave Request Content',
            'description': (
                "Can modify a leave request's own content (dates, duration, "
                "type, reason, attachments) -- distinct from leave_request."
                "review (approve/reject) and from leave_request.full_access "
                "(which also implies it). Never grants approval/rejection "
                "rights."
            ),
            'module': 'leave_request',
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0018_carryover_allocation_preserve_history_on_cancel'),
    ]

    operations = [
        migrations.RunPython(seed_permission, noop),
    ]
