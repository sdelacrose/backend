import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_content", "0004_acceptancegenerationrun_phase_binding"),
        ("project_management", "0169_actionitem_requirements"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AcceptanceActionItemProvenance",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("proposal_key", models.CharField(max_length=128)),
                (
                    "phase_role",
                    models.CharField(
                        choices=[("fat", "FAT"), ("cat", "CAT")],
                        max_length=8,
                    ),
                ),
                ("pairing_key", models.UUIDField(db_index=True, default=uuid.uuid4)),
                ("applied_fields", models.JSONField(default=dict)),
                ("source_evidence", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "action_item",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="acceptance_provenance",
                        to="project_management.actionitem",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_acceptance_action_item_provenance",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "generation_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="action_item_provenance",
                        to="project_content.acceptancegenerationrun",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["generation_run", "phase_role"],
                        name="acceptance_provenance_run_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("generation_run", "proposal_key", "phase_role"),
                        name="acceptance_provenance_unique_role",
                    )
                ],
            },
        )
    ]
