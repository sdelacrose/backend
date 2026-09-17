from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0105_matrix_parts_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="actionitem",
            name="forecast_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
