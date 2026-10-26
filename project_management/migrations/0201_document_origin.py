import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Document Origin: INTERNAL / CLIENT / SUPPLIER and the external issuer (Network company).

    Existing documents predate the distinction and become INTERNAL.
    """

    dependencies = [
        ("project_management", "0200_document_description_tags"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="document_origin",
            field=models.CharField(
                choices=[
                    ("INTERNAL", "Internal"),
                    ("CLIENT", "Client"),
                    ("SUPPLIER", "Supplier"),
                ],
                db_index=True,
                default="INTERNAL",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="external_issuer_company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="issued_documents",
                to="project_management.contactcompany",
            ),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="document_origin",
            field=models.CharField(
                choices=[
                    ("INTERNAL", "Internal"),
                    ("CLIENT", "Client"),
                    ("SUPPLIER", "Supplier"),
                ],
                db_index=True,
                default="INTERNAL",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="historicaldocument",
            name="external_issuer_company",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="+",
                to="project_management.contactcompany",
            ),
        ),
    ]
