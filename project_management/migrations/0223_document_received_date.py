# FINISH-C: Document.received_date -- the business date a CLIENT-origin ("Input Data")
# document was actually received, distinct from the system uploaded_at/DocumentRevision.
# created_at timestamps. Nullable: existing historical CLIENT documents have no authoritative
# receipt date and are never backfilled.
#
# Written by hand rather than via `makemigrations`: this app has the same unrelated, pre-
# existing migration drift noted in every migration since 0207_correspondence_validation_
# workflow (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold in here -- none of
# it belongs to this lot and none of it is included. The two AddField operations below (document
# + its simple-history mirror, historicaldocument) are otherwise exactly what Django generated
# (dry-run, confirmed clean of any other diff for this field).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('project_management', '0222_correspondence_send_event_allow_resend'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='received_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='historicaldocument',
            name='received_date',
            field=models.DateField(blank=True, null=True),
        ),
    ]
