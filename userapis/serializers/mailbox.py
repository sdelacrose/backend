"""MAIL-1.1C1: serializers for the mailbox management API.

Security posture (section 4): company_profile, status and connected_by are never accepted from
a create/update payload -- they are either read-only or set exclusively by the view,
server-side, from request.user/explicit dedicated actions. The one deliberate exception is
owner_user on the admin create endpoint (MAIL-1.2C, individual mailbox): it is restricted to an
active user of the admin's OWN company and refused for a shared mailbox. No field here ever exposes or
accepts an OAuth token (none of these models store one, see MAIL-1.1B). Cross-company
validation (project/user must belong to the mailbox's own company) is never re-implemented
here -- it is enforced once, in the model's own clean()/save() (MAIL-1.1B), and this module
only ever translates the resulting django.core.exceptions.ValidationError into a DRF one.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from models.models import User
from project_management.models import Project
from userapis.mailbox_access import can_use_mailbox, is_mailbox_assigned_to_project
from userapis.models import MailboxConnection, MailboxProjectAssignment, MailboxUserGrant


def _reraise_as_drf_validation_error(error: DjangoValidationError):
    detail = error.message_dict if hasattr(error, "message_dict") else error.messages
    raise serializers.ValidationError(detail)


class MailboxConnectionSerializer(serializers.ModelSerializer):
    """Read representation -- also used to echo back a create/update result."""

    company_profile_name = serializers.CharField(
        source="company_profile.company_name", read_only=True
    )
    owner_user_email = serializers.CharField(source="owner_user.email", read_only=True)
    connected_by_email = serializers.CharField(source="connected_by.email", read_only=True)

    class Meta:
        model = MailboxConnection
        fields = [
            "id",
            "provider",
            "connection_type",
            "email_address",
            "normalized_email_address",
            "display_name",
            "status",
            "company_profile",
            "company_profile_name",
            "owner_user",
            "owner_user_email",
            "connected_by",
            "connected_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "normalized_email_address",
            "status",
            "company_profile",
            "owner_user",
            "connected_by",
            "created_at",
            "updated_at",
        ]


class MailboxConnectionCreateSerializer(serializers.ModelSerializer):
    """Section 3: create a mailbox configuration. Shared by default (the historical payload,
    unchanged). MAIL-1.2C: connection_type="individual" with an owner_user creates a personal
    mailbox for that user of the admin's own company; the owner connects it through the existing
    OAuth action (owner only) -- nothing is connected, granted or made active here."""

    connection_type = serializers.ChoiceField(
        choices=MailboxConnection.ConnectionType.choices,
        required=False,
        default=MailboxConnection.ConnectionType.SHARED,
    )
    owner_user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.none(), required=False, allow_null=True
    )

    class Meta:
        model = MailboxConnection
        fields = ["provider", "email_address", "display_name", "connection_type", "owner_user"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        company_id = getattr(getattr(request, "user", None), "company_id", None)
        # Candidates are only active users of the admin's OWN company: another company's user
        # (or an unknown id) is the same "does not exist" answer, never a membership oracle.
        fields["owner_user"].queryset = (
            User.objects.filter(company_id=company_id, is_active=True, is_deleted=False)
            if company_id
            else User.objects.none()
        )
        return fields

    def validate(self, attrs):
        request = self.context["request"]
        connection_type = attrs.get("connection_type") or MailboxConnection.ConnectionType.SHARED
        owner = attrs.get("owner_user")
        if connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
            if owner is None:
                raise serializers.ValidationError(
                    {"owner_user": "An owner is required for a personal mailbox."}
                )
        # A shared mailbox never has an owner: any owner_user sent with it is ignored, exactly
        # as before this lot (shared behaviour unchanged).
        attrs["connection_type"] = connection_type
        attrs["owner_user"] = owner if connection_type == MailboxConnection.ConnectionType.INDIVIDUAL else None
        # One logical connection per company/provider/address, whatever its type (model
        # constraint) -- reported as a field error instead of a database error.
        if MailboxConnection.objects.filter(
            company_profile_id=request.user.company_id,
            provider=attrs.get("provider"),
            normalized_email_address=(attrs.get("email_address") or "").strip().lower(),
        ).exists():
            raise serializers.ValidationError(
                {"email_address": "A mailbox with this address already exists."}
            )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        # Section 4/5: company_profile is always the admin's own (never a payload field,
        # never trusted from the client); status is always "pending" (section 5 -- never
        # artificially "active" without real OAuth); connected_by stays null until the OAuth
        # callback (individual: the owner) or the dedicated replace-delegate action sets it.
        validated_data["company_profile"] = request.user.company
        validated_data["status"] = MailboxConnection.Status.PENDING
        try:
            return MailboxConnection.objects.create(**validated_data)
        except DjangoValidationError as error:
            _reraise_as_drf_validation_error(error)


class MailboxConnectionUpdateSerializer(serializers.ModelSerializer):
    """display_name only -- every other field (including status/connected_by) has its own
    dedicated action (deactivate, replace_delegate) rather than a free-form PATCH, so there is
    exactly one, explicit, auditable code path for each sensitive change (section 4)."""

    class Meta:
        model = MailboxConnection
        fields = ["display_name"]


class ReplaceDelegateSerializer(serializers.Serializer):
    connected_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    def validate_connected_by(self, value):
        mailbox = self.context["mailbox"]
        # Section 4: the new delegate must belong to the same company as the mailbox --
        # re-derived here (never trusted from the payload beyond "which user id"), same
        # membership check MailboxUserGrant.clean() already uses.
        if getattr(value, "company_id", None) != mailbox.company_profile_id:
            raise serializers.ValidationError(
                "The delegated user must belong to the mailbox's own company."
            )
        return value


class MailboxProjectAssignmentSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.project_name", read_only=True)

    class Meta:
        model = MailboxProjectAssignment
        fields = ["id", "project", "project_name", "assigned_by", "assigned_at"]
        read_only_fields = ["id", "assigned_by", "assigned_at"]

    def create(self, validated_data):
        validated_data["mailbox"] = self.context["mailbox"]
        validated_data["assigned_by"] = self.context["request"].user
        try:
            return MailboxProjectAssignment.objects.create(**validated_data)
        except DjangoValidationError as error:
            _reraise_as_drf_validation_error(error)


class MailboxUserGrantSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = MailboxUserGrant
        fields = [
            "id", "user", "user_email", "can_send", "can_read", "granted_by", "granted_at",
        ]
        read_only_fields = ["id", "granted_by", "granted_at"]

    def create(self, validated_data):
        validated_data["mailbox"] = self.context["mailbox"]
        validated_data["granted_by"] = self.context["request"].user
        try:
            return MailboxUserGrant.objects.create(**validated_data)
        except DjangoValidationError as error:
            _reraise_as_drf_validation_error(error)


class AccessibleMailboxSerializer(serializers.ModelSerializer):
    """Section 6: what the REQUESTING user may see/do with this mailbox -- never a proof of
    real Microsoft Exchange/Google rights, only of the LINKS-side authorization (same caveat
    userapis/mailbox_access.py's own can_use_mailbox docstring already states)."""

    can_send = serializers.SerializerMethodField()
    can_read = serializers.SerializerMethodField()
    accessible_projects = serializers.SerializerMethodField()
    sendable_projects = serializers.SerializerMethodField()

    class Meta:
        model = MailboxConnection
        fields = [
            "id",
            "provider",
            "connection_type",
            "email_address",
            "display_name",
            "status",
            "can_send",
            "can_read",
            "accessible_projects",
            "sendable_projects",
        ]

    def get_can_send(self, obj):
        return can_use_mailbox(self.context["request"].user, obj, for_sending=True)

    def get_can_read(self, obj):
        return can_use_mailbox(self.context["request"].user, obj, for_sending=False)

    def get_accessible_projects(self, obj):
        user = self.context["request"].user
        projects = []
        for assignment in obj.project_assignments.select_related("project").all():
            if is_mailbox_assigned_to_project(obj, assignment.project) and can_use_mailbox(
                user, obj, for_sending=False, project=assignment.project
            ):
                projects.append(
                    {"id": assignment.project_id, "name": assignment.project.project_name}
                )
        return projects

    def get_sendable_projects(self, obj):
        """MAIL-1.2B-CONTRACT: the projects this mailbox may be used to SEND for, by the
        requesting user -- the send-side twin of accessible_projects (which stays READ-based and
        untouched). A project qualifies exactly when can_use_mailbox(user, obj, for_sending=True,
        project=project) would be True: the mailbox is active, the user holds the send right
        (owner of an individual mailbox / grant.can_send on a shared one -- evaluated once here,
        it does not depend on the project), and the project is assigned to the mailbox. The
        assignments come from the view's prefetch, so no per-project query is issued. A project
        of another company is never returned (assignments are same-company by model validation;
        re-checked here anyway). Never a proof of real Exchange rights."""
        if not can_use_mailbox(self.context["request"].user, obj, for_sending=True):
            return []
        return [
            {"id": assignment.project_id, "name": assignment.project.project_name}
            for assignment in obj.project_assignments.all()
            if assignment.project.company_profile_id == obj.company_profile_id
        ]
