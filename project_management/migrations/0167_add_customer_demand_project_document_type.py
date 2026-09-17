from django.db import migrations


CUSTOMER_DEMAND_DOCUMENT_TYPE = "Customer Demand:CDE"
CUSTOMER_DEMAND_LABEL = "Customer Demand"
OTHER_LABEL = "Other"


def _label(document_type):
    return str(document_type or "").split(":", 1)[0]


def add_customer_demand_document_type(apps, schema_editor):
    config_model = apps.get_model("project_management", "ECPMPConfig")
    configs_to_update = []

    configs = config_model.objects.all().only("id", "document_types").iterator()
    for config in configs:
        document_types = list(config.document_types or [])
        if any(_label(item) == CUSTOMER_DEMAND_LABEL for item in document_types):
            continue

        other_index = next(
            (
                index
                for index, item in enumerate(document_types)
                if _label(item) == OTHER_LABEL
            ),
            len(document_types),
        )
        document_types.insert(other_index, CUSTOMER_DEMAND_DOCUMENT_TYPE)
        config.document_types = document_types
        configs_to_update.append(config)

    if configs_to_update:
        config_model.objects.bulk_update(configs_to_update, ["document_types"])


def remove_customer_demand_document_type(apps, schema_editor):
    config_model = apps.get_model("project_management", "ECPMPConfig")
    configs_to_update = []

    configs = config_model.objects.all().only("id", "document_types").iterator()
    for config in configs:
        document_types = list(config.document_types or [])
        updated_types = [
            item for item in document_types if item != CUSTOMER_DEMAND_DOCUMENT_TYPE
        ]
        if updated_types == document_types:
            continue

        config.document_types = updated_types
        configs_to_update.append(config)

    if configs_to_update:
        config_model.objects.bulk_update(configs_to_update, ["document_types"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0166_merge_configuration_visibility_and_aas"),
    ]

    operations = [
        migrations.RunPython(
            add_customer_demand_document_type,
            reverse_code=remove_customer_demand_document_type,
        ),
    ]
