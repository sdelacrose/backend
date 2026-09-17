import django.db.models.deletion
import project_management.models.project
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0127_part_purchase_intent"),
    ]

    operations = [
        migrations.AddField(
            model_name="partpurchaseintent",
            name="delivery_method",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="partpurchaseintent",
            name="general_information",
            field=models.JSONField(blank=True, default=project_management.models.project.default_part_purchase_general_information),
        ),
        migrations.AddField(
            model_name="partpurchaseintent",
            name="project",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="part_purchase_intents",
                to="project_management.project",
            ),
        ),
        migrations.AddIndex(
            model_name="partpurchaseintent",
            index=models.Index(fields=["project", "status", "updated_at"], name="part_intent_project_status_idx"),
        ),
    ]
