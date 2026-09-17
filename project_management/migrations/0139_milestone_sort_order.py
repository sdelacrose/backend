from django.db import migrations, models


def backfill_milestone_sort_order(apps, schema_editor):
    Milestone = apps.get_model("project_management", "Milestone")
    project_ids = (
        Milestone.objects.order_by()
        .values_list("project_id", flat=True)
        .distinct()
    )

    for project_id in project_ids.iterator(chunk_size=500):
        milestones = list(
            Milestone.objects.filter(project_id=project_id)
            .only("id", "sort_order")
            .order_by("id")
        )
        for sort_order, milestone in enumerate(milestones, start=1):
            milestone.sort_order = sort_order
        Milestone.objects.bulk_update(milestones, ["sort_order"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0138_independent_module_status_options_and_bom_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="milestone",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(
            backfill_milestone_sort_order,
            migrations.RunPython.noop,
        ),
        migrations.AlterModelOptions(
            name="milestone",
            options={"ordering": ["sort_order", "id"]},
        ),
        migrations.AddIndex(
            model_name="milestone",
            index=models.Index(
                fields=["project", "sort_order"],
                name="pm_phase_project_order_idx",
            ),
        ),
    ]
