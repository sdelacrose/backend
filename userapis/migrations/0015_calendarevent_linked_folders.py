from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0121_linked_folders"),
        ("userapis", "0014_calendarevent_room"),
    ]

    operations = [
        migrations.AddField(
            model_name="calendarevent",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="calendar_event_linked_folders",
                to="project_management.folder",
            ),
        ),
    ]
