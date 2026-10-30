from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0014_leavetypeaccrualratehistory'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalleaveentitlement',
            name='accrual_override_effective_from',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='leaveentitlement',
            name='accrual_override_effective_from',
            field=models.DateField(blank=True, null=True),
        ),
    ]
