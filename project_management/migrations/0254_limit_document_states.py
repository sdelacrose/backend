from django.db import migrations


def limit_document_states(apps, schema_editor):
    for app, name in [('project_management', 'ECPMPConfig'), ('models', 'CompanyConfig')]:
        model = apps.get_model(app, name)
        model.objects.all().update(document_status=['PREL', 'BPE'])


class Migration(migrations.Migration):
    dependencies = [('project_management', '0253_document_issue_states')]
    operations = [migrations.RunPython(limit_document_states, migrations.RunPython.noop)]
