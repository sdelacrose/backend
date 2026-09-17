import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_content", "0003_acceptancecriterionevidence_generation_run"),
        ("project_management", "0169_actionitem_requirements"),
    ]

    operations = [
        migrations.AddField(
            model_name="acceptancegenerationrun",
            name="schema_version",
            field=models.CharField(default="1", max_length=16),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="acceptancegenerationrun",
            name="fat_phase",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fat_acceptance_generation_runs",
                to="project_management.milestone",
            ),
        ),
        migrations.AddField(
            model_name="acceptancegenerationrun",
            name="cat_phase",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="cat_acceptance_generation_runs",
                to="project_management.milestone",
            ),
        ),
        migrations.AlterField(
            model_name="acceptancegenerationrun",
            name="schema_version",
            field=models.CharField(default="2", max_length=16),
        ),
    ]
