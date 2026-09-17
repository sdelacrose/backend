from django.db import migrations, models
import project_management.models.project


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0130_actionitemreminderdelivery_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='objectassessment',
            name='file',
            field=models.FileField(
                blank=True,
                null=True,
                upload_to=project_management.models.project.get_upload_path,
            ),
        ),
        migrations.AddField(
            model_name='objectassessment',
            name='notes_content_html',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='objectassessment',
            name='notes_content_json',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='historicalobjectassessment',
            name='notes_content_html',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='historicalobjectassessment',
            name='notes_content_json',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
