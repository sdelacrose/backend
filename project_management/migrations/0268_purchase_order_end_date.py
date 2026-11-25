from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('project_management', '0267_evaluation_report')]
    operations = [migrations.AddField(model_name='purchaseorder', name='end_date', field=models.DateField(blank=True, null=True))]
