from django.db import migrations, models


class Migration(migrations.Migration):
    """CORR-SIGN-1: accept the SIGN_AUTHOR workflow type. The verifier/type check constraint is
    widened so SIGN_AUTHOR (like ONE_STEP) must have no verifier; existing rows are ONE_STEP or
    TWO_STEP and still satisfy it."""

    dependencies = [
        ("project_management", "0224_correspondence_send_event_mailbox"),
    ]

    operations = [
        migrations.AlterField(
            model_name="correspondencevalidationworkflow",
            name="workflow_type",
            field=models.CharField(
                choices=[
                    ("ONE_STEP", "Author -> Approver"),
                    ("TWO_STEP", "Author -> Verifier -> Approver"),
                    ("SIGN_AUTHOR", "Simple signature -> Author"),
                ],
                max_length=20,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="correspondencevalidationworkflow",
            name="correspondence_workflow_verifier_matches_type",
        ),
        migrations.AddConstraint(
            model_name="correspondencevalidationworkflow",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("verifier__isnull", True),
                        ("workflow_type__in", ["ONE_STEP", "SIGN_AUTHOR"]),
                    ),
                    models.Q(("verifier__isnull", False), ("workflow_type", "TWO_STEP")),
                    _connector="OR",
                ),
                name="correspondence_workflow_verifier_matches_type",
            ),
        ),
    ]
