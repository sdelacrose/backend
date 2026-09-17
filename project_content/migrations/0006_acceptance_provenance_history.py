from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("project_content", "0005_acceptanceactionitemprovenance"),
        ("project_management", "0169_actionitem_requirements"),
    ]

    operations = [
        migrations.AlterField(
            model_name="acceptanceactionitemprovenance",
            name="action_item",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="acceptance_provenance_history",
                to="project_management.actionitem",
            ),
        ),
        migrations.AddField(
            model_name="acceptanceactionitemprovenance",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
