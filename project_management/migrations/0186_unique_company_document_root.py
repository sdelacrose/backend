from django.db import migrations, models
from django.db.models import Count


def check_roots(apps, schema_editor):
    folder = apps.get_model('project_management', 'Folder')
    duplicates = folder.objects.using(schema_editor.connection.alias).filter(
        parent__isnull=True, company_profile__isnull=False,
    ).values('company_profile_id', 'name').annotate(n=Count('id')).filter(n__gt=1)
    if duplicates.exists():
        raise RuntimeError(
            'Duplicate company roots exist. Back up the database, run '
            'repair_company_document_roots --apply, then retry migrate.'
        )


class Migration(migrations.Migration):
    dependencies = [('project_management', '0185_workstation_source_requirement')]
    operations = [
        migrations.RunPython(check_roots, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='folder',
            constraint=models.UniqueConstraint(
                fields=('company_profile', 'name'),
                condition=models.Q(parent__isnull=True, company_profile__isnull=False),
                name='folder_unique_company_root_name',
            ),
        ),
    ]
