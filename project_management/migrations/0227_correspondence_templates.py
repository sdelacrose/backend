# MAIL-1.5-B: the company's library of e-mail templates -- two NEW tables (CorrespondenceTemplate and its
# simple_history table), additive only: no existing table or column is touched, no data migration, no
# starter template inserted, nothing is deleted. Hand-written from the operations makemigrations
# proposes for these two models, WITHOUT the large batch of unrelated index-rename / field drift it
# also proposes for other models (and for the `models` app) -- same precedent as 0224 and 0226.
# NOT applied by the lot that created it.

import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0129_companyprofile_company_code'),
        ('project_management', '0226_correspondence_send_proofs'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CorrespondenceTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, default='')),
                ('language', models.CharField(choices=[('FR', 'Français'), ('EN', 'English'), ('DE', 'Deutsch')], max_length=2)),
                ('subject_template', models.CharField(max_length=500)),
                ('body_template', models.TextField()),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='correspondence_templates', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_correspondence_templates', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name', 'id'],
            },
        ),
        migrations.CreateModel(
            name='HistoricalCorrespondenceTemplate',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, default='')),
                ('language', models.CharField(choices=[('FR', 'Français'), ('EN', 'English'), ('DE', 'Deutsch')], max_length=2)),
                ('subject_template', models.CharField(max_length=500)),
                ('body_template', models.TextField()),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(blank=True, editable=False)),
                ('updated_at', models.DateTimeField(blank=True, editable=False)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('company_profile', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'historical correspondence template',
                'verbose_name_plural': 'historical correspondence templates',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.AddConstraint(
            model_name='correspondencetemplate',
            constraint=models.UniqueConstraint(fields=('company_profile', 'name'), name='correspondence_template_unique_name_per_company'),
        ),
    ]
