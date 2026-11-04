from django.db import migrations, models

def seed_cycle_permission(apps, schema_editor):
    # Catalog only: never grant it to any role or user.
    apps.get_model("models", "Permission").objects.get_or_create(
        key="documents.manage_cycle",
        defaults={"module": "documents", "name": "Manage document cycle",
                  "description": "Launch validation and create versions without signing for the author; no authority to stop another author's workflow."},
    )

class Migration(migrations.Migration):
    dependencies = [("project_management", "0232_transmittal_delivery_events"),
                    ("models", "0130_companyconfig_correspondence_types")]
    operations = [
        migrations.AlterField(model_name="documentworkflow", name="status", field=models.CharField(
            max_length=20, default="DRAFT", choices=[("DRAFT", "Draft"), ("VERIFY", "Verify"), ("APPROVE", "Approve"),
            ("APPROVED", "Approved"), ("REJECTED", "Rejected"), ("CANCELLED", "Stopped")])),
        migrations.AlterField(model_name="documentworkflowevent", name="step", field=models.CharField(
            max_length=20, choices=[("START", "Start"), ("STOP", "Workflow stopped"), ("AUTHOR_SIGN", "Author signature"),
            ("VERIFY", "Verification"), ("APPROVE", "Approval")])),
        migrations.AlterField(model_name="documentworkflowevent", name="action", field=models.CharField(
            max_length=20, choices=[("START", "Started"), ("STOP", "Stopped"), ("SIGN", "Signed"), ("VALIDATE", "Validated"),
            ("APPROVE", "Approved"), ("REJECT", "Rejected"), ("CHANGE_ACTOR", "Actor changed")])),
        migrations.RunPython(seed_cycle_permission, migrations.RunPython.noop),
    ]
