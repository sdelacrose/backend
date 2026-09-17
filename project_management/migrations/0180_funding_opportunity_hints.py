from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("project_management", "0179_process_assembly_recommendations"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="FundingSourceConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("authority", models.CharField(max_length=255)), ("region", models.CharField(max_length=100)),
                ("base_url", models.URLField(max_length=1000)), ("allowed_hosts", models.JSONField(default=list)),
                ("is_active", models.BooleanField(db_index=True, default=False)), ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, default="", max_length=255)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ], options={"ordering": ["authority", "region", "id"]},
        ),
        migrations.CreateModel(
            name="FundingOpportunity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=255)), ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="opportunities", to="project_management.fundingsourceconfig")),
            ],
        ),
        migrations.CreateModel(
            name="FundingOpportunityRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_url", models.URLField(max_length=2000)), ("retrieved_at", models.DateTimeField(db_index=True)),
                ("title", models.CharField(max_length=500)), ("summary", models.TextField(blank=True, default="")),
                ("application_start", models.DateField(blank=True, null=True)), ("application_end", models.DateField(blank=True, null=True)),
                ("eligibility_rules", models.JSONField(default=dict)), ("evidence", models.JSONField(default=list)),
                ("content_hash", models.CharField(max_length=64)), ("created_at", models.DateTimeField(auto_now_add=True)),
                ("opportunity", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="project_management.fundingopportunity")),
            ], options={"ordering": ["-retrieved_at", "-id"]},
        ),
        migrations.AddField(
            model_name="fundingopportunity", name="current_revision",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="current_for", to="project_management.fundingopportunityrevision"),
        ),
        migrations.CreateModel(
            name="ProjectFundingMatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("relevance_score", models.PositiveSmallIntegerField(default=0)),
                ("eligibility_state", models.CharField(choices=[("eligible", "Eligible"), ("unknown", "Unknown"), ("disqualified", "Disqualified")], default="unknown", max_length=20)),
                ("match_reasons", models.JSONField(default=list)), ("eligibility_conditions", models.JSONField(default=list)),
                ("status", models.CharField(choices=[("suggested", "Suggested"), ("saved", "Saved"), ("dismissed", "Dismissed"), ("converted", "Converted")], default="suggested", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True)),
                ("converted_funding", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="source_opportunity_match", to="project_management.projectfunding")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_funding_matches", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="funding_opportunity_matches", to="project_management.project")),
                ("revision", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="project_matches", to="project_management.fundingopportunityrevision")),
            ], options={"ordering": ["-relevance_score", "-updated_at", "-id"]},
        ),
        migrations.AddConstraint(model_name="fundingopportunity", constraint=models.UniqueConstraint(fields=("source", "external_id"), name="funding_source_external_unique")),
        migrations.AddConstraint(model_name="fundingopportunityrevision", constraint=models.UniqueConstraint(fields=("opportunity", "content_hash"), name="funding_revision_hash_unique")),
        migrations.AddConstraint(model_name="projectfundingmatch", constraint=models.UniqueConstraint(fields=("project", "revision"), name="project_funding_revision_match_unique")),
    ]
