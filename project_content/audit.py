from .models import ProjectContentAuditEvent


def record_project_content_event(
    *,
    project,
    event_type,
    instance,
    actor=None,
    metadata=None,
):
    return ProjectContentAuditEvent.objects.create(
        project=project,
        event_type=event_type,
        object_type=instance._meta.model_name,
        object_id=str(instance.pk),
        actor=actor,
        metadata=metadata or {},
    )
