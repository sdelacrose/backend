from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('project_management', '0251_document_due_date')]
    operations = [
        migrations.AddField(model_name='document', name='planned_client_revision', field=models.CharField(max_length=20, blank=True, null=True)),
        migrations.AddField(model_name='historicaldocument', name='planned_client_revision', field=models.CharField(max_length=20, blank=True, null=True)),
    ]
