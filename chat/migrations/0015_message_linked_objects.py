from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0014_chatroom_group_lifecycle_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="linked_objects",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
