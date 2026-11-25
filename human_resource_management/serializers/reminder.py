from rest_framework import serializers

from human_resource_management.models.reminder import LeaveBalanceReminderRule, LeaveReminderDelivery
from human_resource_management.permissions import (
    has_cumulative_leave_review_permission,
    has_staff_management_view_permission,
)


class LeaveBalanceReminderRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveBalanceReminderRule
        fields = [
            "id", "name", "trigger", "deadline_date", "anticipation_days",
            "leave_types", "all_eligible_staff", "staff", "email_enabled",
            "cc_hr_users", "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def validate(self, attrs):
        trigger = attrs.get("trigger", getattr(self.instance, "trigger", None))
        deadline_date = attrs.get("deadline_date", getattr(self.instance, "deadline_date", None))
        if trigger == LeaveBalanceReminderRule.TRIGGER_DEADLINE and not deadline_date:
            raise serializers.ValidationError({
                "deadline_date": "Required when trigger is 'deadline'."
            })
        return attrs

    def validate_leave_types(self, value):
        # Isolation: a leave type offered by this rule must belong to the
        # SAME CompanyConfig the rule itself is scoped to -- never a leave
        # type from another solution/company, even one the requester
        # happens to also have access to.
        company_config = self.context.get("company_config") or getattr(self.instance, "company_config", None)
        if company_config is not None:
            for leave_type in value:
                if leave_type.company_config_id != company_config.id:
                    raise serializers.ValidationError(
                        f"Leave type {leave_type.id} does not belong to this rule's company/solution."
                    )
        return value

    def validate_staff(self, value):
        company_config = self.context.get("company_config") or getattr(self.instance, "company_config", None)
        if company_config is None:
            return value
        if company_config.user_id:
            allowed_ids = {company_config.user_id}
            for staff in value:
                if staff.user_id not in allowed_ids:
                    raise serializers.ValidationError(
                        f"Staff {staff.id} does not belong to this rule's company/solution."
                    )
        else:
            company_profile_id = company_config.company_profile_id
            for staff in value:
                if staff.user.company_id != company_profile_id:
                    raise serializers.ValidationError(
                        f"Staff {staff.id} does not belong to this rule's company/solution."
                    )
        return value

    def validate_cc_hr_users(self, value):
        # AI-1685: server-side authorization, not just a UI filter -- a cc
        # recipient must genuinely be an HR user of the SAME company (a
        # real leave reviewer or staff-management holder), re-checked again
        # at delivery time (services.leave_reminders._authorized_cc_users)
        # in case their role changes after the rule was saved.
        company_config = self.context.get("company_config") or getattr(self.instance, "company_config", None)
        company_profile_id = getattr(company_config, "company_profile_id", None)
        for user in value:
            if company_profile_id and user.company_id != company_profile_id:
                raise serializers.ValidationError(
                    f"User {user.id} is not in this rule's company."
                )
            if not (has_cumulative_leave_review_permission(user) or has_staff_management_view_permission(user)):
                raise serializers.ValidationError(
                    f"User {user.id} is not an authorized HR user for this company."
                )
        return value


def serialize_delivery(delivery):
    return {
        "id": delivery.id,
        "rule_id": delivery.rule_id,
        "rule_name": delivery.rule.name,
        "staff_id": delivery.staff_id,
        "staff_name": delivery.staff.user.full_name,
        "deadline_date": delivery.deadline_date.isoformat(),
        "channel": delivery.channel,
        "recipient_id": delivery.recipient_id,
        "recipient_name": delivery.recipient.full_name,
        "is_cc": delivery.is_cc,
        "status": delivery.status,
        "triggered_manually": delivery.triggered_manually,
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
        "last_error": delivery.last_error,
        "created_at": delivery.created_at.isoformat(),
    }
