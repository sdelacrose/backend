from django.db import migrations, models
import django.db.models.deletion


def backfill_generation_runs(apps, schema_editor):
    Evidence = apps.get_model("project_content", "AcceptanceCriterionEvidence")
    queryset = Evidence.objects.filter(generation_run__isnull=True).select_related(
        "criterion"
    )
    for evidence in queryset.iterator():
        source_run_id = evidence.criterion.source_run_id
        if source_run_id:
            evidence.generation_run_id = source_run_id
            evidence.save(update_fields=["generation_run"])


class Migration(migrations.Migration):
    dependencies = [
        ("project_content", "0002_projectcontentauditevent"),
    ]

    operations = [
        migrations.AddField(
            model_name="acceptancecriterionevidence",
            name="generation_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="evidence_records",
                to="project_content.acceptancegenerationrun",
            ),
        ),
        migrations.RunPython(backfill_generation_runs, migrations.RunPython.noop),
    ]
