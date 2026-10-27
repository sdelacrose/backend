import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0140_staff_contract_type"),
        ("human_resource_management", "0022_signal_additional_events"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffContractAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("field", models.CharField(choices=[("contract_type", "Contract type"), ("contract_end_date", "Contract end date")], max_length=32)),
                ("old_value", models.CharField(blank=True, max_length=255, null=True)),
                ("new_value", models.CharField(blank=True, max_length=255, null=True)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="contract_audit_log", to="models.staff")),
            ],
            options={
                "ordering": ["staff_id", "-changed_at"],
            },
        ),
        migrations.CreateModel(
            name="LeaveBalanceReminderRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("trigger", models.CharField(choices=[("contract_end", "Contract end date"), ("deadline", "Fixed deadline date")], max_length=16)),
                ("deadline_date", models.DateField(blank=True, null=True)),
                ("anticipation_days", models.PositiveIntegerField(help_text="Calendar days before the deadline the reminder window opens. 0 allowed (reminder only on the deadline day itself).")),
                ("all_eligible_staff", models.BooleanField(default=False)),
                ("email_enabled", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company_config", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="leave_balance_reminder_rules", to="models.companyconfig")),
                ("created_by", models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("leave_types", models.ManyToManyField(related_name="leave_balance_reminder_rules", to="models.leavetype")),
                ("staff", models.ManyToManyField(blank=True, related_name="leave_balance_reminder_rules", to="models.staff")),
                ("cc_hr_users", models.ManyToManyField(blank=True, related_name="cc_leave_balance_reminder_rules", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="LeaveReminderDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("deadline_date", models.DateField()),
                ("channel", models.CharField(choices=[("internal", "Internal notification"), ("email", "Email")], max_length=16)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed"), ("ignored", "Ignored")], default="pending", max_length=16)),
                ("triggered_manually", models.BooleanField(default=False)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("rule", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deliveries", to="human_resource_management.leavebalancereminderrule")),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="leave_reminder_deliveries", to="models.staff")),
            ],
        ),
        migrations.AddConstraint(
            model_name="leavereminderdelivery",
            constraint=models.UniqueConstraint(fields=("rule", "staff", "deadline_date", "channel"), name="unique_leave_reminder_delivery"),
        ),
        migrations.AddIndex(
            model_name="leavereminderdelivery",
            index=models.Index(fields=["deadline_date", "status"], name="leave_reminder_date_status_idx"),
        ),
    ]
