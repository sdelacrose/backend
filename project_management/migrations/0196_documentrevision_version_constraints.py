from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    # Separate transaction from the data population of 0195 (PostgreSQL refuses to ALTER a
    # table that still has pending trigger events from updates in the same transaction).

    dependencies = [
        ("project_management", "0195_documentrevision_version_revision"),
    ]

    operations = [
        migrations.AlterField(
            model_name="documentrevision",
            name="version_number",
            field=models.PositiveIntegerField(),
        ),
        migrations.AddConstraint(
            model_name="documentrevision",
            constraint=models.UniqueConstraint(
                fields=("document", "version_number"),
                name="document_revision_unique_version_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentrevision",
            constraint=models.UniqueConstraint(
                condition=Q(revision_label__isnull=False),
                fields=("document", "revision_label"),
                name="document_revision_unique_revision_label",
            ),
        ),
    ]
