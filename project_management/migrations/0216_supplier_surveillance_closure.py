# L3E: Supplier Surveillance Closure (SupplierSurveillance.closed_by/closure_comment,
# SupplierSurveillanceEvent.event_type += SURVEILLANCE_CLOSED).
#
# Written by hand rather than via `makemigrations`: this app already has the same unrelated,
# pre-existing migration drift noted in every migration since 0207_correspondence_validation_
# workflow (index renames on availableproductpart/billofmaterialpart/equipment/machine/
# matrixcomment/matrixcommentreaction/requirement/solutiondemand/transport, and an AlterField on
# historicalobjectassessment.file) that `makemigrations` would otherwise fold in here -- none of
# it belongs to this lot and none of it is included. The two fields and the event_type choices
# update below are otherwise exactly what Django generated (dry-run, confirmed clean of any
# other diff).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('project_management', '0215_supplier_surveillance_review'),
    ]

    operations = [
        migrations.AddField(
            model_name='suppliersurveillance',
            name='closed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='closed_supplier_surveillances', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='suppliersurveillance',
            name='closure_comment',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AlterField(
            model_name='suppliersurveillanceevent',
            name='event_type',
            field=models.CharField(choices=[('SURVEILLANCE_CREATED', 'Surveillance created'), ('REVIEWER_ASSIGNED', 'Reviewer assigned'), ('REVIEWER_REMOVED', 'Reviewer removed'), ('REVIEW_STARTED', 'Review started'), ('RESPONSE_UPDATED', 'Response updated'), ('REVIEW_COMPLETED', 'Review completed'), ('READY_FOR_CLOSURE', 'Ready for closure'), ('SURVEILLANCE_CLOSED', 'Surveillance closed')], max_length=32),
        ),
    ]
