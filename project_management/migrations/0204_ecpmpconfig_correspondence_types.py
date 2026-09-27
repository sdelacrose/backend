# LOT 3B stabilization: Correspondence Type reference data on ECPMPConfig.
#
# Written by hand: `makemigrations` folds in the same pre-existing, unrelated drift already
# reported in LOT 2D-12/3B (historical index-naming differences, an unrelated
# `historicalobjectassessment.file` alteration). That drift is left untouched -- this
# migration only adds the one field.

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0203_correspondence_foundation'),
    ]

    operations = [
        migrations.AddField(
            model_name='ecpmpconfig',
            name='correspondence_types',
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=255), blank=True, default=list, size=None,
            ),
        ),
    ]
