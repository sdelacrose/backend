from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("project_management", "0268_purchase_order_end_date")]
    operations = [
        migrations.AddField(
            model_name="actiontemplate",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
