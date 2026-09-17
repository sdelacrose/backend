from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0017_chatpresence_is_online_chatpresencesession"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="mentions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
