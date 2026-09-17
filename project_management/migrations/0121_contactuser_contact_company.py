from django.db import migrations, models
import django.db.models.deletion


def backfill_contact_company_links(apps, schema_editor):
    ContactUser = apps.get_model("project_management", "ContactUser")
    ContactCompany = apps.get_model("project_management", "ContactCompany")

    for contact_user in ContactUser.objects.exclude(company_name__isnull=True).exclude(
        company_name=""
    ):
        company_name = contact_user.company_name.strip()
        if not company_name:
            continue

        company_qs = ContactCompany.objects.filter(
            company_name__iexact=company_name,
            is_deleted=False,
        )
        if contact_user.company_profile_id:
            company_qs = company_qs.filter(company_profile_id=contact_user.company_profile_id)
        elif contact_user.user_id:
            company_qs = company_qs.filter(user_id=contact_user.user_id)

        company = company_qs.first()
        if company:
            contact_user.contact_company_id = company.id
            contact_user.save(update_fields=["contact_company_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0120_merge_20260630_2048"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactuser",
            name="contact_company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_contact_users",
                to="project_management.contactcompany",
            ),
        ),
        migrations.RunPython(
            backfill_contact_company_links,
            migrations.RunPython.noop,
        ),
    ]
