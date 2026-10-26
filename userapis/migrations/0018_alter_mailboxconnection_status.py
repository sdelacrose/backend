# MAIL-1.1C1 section 5: adds the "pending" status choice (a configuration created through the
# management API, not yet OAuth-connected -- never "active" without real OAuth, see
# userapis/models.py's own Status docstring). Pure choices/metadata change: no real column
# alteration, no data migration, nothing destructive -- confirmed via the exact SQL below.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('userapis', '0017_seed_mail_admin_permission'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mailboxconnection',
            name='status',
            field=models.CharField(choices=[('pending', 'Pending connection'), ('active', 'Active'), ('revoked', 'Revoked'), ('expired', 'Expired'), ('needs_reconsent', 'Needs reconsent')], default='active', max_length=20),
        ),
    ]
