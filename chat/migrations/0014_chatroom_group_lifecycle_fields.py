from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0013_chatpushdevice_token_uniqueness"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatroom",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatroom",
            name="is_closed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="chatroom",
            name="closed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="closed_chat_rooms",
                to="models.user",
            ),
        ),
    ]
