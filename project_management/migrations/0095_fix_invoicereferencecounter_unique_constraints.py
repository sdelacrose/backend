# Repair DBs that have a legacy unique on company_profile (or freelancer) alone,
# which blocks multiple rows per period. Expected: one row per (owner, period).

from django.db import migrations


def merge_invoice_reference_counters(apps, schema_editor):
    # `period` was repaired at DB level in 0094 but is not present in this
    # migration's historical Django state, so use SQL instead of the ORM.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            WITH ranked AS (
                SELECT id,
                       MAX(last_sequence) OVER (
                           PARTITION BY company_profile_id, freelancer_id, period
                       ) AS max_last_sequence,
                       ROW_NUMBER() OVER (
                           PARTITION BY company_profile_id, freelancer_id, period
                           ORDER BY id ASC
                       ) AS rn
                FROM project_management_invoicereferencecounter
            )
            UPDATE project_management_invoicereferencecounter counter
            SET last_sequence = ranked.max_last_sequence
            FROM ranked
            WHERE counter.id = ranked.id
              AND ranked.rn = 1;
            """
        )
        cursor.execute(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY company_profile_id, freelancer_id, period
                           ORDER BY id ASC
                       ) AS rn
                FROM project_management_invoicereferencecounter
            )
            DELETE FROM project_management_invoicereferencecounter counter
            USING ranked
            WHERE counter.id = ranked.id
              AND ranked.rn > 1;
            """
        )


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0094_invoicereferencecounter_period_if_missing"),
    ]

    operations = [
        migrations.RunPython(
            merge_invoice_reference_counters,
            migrations.RunPython.noop,
        ),
        migrations.RunSQL(
            sql="""
            ALTER TABLE project_management_invoicereferencecounter
            DROP CONSTRAINT IF EXISTS unique_invoice_reference_counter_company;

            ALTER TABLE project_management_invoicereferencecounter
            DROP CONSTRAINT IF EXISTS unique_invoice_reference_counter_freelancer;

            DROP INDEX IF EXISTS unique_invoice_reference_counter_company;
            DROP INDEX IF EXISTS unique_invoice_reference_counter_freelancer;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
            CREATE UNIQUE INDEX IF NOT EXISTS unique_invoice_reference_counter_company_period
            ON project_management_invoicereferencecounter (company_profile_id, period)
            WHERE company_profile_id IS NOT NULL AND freelancer_id IS NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS unique_invoice_reference_counter_freelancer_period
            ON project_management_invoicereferencecounter (freelancer_id, period)
            WHERE company_profile_id IS NULL AND freelancer_id IS NOT NULL;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
