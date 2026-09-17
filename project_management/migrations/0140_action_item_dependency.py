from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0139_milestone_sort_order"),
    ]

    operations = [
        migrations.CreateModel(
            name="ActionItemDependency",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "dependency_type",
                    models.CharField(
                        choices=[
                            ("finish_to_start", "Finish to start"),
                        ],
                        default="finish_to_start",
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "dependent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="incoming_dependencies",
                        to="project_management.actionitem",
                    ),
                ),
                (
                    "predecessor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="outgoing_dependencies",
                        to="project_management.actionitem",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["predecessor", "dependency_type"],
                        name="ai_dep_predecessor_type_idx",
                    ),
                    models.Index(
                        fields=["dependent", "dependency_type"],
                        name="ai_dep_dependent_type_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "predecessor",
                            "dependent",
                            "dependency_type",
                        ),
                        name="unique_action_item_dependency",
                    ),
                    models.CheckConstraint(
                        condition=~models.Q(
                            ("predecessor", models.F("dependent"))
                        ),
                        name="action_item_dependency_not_self",
                    ),
                ],
            },
        ),
    ]
