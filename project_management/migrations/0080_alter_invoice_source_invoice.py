from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0079_merge_20260313_0937"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoice",
            name="source_invoice",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="credit_notes",
                to="project_management.invoice",
            ),
        ),
    ]
