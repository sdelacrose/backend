# L3H-1: Document Review Durations configuration (new DocumentTypeReviewDuration model).
#
# Written by hand rather than via `makemigrations`: this app already has the same unrelated,
# pre-existing migration drift noted in every migration since 0207_correspondence_validation_
# workflow (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold in here -- none of
# it belongs to this lot and none of it is included. The CreateModel below is otherwise exactly
# what Django generated (dry-run, confirmed clean of any other diff): the `project` FK and its
# UniqueConstraint were generated as a separate AddField/AddConstraint pair (Django's own
# ForeignKey-after-CreateModel convention), folded inline here, matching 0217's own precedent.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0217_supplier_surveillance_evidence'),
    ]

    operations = [
        migrations.CreateModel(
            name='DocumentTypeReviewDuration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('document_type', models.CharField(max_length=255)),
                ('supplier_review_duration_days', models.PositiveIntegerField(blank=True, null=True)),
                ('client_review_duration_days', models.PositiveIntegerField(blank=True, null=True)),
                ('internal_client_comment_integration_days', models.PositiveIntegerField(blank=True, null=True)),
                ('supplier_client_comment_return_days', models.PositiveIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='document_type_review_durations', to='project_management.project')),
            ],
            options={
                'ordering': ['document_type', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='documenttypereviewduration',
            constraint=models.UniqueConstraint(fields=('project', 'document_type'), name='document_type_review_duration_unique_per_project'),
        ),
    ]
