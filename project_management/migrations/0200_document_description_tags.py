import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    """Document identity metadata: plain-text description and lightweight tags."""

    dependencies = [
        ("project_management", "0199_document_file_blank"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="document",
            name="tags",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=100),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="tags",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=100),
                blank=True,
                default=list,
                size=None,
            ),
        ),
    ]
