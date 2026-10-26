# LOT 3D-3A TER: two small, additive Correspondence fields to support the validated Lovable
# form -- `external_reference` (the external party's own free-text reference number, distinct
# from `reference`, this system's own permanent auto-generated number) and `responsible` (the
# person accountable for this Correspondence, mirroring Document.assignee's exact FK shape).
#
# Written by hand rather than via `makemigrations`, same reason as 0203_correspondence_foundation:
# this app has unrelated, pre-existing migration drift (index-naming differences on unrelated
# models, an unrelated historicalobjectassessment.file alteration) that `makemigrations` would
# otherwise fold into this migration. That drift is left untouched -- this migration only adds
# the two fields (+ their django-simple-history HistoricalCorrespondence mirrors).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0205_transmittal_foundation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='correspondence',
            name='external_reference',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='responsible',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='responsible_correspondences',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='external_reference',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='responsible',
            field=models.ForeignKey(
                blank=True, db_constraint=False, null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
