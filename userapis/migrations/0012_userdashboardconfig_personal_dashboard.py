from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("userapis", "0011_calendarevent_is_private"),
    ]

    operations = [
        migrations.AddField(
            model_name="userdashboardconfig",
            name="personal_dashboard",
            field=models.JSONField(default=list),
        ),
    ]
