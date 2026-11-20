from django.db import migrations


def add_issue_states(apps, schema_editor):
    for app, name in [('project_management', 'ECPMPConfig'), ('models', 'CompanyConfig')]:
        model = apps.get_model(app, name)
        for config in model.objects.all().only('pk', 'document_status').iterator():
            values = list(config.document_status or [])
            missing = [value for value in ('PREL', 'BPE') if value not in values]
            if missing:
                model.objects.filter(pk=config.pk).update(document_status=values + missing)


class Migration(migrations.Migration):
    dependencies = [('project_management', '0252_document_planned_client_revision')]
    operations = [migrations.RunPython(add_issue_states, migrations.RunPython.noop)]
