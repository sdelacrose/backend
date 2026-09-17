from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    replaces = [("project_management", "0090_companypublicholiday")]

    dependencies = [
        ("project_management", "0090_merge_20260507_0930"),
        ("models", "0068_globalconfig_companyconfig_labels"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanyPublicHoliday",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField()),
                ("name", models.CharField(max_length=255)),
                (
                    "source",
                    models.CharField(
                        choices=[("manual", "Manual"), ("imported", "Imported")],
                        default="manual",
                        max_length=20,
                    ),
                ),
                ("country_code", models.CharField(blank=True, max_length=10, null=True)),
                ("external_id", models.CharField(blank=True, max_length=255, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="public_holidays",
                        to="models.companyprofile",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_public_holidays",
                        to="models.user",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="companypublicholiday",
            constraint=models.UniqueConstraint(
                fields=("company_profile", "date"),
                name="unique_public_holiday_per_company_date",
            ),
        ),
        migrations.AddIndex(
            model_name="companypublicholiday",
            index=models.Index(
                fields=["company_profile", "date"],
                name="pub_holiday_company_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="companypublicholiday",
            index=models.Index(fields=["source"], name="pub_holiday_source_idx"),
        ),
    ]
