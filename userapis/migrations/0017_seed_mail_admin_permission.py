# MAIL-1.1B section 3: provisioning gap identified and fixed, minimally, within authorized
# scope. Permission rows are a catalog table (models.Permission, admin-server-models -- its
# own file is never touched here) that the role-assignment UI only ever READS from
# (companyprofileapis/serializers/role.py::create -- Permission.objects.get(key=...), which
# raises Permission.DoesNotExist -> ValidationError if the row is missing, confirmed by direct
# reading). No fixture/data-migration provisioning this catalog was found anywhere in the
# authorized backend repo (likely lives in admin-server-backend/admin-server-models, outside
# this lot's scope to modify) -- this data migration is the minimal, in-scope alternative: a
# plain Django data migration in `userapis` (this lot's own app), inserting exactly one row
# through the existing, unmodified Permission model class. Without it, a company administrator
# could never actually assign mail.admin_shared_mailbox to a role through the existing role
# management UI, even though the code-level check (MailboxAdminPermission) is already correct.
from django.db import migrations

PERMISSION_KEY = "mail.admin_shared_mailbox"
PERMISSION_MODULE = "mail"
PERMISSION_NAME = "Administer shared mailboxes"


def seed_permission(apps, schema_editor):
    Permission = apps.get_model("models", "Permission")
    Permission.objects.get_or_create(
        key=PERMISSION_KEY,
        defaults={"name": PERMISSION_NAME, "module": PERMISSION_MODULE},
    )


def remove_permission(apps, schema_editor):
    Permission = apps.get_model("models", "Permission")
    Permission.objects.filter(key=PERMISSION_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0130_companyconfig_correspondence_types'),
        ('userapis', '0016_mailbox_connection'),
    ]

    operations = [
        migrations.RunPython(seed_permission, remove_permission),
    ]
