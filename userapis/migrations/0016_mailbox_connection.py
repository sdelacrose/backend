# MAIL-1.1B: MailboxConnection, MailboxProjectAssignment, MailboxUserGrant -- additive only.
#
# Written by hand rather than via `makemigrations`, same precedent as project_management's own
# 0223_document_received_date.py: `makemigrations` wanted to depend on project_management
# migration 0224 and models (admin-server-models) migration 0131, NEITHER of which exists as a
# real file in this environment (confirmed: project_management/migrations/ stops at 0223, and
# `showmigrations models` shows 0130 as the latest actually applied) -- unrelated migration
# drift in those apps, not something this lot introduces or depends on. This migration instead
# depends on the latest REAL, applied migration of each app. The three CreateModel/AddConstraint
# operations below are otherwise exactly what Django generated for these models.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('models', '0130_companyconfig_correspondence_types'),
        ('project_management', '0223_document_received_date'),
        ('userapis', '0015_calendarevent_linked_folders'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MailboxConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(choices=[('microsoft', 'Microsoft'), ('google', 'Google')], max_length=20)),
                ('connection_type', models.CharField(choices=[('individual', 'Individual'), ('shared', 'Shared')], max_length=20)),
                ('email_address', models.EmailField(max_length=254)),
                ('normalized_email_address', models.CharField(blank=True, editable=False, max_length=254)),
                ('display_name', models.CharField(blank=True, default='', max_length=255)),
                ('status', models.CharField(choices=[('active', 'Active'), ('revoked', 'Revoked'), ('expired', 'Expired'), ('needs_reconsent', 'Needs reconsent')], default='active', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mailbox_connections', to='models.companyprofile')),
                ('connected_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='delegated_mailbox_connections', to=settings.AUTH_USER_MODEL)),
                ('owner_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='owned_mailbox_connections', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='MailboxProjectAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('assigned_at', models.DateTimeField(auto_now_add=True)),
                ('assigned_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='mailbox_assignments_made', to=settings.AUTH_USER_MODEL)),
                ('mailbox', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='project_assignments', to='userapis.mailboxconnection')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mailbox_assignments', to='project_management.project')),
            ],
            options={
                'ordering': ['-assigned_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='MailboxUserGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('can_send', models.BooleanField(default=False)),
                ('can_read', models.BooleanField(default=False)),
                ('granted_at', models.DateTimeField(auto_now_add=True)),
                ('granted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='mailbox_grants_made', to=settings.AUTH_USER_MODEL)),
                ('mailbox', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='user_grants', to='userapis.mailboxconnection')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mailbox_grants', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-granted_at', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='mailboxconnection',
            constraint=models.UniqueConstraint(fields=('company_profile', 'provider', 'normalized_email_address'), name='mailbox_connection_unique_per_company_provider_address'),
        ),
        migrations.AddConstraint(
            model_name='mailboxconnection',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('connection_type', 'individual'), ('owner_user__isnull', False)), models.Q(('connection_type', 'shared'), ('owner_user__isnull', True)), _connector='OR'), name='mailbox_connection_owner_matches_connection_type'),
        ),
        migrations.AddConstraint(
            model_name='mailboxprojectassignment',
            constraint=models.UniqueConstraint(fields=('mailbox', 'project'), name='mailbox_project_assignment_unique'),
        ),
        migrations.AddConstraint(
            model_name='mailboxusergrant',
            constraint=models.UniqueConstraint(fields=('mailbox', 'user'), name='mailbox_user_grant_unique'),
        ),
    ]
