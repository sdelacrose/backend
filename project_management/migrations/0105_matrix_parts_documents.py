from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0104_parts_lists"),
    ]

    operations = [
        migrations.AddField(
            model_name="availableproductpart",
            name="documents",
            field=models.ManyToManyField(
                blank=True,
                related_name="available_product_parts",
                to="project_management.document",
            ),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="documents",
            field=models.ManyToManyField(
                blank=True,
                related_name="bom_parts",
                to="project_management.document",
            ),
        ),
        migrations.AddField(
            model_name="requirement",
            name="documents",
            field=models.ManyToManyField(
                blank=True,
                related_name="requirements",
                to="project_management.document",
            ),
        ),
        migrations.AddField(
            model_name="solutiondemand",
            name="documents",
            field=models.ManyToManyField(
                blank=True,
                related_name="solution_demands",
                to="project_management.document",
            ),
        ),
    ]
