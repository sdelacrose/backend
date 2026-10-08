# MAIL-1.1C2-S: MailOAuthState -- fixes the predictable/non-expiring/replayable Mail OAuth
# state found during this security checkpoint. Additive only. Generated cleanly by
# makemigrations (no phantom cross-app dependency this time, unlike 0016/0017 -- confirmed via
# --dry-run before writing this by hand for consistency with this app's other migrations).

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('userapis', '0018_alter_mailboxconnection_status'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MailOAuthState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('mailbox', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='oauth_states', to='userapis.mailboxconnection')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mail_oauth_states', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
