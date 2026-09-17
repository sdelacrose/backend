from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0153_document_review_status_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="historicaldocument",
            name="external_review_status",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="internal_review_status",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="transmittal_category",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
