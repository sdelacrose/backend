from datetime import date

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from models.models import Staff, User
from human_resource_management.models.onboarding import (
    OnboardingInstance, OnboardingStepInstance, OnboardingTemplate, OnboardingTemplateStep, ProcessKind, StepKind,
)
from human_resource_management.permissions import (
    can_act_on_onboarding_step, can_close_onboarding_instance, can_manage_onboarding_templates, can_trigger_onboarding,
    can_view_onboarding_instance, has_offboarding_manage_templates_permission, has_offboarding_trigger_permission,
    has_offboarding_view_all_permission, has_onboarding_manage_templates_permission, has_onboarding_trigger_permission,
    has_onboarding_view_all_permission,
)
from human_resource_management.serializers.onboarding import serialize_event, serialize_instance, serialize_step_instance, serialize_template
from human_resource_management.services.hr_reminders import notify_step_assigned
from human_resource_management.services.offboarding_interview import InterviewSchedulingError, schedule_interview
from human_resource_management.services.onboarding import (
    OnboardingError, apply_arrival_date_change, cancel_onboarding, close_onboarding, complete_step,
    preview_arrival_date_change, preview_trigger, reassign_step, trigger_onboarding,
)


def _parse_datetime(value, field_name):
    parsed = None
    if value:
        from django.utils.dateparse import parse_datetime
        parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValidationError({field_name: "Must be a valid ISO datetime."})
    return parsed


def _parse_date(value, field_name):
    parsed = None
    if value:
        from django.utils.dateparse import parse_date
        parsed = parse_date(str(value))
    if parsed is None:
        raise ValidationError({field_name: "Must be a valid ISO date (YYYY-MM-DD)."})
    return parsed


class OnboardingTemplateViewSet(viewsets.GenericViewSet):
    """L05: company-scoped, never accepts a company id in the request. A new
    POST always creates a NEW template (including its steps in one call) --
    editing is limited to activating/deactivating (partial_update); this
    keeps the already-simple snapshot-at-trigger-time guarantee trivially
    true without needing a separate step-level edit API in this lot."""

    def _company(self, request):
        company = getattr(request.user, "company", None)
        if company is None:
            raise DRFPermissionDenied("No company associated with this account.")
        return company

    @staticmethod
    def _resolve_process_kind(raw):
        if raw in (ProcessKind.ONBOARDING, ProcessKind.OFFBOARDING):
            return raw
        raise ValidationError({"process_kind": "Must be 'onboarding' or 'offboarding'."})

    def list(self, request, *args, **kwargs):
        company = self._company(request)
        # A request filtered to one process_kind is gated by THAT kind's own
        # permissions only; an unfiltered request (legacy callers that never
        # sent process_kind) keeps the exact pre-L05 behaviour -- onboarding
        # permissions only, never silently exposing offboarding templates to
        # someone who merely holds the onboarding rights.
        raw_kind = request.query_params.get("process_kind")
        if raw_kind:
            process_kind = self._resolve_process_kind(raw_kind)
            trigger_perm = has_offboarding_trigger_permission if process_kind == ProcessKind.OFFBOARDING else has_onboarding_trigger_permission
            manage_perm = has_offboarding_manage_templates_permission if process_kind == ProcessKind.OFFBOARDING else has_onboarding_manage_templates_permission
            allowed = trigger_perm(request.user) or manage_perm(request.user) or getattr(company, "admin_user_id", None) == request.user.id
            if not allowed:
                raise DRFPermissionDenied("You do not have permission to view these templates.")
            templates = OnboardingTemplate.objects.filter(company_profile=company, process_kind=process_kind).order_by("name")
        else:
            allowed = (
                has_onboarding_trigger_permission(request.user)
                or has_onboarding_manage_templates_permission(request.user)
                or getattr(company, "admin_user_id", None) == request.user.id
            )
            if not allowed:
                raise DRFPermissionDenied("You do not have permission to view onboarding templates.")
            templates = OnboardingTemplate.objects.filter(company_profile=company, process_kind=ProcessKind.ONBOARDING).order_by("name")
        return Response([serialize_template(t) for t in templates])

    def create(self, request, *args, **kwargs):
        company = self._company(request)
        process_kind = self._resolve_process_kind(request.data.get("process_kind", ProcessKind.ONBOARDING))
        if not can_manage_onboarding_templates(request.user, company, process_kind=process_kind):
            raise DRFPermissionDenied("You do not have permission to manage these templates.")
        name = request.data.get("name")
        steps = request.data.get("steps") or []
        if not name:
            raise ValidationError({"name": "This field is required."})
        if not steps:
            raise ValidationError({"steps": "At least one step is required."})
        with transaction.atomic():
            template = OnboardingTemplate.objects.create(
                company_profile=company, process_kind=process_kind, name=name, created_by=request.user,
            )
            for index, raw_step in enumerate(steps):
                role = raw_step.get("responsible_role")
                if role not in dict(OnboardingTemplateStep.ROLE_CHOICES):
                    raise ValidationError({"steps": f"Step {index}: invalid responsible_role."})
                if role == OnboardingTemplateStep.ROLE_NAMED and not raw_step.get("default_responsible_user"):
                    raise ValidationError({"steps": f"Step {index}: 'named' role requires default_responsible_user."})
                step_kind = raw_step.get("step_kind", StepKind.CHECKLIST)
                if step_kind not in StepKind.values:
                    raise ValidationError({"steps": f"Step {index}: invalid step_kind."})
                OnboardingTemplateStep.objects.create(
                    template=template, order=raw_step.get("order", index), title=raw_step.get("title", ""),
                    step_kind=step_kind,
                    responsible_role=role, default_responsible_user_id=raw_step.get("default_responsible_user"),
                    due_offset_days=raw_step.get("due_offset_days", 0),
                    expected_category_id=raw_step.get("expected_category"),
                    visible_to_employee=bool(raw_step.get("visible_to_employee", False)),
                    is_required=bool(raw_step.get("is_required", True)),
                )
        return Response(serialize_template(template), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None, *args, **kwargs):
        company = self._company(request)
        template = get_object_or_404(OnboardingTemplate, pk=pk, company_profile=company)
        if not can_manage_onboarding_templates(request.user, company, process_kind=template.process_kind):
            raise DRFPermissionDenied("You do not have permission to manage these templates.")
        if "is_active" in request.data:
            template.is_active = bool(request.data["is_active"])
            template.save(update_fields=["is_active"])
        return Response(serialize_template(template))


class OnboardingInstanceViewSet(viewsets.GenericViewSet):
    def _target_staff(self, request, staff_id):
        staff = get_object_or_404(Staff.objects.select_related("user__company", "supervisor__user"), pk=staff_id)
        return staff

    def preview(self, request, staff_id=None, *args, **kwargs):
        staff = self._target_staff(request, staff_id)
        template = get_object_or_404(OnboardingTemplate, pk=request.data.get("template"), company_profile=staff.user.company)
        if not can_trigger_onboarding(request.user, staff, process_kind=template.process_kind):
            raise DRFPermissionDenied("You do not have permission to trigger this process for this staff member.")
        anchor_date = _parse_date(request.data.get("arrival_date"), "arrival_date")
        overrides = self._resolve_overrides(request.data.get("overrides") or {})
        rows = preview_trigger(staff=staff, template=template, anchor_date=anchor_date, overrides=overrides)
        return Response([{**r, "due_date": r["due_date"].isoformat()} for r in rows])

    def _resolve_overrides(self, raw_overrides):
        overrides = {}
        for step_id, user_id in raw_overrides.items():
            if user_id:
                overrides[int(step_id)] = get_object_or_404(User, pk=user_id)
        return overrides

    def trigger(self, request, staff_id=None, *args, **kwargs):
        staff = self._target_staff(request, staff_id)
        template = get_object_or_404(OnboardingTemplate, pk=request.data.get("template"), company_profile=staff.user.company, is_active=True)
        if not can_trigger_onboarding(request.user, staff, process_kind=template.process_kind):
            raise DRFPermissionDenied("You do not have permission to trigger this process for this staff member.")
        anchor_date = _parse_date(request.data.get("arrival_date"), "arrival_date")
        overrides = self._resolve_overrides(request.data.get("overrides") or {})
        try:
            instance, created = trigger_onboarding(staff=staff, template=template, anchor_date=anchor_date, actor=request.user, overrides=overrides)
        except OnboardingError as exc:
            raise ValidationError(str(exc))
        if created:
            for step in instance.step_instances.all():
                transaction.on_commit(lambda step=step: notify_step_assigned(step_instance=step, actor=request.user))
        return Response(serialize_instance(instance, request.user), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def list(self, request, *args, **kwargs):
        if request.query_params.get("all") == "true":
            company = getattr(request.user, "company", None)
            raw_kind = request.query_params.get("process_kind")
            process_kind = OnboardingTemplateViewSet._resolve_process_kind(raw_kind) if raw_kind else ProcessKind.ONBOARDING
            view_all_permission = has_offboarding_view_all_permission if process_kind == ProcessKind.OFFBOARDING else has_onboarding_view_all_permission
            if company is None or not (view_all_permission(request.user) or getattr(company, "admin_user_id", None) == request.user.id):
                raise DRFPermissionDenied("You do not have permission to view all instances of this kind.")
            instances = OnboardingInstance.objects.filter(company_profile=company, process_kind=process_kind).select_related("staff__user")
            return Response([serialize_instance(i, request.user) for i in instances])

        staff = self._target_staff(request, request.query_params.get("staff"))
        raw_kind = request.query_params.get("process_kind")
        # Omitting process_kind defaults to ONBOARDING-only, same backward-
        # compatibility rule as the templates list and the all=true branch
        # above -- a caller that predates L05 (or the Onboarding section's
        # own call, which never sends this param) must never see offboarding
        # instances mixed in.
        process_kind = OnboardingTemplateViewSet._resolve_process_kind(raw_kind) if raw_kind else ProcessKind.ONBOARDING
        instances = OnboardingInstance.objects.filter(staff=staff, process_kind=process_kind).select_related("staff__user")
        instances = [i for i in instances if can_view_onboarding_instance(request.user, i)]
        return Response([serialize_instance(i, request.user) for i in instances])

    def retrieve(self, request, pk=None, *args, **kwargs):
        instance = get_object_or_404(OnboardingInstance.objects.select_related("staff__user"), pk=pk)
        if not can_view_onboarding_instance(request.user, instance):
            raise DRFPermissionDenied("You do not have permission to view this onboarding instance.")
        return Response(serialize_instance(instance, request.user))

    def events(self, request, pk=None, *args, **kwargs):
        instance = get_object_or_404(OnboardingInstance.objects.select_related("staff__user"), pk=pk)
        if not can_view_onboarding_instance(request.user, instance):
            raise DRFPermissionDenied("You do not have permission to view this onboarding instance.")
        return Response([serialize_event(e) for e in instance.events.order_by("created_at")])

    def cancel(self, request, pk=None, *args, **kwargs):
        instance = get_object_or_404(OnboardingInstance.objects.select_related("staff__user"), pk=pk)
        if not can_trigger_onboarding(request.user, instance.staff, process_kind=instance.process_kind):
            raise DRFPermissionDenied("You do not have permission to cancel this instance.")
        try:
            cancel_onboarding(instance=instance, actor=request.user, reason=request.data.get("reason", ""))
        except OnboardingError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_instance(instance, request.user))

    def close(self, request, pk=None, *args, **kwargs):
        instance = get_object_or_404(OnboardingInstance.objects.select_related("staff__user"), pk=pk)
        if not can_close_onboarding_instance(request.user, instance):
            raise DRFPermissionDenied("You do not have permission to close this instance.")
        try:
            close_onboarding(instance=instance, actor=request.user)
        except OnboardingError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_instance(instance, request.user))

    def arrival_date_preview(self, request, pk=None, *args, **kwargs):
        instance = get_object_or_404(OnboardingInstance.objects.select_related("staff__user"), pk=pk)
        if not can_trigger_onboarding(request.user, instance.staff, process_kind=instance.process_kind):
            raise DRFPermissionDenied("You do not have permission to change this instance's date.")
        new_anchor_date = _parse_date(request.data.get("arrival_date"), "arrival_date")
        rows = preview_arrival_date_change(instance=instance, new_anchor_date=new_anchor_date)
        return Response([{**r, "old_due_date": r["old_due_date"].isoformat(), "new_due_date": r["new_due_date"].isoformat()} for r in rows])

    def arrival_date_apply(self, request, pk=None, *args, **kwargs):
        instance = get_object_or_404(OnboardingInstance.objects.select_related("staff__user"), pk=pk)
        if not can_trigger_onboarding(request.user, instance.staff, process_kind=instance.process_kind):
            raise DRFPermissionDenied("You do not have permission to change this instance's date.")
        new_anchor_date = _parse_date(request.data.get("arrival_date"), "arrival_date")
        apply_arrival_date_change(instance=instance, new_anchor_date=new_anchor_date, actor=request.user)
        return Response(serialize_instance(instance, request.user))


class OnboardingStepInstanceViewSet(viewsets.GenericViewSet):
    def complete(self, request, pk=None, *args, **kwargs):
        step = get_object_or_404(OnboardingStepInstance.objects.select_related("instance__staff__user"), pk=pk)
        if not can_act_on_onboarding_step(request.user, step):
            raise DRFPermissionDenied("You do not have permission to act on this step.")
        try:
            complete_step(step_instance=step, actor=request.user)
        except OnboardingError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_step_instance(step))

    def reassign(self, request, pk=None, *args, **kwargs):
        step = get_object_or_404(OnboardingStepInstance.objects.select_related("instance__staff__user"), pk=pk)
        if not can_trigger_onboarding(request.user, step.instance.staff, process_kind=step.instance.process_kind):
            raise DRFPermissionDenied("You do not have permission to reassign this step.")
        new_user = get_object_or_404(User, pk=request.data.get("user"))
        if getattr(new_user, "company_id", None) != step.instance.company_profile_id:
            raise ValidationError({"user": "Must belong to the same company."})
        reassign_step(step_instance=step, new_responsible_user=new_user, actor=request.user)
        transaction.on_commit(lambda: notify_step_assigned(step_instance=step, actor=request.user))
        return Response(serialize_step_instance(step))

    def interview(self, request, pk=None, *args, **kwargs):
        """L05: "planification des entretiens de depart" (spec 6.9,
        criterion 4 -- partial, see services.offboarding_interview's module
        docstring). start_date/end_date are explicit ISO datetimes supplied
        by the caller -- never auto-picked."""
        step = get_object_or_404(OnboardingStepInstance.objects.select_related("instance__staff__user"), pk=pk)
        if not can_act_on_onboarding_step(request.user, step):
            raise DRFPermissionDenied("You do not have permission to act on this step.")
        start_date = _parse_datetime(request.data.get("start_date"), "start_date")
        end_date = _parse_datetime(request.data.get("end_date"), "end_date")
        try:
            schedule_interview(step_instance=step, start_date=start_date, end_date=end_date, actor=request.user)
        except InterviewSchedulingError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_step_instance(step))
