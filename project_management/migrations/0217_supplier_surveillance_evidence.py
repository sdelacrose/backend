# L3G-1: Supplier Surveillance final evidence foundation (new SupplierSurveillanceEvidence
# model; SupplierSurveillanceEvent.event_type += PDF_GENERATED).
#
# Written by hand rather than via `makemigrations`: this app already has the same unrelated,
# pre-existing migration drift noted in every migration since 0207_correspondence_validation_
# workflow (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold in here -- none of
# it belongs to this lot and none of it is included. The CreateModel and the event_type choices
# update below are otherwise exactly what Django generated (dry-run, confirmed clean of any
# other diff): CreateModel depended only on this app's own 0216 + the swappable AUTH_USER_MODEL
# dependency, with no AddField split for the OneToOne/FK fields (both are declared inline below,
# matching Django's own single-CreateModel output for this model).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import project_management.models.supplier_surveillance_evidence


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('project_management', '0216_supplier_surveillance_closure'),
    ]

    operations = [
        migrations.CreateModel(
            name='SupplierSurveillanceEvidence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to=project_management.models.supplier_surveillance_evidence.get_supplier_surveillance_evidence_upload_path)),
                ('generated_at', models.DateTimeField(auto_now_add=True)),
                ('file_sha256', models.CharField(blank=True, max_length=64, null=True)),
                ('evidence_metadata', models.JSONField(blank=True, default=dict)),
                ('generated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='generated_supplier_surveillance_evidence', to=settings.AUTH_USER_MODEL)),
                ('surveillance', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='evidence', to='project_management.suppliersurveillance')),
            ],
            options={
                'ordering': ['-generated_at', '-id'],
            },
        ),
        migrations.AlterField(
            model_name='suppliersurveillanceevent',
            name='event_type',
            field=models.CharField(choices=[('SURVEILLANCE_CREATED', 'Surveillance created'), ('REVIEWER_ASSIGNED', 'Reviewer assigned'), ('REVIEWER_REMOVED', 'Reviewer removed'), ('REVIEW_STARTED', 'Review started'), ('RESPONSE_UPDATED', 'Response updated'), ('REVIEW_COMPLETED', 'Review completed'), ('READY_FOR_CLOSURE', 'Ready for closure'), ('SURVEILLANCE_CLOSED', 'Surveillance closed'), ('PDF_GENERATED', 'Evidence PDF generated')], max_length=32),
        ),
    ]
