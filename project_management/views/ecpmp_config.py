import logging

from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ecpmpapp.constants import StatusType
from models.configuration_sync import (
    PRESERVE_LOCAL,
    ConfigurationSyncError,
    configuration_values_from_instance,
    preserve_local_configuration,
    replace_configuration,
    validate_sync_strategy,
)
from models.configuration_templates import (
    PROJECT_CONFIGURATION_FIELDS,
    get_project_template_defaults,
)
from project_management.models import ActionItem, ECPMPConfig
from project_management.models.constants import COLLABORATIVE_SOLUTION
from project_management.permissions import ECPMPConfigPermission
from project_management.serializers import ECPMPCONFIGSerializer
from project_management.services.project_configuration_templates import (
    ensure_project_config_template,
)
from project_management.utils import get_product_name_from_request

logger = logging.getLogger(__name__)


class ECPMPConfigViewSet(viewsets.ModelViewSet):
    queryset = ECPMPConfig.objects.all()
    serializer_class = ECPMPCONFIGSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['project']

    def get_serializer(self, *args, **kwargs):
        serializer = super(ECPMPConfigViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        return serializer

    def get_permissions(self):
        if self.action == 'create':
            return [ECPMPConfigPermission(action='create', scope='project')]
        elif self.action in ['list', 'retrieve']:
            return [ECPMPConfigPermission(action='view', scope='project')]
        elif self.action == 'update':
            return [ECPMPConfigPermission(action='update', scope='project')]
        elif self.action == 'partial_update':
            return [ECPMPConfigPermission(action='update', scope='project')]
        elif self.action == 'destroy':
            return [ECPMPConfigPermission(action='delete', scope='project')]
        elif self.action == 'sync_with_admin':
            return [ECPMPConfigPermission(action='sync_admin', scope='project')]
        elif self.action == 'reset_config':
            return [ECPMPConfigPermission(action='reset', scope='project')]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ECPMPConfig.objects.none()

        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]

        return ECPMPConfig.objects.filter(
            project__team_members__team_member=user,
            project__solution__in=products,
        ).distinct().order_by('-id')

    def get_object(self):
        obj = get_object_or_404(ECPMPConfig, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj

    def perform_update(self, serializer):
        self._save_configuration_update(serializer)

    def _save_configuration_update(self, serializer):
        instance = serializer.instance
        previous_status_options = list(instance.status_options or [])
        previous_labels = list(instance.labels or [])
        updated_instance = serializer.save()

        if 'status_options' in serializer.validated_data:
            self._normalize_project_action_items(
                updated_instance,
                previous_status_options,
            )
        if 'labels' in serializer.validated_data:
            self._normalize_project_action_item_labels(
                updated_instance,
                previous_labels,
            )
        return updated_instance

    def _apply_configuration_values(self, config, values):
        serializer = self.get_serializer(
            instance=config,
            data=values,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        self._save_configuration_update(serializer)
        return serializer

    @action(detail=True, methods=["post"], url_path="sync")
    @transaction.atomic
    def sync_with_admin(self, request, pk=None):
        config = self.get_object()
        config = ECPMPConfig.objects.select_for_update().get(pk=config.pk)
        try:
            strategy = validate_sync_strategy(
                request.data.get("strategy", PRESERVE_LOCAL)
            )
        except ConfigurationSyncError as exc:
            return Response(
                {"strategy": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        template = ensure_project_config_template(config.project)
        local_values = configuration_values_from_instance(
            config,
            PROJECT_CONFIGURATION_FIELDS,
        )
        template_values = configuration_values_from_instance(
            template,
            PROJECT_CONFIGURATION_FIELDS,
        )
        if strategy == PRESERVE_LOCAL:
            synced_values = preserve_local_configuration(
                local_values,
                template_values,
            )
        else:
            synced_values = replace_configuration(template_values)

        serializer = self._apply_configuration_values(config, synced_values)
        return Response(
            {
                "message": "Project configuration synchronized with Admin.",
                "strategy": strategy,
                "configuration": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reset")
    @transaction.atomic
    def reset_config(self, request, pk=None):
        config = self.get_object()
        config = ECPMPConfig.objects.select_for_update().get(pk=config.pk)
        reset_values = get_project_template_defaults()
        serializer = self._apply_configuration_values(config, reset_values)
        return Response(
            {
                "message": "Project configuration reset to system defaults.",
                "configuration": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def _normalize_project_action_items(self, config, previous_status_options):
        removed_statuses = self._get_removed_statuses(previous_status_options, config.status_options)
        if not removed_statuses:
            return

        status_filters = Q()
        for status in removed_statuses:
            status_filters |= Q(status__iexact=status)

        updated_count = ActionItem.objects.filter(
            Q(milestone__project=config.project)
            | Q(parent_action_item__milestone__project=config.project),
        ).filter(status_filters).update(status=StatusType.OPEN.value)

        logger.info(
            "ECPMPConfig status normalization: project_id=%s removed=%s updated=%s",
            getattr(config.project, "id", None),
            removed_statuses,
            updated_count,
        )

    def _normalize_project_action_item_labels(self, config, previous_labels):
        removed_labels = self._get_removed_plain_values(previous_labels, config.labels)
        if not removed_labels:
            return

        removed_normalized = {label.strip().lower() for label in removed_labels}
        queryset = ActionItem.objects.filter(
            Q(milestone__project=config.project)
            | Q(parent_action_item__milestone__project=config.project),
        ).distinct()

        items_to_update = []
        for action_item in queryset.only("id", "labels"):
            current_labels = [
                str(label).strip()
                for label in (action_item.labels or [])
                if str(label).strip()
            ]
            cleaned_labels = [
                label
                for label in current_labels
                if label.lower() not in removed_normalized
            ]
            if cleaned_labels != current_labels:
                action_item.labels = cleaned_labels
                items_to_update.append(action_item)

        if items_to_update:
            ActionItem.objects.bulk_update(items_to_update, ["labels"])

        logger.info(
            "ECPMPConfig label normalization: project_id=%s removed=%s updated=%s",
            getattr(config.project, "id", None),
            removed_labels,
            len(items_to_update),
        )

    def _get_removed_statuses(self, previous_status_options, current_status_options):
        previous = {
            opt.split(":", 1)[0].strip().lower()
            for opt in (previous_status_options or [])
            if isinstance(opt, str) and opt.strip()
        }
        current = {
            opt.split(":", 1)[0].strip().lower()
            for opt in (current_status_options or [])
            if isinstance(opt, str) and opt.strip()
        }
        return sorted(previous - current)

    def _get_removed_plain_values(self, previous_values, current_values):
        previous = {
            str(item).strip().lower()
            for item in (previous_values or [])
            if str(item).strip()
        }
        current = {
            str(item).strip().lower()
            for item in (current_values or [])
            if str(item).strip()
        }
        return sorted(previous - current)
