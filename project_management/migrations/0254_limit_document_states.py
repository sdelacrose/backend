from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('project_management', '0253_document_issue_states')]
    # Keep the migration identity for local databases that already applied it.
    # 0253 adds PREL/BPE without discarding configured or historical statuses.
    # The manual creation selector owns its PREL/BPE choices; changing that UI
    # must not overwrite every company's/project's existing configuration.
    operations = [migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop)]
