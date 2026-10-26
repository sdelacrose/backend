from django.db import migrations, models

import project_management.models.document


class Migration(migrations.Migration):
    """A register entry may exist before its main file (TO_CREATE): state-only change."""

    dependencies = [
        ("project_management", "0198_document_attachment"),
    ]

    operations = [
        migrations.AlterField(
            model_name="document",
            name="file",
            field=models.FileField(
                blank=True, upload_to=project_management.models.document.get_upload_path
            ),
        ),
        migrations.AlterField(
            model_name="historicaldocument",
            name="file",
            field=models.TextField(blank=True, max_length=100),
        ),
    ]
