# L3H-3: Client Review due date (TransmittalItem.review_duration_days/review_due_date -- the
# frozen operational snapshot, independent of L3H-2's SupplierSurveillance fields).
#
# Written by hand rather than via `makemigrations`: this app already has the same unrelated,
# pre-existing migration drift noted in every migration since 0207_correspondence_validation_
# workflow (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold in here -- none of
# it belongs to this lot and none of it is included. The two AddField operations below are
# otherwise exactly what Django generated (dry-run, confirmed clean of any other diff). Both
# fields are nullable: no backfill for historical rows (L3H-3 section 26).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0219_supplier_surveillance_due_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='transmittalitem',
            name='review_duration_days',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='transmittalitem',
            name='review_due_date',
            field=models.DateField(blank=True, null=True),
        ),
    ]
