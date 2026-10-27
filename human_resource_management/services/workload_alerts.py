"""AI-2054: episode lifecycle + per-recipient delivery for the staff-workload
overload alert. Reuses AI-1685/RH15's exact claim/anti-duplicate pattern
(get_or_create + select_for_update) and create_notification_with_recipients,
with no business dependency on leave reminders themselves -- only the
PATTERN is shared, never the models/rule.

Evaluation period is always the CURRENT CALENDAR MONTH in the company's own
configured IANA timezone (resolve_company_timezone, reused as-is) --
deliberately independent of whatever period an RH has selected on the HRM
screen (services.workload computes that separately, read-only, with no
side effect)."""
from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone

from ecpmpapp.license_queries import get_active_license_user_ids_by_product_names
from ecpmpapp.utils import create_notification_with_recipients
from human_resource_management.models.workload import (
    StaffWorkloadAlertConfig,
    StaffWorkloadAlertDelivery,
    StaffWorkloadAlertEpisode,
)
from human_resource_management.permissions import has_workload_view_permission
from human_resource_management.services.leave_accrual import (
    LeaveAccrualTimezoneError,
    resolve_company_timezone,
)
from human_resource_management.services.workload import (
    compute_staff_workload,
    current_month_bounds,
    period_key_for,
)
from models.models import Staff


class WorkloadAlertBlockedError(Exception):
    """A config could not be evaluated at all (e.g. no valid company
    timezone) -- surfaced explicitly, never a silent skip."""


def _authorized_recipients(config):
    """Re-checked at EVERY evaluation (never trusted from config.recipients
    alone) -- active, same company, actually licensed for this solution, and
    still holding staff_management.view_workload. Mirrors AI-1685's
    _authorized_cc_users exactly."""
    company_id = config.company_config.company_profile_id
    solution = config.company_config.solution
    licensed_ids = (
        set(get_active_license_user_ids_by_product_names([solution])) if solution else set()
    )
    authorized = set()
    for user in config.recipients.all():
        if not user.is_active or getattr(user, "is_deleted", False):
            continue
        if company_id and user.company_id != company_id:
            continue
        if user.id not in licensed_ids:
            continue
        if not has_workload_view_permission(user):
            continue
        authorized.add(user)
    return authorized


def _claim_delivery(episode, recipient):
    with transaction.atomic():
        delivery, created = StaffWorkloadAlertDelivery.objects.get_or_create(
            episode=episode, channel=StaffWorkloadAlertDelivery.Channel.INTERNAL, recipient=recipient,
        )
        if created:
            return delivery
        delivery = StaffWorkloadAlertDelivery.objects.select_for_update().get(pk=delivery.pk)
        if delivery.status != StaffWorkloadAlertDelivery.Status.FAILED:
            # Already pending/sent for this recipient -- never a forced
            # resend, and never affects any other recipient's own row.
            return None
        delivery.status = StaffWorkloadAlertDelivery.Status.PENDING
        delivery.last_error = ""
        delivery.save(update_fields=["status", "last_error", "updated_at"])
        return delivery


def _build_alert_message(staff, episode, rate, rate_is_floor):
    rate_text = f"at least {rate:.1f}%" if rate_is_floor else f"{rate:.1f}%"
    return (
        f"Workload alert: {staff.user.full_name} is above 100% capacity "
        f"for {episode.period_key} ({rate_text}, estimated from Project "
        f"Management action items)."
    )


def _deliver_episode(episode, config, *, rate, rate_is_floor):
    """One independent claim/send per currently-authorized recipient --
    an already-served recipient is never resent to; a newly-authorized
    recipient (e.g. permission granted after the episode opened) gets their
    own first delivery on this call."""
    recipients = _authorized_recipients(config)
    if not recipients:
        episode.had_no_eligible_recipients = True
        episode.save(update_fields=["had_no_eligible_recipients", "updated_at"])
        return {"outcome": "no_eligible_recipients"}
    outcomes = {}
    for recipient in recipients:
        delivery = _claim_delivery(episode, recipient)
        if delivery is None:
            outcomes[recipient.id] = "already_delivered"
            continue
        try:
            create_notification_with_recipients(
                message=_build_alert_message(episode.staff, episode, rate, rate_is_floor),
                content_type=ContentType.objects.get_for_model(StaffWorkloadAlertDelivery),
                object_id=delivery.id,
                content_object=delivery,
                notification_type="staff_workload_overload",
                recipients={recipient},
                bypass_preference_check=True,
            )
            delivery.status = StaffWorkloadAlertDelivery.Status.SENT
            delivery.sent_at = timezone.now()
            delivery.save(update_fields=["status", "sent_at", "updated_at"])
        except Exception as exc:  # pragma: no cover - defensive, mirrors AI-1685
            delivery.status = StaffWorkloadAlertDelivery.Status.FAILED
            delivery.last_error = str(exc)
            delivery.save(update_fields=["status", "last_error", "updated_at"])
        outcomes[recipient.id] = delivery.status
    return outcomes


def evaluate_staff_workload(staff, config, *, as_of=None, manual=False):
    """The one evaluation entry point -- shared by the scheduled task, the
    signal-triggered re-evaluation, and a manual recompute (`manual` only
    affects the result's own metadata; the anti-duplicate claim is identical
    either way, never a forced resend)."""
    try:
        tz = resolve_company_timezone(config.company_config)
    except LeaveAccrualTimezoneError as exc:
        raise WorkloadAlertBlockedError(str(exc)) from exc
    local_today = as_of or timezone.now().astimezone(tz).date()
    period_start, period_end = current_month_bounds(local_today)
    period_key = period_key_for(period_start)

    result = compute_staff_workload(
        staff, period_start, period_end, as_of=local_today,
        solution=config.company_config.solution,
    )

    with transaction.atomic():
        live_episode = (
            StaffWorkloadAlertEpisode.objects.select_for_update()
            .filter(staff=staff, period_key=period_key)
            .exclude(status=StaffWorkloadAlertEpisode.Status.CLOSED)
            .first()
        )

        if result.completeness == "capacity_unresolved":
            if live_episode:
                live_episode.status = StaffWorkloadAlertEpisode.Status.INDETERMINATE
                live_episode.last_evaluated_at = timezone.now()
                live_episode.save(update_fields=["status", "last_evaluated_at", "updated_at"])
                return {"staff_id": staff.id, "period_key": period_key, "outcome": "indeterminate_capacity_unresolved", "episode_id": live_episode.id}
            return {"staff_id": staff.id, "period_key": period_key, "outcome": "capacity_unresolved_no_episode"}

        if result.overloaded is True:
            if live_episode:
                live_episode.status = StaffWorkloadAlertEpisode.Status.OPEN
                live_episode.last_evaluated_at = timezone.now()
                live_episode.last_known_rate = result.rate
                live_episode.save(update_fields=["status", "last_evaluated_at", "last_known_rate", "updated_at"])
                is_new = False
            else:
                try:
                    # A SAVEPOINT (nested atomic) around just the risky
                    # insert -- if another concurrent evaluation wins the
                    # race (the partial unique constraint rejects a second
                    # live row for the same staff/period), only this
                    # savepoint rolls back, never the whole evaluation.
                    with transaction.atomic():
                        live_episode = StaffWorkloadAlertEpisode.objects.create(
                            company_config=config.company_config, staff=staff, period_key=period_key,
                            period_start=period_start, period_end=period_end,
                            status=StaffWorkloadAlertEpisode.Status.OPEN,
                            started_on=local_today, last_known_rate=result.rate,
                        )
                    is_new = True
                except IntegrityError:
                    # Lost the race -- resolve to the row the OTHER
                    # evaluation just created, exactly as if we had found
                    # it to begin with (never a duplicate episode).
                    live_episode = (
                        StaffWorkloadAlertEpisode.objects.select_for_update()
                        .filter(staff=staff, period_key=period_key)
                        .exclude(status=StaffWorkloadAlertEpisode.Status.CLOSED)
                        .get()
                    )
                    is_new = False
            deliveries = _deliver_episode(
                live_episode, config, rate=result.rate, rate_is_floor=result.rate_is_floor,
            )
            return {
                "staff_id": staff.id, "period_key": period_key,
                "outcome": "episode_opened" if is_new else "episode_persisted",
                "episode_id": live_episode.id, "rate": float(result.rate),
                "manual": manual, "deliveries": deliveries,
            }

        if result.overloaded is False:
            # Definite, on SUFFICIENT data -- the only condition that may
            # close a live episode.
            if live_episode:
                live_episode.status = StaffWorkloadAlertEpisode.Status.CLOSED
                live_episode.closed_on = local_today
                live_episode.last_evaluated_at = timezone.now()
                live_episode.last_known_rate = result.rate
                live_episode.save(update_fields=["status", "closed_on", "last_evaluated_at", "last_known_rate", "updated_at"])
                return {"staff_id": staff.id, "period_key": period_key, "outcome": "episode_closed", "episode_id": live_episode.id}
            return {"staff_id": staff.id, "period_key": period_key, "outcome": "no_episode_not_overloaded"}

        # result.overloaded is None under "partial" completeness with a
        # known rate <= 100% -- never announced as "not overloaded", and a
        # live episode is never artificially closed on this ambiguous data.
        if live_episode:
            live_episode.status = StaffWorkloadAlertEpisode.Status.INDETERMINATE
            live_episode.last_evaluated_at = timezone.now()
            live_episode.save(update_fields=["status", "last_evaluated_at", "updated_at"])
            return {"staff_id": staff.id, "period_key": period_key, "outcome": "indeterminate_partial_data", "episode_id": live_episode.id}
        return {"staff_id": staff.id, "period_key": period_key, "outcome": "no_episode_indeterminate"}


def staff_queryset_for_config(config):
    company_config = config.company_config
    if company_config.user_id:
        return Staff.objects.filter(user_id=company_config.user_id)
    return Staff.objects.filter(
        user__company_id=company_config.company_profile_id, user__is_deleted=False,
    )


def evaluate_config(config, *, as_of=None, manual=False):
    """Every staff member scoped to this config's company/solution -- one
    rule's failure never stops another (same isolation precedent as
    AI-1685's process_all_active_rules)."""
    results = []
    for staff in staff_queryset_for_config(config).select_related("user"):
        try:
            results.append(evaluate_staff_workload(staff, config, as_of=as_of, manual=manual))
        except WorkloadAlertBlockedError as exc:
            results.append({"staff_id": staff.id, "blocked": str(exc)})
            break  # the whole config shares one timezone -- a single raise suffices
        except Exception as exc:  # noqa: BLE001 - isolate one staff member's failure from the rest
            results.append({"staff_id": staff.id, "error": str(exc)})
    return results


def evaluate_all_active_configs():
    """Shared by the management command and the scheduled Celery task."""
    results = []
    for config in StaffWorkloadAlertConfig.objects.filter(is_active=True).select_related("company_config"):
        entries = evaluate_config(config)
        results.append({"config_id": config.id, "entries": entries})
    return results
