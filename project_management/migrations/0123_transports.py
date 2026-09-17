# Generated manually to avoid unrelated pending model changes in other apps.

import django.db.models.deletion
import project_management.models.project
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0122_merge_contactuser_linked_folders"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="projectmatrixconfig",
            name="transport_columns",
            field=models.JSONField(
                blank=True,
                default=project_management.models.project.default_transport_columns,
            ),
        ),
        migrations.CreateModel(
            name="Transport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(db_index=True, default=0)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                ("transport_start_date", models.DateField()),
                ("transport_end_date", models.DateField()),
                ("transportation_lanes", models.TextField(blank=True, null=True)),
                ("carrier_per_transportation_lane_and_means_of_transport", models.TextField(blank=True, null=True)),
                ("location", models.TextField(blank=True, null=True)),
                ("location_with_address", models.TextField(blank=True, null=True)),
                ("equipment", models.TextField(blank=True, null=True)),
                ("freight_settlement_document", models.TextField(blank=True, null=True)),
                ("unlocode", models.TextField(blank=True, null=True)),
                ("unlocode_text", models.TextField(blank=True, null=True)),
                ("stops_for_transportation_stages", models.TextField(blank=True, null=True)),
                ("freight_order_freight_booking_freight_unit_root_or_header", models.TextField(blank=True, null=True)),
                ("freight_order_freight_booking_freight_unit_item_or_line_item", models.TextField(blank=True, null=True)),
                ("document_references", models.TextField(blank=True, null=True)),
                ("freight_order_types", models.TextField(blank=True, null=True)),
                ("freight_unit_types", models.TextField(blank=True, null=True)),
                ("transportation_unit_types", models.TextField(blank=True, null=True)),
                ("order_based_transportation_requirement", models.TextField(blank=True, null=True)),
                ("delivery_based_transportation_requirement", models.TextField(blank=True, null=True)),
                ("forwarding_order_types", models.TextField(blank=True, null=True)),
                ("freight_agreement_types", models.TextField(blank=True, null=True)),
                ("freight_agreement_root", models.TextField(blank=True, null=True)),
                ("freight_agreement_positions", models.TextField(blank=True, null=True)),
                ("block_reason_codes", models.TextField(blank=True, null=True)),
                ("texts_for_block_reason_codes", models.TextField(blank=True, null=True)),
                ("freight_unit_formation_rule_header", models.TextField(blank=True, null=True)),
                ("tcc_solution_basis", models.TextField(blank=True, null=True)),
                ("rate_tables", models.TextField(blank=True, null=True)),
                ("transportation_charges", models.TextField(blank=True, null=True)),
                ("freight_order_freight_booking_freight_unit", models.TextField(blank=True, null=True)),
                ("transportation_requirement", models.TextField(blank=True, null=True)),
                ("forwarding_settlement_document", models.TextField(blank=True, null=True)),
                ("vehicle_resource", models.TextField(blank=True, null=True)),
                ("driver", models.TextField(blank=True, null=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_transports", to=settings.AUTH_USER_MODEL)),
                ("documents", models.ManyToManyField(blank=True, related_name="transports", to="project_management.document")),
                ("linked_folders", models.ManyToManyField(blank=True, related_name="transport_linked_folders", to="project_management.folder")),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_transports", to=settings.AUTH_USER_MODEL)),
                ("object_assessments", models.ManyToManyField(blank=True, related_name="transports", to="project_management.objectassessment")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="sub_transports", to="project_management.transport")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="transports", to="project_management.project")),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.AddField(
            model_name="matrixcomment",
            name="transport",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="project_management.transport"),
        ),
        migrations.AlterField(
            model_name="matrixcomment",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("solution_demand", "Solution Demand"),
                    ("requirement", "Requirement"),
                    ("transport", "Transport"),
                    ("available_product_part", "Available Product Part"),
                    ("machine", "Machine"),
                    ("equipment", "Equipment"),
                    ("bom_part", "Bill of Material Part"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="transport",
            index=models.Index(fields=["project", "parent", "sort_order"], name="transport_proj_parent_sort_idx"),
        ),
        migrations.AddIndex(
            model_name="transport",
            index=models.Index(fields=["project", "level"], name="transport_proj_level_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["target_type", "transport", "created_at"], name="comment_transport_idx"),
        ),
    ]
