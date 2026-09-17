from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0081_actionitemcomment_actionitemcommentreaction"),
    ]

    operations = [
        migrations.AddField(
            model_name="actionitemcomment",
            name="mentions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
