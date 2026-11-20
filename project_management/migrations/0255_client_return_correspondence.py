from django.db import migrations, models
import django.db.models.deletion


def seed_surveillance_type(apps, schema_editor):
    for app, name in [('project_management', 'ECPMPConfig'), ('models', 'CompanyConfig')]:
        model = apps.get_model(app, name)
        for config in model.objects.all().only('pk', 'correspondence_types').iterator():
            values = list(config.correspondence_types or [])
            if 'Surveillance' not in values:
                model.objects.filter(pk=config.pk).update(correspondence_types=values + ['Surveillance'])


class Migration(migrations.Migration):
    dependencies = [('project_management', '0254_limit_document_states')]
    operations = [
        migrations.AddField(model_name='clientsurveillancereturn', name='source_correspondence', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='client_surveillance_returns', to='project_management.correspondence')),
        migrations.AddConstraint(model_name='clientsurveillancereturn', constraint=models.UniqueConstraint(fields=('client_issue', 'source_correspondence'), condition=models.Q(source_correspondence__isnull=False, supersedes__isnull=True), name='client_return_issue_corr_once')),
        migrations.RunPython(seed_surveillance_type, migrations.RunPython.noop),
    ]
