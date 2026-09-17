from django.db import transaction
from django.db.models import F
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from companyprofileapis.models import Opportunity
from project_management.models import (
    ActionItem,
    BillOfMaterialPart,
    Document,
    Project,
    ProjectSolutionSpaceItem,
    ProjectWorkstation,
    Requirement,
    SolutionDemand,
)

from .models import (
    AcceptanceActionItemProvenance,
    GeneratedProjectDocument,
    ProjectDocumentGenerationRun,
)
from .folders import ensure_project_content_folders


def _mark_project_out_of_date(project_id):
    if not project_id:
        return
    GeneratedProjectDocument.objects.filter(
        project_id=project_id,
        status=GeneratedProjectDocument.Status.CURRENT,
    ).update(status=GeneratedProjectDocument.Status.OUT_OF_DATE)


@receiver(post_save, sender=SolutionDemand)
@receiver(post_save, sender=Requirement)
@receiver(post_save, sender=ProjectSolutionSpaceItem)
@receiver(post_save, sender=BillOfMaterialPart)
@receiver(post_save, sender=ProjectWorkstation)
@receiver(post_delete, sender=SolutionDemand)
@receiver(post_delete, sender=Requirement)
@receiver(post_delete, sender=ProjectSolutionSpaceItem)
@receiver(post_delete, sender=BillOfMaterialPart)
@receiver(post_delete, sender=ProjectWorkstation)
def structured_source_changed(sender, instance, **kwargs):
    _mark_project_out_of_date(instance.project_id)


@receiver(post_save, sender=Document)
@receiver(post_delete, sender=Document)
def document_source_changed(sender, instance, **kwargs):
    project_id = instance.folder.project_id
    if not project_id:
        return
    # Exports and unrelated uploads are not inputs to the current revision.
    affected_documents = ProjectDocumentGenerationRun.objects.filter(
        project_id=project_id,
        applied_revision_id=F("generated_document__current_revision_id"),
        source_snapshot__documents__contains=[{"id": str(instance.pk)}],
    ).values("generated_document_id")
    GeneratedProjectDocument.objects.filter(
        project_id=project_id,
        pk__in=affected_documents,
        status=GeneratedProjectDocument.Status.CURRENT,
    ).update(status=GeneratedProjectDocument.Status.OUT_OF_DATE)


@receiver(post_save, sender=Opportunity)
@receiver(pre_delete, sender=Opportunity)
def opportunity_source_changed(sender, instance, **kwargs):
    for project_id in instance.projects.values_list("id", flat=True):
        _mark_project_out_of_date(project_id)


@receiver(m2m_changed, sender=Opportunity.projects.through)
def opportunity_project_link_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if action not in {"post_add", "post_remove", "pre_clear"}:
        return
    if reverse:
        _mark_project_out_of_date(instance.id)
        return
    project_ids = (
        instance.projects.values_list("id", flat=True)
        if action == "pre_clear"
        else (pk_set or [])
    )
    for project_id in project_ids:
        _mark_project_out_of_date(project_id)


@receiver(post_save, sender=Project)
def project_content_folders_created(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(lambda: ensure_project_content_folders(instance))
    else:
        _mark_project_out_of_date(instance.id)


@receiver(pre_delete, sender=ActionItem)
def preserve_acceptance_action_item_deletion(sender, instance, **kwargs):
    AcceptanceActionItemProvenance.objects.filter(
        action_item_id=instance.id,
        deleted_at__isnull=True,
    ).update(deleted_at=timezone.now())
