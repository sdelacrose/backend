import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0006_alter_leaverequest_manager'),
        ('models', '0134_globalconfig_leave_type_organization_defaults'),
    ]

    operations = [
        migrations.AddField(
            model_name='leaverequest',
            name='leave_type',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='leave_requests',
                to='models.leavetype',
            ),
        ),
        migrations.AddField(
            model_name='leaverequest',
            name='leave_sub_type',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='leave_requests',
                to='models.leavesubtype',
            ),
        ),
        migrations.AddField(
            model_name='leaverequest',
            name='is_half_day',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='leaverequest',
            name='half_day_period',
            field=models.CharField(
                blank=True,
                choices=[('morning', 'Morning'), ('afternoon', 'Afternoon')],
                max_length=16,
                null=True,
            ),
        ),
    ]
