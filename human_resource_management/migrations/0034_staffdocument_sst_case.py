# LOT L04 (SST): single additive field, hand-written to exclude the same
# pre-existing unrelated drift excluded by 0033.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0033_l04_sst'),
    ]

    operations = [
        migrations.AddField(
            model_name='staffdocument',
            name='sst_case',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='documents', to='human_resource_management.sstcase'),
        ),
    ]
