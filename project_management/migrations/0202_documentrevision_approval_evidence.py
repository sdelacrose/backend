from django.db import migrations, models


class Migration(migrations.Migration):
    """Approval evidence on the immutable Version: file SHA-256 and approval-time snapshot.

    Both nullable: legacy Versions keep NULL (the hash is computed when such a Version is
    approved; the snapshot is written once by the approval transaction).
    """

    dependencies = [
        ("project_management", "0201_document_origin"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentrevision",
            name="file_sha256",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="documentrevision",
            name="approval_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
