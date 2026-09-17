from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0110_timesheetactivity_purchase_order"),
        ("userapis", "0013_calendarevent_object_assessments_and_project"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonthlyTimesheetActivityDocumentLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("month", models.PositiveSmallIntegerField()),
                ("year", models.PositiveSmallIntegerField()),
                ("row_signature", models.CharField(max_length=500)),
                ("source_type", models.CharField(choices=[("company_action_item", "Company Action Item"), ("company_calendar_event", "Company Calendar Event"), ("project", "Project"), ("project_purchase_order", "Project Purchase Order"), ("project_phase", "Project Phase"), ("project_action_item", "Project Action Item")], max_length=40)),
                ("is_invoiceable", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company_action_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_document_links_company_action_item", to="project_management.actionitem")),
                ("company_calendar_event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_document_links_calendar_event", to="userapis.calendarevent")),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_activity_links", to="project_management.document")),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_document_links", to="project_management.project")),
                ("project_action_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_document_links_project_action_item", to="project_management.actionitem")),
                ("project_milestone", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_document_links", to="project_management.milestone")),
                ("purchase_order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_document_links", to="project_management.purchaseorder")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="monthly_timesheet_activity_document_links", to="models.user")),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.AddConstraint(
            model_name="monthlytimesheetactivitydocumentlink",
            constraint=models.UniqueConstraint(fields=("user", "month", "year", "row_signature", "document"), name="unique_monthly_timesheet_activity_document_link"),
        ),
        migrations.AddIndex(
            model_name="monthlytimesheetactivitydocumentlink",
            index=models.Index(fields=["user", "year", "month", "row_signature"], name="mts_doc_link_usr_sig_idx"),
        ),
    ]
