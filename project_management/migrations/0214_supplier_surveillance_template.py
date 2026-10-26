# L3C: Supplier Surveillance Template library (SupplierSurveillanceTemplate,
# SupplierSurveillanceTemplateSection, SupplierSurveillanceTemplateItem).
#
# Written by hand rather than via `makemigrations`: this app already has unrelated, pre-existing
# migration drift (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold into this migration
# -- see 0207_correspondence_validation_workflow through 0213_supplier_surveillance for the same
# precedent. None of it belongs to this lot and none of it is included here. The three models
# below are otherwise exactly what Django generated (dry-run, confirmed clean of any other diff).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('project_management', '0213_supplier_surveillance'),
    ]

    operations = [
        migrations.CreateModel(
            name='SupplierSurveillanceTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, default='')),
                ('version', models.PositiveIntegerField(default=1)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='supplier_surveillance_templates', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_supplier_surveillance_templates', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='SupplierSurveillanceTemplateSection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, default='')),
                ('order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('template', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='project_management.suppliersurveillancetemplate')),
            ],
            options={
                'ordering': ['order', 'id'],
            },
        ),
        migrations.CreateModel(
            name='SupplierSurveillanceTemplateItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, default='')),
                ('item_type', models.CharField(choices=[('CHECK', 'Check'), ('TEXT', 'Text'), ('TEXTAREA', 'Textarea')], default='CHECK', max_length=20)),
                ('required', models.BooleanField(default=False)),
                ('order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('section', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='project_management.suppliersurveillancetemplatesection')),
            ],
            options={
                'ordering': ['order', 'id'],
            },
        ),
    ]
