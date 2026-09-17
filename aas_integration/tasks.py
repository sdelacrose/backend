import logging

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from models.models import User

from project_management.models import Document

from .aasx_preview import parse_and_cache_document_aasx
from .models import AASOutboxEvent
from .services import publish_resource


logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(requests.Timeout, requests.ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
    queue=settings.AAS_PUBLICATION_QUEUE,
)
def process_aas_outbox_event(self, event_id):
    del self
    with transaction.atomic():
        event = AASOutboxEvent.objects.select_for_update().select_related("resource").get(id=event_id)
        if event.status == AASOutboxEvent.Status.COMPLETE:
            return str(event.id)
        if event.scheduled_at > timezone.now():
            process_aas_outbox_event.apply_async(args=[str(event.id)], eta=event.scheduled_at)
            return str(event.id)
        event.status = AASOutboxEvent.Status.PROCESSING
        event.attempts += 1
        event.save(update_fields=["status", "attempts", "updated_at"])
    try:
        actor = User.objects.filter(id=event.payload.get("actor_id")).first()
        publish_resource(event.resource_id, actor=actor)
    except Exception as exc:
        AASOutboxEvent.objects.filter(id=event.id).update(
            status=AASOutboxEvent.Status.FAILED,
            failure_code=type(exc).__name__,
            failure_message=str(exc)[:500],
            updated_at=timezone.now(),
        )
        logger.warning("AAS publication event failed", extra={"event_id": str(event.id), "failure_code": type(exc).__name__})
        raise
    AASOutboxEvent.objects.filter(id=event.id).update(
        status=AASOutboxEvent.Status.COMPLETE,
        failure_code="",
        failure_message="",
        updated_at=timezone.now(),
    )
    return str(event.id)


@shared_task(queue=settings.AAS_PUBLICATION_QUEUE)
def dispatch_due_aas_events(limit=100):
    event_ids = list(
        AASOutboxEvent.objects.filter(status=AASOutboxEvent.Status.PENDING, scheduled_at__lte=timezone.now())
        .order_by("scheduled_at")
        .values_list("id", flat=True)[:limit]
    )
    for event_id in event_ids:
        process_aas_outbox_event.delay(str(event_id))
    return len(event_ids)


@shared_task(queue=settings.AAS_PUBLICATION_QUEUE)
def parse_aasx_document_preview(document_id):
    document = Document.objects.filter(id=document_id).first()
    if document is None:
        return {"status": "discarded", "document_id": document_id}
    return parse_and_cache_document_aasx(document)
