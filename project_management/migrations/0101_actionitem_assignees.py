from django.db import migrations, models


def backfill_action_item_assignees(apps, schema_editor):
    ActionItem = apps.get_model("project_management", "ActionItem")

    for action_item in ActionItem.objects.exclude(assignee__isnull=True).iterator():
        action_item.assignees.add(action_item.assignee_id)


def reverse_backfill_action_item_assignees(apps, schema_editor):
    ActionItem = apps.get_model("project_management", "ActionItem")

    for action_item in ActionItem.objects.filter(assignee__isnull=True).iterator():
        first_assignee_id = (
            action_item.assignees.order_by("id").values_list("id", flat=True).first()
        )
        if first_assignee_id:
            action_item.assignee_id = first_assignee_id
            action_item.save(update_fields=["assignee"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0100_remove_ecpmpconfig_directional_po_status_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="actionitem",
            name="assignees",
            field=models.ManyToManyField(
                blank=True,
                related_name="multi_assigned_action_items",
                to="models.user",
            ),
        ),
        migrations.RunPython(
            backfill_action_item_assignees,
            reverse_backfill_action_item_assignees,
        ),
    ]
