# LOT 3B: Correspondence foundation (Correspondence, CorrespondenceAttachment,
# CorrespondenceReferenceCounter, + django-simple-history's HistoricalCorrespondence).
#
# Written by hand rather than via `makemigrations`: this app already has unrelated,
# pre-existing migration drift (historical index-naming differences on unrelated models, and
# an unrelated `historicalobjectassessment.file` field alteration) that `makemigrations`
# would otherwise fold into this migration. That drift is left untouched and reported
# separately -- this migration only creates the three new tables (+ their history table).

import django.db.models.deletion
import project_management.models.correspondence
import simple_history.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0129_companyprofile_company_code'),
        ('project_management', '0202_documentrevision_approval_evidence'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Correspondence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reference', models.CharField(blank=True, db_index=True, default='', max_length=255)),
                ('direction', models.CharField(choices=[('IN', 'Incoming'), ('OUT', 'Outgoing')], max_length=3)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('ISSUED', 'Issued')], default='ISSUED', max_length=10)),
                ('correspondence_type', models.CharField(blank=True, max_length=255, null=True)),
                ('subject', models.CharField(max_length=500)),
                ('message', models.TextField(blank=True, default='')),
                ('issue_date', models.DateField(blank=True, null=True)),
                ('received_date', models.DateField(blank=True, null=True)),
                ('response_required', models.BooleanField(default=False)),
                ('response_due_date', models.DateField(blank=True, null=True)),
                ('response_status', models.CharField(choices=[('NOT_REQUIRED', 'Not required'), ('TO_ANSWER', 'To answer'), ('IN_PREPARATION', 'In preparation'), ('AWAITING_REPLY', 'Awaiting reply'), ('ANSWERED', 'Answered'), ('OVERDUE', 'Overdue')], default='NOT_REQUIRED', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='CorrespondenceAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(max_length=500, upload_to=project_management.models.correspondence.get_correspondence_attachment_upload_path)),
                ('file_name', models.CharField(max_length=500)),
                ('file_size', models.BigIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='CorrespondenceReferenceCounter',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_sequence', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='HistoricalCorrespondence',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('reference', models.CharField(blank=True, db_index=True, default='', max_length=255)),
                ('direction', models.CharField(choices=[('IN', 'Incoming'), ('OUT', 'Outgoing')], max_length=3)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('ISSUED', 'Issued')], default='ISSUED', max_length=10)),
                ('correspondence_type', models.CharField(blank=True, max_length=255, null=True)),
                ('subject', models.CharField(max_length=500)),
                ('message', models.TextField(blank=True, default='')),
                ('issue_date', models.DateField(blank=True, null=True)),
                ('received_date', models.DateField(blank=True, null=True)),
                ('response_required', models.BooleanField(default=False)),
                ('response_due_date', models.DateField(blank=True, null=True)),
                ('response_status', models.CharField(choices=[('NOT_REQUIRED', 'Not required'), ('TO_ANSWER', 'To answer'), ('IN_PREPARATION', 'In preparation'), ('AWAITING_REPLY', 'Awaiting reply'), ('ANSWERED', 'Answered'), ('OVERDUE', 'Overdue')], default='NOT_REQUIRED', max_length=20)),
                ('created_at', models.DateTimeField(blank=True, editable=False)),
                ('updated_at', models.DateTimeField(blank=True, editable=False)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
            ],
            options={
                'verbose_name': 'historical correspondence',
                'verbose_name_plural': 'historical correspondences',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='company_profile',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='correspondences', to='models.companyprofile'),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_correspondences', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='freelancer',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='freelancer_correspondences', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='parent_correspondence',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='responses', to='project_management.correspondence'),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='correspondences', to='project_management.project'),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='recipient_company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='correspondences_received', to='project_management.contactcompany'),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='recipient_contact',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='correspondences_received', to='project_management.contactuser'),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='sender_company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='correspondences_sent', to='project_management.contactcompany'),
        ),
        migrations.AddField(
            model_name='correspondence',
            name='sender_contact',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='correspondences_sent', to='project_management.contactuser'),
        ),
        migrations.AddField(
            model_name='correspondenceattachment',
            name='correspondence',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='project_management.correspondence'),
        ),
        migrations.AddField(
            model_name='correspondenceattachment',
            name='uploaded_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_correspondence_attachments', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='correspondencereferencecounter',
            name='company_profile',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='correspondence_reference_counters', to='models.companyprofile'),
        ),
        migrations.AddField(
            model_name='correspondencereferencecounter',
            name='freelancer',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='correspondence_reference_counters', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='correspondencereferencecounter',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='correspondence_reference_counters', to='project_management.project'),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='company_profile',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='models.companyprofile'),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='created_by',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='freelancer',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='history_user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='parent_correspondence',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.correspondence'),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='project',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.project'),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='recipient_company',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.contactcompany'),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='recipient_contact',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.contactuser'),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='sender_company',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.contactcompany'),
        ),
        migrations.AddField(
            model_name='historicalcorrespondence',
            name='sender_contact',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='project_management.contactuser'),
        ),
        migrations.AddConstraint(
            model_name='correspondence',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('project__isnull', False), ('company_profile__isnull', True), ('freelancer__isnull', True)), models.Q(('project__isnull', True), ('company_profile__isnull', False), ('freelancer__isnull', True)), models.Q(('project__isnull', True), ('company_profile__isnull', True), ('freelancer__isnull', False)), _connector='OR'), name='correspondence_exactly_one_owner_scope'),
        ),
        migrations.AddConstraint(
            model_name='correspondencereferencecounter',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('project__isnull', False), ('company_profile__isnull', True), ('freelancer__isnull', True)), models.Q(('project__isnull', True), ('company_profile__isnull', False), ('freelancer__isnull', True)), models.Q(('project__isnull', True), ('company_profile__isnull', True), ('freelancer__isnull', False)), _connector='OR'), name='correspondence_counter_exactly_one_owner_scope'),
        ),
        migrations.AddConstraint(
            model_name='correspondencereferencecounter',
            constraint=models.UniqueConstraint(condition=models.Q(('project__isnull', False)), fields=('project',), name='unique_correspondence_counter_project'),
        ),
        migrations.AddConstraint(
            model_name='correspondencereferencecounter',
            constraint=models.UniqueConstraint(condition=models.Q(('company_profile__isnull', False)), fields=('company_profile',), name='unique_correspondence_counter_company'),
        ),
        migrations.AddConstraint(
            model_name='correspondencereferencecounter',
            constraint=models.UniqueConstraint(condition=models.Q(('freelancer__isnull', False)), fields=('freelancer',), name='unique_correspondence_counter_freelancer'),
        ),
    ]
