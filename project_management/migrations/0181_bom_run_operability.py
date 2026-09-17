from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("project_management", "0180_funding_opportunity_hints")]

    operations = [
        migrations.AddField(
            model_name="bomgenerationrun",
            name="request_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="bomgenerationrun",
            name="metrics",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="bomgenerationrun",
            name="node_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="bomgenerationrun",
            name="duration_ms",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="bomgenerationrun",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="bomgenerationrun",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="bomgenerationrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("preview", "Preview"),
                    ("applied", "Applied"),
                    ("stale", "Stale"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="preview",
                max_length=16,
            ),
        ),
    ]
