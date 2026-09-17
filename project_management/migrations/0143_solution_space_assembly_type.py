from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0142_product_attribute_extraction"),
    ]

    operations = [
        migrations.AddField(
            model_name="companysolutionspaceitem",
            name="assembly_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("components", "Components"),
                    ("production_lines", "Production Lines"),
                ],
                db_index=True,
                max_length=32,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="projectsolutionspaceitem",
            name="assembly_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("components", "Components"),
                    ("production_lines", "Production Lines"),
                ],
                db_index=True,
                max_length=32,
                null=True,
            ),
        ),
    ]
