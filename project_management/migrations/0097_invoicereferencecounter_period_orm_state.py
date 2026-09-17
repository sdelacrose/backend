# Continuous invoice reference counter: one DB row per company or freelancer (no period column).
# Merges duplicate rows from older period-based data and drops period indexes/column if present.

from django.db import migrations


_MERGE_AND_RESET_SQL = """
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY company_profile_id, freelancer_id
               ORDER BY last_sequence DESC, id DESC
           ) AS rn
    FROM project_management_invoicereferencecounter
)
DELETE FROM project_management_invoicereferencecounter c
WHERE c.id IN (SELECT id FROM ranked WHERE rn > 1);

DROP INDEX IF EXISTS unique_invoice_reference_counter_company_period;
DROP INDEX IF EXISTS unique_invoice_reference_counter_freelancer_period;

ALTER TABLE project_management_invoicereferencecounter
DROP COLUMN IF EXISTS period;

CREATE UNIQUE INDEX IF NOT EXISTS unique_invoice_reference_counter_company
ON project_management_invoicereferencecounter (company_profile_id)
WHERE company_profile_id IS NOT NULL AND freelancer_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS unique_invoice_reference_counter_freelancer
ON project_management_invoicereferencecounter (freelancer_id)
WHERE company_profile_id IS NULL AND freelancer_id IS NOT NULL;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0096_merge_timecard_invoiceable_and_invoicereferencecounter"),
    ]

    operations = [
        migrations.RunSQL(
            sql=_MERGE_AND_RESET_SQL,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
