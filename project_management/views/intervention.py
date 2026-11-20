"""DEMO-01: intervention requests of a project and their reviewed CSV import.

    GET  intervention-requests?project=<id>[&search=&ordering=&page=&page_size=]
    GET  intervention-requests/<id>
    POST intervention-requests/import/preview     multipart: project, file, [source]
    GET  intervention-requests/import/<uuid>      batch with its classified rows
    POST intervention-requests/import/<uuid>/confirm

Rights reuse the Action Item module at project scope (the import creates Action Items):
  * read    action_item view / view_all / full_access on the project;
  * import  action_item create / full_access on the project.
Every query goes through `accessible_projects`, so a project (or request) of another company or a
project the user cannot reach is a plain 404. There is no DELETE and no project change in this
first scope: a request that owns actions is PROTECTed anyway.
"""
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.models import (
    ActionItem,
    InterventionImportBatch,
    InterventionImportRow,
    InterventionRequest,
)
from project_management.pagination import CustomPageNumberPagination
from project_management.permissions import ActionItemPermission, check_company_project_permission
from project_management.project_access import accessible_projects
from project_management.services.intervention_import import (
    MOTHER_ACTION_KEY,
    BatchNotConfirmable,
    AssignmentNotAtImport,
    InvalidAssignee,
    ImportFileError,
    PlanIncomplete,
    PlanRejected,
    confirm_import,
    preview_import,
    refresh_summary,
)
from project_management.services.intervention_plan import (
    MAX_SUB_ACTIONS,
    MAX_SUB_TITLE,
    PlanError,
    can_manage_templates,
    clean_plan,
    plan_lines_from_template,
    template_plan,
)
from project_management.services.action_progress import compute_progress, effective_bpu


def _project_or_404(user, project_id):
    try:
        return accessible_projects(user).get(pk=int(project_id))
    except (TypeError, ValueError, accessible_projects(user).model.DoesNotExist):
        raise Http404


def can_view_interventions(user, project):
    perm = ActionItemPermission("view", "project")
    ctx = {"project": project}
    return bool(
        check_company_project_permission(perm, user, "full_access", ctx)
        or check_company_project_permission(perm, user, "view_all_access", ctx)
        or perm._check_permission(user, "action_item", "full_access", "project", ctx)
        or perm._check_permission(user, "action_item", "view", "project", ctx)
    )


def can_import_interventions(user, project):
    return bool(ActionItemPermission("create", "project")._check_create_permission(user, {"project": project}))


class InterventionActionSerializer(serializers.ModelSerializer):
    # MOD-02: hierarchy and points. The mother has no parent and no points.
    # MOD-02b: `is_bpu` is the EFFECTIVE BPU (mother's own value; sub-actions inherit it, see
    # action_progress.effective_bpu), `bpu_source` says where it comes from, `sub_reference` is the
    # "<mother id>-<rank>" number of a sub-action (None for the mother and for older sub-actions).
    is_bpu = serializers.SerializerMethodField()
    bpu_source = serializers.SerializerMethodField()
    sub_reference = serializers.CharField(read_only=True, allow_null=True)
    in_progress_points = serializers.SerializerMethodField()
    closed_points = serializers.SerializerMethodField()

    @staticmethod
    def _setting(obj):
        return getattr(obj, "progress_setting", None)

    def get_is_bpu(self, obj):
        return effective_bpu(obj)[0]

    def get_bpu_source(self, obj):
        return effective_bpu(obj)[1]

    def get_in_progress_points(self, obj):
        setting = self._setting(obj)
        return setting.in_progress_points if setting else None

    def get_closed_points(self, obj):
        setting = self._setting(obj)
        return setting.closed_points if setting else None

    class Meta:
        model = ActionItem
        fields = (
            "id", "name", "status", "start_date", "target_date", "intervention_action_key",
            "parent_action_item", "sub_reference", "is_bpu", "bpu_source",
            "in_progress_points", "closed_points",
        )


class InterventionRequestSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.project_name", read_only=True)
    actions_count = serializers.SerializerMethodField()
    actions_closed_count = serializers.SerializerMethodField()
    progress_status = serializers.SerializerMethodField()
    # PROSEVIEW progress bar: same calculation as MOD-01 (points of the sub-actions / mother alone
    # 0-10-100), exposed as a percentage; `progress_to_close` = 100 % reached while something is
    # still open (shown as "to close", never as closed).
    progress_percent = serializers.SerializerMethodField()
    progress_to_close = serializers.SerializerMethodField()
    # MOD-03: effective closure date of the linked MOTHER action (ActionItem.closed_at, set when it is
    # really closed). None while it is not closed and for older requests without a mother: never
    # deduced from the expected end, from updated_at or from the last sub-action.
    closure_date = serializers.SerializerMethodField()

    # MOD-03: REAL status of the linked mother action (as stored, custom statuses included), so that the
    # bar can take the colour of the status badge. None for legacy requests without a mother. Display
    # only: nothing is derived from it, the progress calculation is unchanged.
    mother_status = serializers.SerializerMethodField()

    def get_mother_status(self, obj):
        mother = next(
            (a for a in obj.action_items.all() if a.intervention_action_key == MOTHER_ACTION_KEY), None
        )
        return mother.status if mother is not None else None

    def get_closure_date(self, obj):
        mother = next(
            (a for a in obj.action_items.all() if a.intervention_action_key == MOTHER_ACTION_KEY), None
        )
        if mother is None or (mother.status or "").strip().lower() != "closed":
            return None
        return mother.closed_at

    @staticmethod
    def _tracked(obj):
        items = list(obj.action_items.all())
        mother = next((a for a in items if a.intervention_action_key == MOTHER_ACTION_KEY), None)
        subs = list(mother.sub_action_items.all()) if mother is not None else []
        return mother, subs, items

    def _percent(self, obj):
        cache = getattr(obj, "_progress_percent_cache", None)
        if cache is not None:
            return cache
        mother, subs, items = self._tracked(obj)
        status = self.get_progress_status(obj)
        if status == "completed":
            percent = 100
        elif mother is not None:
            result = compute_progress(mother, subs=subs)
            percent = result["progress"]
            if percent is None:  # sub-actions without points: share of closed ones
                closed = sum(1 for s in subs if (s.status or "").strip().lower() == "closed")
                percent = round(100 * closed / len(subs)) if subs else 0
        else:  # legacy request (flat actions): share of closed actions
            closed = sum(1 for a in items if (a.status or "").strip().lower() == "closed")
            percent = round(100 * closed / len(items)) if items else 0
        obj._progress_percent_cache = int(percent)
        return obj._progress_percent_cache

    def get_progress_percent(self, obj):
        return self._percent(obj)

    def get_progress_to_close(self, obj):
        return self._percent(obj) >= 100 and self.get_progress_status(obj) != "completed"

    @staticmethod
    def _statuses(obj):
        # .all() uses the prefetch done by the views: no query per row. MOD-02: the mother action
        # is NOT counted as a sub-action: progress is derived from its sub-actions (the existing ones
        # and those added by hand). Legacy requests (flat actions, no mother) keep counting their actions.
        items = list(obj.action_items.all())
        mother = next((a for a in items if a.intervention_action_key == MOTHER_ACTION_KEY), None)
        if mother is not None:
            items = list(mother.sub_action_items.all()) or [mother]  # no sub-action: the mother itself
        return [(a.status or "").strip().lower() for a in items]

    def get_actions_count(self, obj):
        return len(self._statuses(obj))

    def get_actions_closed_count(self, obj):
        return sum(1 for s in self._statuses(obj) if s == "closed")

    def get_progress_status(self, obj):
        # Derived from the generated actions, never stored: not_started (all still open),
        # completed (all closed), in_progress otherwise.
        statuses = self._statuses(obj)
        if not statuses or all(s == "open" for s in statuses):
            return "not_started"
        if all(s == "closed" for s in statuses):
            return "completed"
        return "in_progress"

    class Meta:
        model = InterventionRequest
        fields = (
            "id", "project", "project_name", "company_profile", "reference", "title", "received_date",
            "expected_end_date", "budget", "currency", "description", "source", "external_reference",
            "source_modified_at", "created_at", "actions_count", "actions_closed_count", "progress_status",
            "progress_percent", "progress_to_close", "closure_date", "mother_status",
        )
        read_only_fields = fields


class InterventionRequestDetailSerializer(InterventionRequestSerializer):
    # The mother and its sub-actions, including those added by hand afterwards (they are children of
    # the mother but not linked to the request): the detail must show everything that counts in the
    # progress, e.g. an open 0-point sub-action holding a 100 % bar "to close".
    actions = serializers.SerializerMethodField()

    def get_actions(self, obj):
        items = {a.pk: a for a in obj.action_items.all()}
        mother = next((a for a in items.values() if a.intervention_action_key == MOTHER_ACTION_KEY), None)
        if mother is not None:
            for sub in mother.sub_action_items.all():
                items.setdefault(sub.pk, sub)
        return InterventionActionSerializer(sorted(items.values(), key=lambda a: a.pk), many=True).data

    class Meta(InterventionRequestSerializer.Meta):
        fields = InterventionRequestSerializer.Meta.fields + ("actions",)
        read_only_fields = fields


class InterventionImportRowSerializer(serializers.ModelSerializer):
    existing_reference = serializers.CharField(source="existing_request.reference", read_only=True, default=None)

    class Meta:
        model = InterventionImportRow
        fields = ("line_number", "external_reference", "data", "outcome", "errors", "existing_reference", "plan")


def _batch_payload(batch, include_rows=True):
    payload = {
        "id": str(batch.id),
        "project": batch.project_id,
        "status": batch.status,
        "source": batch.source,
        "file_name": batch.file_name,
        "summary": batch.summary,
        "result": batch.result,
        "can_manage_templates": None,  # filled by the views that know the user
        "limits": {"max_sub_actions": MAX_SUB_ACTIONS, "max_title": MAX_SUB_TITLE},
        "created_at": batch.created_at,
        "confirmed_at": batch.confirmed_at,
    }
    if include_rows:
        payload["rows"] = InterventionImportRowSerializer(batch.rows.select_related("existing_request"), many=True).data
    return payload


class InterventionRequestListView(APIView):
    permission_classes = [IsAuthenticated]
    ORDERING = {"reference", "title", "received_date", "expected_end_date", "created_at", "id"}

    def get(self, request):
        project = _project_or_404(request.user, request.query_params.get("project"))
        if not can_view_interventions(request.user, project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        queryset = InterventionRequest.objects.filter(project=project).select_related("project").prefetch_related("action_items__sub_action_items__progress_setting")
        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(reference__icontains=search) | Q(title__icontains=search) | Q(external_reference__icontains=search)
            )
        ordering = request.query_params.get("ordering") or "-id"
        if ordering.lstrip("-") not in self.ORDERING:
            ordering = "-id"
        queryset = queryset.order_by(ordering, "-id")
        paginator = CustomPageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = InterventionRequestSerializer(page, many=True).data
        response = paginator.get_paginated_response(data)
        response.data["can_import"] = can_import_interventions(request.user, project)
        return response


class InterventionRequestDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        queryset = InterventionRequest.objects.filter(project__in=accessible_projects(request.user))
        try:
            intervention = queryset.select_related("project").prefetch_related(
                "action_items__sub_action_items__progress_setting", "action_items__progress_setting",
                "action_items__sub_action_items__parent_action_item"
            ).prefetch_related("action_items__parent_action_item").get(pk=pk)
        except InterventionRequest.DoesNotExist:
            raise Http404
        if not can_view_interventions(request.user, intervention.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        return Response(InterventionRequestDetailSerializer(intervention).data)


class InterventionImportPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        project = _project_or_404(request.user, request.data.get("project"))
        if not can_import_interventions(request.user, project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"code": "missing_file", "detail": "A CSV file is required."}, status=400)
        raw = upload.read(2 * 1024 * 1024 + 1)
        try:
            batch = preview_import(
                project=project, user=request.user, file_name=upload.name, raw=raw,
                source=request.data.get("source") or "csv",
            )
        except ImportFileError as exc:
            return Response({"code": exc.code, "detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        payload = _batch_payload(batch)
        payload["can_manage_templates"] = can_manage_templates(request.user)
        return Response(payload, status=status.HTTP_201_CREATED)


def _batch_for_user(user, batch_id):
    try:
        return InterventionImportBatch.objects.select_related("project").get(
            pk=batch_id, project__in=accessible_projects(user)
        )
    except (InterventionImportBatch.DoesNotExist, ValueError, TypeError):
        raise Http404


class InterventionImportBatchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, batch_id):
        batch = _batch_for_user(request.user, batch_id)
        if not can_import_interventions(request.user, batch.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        payload = _batch_payload(batch)
        payload["can_manage_templates"] = can_manage_templates(request.user)
        return Response(payload)

    def delete(self, request, batch_id):
        """Cancel a preview: removes the previewed batch and its rows. Nothing else exists to remove
        (a preview creates no request, action or template)."""
        batch = _batch_for_user(request.user, batch_id)
        if not can_import_interventions(request.user, batch.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        if batch.status != InterventionImportBatch.Status.PREVIEWED:
            return Response({"code": "batch_not_confirmable", "detail": "Only a preview can be cancelled."}, status=409)
        batch.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class InterventionImportTemplatesView(APIView):
    """Company templates the import user may pick by hand in the preview. The company comes from the
    batch's authorized project; readable with the import right (no configuration right needed)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, batch_id):
        batch = _batch_for_user(request.user, batch_id)
        if not can_import_interventions(request.user, batch.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        from project_management.models import ActionTemplate

        templates = ActionTemplate.objects.filter(
            company_id=batch.project.company_profile_id, needs_review=False
        ).prefetch_related("sub_actions")
        return Response([
            {"id": t.pk, "title": t.title, "sub_actions": plan_lines_from_template(t)} for t in templates
        ])


class InterventionImportRowView(APIView):
    """Edit the sub-action plan of ONE new row of a previewed batch (request-specific: the company
    template is never modified). Same import right as the preview; full server-side validation."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, batch_id, line_number):
        batch = _batch_for_user(request.user, batch_id)
        if not can_import_interventions(request.user, batch.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        with transaction.atomic():
            batch = InterventionImportBatch.objects.select_for_update().get(pk=batch.pk)
            if batch.status != InterventionImportBatch.Status.PREVIEWED:
                return Response({"code": "batch_not_confirmable", "detail": "The batch can no longer be edited."}, status=409)
            try:
                row = batch.rows.select_for_update().get(line_number=line_number)
            except InterventionImportRow.DoesNotExist:
                raise Http404
            if row.outcome != InterventionImportRow.Outcome.NEW:
                return Response({"code": "row_not_editable", "detail": "Only new requests have a plan."}, status=409)
            stored_bpu = bool((row.plan or {}).get("is_bpu", False))
            try:
                if "is_bpu" in request.data and "plan" not in request.data and request.data.get("apply_template") in (None, ""):
                    # BPU of the request, chosen at the selection step (before the sub-actions): it is kept
                    # in the row's plan whatever the rest of the plan is (even still empty).
                    flag = request.data.get("is_bpu")
                    if not isinstance(flag, bool):
                        raise PlanError("bpu_invalid", "BPU must be true or false.")
                    row.plan = {**(row.plan or {}), "is_bpu": flag}
                elif request.data.get("apply_template") not in (None, ""):
                    # MOD-02b: template chosen by hand (any title); the request/mother title is kept
                    chosen = request.data.get("apply_template")
                    row.plan = template_plan(batch.project, chosen if isinstance(chosen, int) and not isinstance(chosen, bool) else None)
                    row.plan["is_bpu"] = stored_bpu  # the BPU choice survives a template change
                else:
                    raw_plan = request.data.get("plan")
                    if isinstance(raw_plan, dict) and "is_bpu" not in raw_plan:
                        raw_plan = {**raw_plan, "is_bpu": stored_bpu}  # BPU is no longer entered at this step
                    row.plan = clean_plan(batch.project, raw_plan, user=request.user)
            except PlanError as exc:
                code = status.HTTP_403_FORBIDDEN if exc.code == "template_right_required" else status.HTTP_400_BAD_REQUEST
                return Response({"code": exc.code, "detail": exc.detail}, status=code)
            row.save(update_fields=["plan"])
            refresh_summary(batch)
        return Response({"row": InterventionImportRowSerializer(row).data, "summary": batch.summary})


class InterventionSubActionsView(APIView):
    """Adapt the sub-actions (add, rename, re-weight, remove) of an already imported intervention, in ONE
    atomic operation (services/intervention_subactions.py). Same rights as editing the mother action;
    removing a sub-action also needs the delete right. The company template is never touched."""

    permission_classes = [IsAuthenticated]

    def _mother(self, request, pk):
        projects = accessible_projects(request.user)
        mother = (
            ActionItem.objects.filter(pk=pk)
            .filter(Q(milestone__project__in=projects) | Q(parent_action_item__milestone__project__in=projects))
            .first()
        )
        if mother is None:
            raise Http404
        return mother

    def put(self, request, pk):
        from project_management.services.intervention_subactions import SubActionError, replace_sub_actions

        mother = self._mother(request, pk)
        if not ActionItemPermission(action="update", scope="project").can_update_object(request.user, mother):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        delete_perm = ActionItemPermission(action="delete", scope="project")
        can_delete = bool(
            delete_perm._check_delete_permission_project(
                request.user, mother, delete_perm._get_context(request=None, view=None, scope="project", obj=mother)
            )
        )
        try:
            result = replace_sub_actions(
                mother, request.data.get("sub_actions"), user=request.user, can_delete=can_delete
            )
        except SubActionError as exc:
            code = status.HTTP_409_CONFLICT if exc.code in ("mother_closed", "closed_locked", "weight_insufficient") else status.HTTP_400_BAD_REQUEST
            if exc.code == "delete_forbidden":
                code = status.HTTP_403_FORBIDDEN
            return Response({"code": exc.code, "detail": exc.detail}, status=code)
        return Response(result, status=status.HTTP_200_OK)


class InterventionImportConfirmView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, batch_id):
        batch = _batch_for_user(request.user, batch_id)
        if not can_import_interventions(request.user, batch.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        try:
            batch, applied_now = confirm_import(
                batch_id=batch.id, user=request.user, assignee_id=request.data.get("assignee")
            )
        except PlanIncomplete as exc:
            return Response(
                {"code": "plan_incomplete", "detail": str(exc), "lines": exc.lines}, status=status.HTTP_400_BAD_REQUEST
            )
        except PlanRejected as exc:
            code = status.HTTP_403_FORBIDDEN if exc.code == "template_right_required" else status.HTTP_400_BAD_REQUEST
            return Response({"code": exc.code, "detail": exc.detail, "line": exc.line}, status=code)
        except AssignmentNotAtImport as exc:
            return Response({"code": "assignee_not_at_import", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except InvalidAssignee as exc:
            return Response({"code": "invalid_assignee", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except BatchNotConfirmable as exc:
            return Response({"code": "batch_not_confirmable", "detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        payload = _batch_payload(batch)
        payload["can_manage_templates"] = can_manage_templates(request.user)
        payload["applied_now"] = applied_now
        return Response(payload, status=status.HTTP_200_OK)
