from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from model_utils import FieldTracker
from simple_history.models import HistoricalRecords

from models.models import User, CompanyProfile
from project_management.models import Document, Folder, ObjectAssessment, Project

class UserDashboardConfig(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="dashboard_configs")
    project_dashboard = models.JSONField(default=list)
    company_dashboard = models.JSONField(default=list)
    personal_dashboard = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s Dashboard"


class CalendarEvent(models.Model):
    company_profile = models.ForeignKey(CompanyProfile, related_name="calendar_events", on_delete=models.CASCADE, null=True, blank=True)
    freelancer = models.ForeignKey(User, related_name="freelancer_calendar_events", on_delete=models.CASCADE, null=True, blank=True)
    project = models.ForeignKey(Project, related_name="calendar_events", on_delete=models.CASCADE, null=True, blank=True)
    
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    is_private = models.BooleanField(default=False)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='events', null=True, blank=True)
    outlook_event_id = models.TextField(blank=True, null=True)
    assignees = models.ManyToManyField(User, related_name='assigned_events', blank=True)
    documents = models.ManyToManyField(Document, related_name='attachments', blank=True)
    linked_folders = models.ManyToManyField(
        Folder,
        related_name='calendar_event_linked_folders',
        blank=True,
    )
    object_assessments = models.ManyToManyField(
        ObjectAssessment,
        related_name='calendar_events',
        blank=True,
    )
    meeting_link = models.URLField(max_length=2048, blank=True, null=True)
    room = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, related_name='created_calendar_events', on_delete=models.SET_NULL, null=True, blank=True)
    tracker = FieldTracker()

    def __str__(self):
        return self.name


# MAIL-1.1B: Mailbox connections (Microsoft 365 / Gmail), individual or Company-administered
# shared, plus the project assignment and per-user access grant that govern a SHARED mailbox's
# usage. No OAuth token lives on any of these three models -- a connection only ever references
# the existing per-user token storage (UserOutlookCredentials, admin-server-models, untouched by
# this lot) via `connected_by`; MAIL-1.1C's job is OAuth itself, never this one's.
#
# MAIL-1.1A's own architecture doc: owner_user is set only for an INDIVIDUAL connection
# (connected_by is typically the same user, but the two fields are kept independent --
# connected_by is "who currently holds the OAuth delegation", which MUST be replaceable, per
# the ticket's own explicit requirement, without ever touching owner_user or any of a shared
# mailbox's MailboxProjectAssignment/MailboxUserGrant rows, which all key off `mailbox`, never
# off `connected_by`).
class MailboxConnection(models.Model):
    class Provider(models.TextChoices):
        MICROSOFT = "microsoft", "Microsoft"
        GOOGLE = "google", "Google"

    class ConnectionType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        SHARED = "shared", "Shared"

    class Status(models.TextChoices):
        # MAIL-1.1C1 section 5: a configuration an administrator creates through the
        # management API (this lot) has no OAuth delegation yet -- none of the other four
        # values mean that ("active" specifically must never be used for a mailbox nobody has
        # actually authenticated, per the ticket's own explicit rule). This is the new
        # MailboxConnectionViewSet.create's only possible initial status; nothing in this lot
        # ever transitions a connection TO "active" -- that is real OAuth's job (MAIL-1.1C2+,
        # never simulated here).
        PENDING = "pending", "Pending connection"
        ACTIVE = "active", "Active"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"
        NEEDS_RECONSENT = "needs_reconsent", "Needs reconsent"

    provider = models.CharField(max_length=20, choices=Provider.choices)
    connection_type = models.CharField(max_length=20, choices=ConnectionType.choices)
    email_address = models.EmailField()
    # The actual uniqueness anchor (section 3: "L'adresse email doit être normalisée pour
    # éviter les doublons") -- lowercased/trimmed, always derived in save(), never
    # hand-maintained by a caller.
    normalized_email_address = models.CharField(max_length=254, editable=False, blank=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    # "Une boîte appartient à une seule entreprise" -- required, never null.
    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="mailbox_connections",
    )
    # Individual connection only -- null for a shared mailbox (CheckConstraint below).
    owner_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_mailbox_connections",
    )
    # The user currently holding the OAuth delegation this connection relies on -- for an
    # individual mailbox this is normally the same person as owner_user, but the two fields
    # are deliberately independent so connected_by alone can be replaced (section 3's own
    # explicit requirement) without touching owner_user, MailboxProjectAssignment, or
    # MailboxUserGrant rows, none of which ever reference connected_by.
    connected_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delegated_mailbox_connections",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # Section 3: "Une connexion logique unique par entreprise, fournisseur et adresse
            # normalisée" -- MAIL-1.1A's "une seule connexion technique par boîte partagée et
            # entreprise propriétaire" applied uniformly (the same physical mailbox should
            # never have two connection rows in one company regardless of individual/shared).
            models.UniqueConstraint(
                fields=["company_profile", "provider", "normalized_email_address"],
                name="mailbox_connection_unique_per_company_provider_address",
            ),
            models.CheckConstraint(
                check=(
                    Q(connection_type="individual", owner_user__isnull=False)
                    | Q(connection_type="shared", owner_user__isnull=True)
                ),
                name="mailbox_connection_owner_matches_connection_type",
            ),
        ]

    def save(self, *args, **kwargs):
        self.normalized_email_address = (self.email_address or "").strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.email_address} ({self.connection_type})"


class MailboxProjectAssignment(models.Model):
    """A SHARED MailboxConnection made available to one Project of its own owning company.
    Never implies universal project-member access on its own -- see MailboxUserGrant, and
    userapis/mailbox_access.py's can_use_mailbox for the actual cumulative check."""

    mailbox = models.ForeignKey(
        MailboxConnection, on_delete=models.CASCADE, related_name="project_assignments",
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="mailbox_assignments",
    )
    assigned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mailbox_assignments_made",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-assigned_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["mailbox", "project"], name="mailbox_project_assignment_unique",
            ),
        ]

    def clean(self):
        # "Le projet doit appartenir à l'entreprise propriétaire de la boîte" / "Aucune
        # affectation inter-entreprises" -- a cross-table rule a single-table CheckConstraint
        # cannot express; enforced in Python, same precedent as Correspondence's own save()
        # (project_management/models/correspondence.py) checking rules a DB constraint alone
        # cannot cover. Enforced in save() too (not just clean()) so trusted/internal service
        # code gets the same protection a serializer-driven call would.
        if self.mailbox_id and self.project_id:
            if self.project.company_profile_id != self.mailbox.company_profile_id:
                raise ValidationError(
                    "A mailbox can only be assigned to a project of its own owning company."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.mailbox_id} -> project {self.project_id}"


class MailboxUserGrant(models.Model):
    """Explicit, per-user, per-SHARED-mailbox send/read authorization. Never implied by project
    membership alone (MAIL-1.1A's own "une boîte partagée ne devient pas automatiquement
    accessible à tous les membres du projet") -- see userapis/mailbox_access.py."""

    mailbox = models.ForeignKey(
        MailboxConnection, on_delete=models.CASCADE, related_name="user_grants",
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="mailbox_grants",
    )
    can_send = models.BooleanField(default=False)
    can_read = models.BooleanField(default=False)
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mailbox_grants_made",
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-granted_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["mailbox", "user"], name="mailbox_user_grant_unique",
            ),
        ]

    def clean(self):
        # Section 3: "Aucun utilisateur extérieur à l'entreprise ne peut recevoir de droits
        # sans mécanisme d'appartenance explicitement prévu par l'architecture existante" --
        # the existing mechanism is User.company (the same field _get_user_role already
        # resolves a company-scope role from, ecpmpapp/permissions.py), reused here rather than
        # inventing a second notion of company membership.
        if self.mailbox_id and self.user_id:
            if getattr(self.user, "company_id", None) != self.mailbox.company_profile_id:
                raise ValidationError(
                    "A mailbox grant can only be given to a user of the mailbox's own company."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user_id} on mailbox {self.mailbox_id} (send={self.can_send}, read={self.can_read})"
