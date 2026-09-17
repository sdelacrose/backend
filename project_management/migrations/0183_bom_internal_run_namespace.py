from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("project_management", "0182_bom_run_claim_lease")]

    operations = [
        migrations.AddField(
            model_name="bomgenerationrun",
            name="internal_run",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddConstraint(
            model_name="bomgenerationrun",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ("idempotency_key__isnull", False),
                        ("idempotency_key__startswith", "__bom_worker__:"),
                        ("internal_run", True),
                    )
                    | (
                        models.Q(("internal_run", False))
                        & ~models.Q(
                            ("idempotency_key__startswith", "__bom_worker__:"),
                        )
                    )
                ),
                name="bom_internal_key_namespace",
            ),
        ),
    ]
