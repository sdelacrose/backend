from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("project_management", "0181_bom_run_operability")]

    operations = [
        migrations.AddField(
            model_name="bomgenerationrun",
            name="claim_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="bomgenerationrun",
            name="claim_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="bomgenerationrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
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
