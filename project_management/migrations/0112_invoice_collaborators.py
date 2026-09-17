from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0071_seed_company_roles"),
        ("project_management", "0111_monthlytimesheetactivitydocumentlink"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="collaborators",
            field=models.ManyToManyField(
                blank=True,
                related_name="collaborator_invoices",
                to="models.user",
            ),
        ),
    ]
