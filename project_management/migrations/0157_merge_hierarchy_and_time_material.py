from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0156_merge_hierarchy_and_misc_document_type"),
        (
            "project_management",
            "0156_remap_time_and_materials_to_time_and_material",
        ),
    ]

    operations = []
