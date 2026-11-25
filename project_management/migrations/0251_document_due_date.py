from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('project_management', '0250_transmittal_client_return_lines')]
    operations = [
        migrations.AddField(model_name='document', name='due_date', field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name='historicaldocument', name='due_date', field=models.DateField(blank=True, null=True)),
    ]
