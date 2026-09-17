from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0114_projectphase_purchase_order_single"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactuser",
            name="spoken_languages",
            field=ArrayField(
                base_field=models.CharField(max_length=100),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="contactuser",
            name="availability_from",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contactuser",
            name="availability_until",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contactuser",
            name="availability_location",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contactuser",
            name="open_to_new_challenges",
            field=models.BooleanField(default=False),
        ),
    ]
