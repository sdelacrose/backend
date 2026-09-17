import django.contrib.postgres.indexes
import django.contrib.postgres.search
import django.db.models.deletion
import django.utils.timezone
from django.contrib.postgres.operations import TrigramExtension, UnaccentExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0133_centralize_skills_configuration"),
    ]

    operations = [
        TrigramExtension(),
        UnaccentExtension(),
        migrations.AddIndex(
            model_name="document",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["file_name"],
                name="document_filename_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        migrations.CreateModel(
            name="DocumentSearchIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("searchable", "Searchable"), ("partial", "Partial"), ("failed", "Failed"), ("unsupported", "Unsupported")], default="pending", max_length=32)),
                ("content_hash", models.CharField(blank=True, default="", max_length=64)),
                ("mime_type", models.CharField(blank=True, default="", max_length=255)),
                ("language", models.CharField(blank=True, default="", max_length=64)),
                ("extraction_method", models.CharField(choices=[("none", "None"), ("native", "Native"), ("ocr", "OCR"), ("mixed", "Mixed")], default="none", max_length=32)),
                ("page_count", models.PositiveIntegerField(default=0)),
                ("parser_version", models.CharField(blank=True, default="", max_length=64)),
                ("error", models.TextField(blank=True, default="")),
                ("failure_category", models.CharField(blank=True, default="", max_length=64)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("indexed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("document", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="search_index", to="project_management.document")),
            ],
        ),
        migrations.CreateModel(
            name="DocumentIndexJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("queued", "Queued"), ("running", "Running"), ("retrying", "Retrying"), ("failed", "Failed"), ("cancelled", "Cancelled"), ("completed", "Completed")], default="queued", max_length=32)),
                ("content_hash", models.CharField(blank=True, default="", max_length=64)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("max_attempts", models.PositiveIntegerField(default=3)),
                ("available_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("failure_category", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="index_jobs", to="project_management.document")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="DocumentIndexAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(max_length=32)),
                ("parser", models.CharField(blank=True, default="", max_length=64)),
                ("failure_category", models.CharField(blank=True, default="", max_length=64)),
                ("error", models.TextField(blank=True, default="")),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("source_size_bytes", models.BigIntegerField(default=0)),
                ("extracted_size_bytes", models.BigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="index_attempts", to="project_management.document")),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attempts", to="project_management.documentindexjob")),
            ],
            options={"ordering": ["-id"]},
        ),
        migrations.CreateModel(
            name="DocumentContentSegment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("segment_type", models.CharField(choices=[("document", "Document"), ("page", "Page"), ("sheet", "Sheet"), ("slide", "Slide"), ("email_body", "Email body"), ("attachment", "Attachment")], max_length=32)),
                ("sequence", models.PositiveIntegerField(default=1)),
                ("location_label", models.CharField(blank=True, default="", max_length=255)),
                ("attachment_name", models.CharField(blank=True, default="", max_length=500)),
                ("text", models.TextField()),
                ("search_vector", django.contrib.postgres.search.SearchVectorField(blank=True, null=True)),
                ("ocr_confidence", models.FloatField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="content_segments", to="project_management.document")),
                ("index", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="segments", to="project_management.documentsearchindex")),
            ],
            options={"ordering": ["document_id", "sequence", "id"]},
        ),
        migrations.AddIndex(
            model_name="documentsearchindex",
            index=models.Index(fields=["status", "updated_at"], name="doc_search_status_idx"),
        ),
        migrations.AddIndex(
            model_name="documentsearchindex",
            index=models.Index(fields=["content_hash"], name="doc_search_hash_idx"),
        ),
        migrations.AddIndex(
            model_name="documentindexjob",
            index=models.Index(fields=["status", "available_at"], name="doc_job_status_ready_idx"),
        ),
        migrations.AddIndex(
            model_name="documentindexjob",
            index=models.Index(fields=["document", "content_hash"], name="doc_job_doc_hash_idx"),
        ),
        migrations.AddConstraint(
            model_name="documentindexjob",
            constraint=models.UniqueConstraint(condition=models.Q(status__in=["queued", "running", "retrying"]), fields=("document", "content_hash"), name="doc_job_unique_active_hash"),
        ),
        migrations.AddIndex(
            model_name="documentindexattempt",
            index=models.Index(fields=["document", "created_at"], name="doc_attempt_doc_created_idx"),
        ),
        migrations.AddIndex(
            model_name="documentcontentsegment",
            index=django.contrib.postgres.indexes.GinIndex(fields=["search_vector"], name="doc_segment_vector_gin"),
        ),
        migrations.AddIndex(
            model_name="documentcontentsegment",
            index=models.Index(fields=["document", "segment_type"], name="doc_segment_doc_type_idx"),
        ),
    ]
