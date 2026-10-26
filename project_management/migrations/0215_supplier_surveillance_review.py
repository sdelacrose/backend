# L3D: Supplier Surveillance Reviewers & Execution (SupplierSurveillanceReviewerAssignment,
# SupplierSurveillanceResponse, SupplierSurveillanceEvent).
#
# Written by hand rather than via `makemigrations`: this app already has unrelated, pre-existing
# migration drift (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold into this migration
# -- see 0207_correspondence_validation_workflow through 0214_supplier_surveillance_template for
# the same precedent. None of it belongs to this lot and none of it is included here. The three
# models below are otherwise exactly what Django generated (dry-run, confirmed clean of any
# other diff).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('project_management', '0214_supplier_surveillance_template'),
    ]

    operations = [
        migrations.CreateModel(
            name='SupplierSurveillanceReviewerAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('ASSIGNED', 'Assigned'), ('IN_PROGRESS', 'In progress'), ('COMPLETED', 'Completed')], default='ASSIGNED', max_length=20)),
                ('assigned_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assigned_supplier_surveillance_reviewers', to=settings.AUTH_USER_MODEL)),
                ('reviewer', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='supplier_surveillance_reviewer_assignments', to=settings.AUTH_USER_MODEL)),
                ('surveillance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reviewer_assignments', to='project_management.suppliersurveillance')),
            ],
            options={
                'ordering': ['assigned_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='SupplierSurveillanceResponse',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_snapshot_id', models.PositiveIntegerField()),
                ('item_type', models.CharField(max_length=20)),
                ('check_value', models.CharField(blank=True, choices=[('COMPLIANT', 'Compliant'), ('NON_COMPLIANT', 'Non-compliant'), ('NOT_APPLICABLE', 'Not applicable')], max_length=20, null=True)),
                ('text_value', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='responses', to='project_management.suppliersurveillancereviewerassignment')),
            ],
            options={
                'ordering': ['item_snapshot_id', 'id'],
            },
        ),
        migrations.CreateModel(
            name='SupplierSurveillanceEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('SURVEILLANCE_CREATED', 'Surveillance created'), ('REVIEWER_ASSIGNED', 'Reviewer assigned'), ('REVIEWER_REMOVED', 'Reviewer removed'), ('REVIEW_STARTED', 'Review started'), ('RESPONSE_UPDATED', 'Response updated'), ('REVIEW_COMPLETED', 'Review completed'), ('READY_FOR_CLOSURE', 'Ready for closure')], max_length=32)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='supplier_surveillance_events', to=settings.AUTH_USER_MODEL)),
                ('assignment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='events', to='project_management.suppliersurveillancereviewerassignment')),
                ('surveillance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='project_management.suppliersurveillance')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='suppliersurveillancereviewerassignment',
            constraint=models.UniqueConstraint(fields=('surveillance', 'reviewer'), name='supplier_surveillance_reviewer_unique'),
        ),
        migrations.AddConstraint(
            model_name='suppliersurveillanceresponse',
            constraint=models.UniqueConstraint(fields=('assignment', 'item_snapshot_id'), name='supplier_surveillance_response_unique_item'),
        ),
    ]
