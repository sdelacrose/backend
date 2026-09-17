from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0108_purchaseorder_customer_reference"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE project_management_contactcompany "
                        "ADD COLUMN IF NOT EXISTS company_role varchar(255) NULL;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE project_management_contactcompany "
                        "DROP COLUMN IF EXISTS company_role;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="contactcompany",
                    name="company_role",
                    field=models.CharField(blank=True, max_length=255, null=True),
                ),
            ],
        ),
    ]
