from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0087_invoice_line_item_quantity_float"),
        ("userapis", "0012_userdashboardconfig_personal_dashboard"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonthlyTimesheetSubmission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("month", models.PositiveSmallIntegerField()),
                ("year", models.PositiveSmallIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                        ],
                        default="draft",
                        max_length=20,
                    ),
                ),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reviewed_monthly_timesheet_submissions",
                        to="models.user",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="monthly_timesheet_submissions",
                        to="models.user",
                    ),
                ),
            ],
            options={
                "ordering": ["-year", "-month", "-id"],
                "indexes": [
                    models.Index(
                        fields=["user", "year", "month"],
                        name="mts_user_period_idx",
                    ),
                    models.Index(fields=["status"], name="monthly_timesheet_status_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "month", "year"),
                        name="unique_monthly_timesheet_submission_per_user_period",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="TimesheetActivity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("week_start_date", models.DateField()),
                ("week_end_date", models.DateField()),
                (
                    "source_type",
                    models.CharField(
                        choices=[
                            ("company_action_item", "Company Action Item"),
                            ("company_calendar_event", "Company Calendar Event"),
                            ("project", "Project"),
                            ("project_phase", "Project Phase"),
                            ("project_action_item", "Project Action Item"),
                        ],
                        max_length=40,
                    ),
                ),
                ("activity_code", models.CharField(blank=True, default="", max_length=255)),
                ("activity_name", models.CharField(max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company_action_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="company_timesheet_activities",
                        to="project_management.actionitem",
                    ),
                ),
                (
                    "company_calendar_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timesheet_activities",
                        to="userapis.calendarevent",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_timesheet_activities",
                        to="models.user",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timesheet_activities",
                        to="project_management.project",
                    ),
                ),
                (
                    "project_action_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_timesheet_activities",
                        to="project_management.actionitem",
                    ),
                ),
                (
                    "project_milestone",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timesheet_activities",
                        to="project_management.milestone",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timesheet_activities",
                        to="models.user",
                    ),
                ),
            ],
            options={
                "ordering": ["week_start_date", "id"],
                "indexes": [
                    models.Index(
                        fields=["user", "week_start_date"],
                        name="ts_act_user_week_idx",
                    ),
                    models.Index(
                        fields=["user", "source_type"],
                        name="ts_act_user_src_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="MonthlyTimesheetFeedback",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "feedback_type",
                    models.CharField(
                        choices=[
                            ("rejection", "Rejection"),
                            ("resubmission", "Resubmission"),
                            ("comment", "Comment"),
                        ],
                        default="rejection",
                        max_length=20,
                    ),
                ),
                ("comment", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="monthly_timesheet_feedback_entries",
                        to="models.user",
                    ),
                ),
                (
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feedback_entries",
                        to="project_management.monthlytimesheetsubmission",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["submission", "created_at"],
                        name="mts_feedback_sub_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="TimesheetEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entry_date", models.DateField()),
                ("hours", models.FloatField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "activity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entries",
                        to="project_management.timesheetactivity",
                    ),
                ),
            ],
            options={
                "ordering": ["entry_date", "id"],
                "indexes": [
                    models.Index(fields=["entry_date"], name="timesheet_entry_date_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("activity", "entry_date"),
                        name="unique_timesheet_entry_per_activity_date",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="timecard",
            name="is_generated_from_timesheet",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="timecard",
            name="timesheet_entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="generated_time_cards",
                to="project_management.timesheetentry",
            ),
        ),
        migrations.AddField(
            model_name="timecard",
            name="timesheet_submission",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="generated_time_cards",
                to="project_management.monthlytimesheetsubmission",
            ),
        ),
    ]
