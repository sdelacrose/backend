import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("aas_integration", "0003_solution_space_aas_resources"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="aaspublication",
            name="published_identifiers",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RemoveConstraint(
            model_name="aasresourcebinding",
            name="aas_binding_product_field_unique",
        ),
        migrations.RemoveConstraint(
            model_name="aasresourcebinding",
            name="aas_binding_bom_field_unique",
        ),
        migrations.AddConstraint(
            model_name="aasresourcebinding",
            constraint=models.UniqueConstraint(condition=models.Q(("company_product__isnull", False)), fields=("resource", "company_product", "source_field", "submodel_id", "element_path"), name="aas_binding_product_field_unique"),
        ),
        migrations.AddConstraint(
            model_name="aasresourcebinding",
            constraint=models.UniqueConstraint(condition=models.Q(("bom_part__isnull", False)), fields=("resource", "bom_part", "source_field", "submodel_id", "element_path"), name="aas_binding_bom_field_unique"),
        ),
        migrations.CreateModel(
            name="AASSubmodelTemplate",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("template_identifier", models.CharField(max_length=1000)),
                ("semantic_id", models.CharField(blank=True, max_length=1000)),
                ("id_short", models.CharField(max_length=128)),
                ("name", models.CharField(max_length=255)),
                ("version", models.CharField(max_length=64)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("active", "Active"), ("deprecated", "Deprecated"), ("invalid", "Invalid")], db_index=True, default="draft", max_length=24)),
                ("source_format", models.CharField(choices=[("json", "JSON"), ("aasx", "AASX")], default="json", max_length=16)),
                ("definition", models.JSONField(default=dict)),
                ("profile_keys", models.JSONField(blank=True, default=list)),
                ("validation_report", models.JSONField(blank=True, default=dict)),
                ("checksum", models.CharField(max_length=64)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("activated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="activated_aas_submodel_templates", to=settings.AUTH_USER_MODEL)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="aas_submodel_templates", to="models.companyprofile")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_aas_submodel_templates", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["name", "template_identifier", "-created_at"],
            },
        ),
        migrations.CreateModel(
            name="AASSubmodelFieldMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("element_path", models.CharField(max_length=1000)),
                ("element_id_short", models.CharField(max_length=128)),
                ("model_type", models.CharField(max_length=64)),
                ("semantic_id", models.CharField(blank=True, max_length=1000)),
                ("value_type", models.CharField(blank=True, max_length=64)),
                ("source", models.CharField(choices=[("profile_field", "Product AAS field"), ("product_name", "Product name"), ("article_number", "Article number"), ("manufacturer_name", "Manufacturer name"), ("custom_field", "Configured product column"), ("template_default", "Template default")], default="profile_field", max_length=32)),
                ("source_key", models.CharField(blank=True, max_length=500)),
                ("editable", models.BooleanField(default=True)),
                ("required", models.BooleanField(default=False)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="field_mappings", to="aas_integration.aassubmodeltemplate")),
            ],
            options={
                "ordering": ["sort_order", "element_path"],
            },
        ),
        migrations.AddConstraint(
            model_name="aassubmodeltemplate",
            constraint=models.UniqueConstraint(fields=("company", "template_identifier", "version"), name="aas_submodel_template_company_identifier_version_unique"),
        ),
        migrations.AddConstraint(
            model_name="aassubmodeltemplate",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "active")), fields=("company", "template_identifier"), name="aas_submodel_template_one_active_version"),
        ),
        migrations.AddIndex(
            model_name="aassubmodeltemplate",
            index=models.Index(fields=["company", "status", "id_short"], name="aas_sm_tpl_company_status_idx"),
        ),
        migrations.AddIndex(
            model_name="aassubmodeltemplate",
            index=models.Index(fields=["company", "template_identifier"], name="aas_sm_tpl_company_ident_idx"),
        ),
        migrations.AddConstraint(
            model_name="aassubmodelfieldmapping",
            constraint=models.UniqueConstraint(fields=("template", "element_path"), name="aas_submodel_mapping_template_path_unique"),
        ),
    ]
