# L3B: Supplier Surveillance foundation (SupplierSurveillance, SupplierSurveillanceReferenceCounter).
#
# Written by hand rather than via `makemigrations`: this app already has unrelated, pre-existing
# migration drift (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold into this migration
# -- see 0207_correspondence_validation_workflow through 0212_documentcontrol_verifier for the
# same precedent. None of it belongs to this lot and none of it is included here. The two models
# below are otherwise exactly what Django generated (dry-run, confirmed clean of any other diff).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('project_management', '0212_documentcontrol_verifier'),
    ]

    operations = [
        migrations.CreateModel(
            name='SupplierSurveillance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reference', models.CharField(max_length=64, unique=True)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('UNDER_REVIEW', 'Under review'), ('READY_FOR_CLOSURE', 'Ready for closure'), ('CLOSED', 'Closed')], default='DRAFT', max_length=20)),
                ('result', models.CharField(blank=True, choices=[('REVIEWED', 'Reviewed'), ('COMMENTED', 'Commented'), ('REJECTED', 'Rejected')], max_length=20, null=True)),
                ('new_revision_required', models.BooleanField(blank=True, null=True)),
                ('template_snapshot', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_supplier_surveillances', to=settings.AUTH_USER_MODEL)),
                ('document_revision', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='supplier_surveillance', to='project_management.documentrevision')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='supplier_surveillances', to='project_management.project')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='SupplierSurveillanceReferenceCounter',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_sequence', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='supplier_surveillance_reference_counter', to='project_management.project')),
            ],
        ),
    ]
