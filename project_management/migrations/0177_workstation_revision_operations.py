import uuid

from django.db import migrations, models
from django.db.models import Q


def assign_unique_occurrence_ids(apps, schema_editor):
    project_workstation_node = apps.get_model(
        "project_management",
        "ProjectWorkstationNode",
    )
    for node in project_workstation_node.objects.filter(occurrence_id__isnull=True).iterator():
        node.occurrence_id = uuid.uuid4()
        node.save(update_fields=["occurrence_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0176_project_workstations"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectworkstationnode",
            name="occurrence_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            assign_unique_occurrence_ids,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="projectworkstationnode",
            name="occurrence_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, db_index=True),
        ),
        migrations.AddField(
            model_name="workstationrevision",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="workstationrevision",
            name="summary",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddConstraint(
            model_name="workstationrevision",
            constraint=models.UniqueConstraint(
                condition=Q(idempotency_key__isnull=False),
                fields=("workstation", "idempotency_key"),
                name="unique_workstation_revision_idempotency_key",
            ),
        ),
        migrations.AddIndex(
            model_name="projectworkstationnode",
            index=models.Index(
                fields=["workstation", "occurrence_id", "revision"],
                name="workstation_occurrence_rev_idx",
            ),
        ),
    ]
