# L03/L05: schema for private HR documents (StaffDocumentCategory/
# StaffDocument/StaffDocumentFile/StaffDocumentFileStatusEvent/
# StaffDocumentGrant/StaffDocumentEvent) and onboarding (OnboardingTemplate/
# ...Step, OnboardingInstance/...StepInstance/...Event), plus the two
# lightweight reminder-delivery ledgers. Also seeds 5 dedicated, additive
# staff_management permissions -- never assigned to any role by default,
# mirroring 0027/0030's exact pattern.
import django.core.files.storage
import django.db.models.deletion
import human_resource_management.models.staff_documents
from django.conf import settings
from django.db import migrations, models


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model('models', 'Permission')
    permissions = [
        (
            'staff_management.view_documents',
            'View Staff Documents',
            (
                "Can view a company's/collaborator's HR documents (shared or "
                "private) -- a DELIBERATELY separate right from staff_"
                "management.view/full_access AND from view_compensation. "
                "Never grants access to compensation figures or health/SST "
                "notes, which remain under their own dedicated rights. Never "
                "assigned by default."
            ),
        ),
        (
            'staff_management.update_documents',
            'Manage Staff Documents',
            (
                "Can upload/rename/verify/accept a collaborator's HR "
                "documents and grant/revoke individual access to them. "
                "Separate from view_documents. The collaborator concerned "
                "may still upload their OWN requested pieces without this "
                "right, but can never verify/accept their own document "
                "(see can_transition_staff_document_file_status)."
            ),
        ),
        (
            'staff_management.manage_onboarding_templates',
            'Manage Onboarding Templates',
            "Can create/edit a company's onboarding templates. Never implies the right to trigger one.",
        ),
        (
            'staff_management.onboarding_trigger',
            'Trigger Onboarding',
            "Can launch an onboarding instance for an existing Staff dossier. Never implies managing templates.",
        ),
        (
            'staff_management.onboarding_view_all',
            'View All Onboarding Instances',
            (
                "RH global tracking view across every onboarding instance of "
                "the company. Acting on one's OWN assigned steps never "
                "requires this right -- assignment itself is sufficient "
                "authorization for that narrower case."
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
        ('human_resource_management', '0030_l01b1_staff_create_permission'),
        ('models', '0140_staff_contract_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OnboardingInstance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('template_snapshot', models.JSONField()),
                ('arrival_date', models.DateField()),
                ('status', models.CharField(choices=[('active', 'Active'), ('cancelled', 'Cancelled'), ('completed', 'Completed')], default='active', max_length=16)),
                ('triggered_at', models.DateTimeField(auto_now_add=True)),
                ('cancelled_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('cancelled_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='onboarding_instances', to='models.companyprofile')),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='onboarding_instances', to='models.staff')),
                ('triggered_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['staff_id', '-triggered_at'],
            },
        ),
        migrations.CreateModel(
            name='OnboardingStepInstance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveIntegerField()),
                ('title', models.CharField(max_length=255)),
                ('due_date', models.DateField()),
                ('visible_to_employee', models.BooleanField(default=False)),
                ('is_required', models.BooleanField(default=True)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('done', 'Done'), ('cancelled', 'Cancelled')], default='pending', max_length=16)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='step_instances', to='human_resource_management.onboardinginstance')),
                ('responsible_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='onboarding_step_responsibilities', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['instance_id', 'order'],
            },
        ),
        migrations.CreateModel(
            name='OnboardingInstanceEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('triggered', 'Triggered'), ('step_completed', 'Step completed'), ('step_reassigned', 'Step reassigned'), ('step_cancelled', 'Step cancelled'), ('arrival_date_changed', 'Arrival date changed'), ('cancelled', 'Cancelled'), ('completed', 'Completed')], max_length=24)),
                ('detail', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='human_resource_management.onboardinginstance')),
                ('step_instance', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='events', to='human_resource_management.onboardingstepinstance')),
            ],
            options={
                'ordering': ['instance_id', 'created_at'],
            },
        ),
        migrations.CreateModel(
            name='OnboardingTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('version', models.PositiveIntegerField(default=1)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='onboarding_templates', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['company_profile_id', 'name'],
            },
        ),
        migrations.AddField(
            model_name='onboardinginstance',
            name='template',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='instances', to='human_resource_management.onboardingtemplate'),
        ),
        migrations.CreateModel(
            name='StaffDocumentCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('scope', models.CharField(choices=[('private', 'Private to one collaborator'), ('shared', 'Shared within the company')], default='private', max_length=16)),
                ('requires_validity_date', models.BooleanField(default=False)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='staff_document_categories', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['company_profile_id', 'name'],
            },
        ),
        migrations.CreateModel(
            name='StaffDocument',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shared_staff_documents', to='models.companyprofile')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('staff', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='documents', to='models.staff')),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='documents', to='human_resource_management.staffdocumentcategory')),
            ],
            options={
                'ordering': ['staff_id', 'category_id', 'id'],
            },
        ),
        migrations.CreateModel(
            name='OnboardingTemplateStep',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveIntegerField()),
                ('title', models.CharField(max_length=255)),
                ('responsible_role', models.CharField(choices=[('supervisor', "Employee's supervisor (Staff.supervisor)"), ('company_admin', 'Company Admin (canonical)'), ('named', 'A specifically named person')], max_length=16)),
                ('due_offset_days', models.IntegerField()),
                ('visible_to_employee', models.BooleanField(default=False)),
                ('is_required', models.BooleanField(default=True)),
                ('default_responsible_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('template', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='steps', to='human_resource_management.onboardingtemplate')),
                ('expected_category', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='human_resource_management.staffdocumentcategory')),
            ],
            options={
                'ordering': ['template_id', 'order'],
            },
        ),
        migrations.AddField(
            model_name='onboardingstepinstance',
            name='expected_category',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='human_resource_management.staffdocumentcategory'),
        ),
        migrations.CreateModel(
            name='StaffDocumentEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('title_changed', 'Title changed'), ('grant_created', 'Grant created'), ('grant_revoked', 'Grant revoked')], max_length=24)),
                ('detail', models.CharField(blank=True, default='', max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('staff_document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='human_resource_management.staffdocument')),
            ],
            options={
                'ordering': ['staff_document_id', 'created_at'],
            },
        ),
        migrations.CreateModel(
            name='StaffDocumentFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version_number', models.PositiveIntegerField()),
                ('file', models.FileField(max_length=500, storage=django.core.files.storage.FileSystemStorage(base_url=None, location='/app/private_media/staff_documents'), upload_to=human_resource_management.models.staff_documents._staff_document_upload_path)),
                ('file_name', models.CharField(max_length=255)),
                ('file_size', models.PositiveIntegerField(blank=True, null=True)),
                ('file_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('valid_from', models.DateField(blank=True, null=True)),
                ('valid_until', models.DateField(blank=True, null=True)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('staff_document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='files', to='human_resource_management.staffdocument')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['staff_document_id', '-version_number'],
            },
        ),
        migrations.CreateModel(
            name='StaffDocumentFileStatusEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('submitted', 'Submitted'), ('verified', 'Verified'), ('accepted', 'Accepted')], max_length=16)),
                ('note', models.CharField(blank=True, default='', max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('file', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='status_events', to='human_resource_management.staffdocumentfile')),
            ],
            options={
                'ordering': ['file_id', 'created_at'],
            },
        ),
        migrations.CreateModel(
            name='StaffDocumentGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('granted_at', models.DateTimeField(auto_now_add=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('granted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('revoked_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('staff_document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='grants', to='human_resource_management.staffdocument')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='staff_document_grants', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['staff_document_id', '-granted_at'],
            },
        ),
        migrations.CreateModel(
            name='StaffDocumentValidityReminderDelivery',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel', models.CharField(choices=[('internal', 'Internal notification'), ('email', 'Email')], max_length=16)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed'), ('ignored', 'Ignored')], default='pending', max_length=16)),
                ('triggered_manually', models.BooleanField(default=False)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('file', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='validity_reminder_deliveries', to='human_resource_management.staffdocumentfile')),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='staff_document_validity_reminder_deliveries', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='OnboardingStepReminderDelivery',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel', models.CharField(choices=[('internal', 'Internal notification'), ('email', 'Email')], max_length=16)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed'), ('ignored', 'Ignored')], default='pending', max_length=16)),
                ('triggered_manually', models.BooleanField(default=False)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='onboarding_step_reminder_deliveries', to=settings.AUTH_USER_MODEL)),
                ('step_instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reminder_deliveries', to='human_resource_management.onboardingstepinstance')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('step_instance', 'recipient', 'channel'), name='unique_onboarding_step_reminder_delivery')],
            },
        ),
        migrations.AddConstraint(
            model_name='onboardinginstance',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'active')), fields=('staff',), name='unique_active_onboarding_instance_per_staff'),
        ),
        migrations.AddConstraint(
            model_name='onboardingtemplatestep',
            constraint=models.UniqueConstraint(fields=('template', 'order'), name='unique_onboarding_template_step_order'),
        ),
        migrations.AddConstraint(
            model_name='staffdocumentfile',
            constraint=models.UniqueConstraint(fields=('staff_document', 'version_number'), name='unique_staff_document_file_version'),
        ),
        migrations.AddConstraint(
            model_name='staffdocumentvalidityreminderdelivery',
            constraint=models.UniqueConstraint(fields=('file', 'recipient', 'channel'), name='unique_staff_document_validity_reminder_delivery'),
        ),
        migrations.RunPython(seed_permissions, noop),
    ]
