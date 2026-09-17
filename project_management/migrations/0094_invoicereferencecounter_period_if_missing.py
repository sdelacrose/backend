# Idempotent repair: some DBs never got `period` on InvoiceReferenceCounter.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0093_projectphase_purchase_order"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            ALTER TABLE project_management_invoicereferencecounter
            ADD COLUMN IF NOT EXISTS period varchar(6);

            UPDATE project_management_invoicereferencecounter
            SET period = '197001'
            WHERE period IS NULL OR period = '';

            ALTER TABLE project_management_invoicereferencecounter
            ALTER COLUMN period SET NOT NULL;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
