from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0112_invoice_collaborators"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="excluded_auto_documents",
            field=models.ManyToManyField(
                blank=True,
                related_name="billing_invoices_auto_excluded",
                to="project_management.document",
            ),
        ),
    ]
