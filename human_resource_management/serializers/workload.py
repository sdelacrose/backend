from rest_framework import serializers

from human_resource_management.models.workload import StaffWorkloadAlertConfig
from human_resource_management.permissions import has_workload_view_permission


class StaffWorkloadAlertConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = StaffWorkloadAlertConfig
        fields = ["id", "is_active", "recipients", "created_by", "created_at", "updated_at"]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def validate_recipients(self, value):
        # AI-2054: server-side authorization, not just a UI filter -- a
        # recipient must genuinely hold staff_management.view_workload in
        # the SAME company, re-checked again at delivery time
        # (services.workload_alerts._authorized_recipients) in case their
        # role changes after the config was saved.
        company_config = self.context.get("company_config") or getattr(self.instance, "company_config", None)
        company_profile_id = getattr(company_config, "company_profile_id", None)
        for user in value:
            if company_profile_id and user.company_id != company_profile_id:
                raise serializers.ValidationError(f"User {user.id} is not in this company.")
            if not has_workload_view_permission(user):
                raise serializers.ValidationError(
                    f"User {user.id} does not hold staff_management.view_workload."
                )
        return value


def _anomaly_dict(anomaly):
    return {
        "code": anomaly.code,
        "message": anomaly.message,
        "action_item_id": anomaly.action_item_id,
        "details": anomaly.details,
    }


def _contribution_dict(c):
    return {
        "action_item_id": c.action_item_id,
        "name": c.name,
        "status": c.status,
        "start_date": c.start_date.isoformat() if c.start_date else None,
        "target_date": c.target_date.isoformat(),
        "is_sub_action": c.is_sub_action,
        "project_id": c.project_id,
        "project_name": c.project_name,
        "milestone_id": c.milestone_id,
        "milestone_name": c.milestone_name,
        "budget_total": c.budget_total,
        "assignee_count": c.assignee_count,
        "individual_share": float(c.individual_share),
        "working_days_total": c.working_days_total,
        "working_days_in_period": c.working_days_in_period,
        "contribution": round(float(c.contribution), 4),
        "outside_contract_window": c.outside_contract_window,
    }


def serialize_workload_result(result):
    return {
        "staff_id": result.staff_id,
        "period_start": result.period_start.isoformat(),
        "period_end": result.period_end.isoformat(),
        "capacity": round(float(result.capacity), 4) if result.capacity is not None else None,
        "capacity_state": result.capacity_state,
        "capacity_anomalies": [_anomaly_dict(a) for a in result.capacity_anomalies],
        "charge_known": round(float(result.charge_known), 4),
        "rate": round(float(result.rate), 2) if result.rate is not None else None,
        "rate_is_floor": result.rate_is_floor,
        "completeness": result.completeness,
        "overloaded": result.overloaded,
        "anomalies": [_anomaly_dict(a) for a in result.anomalies],
        "contributions": [_contribution_dict(c) for c in result.contributions],
        "historical_caveat": result.historical_caveat,
        "as_of": result.as_of.isoformat(),
    }


def serialize_episode(episode):
    return {
        "id": episode.id,
        "staff_id": episode.staff_id,
        "staff_name": episode.staff.user.full_name,
        "period_key": episode.period_key,
        "period_start": episode.period_start.isoformat(),
        "period_end": episode.period_end.isoformat(),
        "status": episode.status,
        "started_on": episode.started_on.isoformat(),
        "closed_on": episode.closed_on.isoformat() if episode.closed_on else None,
        "last_evaluated_at": episode.last_evaluated_at.isoformat(),
        "last_known_rate": float(episode.last_known_rate) if episode.last_known_rate is not None else None,
        "had_no_eligible_recipients": episode.had_no_eligible_recipients,
    }
