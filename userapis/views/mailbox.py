"""MAIL-1.1C1: mailbox management API.

Security posture (section 4), enforced at every action below:
- company_profile is ALWAYS request.user.company -- never read from the URL or payload.
- Every object lookup is scoped to request.user.company (get_queryset) AND re-checked at the
  object level via MailboxAdminPermission.has_object_permission (belt-and-suspenders, same
  precedent as every other *Permission class in this codebase) -- an id belonging to another
  company is a 404/403, never a leak of its existence.
- status is never settable from a create/update payload (MailboxConnectionUpdateSerializer
  only exposes display_name); it only ever changes through deactivate() (-> revoked) or a real
  OAuth callback (not built in this lot -- never simulated).
- connected_by only ever changes through replace_delegate(), never a plain PATCH field.
- No destroy/DELETE route on MailboxConnection itself (section 3: "Ne pas créer d'endpoint de
  suppression physique si une désactivation suffit") -- MailboxProjectAssignment/
  MailboxUserGrant rows ARE actually removable (unassign/revoke), since they are pure
  access-control relations, not business evidence.
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from userapis import mailbox_oauth
from userapis.models import MailboxConnection, MailboxProjectAssignment, MailboxUserGrant
from userapis.permissions import MailboxAdminPermission
from userapis.recette_guard import RecetteMailBlocked, check_mailbox_address
from userapis.serializers.mailbox import (
    AccessibleMailboxSerializer,
    MailboxConnectionCreateSerializer,
    MailboxConnectionSerializer,
    MailboxConnectionUpdateSerializer,
    MailboxProjectAssignmentSerializer,
    MailboxUserGrantSerializer,
    ReplaceDelegateSerializer,
)


class MailboxConnectionViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated, MailboxAdminPermission]
    queryset = MailboxConnection.objects.all()

    def get_permissions(self):
        # MAIL-1.1C2: connect_oauth is the one action NOT admin-only -- an individual
        # mailbox's own (typically non-admin) owner must be able to connect it themselves.
        # get_queryset() still scopes the lookup to this user's own company (so a foreign
        # company's mailbox id is a 404, independent of this), and
        # mailbox_oauth.can_start_mail_oauth() re-derives the real admin-or-owner check
        # inside the action body -- MailboxAdminPermission is simply not the right gate for
        # this one specific action, never bypassed, just not applicable to it.
        if self.action == "connect_oauth":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            return MailboxConnectionCreateSerializer
        if self.action in ("update", "partial_update"):
            return MailboxConnectionUpdateSerializer
        return MailboxConnectionSerializer

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated or not getattr(user, "company_id", None):
            return MailboxConnection.objects.none()
        # Section 3 "Lister les boîtes de son entreprise" -- always this user's own company,
        # never a company_id taken from a query param.
        return MailboxConnection.objects.filter(company_profile_id=user.company_id)

    @staticmethod
    def _enforce_recette_address(request):
        """LINKS-RECETTE-03: in recette only allow-listed mailbox addresses can be registered
        or changed; a no-op in every other environment."""
        if "email_address" in request.data:
            try:
                check_mailbox_address(request.data.get("email_address"))
            except RecetteMailBlocked as error:
                raise ValidationError({"email_address": str(error)}) from error

    def create(self, request, *args, **kwargs):
        self._enforce_recette_address(request)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mailbox = serializer.save()
        return Response(
            MailboxConnectionSerializer(mailbox).data, status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        self._enforce_recette_address(request)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=kwargs.get("partial", False))
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(MailboxConnectionSerializer(instance).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        mailbox = self.get_object()
        mailbox.status = MailboxConnection.Status.REVOKED
        mailbox.save(update_fields=["status", "updated_at"])
        return Response(MailboxConnectionSerializer(mailbox).data)

    @action(detail=True, methods=["post"], url_path="replace-delegate")
    def replace_delegate(self, request, pk=None):
        mailbox = self.get_object()
        serializer = ReplaceDelegateSerializer(
            data=request.data, context={"mailbox": mailbox}
        )
        serializer.is_valid(raise_exception=True)
        mailbox.connected_by = serializer.validated_data["connected_by"]
        update_fields = ["connected_by", "updated_at"]
        # Security fix: a connection marked "active" asserts a real, validated OAuth
        # delegation is currently usable -- that assertion is specifically about the OLD
        # connected_by, so it must never silently carry over to whoever is newly assigned
        # here, who has not (yet) completed OAuth themselves. Only "active" is downgraded:
        # pending/revoked/expired/needs_reconsent already correctly say "not usable without
        # further action" and are left exactly as they were. Never touches
        # MailboxProjectAssignment/MailboxUserGrant rows, and the OLD delegate's own
        # UserOutlookCredentials (admin-server-models) is never read or written here at all.
        if mailbox.status == MailboxConnection.Status.ACTIVE:
            mailbox.status = MailboxConnection.Status.NEEDS_RECONSENT
            update_fields.append("status")
        mailbox.save(update_fields=update_fields)
        return Response(MailboxConnectionSerializer(mailbox).data)

    @action(detail=True, methods=["post"], url_path="connect-oauth")
    def connect_oauth(self, request, pk=None):
        """MAIL-1.1C2: starts Microsoft OAuth for this mailbox. Individual: owner only;
        shared: a mail.admin_shared_mailbox admin OR the mailbox's own current delegate
        (self-reconnect after needs_reconsent/expired) -- re-checked independently at the
        callback (userapis/mailbox_oauth.py), this is only the up-front gate."""
        mailbox = self.get_object()
        if not mailbox_oauth.can_start_mail_oauth(request.user, mailbox):
            raise PermissionDenied(
                "You are not authorized to connect this mailbox."
            )
        return Response(
            {"oauth_url": mailbox_oauth.build_mail_oauth_url(request.user, mailbox)}
        )

    @action(detail=True, methods=["get", "post"])
    def projects(self, request, pk=None):
        mailbox = self.get_object()
        if request.method == "GET":
            assignments = mailbox.project_assignments.select_related("project").all()
            return Response(MailboxProjectAssignmentSerializer(assignments, many=True).data)
        serializer = MailboxProjectAssignmentSerializer(
            data=request.data, context={"mailbox": mailbox, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        assignment = serializer.save()
        return Response(
            MailboxProjectAssignmentSerializer(assignment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["delete"], url_path=r"projects/(?P<project_id>\d+)")
    def unassign_project(self, request, pk=None, project_id=None):
        mailbox = self.get_object()
        deleted, _ = MailboxProjectAssignment.objects.filter(
            mailbox=mailbox, project_id=project_id
        ).delete()
        if not deleted:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post"])
    def grants(self, request, pk=None):
        mailbox = self.get_object()
        if request.method == "GET":
            grants = mailbox.user_grants.select_related("user").all()
            return Response(MailboxUserGrantSerializer(grants, many=True).data)
        serializer = MailboxUserGrantSerializer(
            data=request.data, context={"mailbox": mailbox, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        grant = serializer.save()
        return Response(MailboxUserGrantSerializer(grant).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"grants/(?P<user_id>\d+)")
    def grant_detail(self, request, pk=None, user_id=None):
        mailbox = self.get_object()
        grant = get_object_or_404(MailboxUserGrant, mailbox=mailbox, user_id=user_id)
        if request.method == "DELETE":
            grant.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        # PATCH: only can_send/can_read are ever accepted -- never `user` or `mailbox`.
        serializer = MailboxUserGrantSerializer(
            grant,
            data={k: v for k, v in request.data.items() if k in ("can_send", "can_read")},
            partial=True,
            context={"mailbox": mailbox, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(MailboxUserGrantSerializer(grant).data)


class AccessibleMailboxesView(APIView):
    """Section 6: the mailboxes the REQUESTING user may actually use -- individual mailboxes
    they own, plus shared mailboxes of their own company they hold an explicit
    MailboxUserGrant on. Never another company's mailboxes (scoped to request.user.company_id
    unconditionally); never a stand-in for real Microsoft/Google authorization."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not getattr(user, "company_id", None):
            return Response([])
        mailboxes = (
            MailboxConnection.objects.filter(company_profile_id=user.company_id)
            .filter(
                Q(
                    connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
                    owner_user=user,
                )
                | Q(
                    connection_type=MailboxConnection.ConnectionType.SHARED,
                    user_grants__user=user,
                )
            )
            .distinct()
            .prefetch_related("project_assignments__project")
        )
        serializer = AccessibleMailboxSerializer(
            mailboxes, many=True, context={"request": request}
        )
        return Response(serializer.data)
