"""QUALITY Q2: evaluation template library API (list, create, read, edit, activate, duplicate).

Tenant isolation: templates are always filtered on the authenticated user's own company; another
company's template is a 404, never a 403 that would confirm it exists. Reading needs `quality.view`,
writing needs `quality.template_manage` (or `quality.full_access`).
"""

from django.http import Http404
from rest_framework.response import Response

from project_management.models import EvaluationTemplate
from project_management.serializers.supplier_evaluation_template import (
    EvaluationTemplateSerializer,
)
from project_management.services.supplier_evaluation_template import duplicate_template
from project_management.views.quality import (
    _QualityView,
    user_can_manage_templates,
    user_can_view_quality,
)


def _templates(user):
    return EvaluationTemplate.objects.filter(company_id=user.company_id).select_related(
        "created_by"
    ).prefetch_related("sections__criteria")


def _context(request):
    return {"request": request, "company": request.user.company, "user": request.user}


class _TemplateView(_QualityView):
    def _denied(self, request, *, write):
        allowed = user_can_manage_templates(request.user) if write else user_can_view_quality(request.user)
        if allowed:
            return None
        return self._forbidden()

    def _get(self, request, pk):
        template = _templates(request.user).filter(pk=pk).first()
        if template is None:
            raise Http404
        return template


class EvaluationTemplateListView(_TemplateView):
    def get(self, request):
        denied = self._denied(request, write=False)
        if denied:
            return denied
        active = request.query_params.get("is_active")
        queryset = _templates(request.user)
        if active in ("true", "false"):
            queryset = queryset.filter(is_active=active == "true")
        data = EvaluationTemplateSerializer(queryset, many=True, context=_context(request)).data
        return Response({"count": len(data), "results": data})

    def post(self, request):
        denied = self._denied(request, write=True)
        if denied:
            return denied
        serializer = EvaluationTemplateSerializer(data=request.data, context=_context(request))
        serializer.is_valid(raise_exception=True)
        template = serializer.save()
        return Response(
            EvaluationTemplateSerializer(_templates(request.user).get(pk=template.pk),
                                         context=_context(request)).data,
            status=201,
        )


class EvaluationTemplateDetailView(_TemplateView):
    def get(self, request, pk):
        denied = self._denied(request, write=False)
        if denied:
            return denied
        return Response(
            EvaluationTemplateSerializer(self._get(request, pk), context=_context(request)).data
        )

    def patch(self, request, pk):
        denied = self._denied(request, write=True)
        if denied:
            return denied
        template = self._get(request, pk)
        serializer = EvaluationTemplateSerializer(
            template, data=request.data, partial=True, context=_context(request)
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            EvaluationTemplateSerializer(self._get(request, pk), context=_context(request)).data
        )


class EvaluationTemplateDuplicateView(_TemplateView):
    def post(self, request, pk):
        denied = self._denied(request, write=True)
        if denied:
            return denied
        source = self._get(request, pk)
        name = (request.data.get("name") or "").strip() or f"{source.name} (copy)"
        serializer = EvaluationTemplateSerializer(data={"name": name}, context=_context(request))
        serializer.is_valid(raise_exception=True)  # same unique-name rule as a normal create
        copy = duplicate_template(source, user=request.user, name=serializer.validated_data["name"])
        return Response(
            EvaluationTemplateSerializer(self._get(request, copy.pk), context=_context(request)).data,
            status=201,
        )
