from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0115_contactuser_profile_search_fields"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE project_management_contactuser "
                        "ALTER COLUMN availability_from TYPE date USING NULL, "
                        "ALTER COLUMN availability_until TYPE date USING NULL"
                    ),
                    reverse_sql=(
                        "ALTER TABLE project_management_contactuser "
                        "ALTER COLUMN availability_from TYPE time USING NULL, "
                        "ALTER COLUMN availability_until TYPE time USING NULL"
                    ),
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="contactuser",
                    name="availability_from",
                    field=models.DateField(blank=True, null=True),
                ),
                migrations.AlterField(
                    model_name="contactuser",
                    name="availability_until",
                    field=models.DateField(blank=True, null=True),
                ),
            ],
        ),
    ]
