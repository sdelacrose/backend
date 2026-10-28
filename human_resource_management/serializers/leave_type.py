from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from human_resource_management.services.leave_accrual import (
    first_of_next_month,
    record_policy_change,
    record_rate_change,
)
from models.leave_type_sync import sync_leave_types_array
from models.models import LeaveSubType, LeaveType


class LeaveSubTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveSubType
        fields = ["id", "leave_type", "key", "name", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def _save_with_model_validation(self, instance):
        try:
            instance.full_clean(exclude=["id"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        instance.save()
        return instance

    def create(self, validated_data):
        return self._save_with_model_validation(LeaveSubType(**validated_data))

    def update(self, instance, validated_data):
        for field_name, value in validated_data.items():
            setattr(instance, field_name, value)
        return self._save_with_model_validation(instance)


class LeaveTypeSerializer(serializers.ModelSerializer):
    effective_config = serializers.SerializerMethodField()
    overridden_fields = serializers.SerializerMethodField()
    sub_types = LeaveSubTypeSerializer(many=True, read_only=True)
    # LOT RH10: monthly_accrual_rate/accrual_effective_date are deliberately NOT part of
    # INHERITABLE_FIELDS/effective_config (see LeaveType's own docstring -- there is no
    # sensible organization-wide default rate), so their resolved values are exposed
    # separately here via the type's own effective_accrual_rate()/
    # effective_accrual_effective_date() helpers.
    effective_accrual_rate = serializers.SerializerMethodField()
    effective_accrual_effective_date = serializers.SerializerMethodField()
    # LOT RH10F: write-only, not a model field -- lets the caller pick WHEN a
    # rate CHANGE (not the type's very first recorded rate -- that one still
    # anchors to accrual_effective_date, see _save_with_model_validation)
    # takes effect. Never applied retroactively mid-month: validated below to
    # be the 1st of some month, and defaults to the 1st of NEXT month when
    # omitted, so an unspecified change is never silently mid-month either.
    rate_effective_from = serializers.DateField(
        write_only=True, required=False, allow_null=True
    )
    # LOT RH12: write-only, not a model field -- same convention as
    # rate_effective_from, for a CHANGE (not the type's very first recorded
    # policy value -- that one still anchors to accrual_effective_date, see
    # _save_with_model_validation) of `prorate_accrual_by_quotite`.
    quotite_policy_effective_from = serializers.DateField(
        write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = LeaveType
        fields = [
            "id",
            "key",
            "name",
            "inherits_from",
            "is_active",
            "deduction_mode",
            "cycle_start_date",
            "cycle_end_date",
            "allow_half_day",
            "requires_supporting_document",
            "consumes_balance",
            "monthly_accrual_rate",
            "accrual_effective_date",
            "rate_effective_from",
            # LOT RH11: like monthly_accrual_rate/accrual_effective_date
            # above, deliberately NOT part of INHERITABLE_FIELDS/
            # effective_config (see LeaveType's own docstring) -- a
            # company explicitly configures report/cap/delay per type,
            # never inherited from an org-wide default.
            "carry_over_enabled",
            "carry_over_cap",
            "carry_over_expiration_months",
            # LOT RH12: like carry_over_* above, deliberately NOT part of
            # INHERITABLE_FIELDS -- disabled by default, never inherited,
            # explicit activation per type only.
            "prorate_accrual_by_quotite",
            "quotite_policy_effective_from",
            "effective_accrual_rate",
            "effective_accrual_effective_date",
            "effective_config",
            "overridden_fields",
            "sub_types",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_effective_config(self, obj):
        return obj.effective_config()

    def get_overridden_fields(self, obj):
        return obj.overridden_fields

    def get_effective_accrual_rate(self, obj):
        rate = obj.effective_accrual_rate()
        return float(rate) if rate is not None else None

    def get_effective_accrual_effective_date(self, obj):
        value = obj.effective_accrual_effective_date()
        return value.isoformat() if value is not None else None

    def validate_inherits_from(self, value):
        if value is not None and not value.global_config_id:
            raise serializers.ValidationError(
                "A company-scope leave type can only inherit from an Admin-scope leave type."
            )
        return value

    def validate_rate_effective_from(self, value):
        # LOT RH10F: a rate CHANGE must take effect on the first day of a
        # month -- never silently backdated/mid-month. (The type's very
        # first-ever rate is exempt: it still anchors to accrual_effective_date,
        # which this field is never used for -- see _save_with_model_validation.)
        if value is not None and value.day != 1:
            raise serializers.ValidationError(
                "A rate change can only take effect on the first day of a month."
            )
        return value

    def validate_quotite_policy_effective_from(self, value):
        # LOT RH12: same first-of-month rule as rate_effective_from -- a
        # policy CHANGE is never silently backdated/mid-month.
        if value is not None and value.day != 1:
            raise serializers.ValidationError(
                "A quotite-proration policy change can only take effect on the first day of a month."
            )
        return value

    def _save_with_model_validation(
        self, instance, rate_effective_from=None, quotite_policy_effective_from=None,
        is_create=False,
    ):
        try:
            instance.full_clean(exclude=["id"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        instance.save()
        sync_leave_types_array(instance.company_config)
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        # LOT RH10 follow-up: record this rate in the type's accrual rate
        # history whenever it's set -- never from credit_month, only from
        # here, the one write path for `monthly_accrual_rate`. A no-op save
        # that doesn't actually change the rate adds no spurious row (see
        # record_rate_change). The FIRST-ever row for a type is anchored to
        # its own accrual_effective_date (if already set), so the whole
        # history from day one resolves without a gap.
        if instance.monthly_accrual_rate is not None:
            is_first = not instance.accrual_rate_history.exists()
            if is_first:
                effective_from = (
                    instance.accrual_effective_date
                    if instance.accrual_effective_date else None
                )
            else:
                # LOT RH10F: a genuine rate CHANGE (not the first-ever rate)
                # only ever takes effect on the 1st of a month (validated
                # above) -- caller-supplied, or the 1st of NEXT month by
                # default so an unspecified change is never retroactively
                # applied mid-month either. Never touches any already-posted
                # LeaveAccrualCredit (see that model's own docstring).
                effective_from = rate_effective_from or first_of_next_month()
            record_rate_change(
                instance, instance.monthly_accrual_rate,
                effective_from=effective_from, created_by=user,
            )
        # LOT RH12 follow-up: the policy's very FIRST recorded value anchors
        # to accrual_effective_date ONLY for a genuinely BRAND-NEW type
        # (is_create=True) -- "entree initiale reellement configuree des
        # l'origine" (e.g. a new type created with the box already checked):
        # there is no past for it yet, so anchoring never silently applies a
        # policy to months that already elapsed under a DIFFERENT, already-
        # running type. For an EXISTING type switching the policy for the
        # first time (is_create=False), the change is NEVER implicitly
        # backdated to accrual_effective_date -- it defaults to the 1st of
        # NEXT month, exactly like any other change, unless RH explicitly
        # supplies a historical `quotite_policy_effective_from` (validated
        # above to still be the 1st of some month). Only recorded for a type
        # actually in monthly-accrual mode (same guard as the rate above) --
        # a pure quota-mode type never accumulates policy history it can
        # never be credited against anyway.
        if instance.monthly_accrual_rate is not None:
            is_first_policy = not instance.quotite_proration_history.exists()
            if is_first_policy and is_create:
                policy_effective_from = (
                    instance.accrual_effective_date
                    if instance.accrual_effective_date else None
                )
            else:
                policy_effective_from = quotite_policy_effective_from or first_of_next_month()
            record_policy_change(
                instance, instance.prorate_accrual_by_quotite,
                effective_from=policy_effective_from, created_by=user,
            )
        return instance

    def create(self, validated_data):
        rate_effective_from = validated_data.pop("rate_effective_from", None)
        quotite_policy_effective_from = validated_data.pop("quotite_policy_effective_from", None)
        instance = LeaveType(**validated_data)
        return self._save_with_model_validation(
            instance, rate_effective_from=rate_effective_from,
            quotite_policy_effective_from=quotite_policy_effective_from, is_create=True,
        )

    def update(self, instance, validated_data):
        rate_effective_from = validated_data.pop("rate_effective_from", None)
        quotite_policy_effective_from = validated_data.pop("quotite_policy_effective_from", None)
        for field_name, value in validated_data.items():
            setattr(instance, field_name, value)
        return self._save_with_model_validation(
            instance, rate_effective_from=rate_effective_from,
            quotite_policy_effective_from=quotite_policy_effective_from, is_create=False,
        )
