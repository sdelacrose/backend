from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0021_absence_impact_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='leaveaccrualabsencesignal',
            name='additional_events',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
