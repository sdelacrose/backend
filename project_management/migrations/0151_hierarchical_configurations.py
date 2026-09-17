from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0150_add_document_type_codes"),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="nested_configurations",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="document",
            name="discipline_code",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="discipline_group",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="discipline_group_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="discipline_option_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="discipline_code",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="discipline_group",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="discipline_group_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="discipline_option_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
