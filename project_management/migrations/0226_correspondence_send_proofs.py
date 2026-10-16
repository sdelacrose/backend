# MAIL-1.4-A: the structures of the proof of an OUT send -- three NEW tables (CorrespondenceSendProof,
# CorrespondenceSendProofFile, EvidenceBlob), additive only: no existing table or column is touched,
# no data migration, nothing is deleted. Hand-written against the latest REAL, applied migrations
# (project_management 0225, userapis 0022) rather than accepting makemigrations' own full diff, which
# also pulls in a large batch of pre-existing, unrelated index-rename drift (and an unrelated change
# to the `models` app) -- same precedent as 0224.

import django.db.models.deletion
import project_management.models.correspondence_send_proof
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0225_correspondence_workflow_sign_author'),
        ('userapis', '0022_mailboximportedfile'),
    ]

    operations = [
        migrations.CreateModel(
            name='CorrespondenceSendProof',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state', models.CharField(choices=[('PENDING', 'Pending'), ('ARCHIVED', 'Archived'), ('PARTIAL', 'Partial'), ('UNAVAILABLE', 'Unavailable'), ('FAILED', 'Failed')], default='PENDING', max_length=20)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, default='', max_length=64)),
                ('archived_at', models.DateTimeField(blank=True, null=True)),
                ('distribution_snapshot', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('event', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='proof', to='project_management.correspondencesendevent')),
                ('mailbox_message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='send_proofs', to='userapis.mailboxmessage')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='EvidenceBlob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sha256', models.CharField(max_length=64)),
                ('file_size', models.BigIntegerField()),
                ('content_type', models.CharField(blank=True, default='', max_length=255)),
                ('file', models.FileField(max_length=500, upload_to=project_management.models.correspondence_send_proof.get_evidence_blob_upload_path)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('company_profile', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='evidence_blobs', to='models.companyprofile')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.CreateModel(
            name='CorrespondenceSendProofFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('eml', 'Original message (.eml)'), ('attachment', 'Sent attachment')], max_length=20)),
                ('file_name', models.CharField(max_length=500)),
                ('content_type', models.CharField(blank=True, default='', max_length=255)),
                ('file_size', models.BigIntegerField()),
                ('sha256', models.CharField(max_length=64)),
                ('graph_attachment_id', models.CharField(blank=True, default='', max_length=512)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('blob', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='proof_files', to='project_management.evidenceblob')),
                ('proof', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='files', to='project_management.correspondencesendproof')),
                ('source_attachment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='send_proof_files', to='project_management.correspondenceattachment')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.AddConstraint(
            model_name='correspondencesendproof',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('archived_at__isnull', False), ('state', 'ARCHIVED')), models.Q(('state', 'ARCHIVED'), _negated=True), _connector='OR'), name='send_proof_archived_has_date'),
        ),
        migrations.AddConstraint(
            model_name='evidenceblob',
            constraint=models.UniqueConstraint(fields=('company_profile', 'sha256'), name='evidence_blob_unique_content_per_company'),
        ),
        migrations.AddIndex(
            model_name='correspondencesendprooffile',
            index=models.Index(fields=['sha256'], name='send_proof_file_sha256_idx'),
        ),
        migrations.AddConstraint(
            model_name='correspondencesendprooffile',
            constraint=models.UniqueConstraint(condition=models.Q(('kind', 'eml')), fields=('proof',), name='send_proof_file_one_eml_per_proof'),
        ),
        migrations.AddConstraint(
            model_name='correspondencesendprooffile',
            constraint=models.UniqueConstraint(condition=models.Q(('kind', 'attachment')), fields=('proof', 'graph_attachment_id'), name='send_proof_file_one_row_per_graph_attachment'),
        ),
    ]
