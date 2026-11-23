"""Q5 data step: one workflow only. PENDING_REVIEW becomes SUBMITTED; CANCELLED (never reachable through any
Q1-Q4 API) goes back to IN_PROGRESS so no row is lost. Evaluations that already exist get their CREATED event."""

from django.db import migrations


def forwards(apps, schema_editor):
    Evaluation = apps.get_model("project_management", "SupplierEvaluation")
    Event = apps.get_model("project_management", "SupplierEvaluationEvent")
    Evaluation.objects.filter(status="PENDING_REVIEW").update(status="SUBMITTED")
    Evaluation.objects.filter(status="CANCELLED").update(status="IN_PROGRESS")
    for evaluation in Evaluation.objects.filter(events__isnull=True, created_by__isnull=False).iterator():
        event = Event.objects.create(
            evaluation=evaluation, event_type="CREATED", actor_id=evaluation.created_by_id,
        )
        # created_at is auto_now_add: align it with the evaluation (historical models have no insert-only guard)
        Event.objects.filter(pk=event.pk).update(created_at=evaluation.created_at)


def backwards(apps, schema_editor):
    Evaluation = apps.get_model("project_management", "SupplierEvaluation")
    Evaluation.objects.filter(status__in=["SUBMITTED", "UNDER_REVIEW"]).update(status="PENDING_REVIEW")


class Migration(migrations.Migration):
    dependencies = [("project_management", "0265_evaluation_workflow")]
    operations = [migrations.RunPython(forwards, backwards)]
