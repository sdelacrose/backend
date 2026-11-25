import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import human_resource_management.models.leave


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0007_leaverequest_half_day_and_type_link'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LeaveRequestAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to=human_resource_management.models.leave.leave_attachment_upload_path)),
                ('file_name', models.CharField(max_length=255)),
                ('file_size', models.PositiveIntegerField(blank=True, null=True)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('leave_request', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='human_resource_management.leaverequest')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-uploaded_at'],
            },
        ),
    ]
