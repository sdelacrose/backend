from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0163_process_requirements_extraction"),
    ]

    operations = [
        migrations.AddField(
            model_name="companysolutionspaceitem",
            name="aas_product",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="solution_space_assembly",
                to="project_management.availableproductpart",
            ),
        ),
    ]
