from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0152_document_review_and_transmittal_configs"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="external_review_status",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="internal_review_status",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="transmittal_category",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
