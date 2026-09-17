from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("project_management", "0184_catalog_recommendation")]

    operations = [
        migrations.AddField(
            model_name="projectworkstation",
            name="source_requirement",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="project_workstation", to="project_management.requirement",
            ),
        ),
    ]
