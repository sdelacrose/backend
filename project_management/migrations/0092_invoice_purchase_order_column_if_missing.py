# Idempotent repair: some environments had 0091 recorded without this column.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0091_poreferencecounter_invoice_purchase_order_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            ALTER TABLE project_management_invoice
            ADD COLUMN IF NOT EXISTS purchase_order_id bigint NULL;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_name = 'project_management_invoice'
                      AND kcu.column_name = 'purchase_order_id'
                ) THEN
                    ALTER TABLE project_management_invoice
                    ADD CONSTRAINT project_management_invoice_purchase_order_id_fkey
                    FOREIGN KEY (purchase_order_id)
                    REFERENCES project_management_purchaseorder(id)
                    DEFERRABLE INITIALLY DEFERRED;
                END IF;
            END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS project_management_invoice_purchase_order_id_0b6cb93b
            ON project_management_invoice (purchase_order_id);
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
