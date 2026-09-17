from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0185_workstation_source_requirement"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactuser",
            name="main_area_of_expertise",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="contactuser",
            name="sub_area_of_expertise",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="contactcompany",
            name="main_area_of_expertise",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="contactcompany",
            name="sub_area_of_expertise",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
