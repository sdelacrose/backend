# L05: extends the L03/L05 onboarding engine with `process_kind`
# (onboarding/offboarding) and `step_kind` (checklist/interview/document)
# rather than a parallel engine (spec section 6.9: "un moteur commun
# Onboarding/Offboarding"). Additive/renaming only -- every existing
# onboarding row defaults to process_kind=onboarding, step_kind=checklist,
# unchanged behaviour. Also seeds 3 dedicated, additive staff_management
# permissions for Offboarding (deliberately SEPARATE keys from the L03/L05
# onboarding ones -- arbitrage OFF-2: launching/managing a departure is
# never implied by holding the onboarding equivalent), never assigned to
# any role by default, mirroring 0031's exact pattern.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    permissions = [
        (
            'staff_management.manage_offboarding_templates',
            'Manage Offboarding Templates',
            (
                "Can create/edit a company's offboarding templates. Separate "
                "from staff_management.manage_onboarding_templates -- never "
                "cumulated by default. Never implies the right to trigger a "
                "departure."
            ),
        ),
        (
            'staff_management.offboarding_trigger',
            'Trigger Offboarding',
            (
                "Can launch an offboarding instance for an existing Staff "
                "dossier (manual trigger). Separate from staff_management."
                "onboarding_trigger -- never cumulated by default. Never "
                "implies managing templates."
            ),
        ),
        (
            'staff_management.offboarding_view_all',
            'View All Offboarding Instances',
            (
                "RH global tracking view across every offboarding instance "
                "of the company (the departures dashboard). Separate from "
                "staff_management.onboarding_view_all. Acting on one's OWN "
                "assigned steps never requires this right."
            ),
        ),
    ]
    for key, name, description in permissions:
        Permission.objects.get_or_create(
            key=key, defaults={'name': name, 'description': description, 'module': 'staff_management'},
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('human_resource_management', '0031_l03_l05_staff_documents_and_onboarding'),
        ('models', '0140_staff_contract_type'),
        ('userapis', '0022_mailboximportedfile'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OffboardingAutoTriggerConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_enabled', models.BooleanField(default=False)),
                ('enabled_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='OffboardingTemplateMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('contract_type', models.CharField(choices=[('permanent', 'Permanent'), ('temporary', 'Temporary')], max_length=16)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.RemoveConstraint(
            model_name='onboardinginstance',
            name='unique_active_onboarding_instance_per_staff',
        ),
        migrations.RenameField(
            model_name='onboardinginstance',
            old_name='arrival_date',
            new_name='anchor_date',
        ),
        migrations.AddField(
            model_name='onboardinginstance',
            name='process_kind',
            field=models.CharField(choices=[('onboarding', 'Onboarding'), ('offboarding', 'Offboarding')], default='onboarding', max_length=16),
        ),
        migrations.AddField(
            model_name='onboardinginstance',
            name='source',
            field=models.CharField(choices=[('manual', 'Manual'), ('automatic', 'Automatic')], default='manual', max_length=16),
        ),
        migrations.AddField(
            model_name='onboardinginstance',
            name='trigger_key',
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name='onboardingstepinstance',
            name='calendar_event',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='userapis.calendarevent'),
        ),
        migrations.AddField(
            model_name='onboardingstepinstance',
            name='step_kind',
            field=models.CharField(choices=[('checklist', 'Checklist'), ('interview', 'Interview'), ('document', 'Document')], default='checklist', max_length=16),
        ),
        migrations.AddField(
            model_name='onboardingtemplate',
            name='process_kind',
            field=models.CharField(choices=[('onboarding', 'Onboarding'), ('offboarding', 'Offboarding')], default='onboarding', max_length=16),
        ),
        migrations.AddField(
            model_name='onboardingtemplatestep',
            name='step_kind',
            field=models.CharField(choices=[('checklist', 'Checklist'), ('interview', 'Interview'), ('document', 'Document')], default='checklist', max_length=16),
        ),
        migrations.AlterField(
            model_name='onboardinginstanceevent',
            name='event_type',
            field=models.CharField(choices=[('triggered', 'Triggered'), ('step_completed', 'Step completed'), ('step_reassigned', 'Step reassigned'), ('step_cancelled', 'Step cancelled'), ('arrival_date_changed', 'Arrival date changed'), ('cancelled', 'Cancelled'), ('completed', 'Completed'), ('interview_scheduled', 'Interview scheduled')], max_length=24),
        ),
        migrations.AddConstraint(
            model_name='onboardinginstance',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'active')), fields=('staff', 'process_kind'), name='unique_active_process_instance_per_staff_and_kind'),
        ),
        migrations.AddField(
            model_name='offboardingautotriggerconfig',
            name='company_profile',
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='offboarding_auto_trigger_config', to='models.companyprofile'),
        ),
        migrations.AddField(
            model_name='offboardingautotriggerconfig',
            name='default_template',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='human_resource_management.onboardingtemplate'),
        ),
        migrations.AddField(
            model_name='offboardingautotriggerconfig',
            name='enabled_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='offboardingtemplatemapping',
            name='company_profile',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='offboarding_template_mappings', to='models.companyprofile'),
        ),
        migrations.AddField(
            model_name='offboardingtemplatemapping',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='offboardingtemplatemapping',
            name='template',
            field=models.ForeignKey(limit_choices_to={'process_kind': 'offboarding'}, on_delete=django.db.models.deletion.CASCADE, related_name='offboarding_contract_mappings', to='human_resource_management.onboardingtemplate'),
        ),
        migrations.AddConstraint(
            model_name='offboardingtemplatemapping',
            constraint=models.UniqueConstraint(fields=('company_profile', 'contract_type'), name='unique_offboarding_mapping_per_contract_type'),
        ),
        migrations.RunPython(seed_permissions, noop),
    ]
