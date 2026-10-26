# MAIL-1.6D: makes sure the two Permission catalog rows the Mail features check exist on every
# environment: `documents.correspondence_send` (Section 14 of the send view) and
# `documents.correspondence_template_manage` (MAIL-1.5 e-mail templates administration).
#
# Same pattern as userapis/0017_seed_mail_admin_permission: a plain, idempotent data migration inserting
# rows through the existing Permission model of the `models` app. It ONLY creates a missing row:
#   * a row that already exists (seeded by hand, by local-dev-auth or by another environment) is left
#     exactly as it is -- `get_or_create` never updates it;
#   * no RolePermission is created or changed: nobody is granted anything by this migration;
#   * the reverse is a no-op on purpose -- deleting the rows would silently remove the rights from every
#     role that already holds them.
from django.db import migrations

PERMISSIONS = [
    (
        "documents.correspondence_send",
        "Send correspondence",
        "Send an issued OUT correspondence through the company's e-mail provider -- a right separate "
        "from documents.correspondence_update (an external action with real business consequences).",
    ),
    (
        "documents.correspondence_template_manage",
        "Manage correspondence e-mail templates",
        "Create, edit and (de)activate the company's e-mail templates used to prefill the subject and "
        "body of a correspondence. Using a template needs only documents.correspondence_create.",
    ),
]


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model("models", "Permission")
    for key, name, description in PERMISSIONS:
        Permission.objects.get_or_create(
            key=key,
            defaults={"name": name, "description": description, "module": "documents"},
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0130_companyconfig_correspondence_types"),
        ("project_management", "0227_correspondence_templates"),
    ]

    operations = [
        migrations.RunPython(seed_permissions, noop),
    ]
