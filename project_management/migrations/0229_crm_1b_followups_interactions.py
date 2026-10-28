# CRM-1B: purely additive -- two nullable FKs, two M2M link tables and one new table.
# No existing column is altered, no data is rewritten.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0129_companyprofile_company_code"),
        ("companyprofileapis", "0015_backfill_offer_pdf_documents"),
        ("project_management", "0228_seed_correspondence_permissions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="contactuser",
            name="owner",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="owned_contact_users", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="contactcompany",
            name="owner",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="owned_contact_companies", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="actionitem",
            name="contact_users",
            field=models.ManyToManyField(
                blank=True, related_name="action_items", to="project_management.contactuser",
            ),
        ),
        migrations.AddField(
            model_name="actionitem",
            name="contact_companies",
            field=models.ManyToManyField(
                blank=True, related_name="action_items", to="project_management.contactcompany",
            ),
        ),
        migrations.CreateModel(
            name="CrmInteraction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("type", models.CharField(choices=[("note", "Note"), ("call", "Call"), ("meeting", "Meeting")], max_length=16)),
                ("occurred_at", models.DateTimeField()),
                ("summary", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("author", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="crm_interactions", to=settings.AUTH_USER_MODEL,
                )),
                ("company_profile", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name="crm_interactions", to="models.companyprofile",
                )),
                ("freelancer", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name="freelancer_crm_interactions", to=settings.AUTH_USER_MODEL,
                )),
                ("contact_user", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="crm_interactions", to="project_management.contactuser",
                )),
                ("contact_company", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="crm_interactions", to="project_management.contactcompany",
                )),
                ("opportunity", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="crm_interactions", to="companyprofileapis.opportunity",
                )),
            ],
            options={
                "ordering": ["-occurred_at", "-id"],
                "indexes": [
                    models.Index(fields=["company_profile", "occurred_at"], name="crm_inter_company_idx"),
                    models.Index(fields=["freelancer", "occurred_at"], name="crm_inter_freelancer_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            (models.Q(company_profile__isnull=False) & models.Q(freelancer__isnull=True))
                            | (models.Q(company_profile__isnull=True) & models.Q(freelancer__isnull=False))
                        ),
                        name="crm_interaction_single_tenant",
                    ),
                ],
            },
        ),
    ]
