from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0107_contactuser_mobile_number"),
        ("userapis", "0012_userdashboardconfig_personal_dashboard"),
    ]

    operations = [
        migrations.AddField(
            model_name="calendarevent",
            name="object_assessments",
            field=models.ManyToManyField(
                blank=True,
                related_name="calendar_events",
                to="project_management.objectassessment",
            ),
        ),
        migrations.AddField(
            model_name="calendarevent",
            name="project",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="calendar_events",
                to="project_management.project",
            ),
        ),
    ]
