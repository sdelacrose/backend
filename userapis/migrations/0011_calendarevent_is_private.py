from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("userapis", "0010_alter_calendarevent_assignees"),
    ]

    operations = [
        migrations.AddField(
            model_name="calendarevent",
            name="is_private",
            field=models.BooleanField(default=False),
        ),
    ]
