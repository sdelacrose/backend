import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0174_document_reference_counter"),
    ]

    operations = [
        migrations.AddField(
            model_name="billofmaterialpart",
            name="source_project_assembly",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="copied_bom_parts",
                to="project_management.projectsolutionspaceitem",
            ),
        ),
    ]
