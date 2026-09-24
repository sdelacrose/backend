import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0193_documentrevision"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentWorkflow",
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
                    "workflow_type",
                    models.CharField(
                        choices=[
                            ("TWO_STEP", "Author -> Approver"),
                            ("THREE_STEP", "Author -> Verifier -> Approver"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("VERIFY", "Verify"),
                            ("APPROVE", "Approve"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                        ],
                        default="DRAFT",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "approver",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approver_document_workflows",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="authored_document_workflows",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_document_workflows",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workflows",
                        to="project_management.documentrevision",
                    ),
                ),
                (
                    "verifier",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="verifier_document_workflows",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="DocumentWorkflowEvent",
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
                    "step",
                    models.CharField(
                        choices=[
                            ("START", "Start"),
                            ("AUTHOR_SIGN", "Author signature"),
                            ("VERIFY", "Verification"),
                            ("APPROVE", "Approval"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("START", "Started"),
                            ("SIGN", "Signed"),
                            ("VALIDATE", "Validated"),
                            ("APPROVE", "Approved"),
                            ("REJECT", "Rejected"),
                            ("CHANGE_ACTOR", "Actor changed"),
                        ],
                        max_length=20,
                    ),
                ),
                ("comment", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "new_actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "previous_actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="document_workflow_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "workflow",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="project_management.documentworkflow",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="documentworkflow",
            constraint=models.UniqueConstraint(
                condition=Q(status__in=["DRAFT", "VERIFY", "APPROVE"]),
                fields=("revision",),
                name="document_workflow_one_active_per_revision",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentworkflow",
            constraint=models.CheckConstraint(
                condition=(
                    Q(workflow_type="TWO_STEP", verifier__isnull=True)
                    | Q(workflow_type="THREE_STEP", verifier__isnull=False)
                ),
                name="document_workflow_verifier_matches_type",
            ),
        ),
    ]
