from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('project_management', '0231_document_logical_withdrawal')]
    operations = [migrations.AlterField(
        model_name='transmittalevent', name='event_type',
        field=models.CharField(max_length=20, choices=[
            ('CREATED','Created'),('ITEM_ADDED','Item added'),('ITEM_REMOVED','Item removed'),
            ('ISSUED','Issued'),('RECEIVED','Received'),('SEND_STARTED','Sending'),
            ('SEND_SUCCEEDED','Sent'),('SEND_FAILED','Send failed'),
        ]),
    )]
