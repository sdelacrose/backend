import logging

from celery import shared_task
from django.utils import timezone

from project_management.utils import build_structure
from .models import Folder, Project
from models.models import CompanyProfile, User

logger = logging.getLogger(__name__)


@shared_task(name="project_management.send_action_item_saved_notification")
def send_action_item_saved_notification(
    action_item_id,
    actor_id,
    created,
    changed_fields,
    notify_assignees,
):
    from project_management.services.action_item_notifications import (
        send_action_item_saved_notification as send_notification,
    )

    send_notification(
        action_item_id=action_item_id,
        actor_id=actor_id,
        created=created,
        changed_fields=changed_fields,
        notify_assignees=notify_assignees,
    )


@shared_task(name="project_management.send_action_item_assignee_notification")
def send_action_item_assignee_notification(action_item_id, actor_id, event):
    from project_management.services.action_item_notifications import (
        send_action_item_assignee_notification as send_notification,
    )

    send_notification(
        action_item_id=action_item_id,
        actor_id=actor_id,
        event=event,
    )


@shared_task(name="project_management.send_bulk_action_item_update_notification")
def send_bulk_action_item_update_notification(
    action_item_ids,
    actor_id,
    changed_fields,
):
    from project_management.services.action_item_notifications import (
        send_bulk_action_item_update_summary,
    )

    send_bulk_action_item_update_summary(
        action_item_ids=action_item_ids,
        actor_id=actor_id,
        changed_fields=changed_fields,
    )


@shared_task(name="project_management.send_bulk_document_update_notification")
def send_bulk_document_update_notification(
    document_ids,
    actor_id,
    changed_fields,
):
    from project_management.services.document_notifications import (
        send_bulk_document_update_summary,
    )

    send_bulk_document_update_summary(
        document_ids=document_ids,
        actor_id=actor_id,
        changed_fields=changed_fields,
    )


@shared_task(
    name="project_management.send_bulk_object_assessment_update_notification"
)
def send_bulk_object_assessment_update_notification(
    object_assessment_ids,
    actor_id,
    changed_fields,
):
    from project_management.services.object_assessment_notifications import (
        send_bulk_object_assessment_update_summary,
    )

    send_bulk_object_assessment_update_summary(
        object_assessment_ids=object_assessment_ids,
        actor_id=actor_id,
        changed_fields=changed_fields,
    )

@shared_task
def fetch_sharepoint_data(initial_data, parent_folder_id, site_id, headers, request_user_id, project_id=None, company_id=None, freelancer_id=None):
    try:
        logger.info("Celery task 'fetch_sharepoint_data' called.")
        
        try:
            project = None
            company = None
            freelancer = None
            
            sharepoint_site_folder = Folder.objects.get(id=parent_folder_id)
            request_user = User.objects.get(id=request_user_id)
            if project_id:
                project = Project.objects.get(id=project_id)
            elif company_id:
                company = CompanyProfile.objects.get(id=company_id)
            elif freelancer_id:
                freelancer = User.objects.get(id=freelancer_id)
        except Folder.DoesNotExist:
            logger.error(f"Folder with id {parent_folder_id} does not exist.")
            return {"error": f"Folder with id {parent_folder_id} does not exist."}
        except User.DoesNotExist:
            logger.error(f"User with id {request_user_id} does not exist.")
            return {"error": f"User with id {request_user_id} does not exist."}
        except Project.DoesNotExist:
            logger.error(f"Project with id {project_id} does not exist.")
            return {"error": f"Project with id {project_id} does not exist."}
        except CompanyProfile.DoesNotExist:
            logger.error(f"Company with id {company_id} does not exist.")
            return {"error": f"Company with id {company_id} does not exist."}
        except User.DoesNotExist:
            logger.error(f"Freelancer with id {freelancer_id} does not exist.")
            return {"error": f"Freelancer with id {freelancer_id} does not exist."}
        
        for item in initial_data:
            try:
                logger.info(f'Processing item: {item["name"]}.')
                build_structure(item, sharepoint_site_folder, site_id, headers, request_user, project=project, company=company, freelancer=freelancer)
            except Exception as e:
                logger.error(f"Error processing item {item['name']}: {e}", exc_info=True)
        
        logger.info("SharePoint data has been successfully stored.")
        return {"message": "SharePoint data has been successfully stored."}
    
    except Exception as e:
        logger.error(f"Unexpected error in 'fetch_sharepoint_data' task: {e}", exc_info=True)
        return {"error": "An unexpected error occurred."}


@shared_task(bind=True, name="project_management.process_document_index_job")
def process_document_index_job(self, job_id):
    from project_management.services.document_indexing import process_index_job

    process_index_job(job_id)


@shared_task(bind=True, name="project_management.process_search_index_job")
def process_search_index_job(self, job_id):
    from project_management.services.search_indexing import process_search_job

    process_search_job(job_id)


@shared_task(bind=True, name="project_management.process_product_assembly_extraction_run")
def process_product_assembly_extraction_run(self, run_id):
    from project_management.services.product_assembly_extraction import (
        process_extraction_run,
    )

    process_extraction_run(run_id)


@shared_task(name="project_management.process_catalog_recommendation_run")
def process_catalog_recommendation_run(run_id):
    from project_management.services.catalog_recommendation import process_catalog_run
    process_catalog_run(run_id)


@shared_task(bind=True, name="project_management.process_product_attribute_extraction_run")
def process_product_attribute_extraction_run(self, run_id):
    from project_management.services.product_attribute_extraction import (
        process_attribute_extraction_run,
    )

    process_attribute_extraction_run(run_id)


@shared_task(bind=True, name="project_management.process_process_assembly_extraction_run")
def process_process_assembly_extraction_run(self, run_id):
    from project_management.services.process_requirements_extraction import (
        process_process_assembly_run,
    )

    process_process_assembly_run(run_id)


@shared_task(bind=True, name="project_management.process_process_attribute_extraction_run")
def process_process_attribute_extraction_run(self, run_id):
    from project_management.services.process_requirements_extraction import (
        process_process_attribute_run,
    )

    process_process_attribute_run(run_id)


@shared_task(
    bind=True,
    name="project_management.process_bom_generation_run",
    max_retries=3,
    default_retry_delay=15,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_bom_generation_run(self, run_id):
    from project_management.models import BomGenerationRun
    from project_management.services.workstation_bom import process_queued_bom_plan

    run = process_queued_bom_plan(run_id)
    if (
        run.status == BomGenerationRun.STATUS_RUNNING
        and run.claim_expires_at
        and self.request.retries < self.max_retries
    ):
        remaining_seconds = max(
            1,
            int((run.claim_expires_at - timezone.now()).total_seconds()) + 1,
        )
        raise self.retry(
            exc=RuntimeError("bom_plan_worker_claim_active"),
            countdown=remaining_seconds,
        )
    if run.status == BomGenerationRun.STATUS_FAILED and self.request.retries < self.max_retries:
        metrics = dict(run.metrics or {})
        metrics["retry_count"] = int(metrics.get("retry_count", 0)) + 1
        run.status = BomGenerationRun.STATUS_QUEUED
        run.metrics = metrics
        run.save(update_fields=["status", "metrics", "updated_at"])
        raise self.retry(
            exc=RuntimeError("bom_plan_generation_failed"),
            countdown=min(15 * (2 ** self.request.retries), 120),
        )


# MAIL-1.4-C: archiving the proof of an OUT send (the .eml and the attachments really sent, kept
# from the Sent Items copy). These tasks only READ the mailbox that sent and write LINKS rows/files:
# they can never send an e-mail. Both do nothing unless MAIL_SEND_PROOF_ARCHIVING_ENABLED and
# MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE are set (see services/correspondence_send_proof_runner.py).
# Retries are the sweep's job (its own 1 min -> 24 h schedule), not Celery's: max_retries=0.
@shared_task(name="project_management.archive_send_proof", max_retries=0, ignore_result=True)
def archive_send_proof_task(proof_id):
    from project_management.models import CorrespondenceSendProof
    from project_management.services.correspondence_send_proof_runner import (
        auto_archiving_applies,
        run_archive,
    )

    proof = CorrespondenceSendProof.objects.select_related("event").filter(pk=proof_id).first()
    if proof is None or not auto_archiving_applies(proof):
        return {"proof": proof_id, "outcome": "skipped"}
    result = run_archive(proof_id)
    return {"proof": proof_id, "outcome": result.outcome, "state": result.state}


@shared_task(name="project_management.sweep_send_proofs", max_retries=0)
def sweep_send_proofs_task():
    from project_management.services.correspondence_send_proof_runner import sweep_send_proofs

    result = sweep_send_proofs()
    return {
        "enabled": result.enabled, "skipped": result.skipped, "examined": result.examined,
        "archived": result.archived, "partial": result.partial, "pending": result.pending,
        "failed": result.failed, "unavailable": result.unavailable, "busy": result.busy,
    }


# MAIL-1.4-E: a manual retry asked for in the interface, run by the worker instead of inside the HTTP
# request. The request has already checked everything (check_send_proof_retry); the task checks it
# again -- a switch turned off or a right withdrawn meanwhile stops it -- then runs the same service.
# It reads the sending mailbox and writes LINKS rows/files; it can never send an e-mail.
@shared_task(name="project_management.retry_send_proof_archive", max_retries=0, ignore_result=True)
def retry_send_proof_archive_task(proof_id, requested_by_id):
    from models.models import User
    from project_management.services.correspondence_send_proof_runner import (
        SendProofRetryRefused,
        retry_send_proof_archive,
    )

    user = User.objects.filter(pk=requested_by_id).first()
    try:
        result = retry_send_proof_archive(proof_id, requested_by=user)
    except SendProofRetryRefused as refused:
        return {"proof": proof_id, "outcome": "refused", "code": refused.code}
    return {"proof": proof_id, "outcome": result.outcome, "state": result.state}
