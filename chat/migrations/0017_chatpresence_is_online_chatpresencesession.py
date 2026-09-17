import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0016_chatpresence"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatpresence",
            name="is_online",
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name="chatpresence",
            index=models.Index(fields=["is_online"], name="chat_chatpr_is_onli_81e6d6_idx"),
        ),
        migrations.CreateModel(
            name="ChatPresenceSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_key", models.CharField(max_length=128)),
                ("last_heartbeat_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="chat_presence_sessions", to="models.user"),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["user", "closed_at"], name="chat_chatpr_user_id_1ea15c_idx"),
                    models.Index(fields=["session_key"], name="chat_chatpr_session_2151c1_idx"),
                    models.Index(fields=["last_heartbeat_at"], name="chat_chatpr_last_he_91a57f_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("user", "session_key"), name="chat_presence_session_user_key_unique"),
                ],
            },
        ),
    ]
