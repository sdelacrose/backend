"""PROSEVIEW MOD-01: Action Item templates of a COMPANY (company configuration) and weighted progress.

    GET    action-templates                       templates of the user's company
    POST   action-templates                       {title, sub_actions:[{title, in_progress_points, closed_points}]}
    GET    action-templates/<id>
    PATCH  action-templates/<id>                  title and/or sub_actions (full ordered list replaces the old one)
    DELETE action-templates/<id>                  actions already created keep their copied points
    GET    action-templates/lookup?project=<id>&title=<text>
                                                  exact match, company read from the (authorized) project
    GET    action_items/<id>/weighted-progress

Rights are the existing COMPANY configuration rights (`company_configurations`, scope company, same
checks as CompanyConfigurationsPermission): view to read, create/update/full_access to write,
delete/full_access to delete. The company is always the user's own company (never a client value):
another company's template is a plain 404. Templates stay usable in every project of the company;
requests, parent actions and sub-actions remain project data.
"""
from django.db import IntegrityError, transaction
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from companyprofileapis.permissions import CompanyConfigurationsPermission
from project_management.models import ActionItem, ActionTemplate, ActionTemplateSubAction
from project_management.models.action_template import normalize_title
from project_management.project_access import accessible_projects
from project_management.services.action_progress import (
    compute_progress,
    find_template,
    refresh_review_flags,
    validate_points,
)
from project_management.views.intervention import can_view_interventions


def _company_id(user):
    """The user's own company; freelancers have none and therefore no company templates."""
    if getattr(user, "is_freelancer", False) or not getattr(user, "company_id", None):
        raise Http404
    return user.company_id


def _config_allowed(user, level):
    perm = CompanyConfigurationsPermission(action=level, scope="company")
    if level == "create":
        return bool(perm._check_create_permission(user))
    check = {
        "view": perm._check_view_permission,
        "update": perm._check_update_permission,
        "delete": perm._check_delete_permission,
    }[level]
    return bool(check(user, None))


def _forbidden():
    return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)


def _can_write(user):
    return _config_allowed(user, "create") or _config_allowed(user, "update")


class SubActionSerializer(serializers.ModelSerializer):
    title = serializers.CharField(max_length=255)
    in_progress_points = serializers.IntegerField(min_value=0, max_value=100)
    closed_points = serializers.IntegerField(min_value=0, max_value=100)

    class Meta:
        model = ActionTemplateSubAction
        fields = ("id", "position", "title", "in_progress_points", "closed_points")
        read_only_fields = ("id", "position")

    def validate_title(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("A sub-action title is required.")
        return value


class ActionTemplateSerializer(serializers.ModelSerializer):
    title = serializers.CharField(max_length=255)
    sub_actions = SubActionSerializer(many=True)

    class Meta:
        model = ActionTemplate
        fields = ("id", "title", "sub_actions", "needs_review", "created_at", "updated_at")
        read_only_fields = ("id", "needs_review", "created_at", "updated_at")

    def validate_title(self, value):
        value = value.strip()
        if not normalize_title(value):
            raise serializers.ValidationError("A title is required.")
        return value

    def validate(self, attrs):
        company_id = self.context["company_id"]
        if "title" in attrs:
            clash = ActionTemplate.objects.filter(
                company_id=company_id, normalized_title=normalize_title(attrs["title"])
            )
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"title": "A template with this title already exists in the company."}
                )
        if "sub_actions" in attrs:
            try:
                validate_points(attrs["sub_actions"])
            except ValueError as error:
                raise serializers.ValidationError({"sub_actions": str(error)})
        return attrs

    def _write_sub_actions(self, template, sub_actions):
        template.sub_actions.all().delete()
        ActionTemplateSubAction.objects.bulk_create(
            ActionTemplateSubAction(template=template, position=index, **sub)
            for index, sub in enumerate(sub_actions, start=1)
        )

    @transaction.atomic
    def create(self, validated_data):
        sub_actions = validated_data.pop("sub_actions")
        try:
            with transaction.atomic():
                template = ActionTemplate.objects.create(
                    company_id=self.context["company_id"],
                    created_by=self.context["request"].user,
                    **validated_data,
                )
        except IntegrityError:  # concurrent twin: the DB constraint is the backstop
            raise serializers.ValidationError({"title": "A template with this title already exists in the company."})
        self._write_sub_actions(template, sub_actions)
        return template

    @transaction.atomic
    def update(self, instance, validated_data):
        sub_actions = validated_data.pop("sub_actions", None)
        old_key = instance.normalized_title
        if "title" in validated_data:
            instance.title = validated_data["title"]
            try:
                with transaction.atomic():
                    instance.save()
            except IntegrityError:
                raise serializers.ValidationError({"title": "A template with this title already exists in the company."})
            refresh_review_flags(instance.company_id, [old_key, instance.normalized_title])
        if sub_actions is not None:
            self._write_sub_actions(instance, sub_actions)
        return instance


def _templates_for(user):
    return ActionTemplate.objects.filter(company_id=_company_id(user)).prefetch_related("sub_actions")


def _context(request):
    return {"company_id": _company_id(request.user), "request": request}


class ActionTemplateListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _config_allowed(request.user, "view"):
            return _forbidden()
        return Response(
            ActionTemplateSerializer(_templates_for(request.user), many=True, context=_context(request)).data
        )

    def post(self, request):
        _company_id(request.user)
        if not _can_write(request.user):
            return _forbidden()
        serializer = ActionTemplateSerializer(data=request.data, context=_context(request))
        serializer.is_valid(raise_exception=True)
        template = _templates_for(request.user).get(pk=serializer.save().pk)
        return Response(
            ActionTemplateSerializer(template, context=_context(request)).data, status=status.HTTP_201_CREATED
        )


class ActionTemplateDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        try:
            return _templates_for(request.user).get(pk=pk)
        except ActionTemplate.DoesNotExist:
            raise Http404

    def get(self, request, pk):
        template = self._get(request, pk)
        if not _config_allowed(request.user, "view"):
            return _forbidden()
        return Response(ActionTemplateSerializer(template, context=_context(request)).data)

    def patch(self, request, pk):
        template = self._get(request, pk)
        if not _config_allowed(request.user, "update"):
            return _forbidden()
        serializer = ActionTemplateSerializer(template, data=request.data, partial=True, context=_context(request))
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ActionTemplateSerializer(self._get(request, pk), context=_context(request)).data)

    def delete(self, request, pk):
        template = self._get(request, pk)
        if not _config_allowed(request.user, "delete"):
            return _forbidden()
        company_id, key = template.company_id, template.normalized_title
        template.delete()
        refresh_review_flags(company_id, [key])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ActionTemplateLookupView(APIView):
    """Exact (normalized) title match. The company comes from the AUTHORIZED project, never from
    the request; readable by whoever can read the project's intervention requests."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            project = accessible_projects(request.user).get(pk=int(request.query_params.get("project")))
        except (TypeError, ValueError, accessible_projects(request.user).model.DoesNotExist):
            raise Http404
        if not can_view_interventions(request.user, project):
            return _forbidden()
        template = find_template(project, request.query_params.get("title"))
        if template is None:
            return Response({"match": None})
        template = ActionTemplate.objects.prefetch_related("sub_actions").get(pk=template.pk)
        return Response(
            {"match": ActionTemplateSerializer(template, context={"company_id": template.company_id, "request": request}).data}
        )


class ActionItemWeightedProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            action = ActionItem.objects.select_related("milestone__project").get(
                pk=pk, milestone__project__in=accessible_projects(request.user), parent_action_item__isnull=True
            )
        except ActionItem.DoesNotExist:
            raise Http404
        if not can_view_interventions(request.user, action.milestone.project):
            return _forbidden()
        return Response(compute_progress(action))
