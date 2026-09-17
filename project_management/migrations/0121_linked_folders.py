from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0120_merge_20260630_2048"),
    ]

    operations = [
        migrations.AddField(
            model_name="actionitem",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="action_item_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="availableproductpart",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="available_product_part_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="bom_part_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="equipment_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="machine",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="machine_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="milestone",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="milestone_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="purchase_order_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="requirement",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="requirement_linked_folders",
                to="project_management.folder",
            ),
        ),
        migrations.AddField(
            model_name="solutiondemand",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="solution_demand_linked_folders",
                to="project_management.folder",
            ),
        ),
    ]
