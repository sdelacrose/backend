import logging

from celery import shared_task

from human_resource_management.services.leave_accrual import (
    LeaveAccrualConcurrentRunError,
    run_monthly_accrual,
)
from human_resource_management.services.leave_carryover import (
    LeaveCarryOverError,
    run_cycle_closure,
)
from human_resource_management.services.leave_reminders import process_all_active_rules
from human_resource_management.services.workload_alerts import evaluate_all_active_configs
from human_resource_management.services.hr_reminders import (
    process_onboarding_step_reminders, process_staff_document_validity_reminders,
)

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="human_resource_management.run_monthly_leave_accrual",
    max_retries=0,
)
def run_monthly_leave_accrual_task(self, company_profile_id=None):
    """LOT RH10F: the scheduled (Celery Beat) entry point for the exact same
    idempotent, catch-up-capable batch job the `run_monthly_leave_accrual`
    management command exposes for a manual/ad-hoc run -- both call
    `run_monthly_accrual()` directly, so the two never drift apart.

    Concurrency protection is already handled one layer down, by the
    Postgres advisory lock inside `run_monthly_accrual` itself: if a manual
    run or another scheduled tick is already in progress, this task simply
    logs that it was skipped and returns cleanly -- it is never retried (a
    Celery retry would just hit the same lock again immediately; the NEXT
    scheduled tick is the natural, already-idempotent retry).
    """
    try:
        report = run_monthly_accrual(company_profile_id=company_profile_id, actor=None)
    except LeaveAccrualConcurrentRunError as exc:
        logger.warning("run_monthly_leave_accrual_task: skipped -- %s", exc)
        return {"skipped": True, "reason": str(exc)}
    return {
        "processed": report.processed,
        "credited": report.credited,
        "already_done": report.already_done,
        "skipped": len(report.skipped),
        "errors": len(report.errors),
    }


@shared_task(
    bind=True,
    name="human_resource_management.run_leave_cycle_closure",
    max_retries=0,
)
def run_leave_cycle_closure_task(self, company_profile_id=None):
    """LOT RH11: scheduled entry point for capped carry-over + mandatory
    expiration cycle closure -- same idempotent/concurrency-protected
    pattern as `run_monthly_leave_accrual_task` (its OWN, separate
    Postgres advisory lock, never retried on concurrency refusal)."""
    try:
        report = run_cycle_closure(company_profile_id=company_profile_id, actor=None)
    except LeaveCarryOverError as exc:
        logger.warning("run_leave_cycle_closure_task: skipped -- %s", exc)
        return {"skipped": True, "reason": str(exc)}
    return {"closed": report.closed, "errors": len(report.errors)}


@shared_task(
    bind=True,
    name="human_resource_management.send_leave_balance_reminders",
    max_retries=0,
)
def send_leave_balance_reminders_task(self):
    """AI-1685: the scheduled (Celery Beat) entry point for the exact same
    `process_all_active_rules()` the `send_leave_balance_reminders`
    management command exposes for a manual/ad-hoc run -- both call it
    directly, so the two never drift apart. Per-rule isolation and the
    anti-duplicate claim are already handled inside process_all_active_
    rules/process_rule -- a rule blocked (e.g. no valid company timezone)
    or a single rule's failure never stops any other rule, and a tick that
    re-processes an already-delivered occurrence is a pure no-op."""
    results = process_all_active_rules()
    blocked = sum(1 for r in results if "blocked" in r)
    errors = sum(1 for r in results if "error" in r)
    if blocked or errors:
        logger.warning(
            "send_leave_balance_reminders_task: %s rule(s) blocked, %s rule(s) failed -- %s",
            blocked, errors, results,
        )
    return {"rules_processed": len(results), "blocked": blocked, "errors": errors}


@shared_task(
    bind=True,
    name="human_resource_management.send_hr_reminders",
    max_retries=0,
)
def send_hr_reminders_task(self):
    """L03/L05: the scheduled (Celery Beat) entry point for the exact same
    process_onboarding_step_reminders/process_staff_document_validity_
    reminders the `send_hr_reminders` management command exposes for a
    manual run -- both call them directly, so the two never drift apart.
    Same local-only scheduling precedent as send_leave_balance_reminders_
    task above -- NOT wired into the real production CronJob."""
    step_result = process_onboarding_step_reminders(manual=False)
    doc_result = process_staff_document_validity_reminders(manual=False)
    if step_result["failed"] or doc_result["failed"]:
        logger.warning("send_hr_reminders_task: some deliveries failed -- %s / %s", step_result, doc_result)
    return {"onboarding_steps": step_result, "document_validity": doc_result}


@shared_task(
    bind=True,
    name="human_resource_management.auto_trigger_offboarding",
    max_retries=0,
)
def auto_trigger_offboarding_task(self):
    """L05: the scheduled (Celery Beat) entry point for the exact same
    run_auto_trigger_for_company the `auto_trigger_offboarding` management
    command exposes for a manual run -- both call it directly. Same
    local-only scheduling precedent as send_hr_reminders_task above -- NOT
    wired into the real production CronJob."""
    from human_resource_management.models.process_automation import OffboardingAutoTriggerConfig
    from human_resource_management.services.offboarding_automation import run_auto_trigger_for_company

    total_triggered, total_blocked = 0, 0
    for config in OffboardingAutoTriggerConfig.objects.filter(is_enabled=True).select_related("company_profile"):
        result = run_auto_trigger_for_company(config.company_profile)
        total_triggered += result.get("triggered", 0)
        total_blocked += result.get("blocked", 0)
    return {"triggered": total_triggered, "blocked": total_blocked}


@shared_task(
    bind=True,
    name="human_resource_management.evaluate_staff_workload_alerts",
    max_retries=0,
)
def evaluate_staff_workload_alerts_task(self):
    """AI-2054: the scheduled (Celery Beat) entry point for the exact same
    `evaluate_all_active_configs()` the `evaluate_staff_workload_alerts`
    management command exposes for a manual/ad-hoc run -- both call it
    directly, so the two never drift apart. Also the daily catch-all for
    changes that need no PM write to become effective (a month boundary
    rolling over, a scheduled contractual change reaching its effective_from
    date) -- the signal-triggered re-evaluation (human_resource_management.
    signals) only fires on an ACTUAL PM write and can never, by itself,
    catch the mere passage of time."""
    results = evaluate_all_active_configs()
    errors = sum(1 for r in results for e in r.get("entries", []) if "error" in e)
    blocked = sum(1 for r in results for e in r.get("entries", []) if "blocked" in e)
    if blocked or errors:
        logger.warning(
            "evaluate_staff_workload_alerts_task: %s blocked, %s failed -- %s",
            blocked, errors, results,
        )
    return {"configs_processed": len(results), "blocked": blocked, "errors": errors}


@shared_task(
    bind=True,
    name="human_resource_management.evaluate_staff_workload_for_signal",
    max_retries=0,
)
def evaluate_staff_workload_for_signal_task(self, staff_id, config_id):
    """AI-2054: the lightweight, SPECIFIC counterpart to
    evaluate_staff_workload_alerts_task -- dispatched (via
    transaction.on_commit, never inline) by human_resource_management.
    signals whenever a Project Management write (ActionItem save/delete,
    assignees change, a quotite-history save) could plausibly change ONE
    staff member's workload. Isolated on its own Celery task so a slow or
    failing evaluation can NEVER block or roll back the PM write that
    triggered it -- a pure, best-effort async side effect."""
    from human_resource_management.models.workload import StaffWorkloadAlertConfig
    from human_resource_management.services.workload_alerts import (
        WorkloadAlertBlockedError,
        evaluate_staff_workload,
    )
    from models.models import Staff

    try:
        staff = Staff.objects.select_related("user").get(pk=staff_id)
        config = StaffWorkloadAlertConfig.objects.select_related("company_config").get(
            pk=config_id, is_active=True,
        )
    except (Staff.DoesNotExist, StaffWorkloadAlertConfig.DoesNotExist):
        return {"skipped": "staff_or_config_no_longer_exists"}
    try:
        return evaluate_staff_workload(staff, config)
    except WorkloadAlertBlockedError as exc:
        logger.warning("evaluate_staff_workload_for_signal_task: blocked -- %s", exc)
        return {"blocked": str(exc)}
