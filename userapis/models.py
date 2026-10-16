import uuid
from datetime import timedelta

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


# MAIL-1.1C2-S: fixes a real gap found in this security checkpoint -- the Mail OAuth "state"
# previously embedded raw user_id/mailbox_id directly (predictable, no expiry, reusable
# indefinitely), so it was a mere unverified CLAIM, never actual proof that the person
# completing the callback is who started the flow (ticket's own explicit warning: "le préfixe
# mail: ne doit servir qu'au routage, jamais constituer une preuve d'autorisation" -- the same
# was true of the raw ids it wrapped). Mirrors project_management.models.sharepoint.OAuthState
# (same UUID-token-as-unguessable-nonce precedent, already proven in this codebase for the
# exact same class of problem) rather than inventing a new pattern, with mailbox/expiry/
# single-use added since that model has neither and is owned by an unrelated feature
# (SharePoint) this lot must not couple itself to.
class MailOAuthState(models.Model):
    EXPIRY_MINUTES = 15

    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="mail_oauth_states",
    )
    mailbox = models.ForeignKey(
        MailboxConnection, on_delete=models.CASCADE, related_name="oauth_states",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Single-use (replay protection): set the moment the callback starts processing this
    # token, before anything else happens -- a second attempt with the same token, whether a
    # genuine double-submit or a replay, is rejected.
    consumed_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=self.EXPIRY_MINUTES)

    def __str__(self):
        return f"mail oauth state for user {self.user_id} / mailbox {self.mailbox_id}"


# MAIL-1.3B: minimal METADATA of messages read from a MailboxConnection through Microsoft Graph
# -- a reading foundation only (nothing syncs, imports or exposes these yet). Deliberately NOT a
# second mailbox: no body, no HTML, no attachment, no token is ever stored here, only what is
# needed later to recognise a reply (Graph id, internetMessageId, conversationId, sender,
# recipients, subject, date, attachment flag) and to link a message to a LINKS Correspondence.
#
# Rights: a row belongs to exactly ONE mailbox and its access follows THAT mailbox's rights
# (userapis.mailbox_messages.messages_visible_to). The same message sitting in two mailboxes is
# two independent rows -- never merged, never sharing visibility on the strength of the same
# internetMessageId alone.
class MailboxMessage(models.Model):
    class ProcessingState(models.TextChoices):
        # Seen and stored, nothing decided yet. The other values are for the later review lots
        # (import / ignore / link) and are never set by this lot.
        NEW = "new", "New"
        IMPORTED = "imported", "Imported"
        IGNORED = "ignored", "Ignored"

    mailbox = models.ForeignKey(
        MailboxConnection, on_delete=models.CASCADE, related_name="messages",
    )
    # Denormalized on purpose (company isolation is then a plain column filter); save() refuses
    # a value that differs from the mailbox's own company.
    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="mailbox_messages",
    )
    folder = models.CharField(max_length=32, blank=True, default="")
    graph_message_id = models.CharField(max_length=512)
    # Absent on some messages (drafts, some system mail): "" means unknown, never a placeholder.
    internet_message_id = models.CharField(max_length=998, blank=True, default="")
    conversation_id = models.CharField(max_length=512, blank=True, default="")
    sender_address = models.CharField(max_length=254, blank=True, default="")
    sender_name = models.CharField(max_length=255, blank=True, default="")
    to_addresses = models.JSONField(default=list, blank=True)
    cc_addresses = models.JSONField(default=list, blank=True)
    subject = models.CharField(max_length=500, blank=True, default="")
    received_at = models.DateTimeField(null=True, blank=True)
    has_attachments = models.BooleanField(default=False)
    state = models.CharField(
        max_length=20, choices=ProcessingState.choices, default=ProcessingState.NEW,
    )
    correspondence = models.ForeignKey(
        "project_management.Correspondence", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="mailbox_messages",
    )
    # MAIL-1.3D-C2-A: the LINKS correspondence this message was IMPORTED into (or attached to) by a
    # person -- kept apart from `correspondence` above, which is the OUT a detected reply answers
    # and is never overwritten. One correspondence has at most one such source message.
    imported_correspondence = models.ForeignKey(
        "project_management.Correspondence", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="imported_from_messages",
    )
    imported_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="imported_mailbox_messages",
    )
    imported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["imported_correspondence"],
                condition=Q(imported_correspondence__isnull=False),
                name="mailbox_message_unique_imported_correspondence",
            ),
            # One row per Graph item in a mailbox...
            models.UniqueConstraint(
                fields=["mailbox", "graph_message_id"], name="mailbox_message_unique_graph_id",
            ),
            # ...and one row per Internet message in a mailbox (the same message can appear under
            # several Graph ids, e.g. Inbox and Sent Items), whenever that id is known. Always
            # scoped to ONE mailbox: another mailbox holding the same message is a separate row.
            models.UniqueConstraint(
                fields=["mailbox", "internet_message_id"],
                condition=~Q(internet_message_id=""),
                name="mailbox_message_unique_internet_id",
            ),
        ]
        indexes = [
            models.Index(fields=["mailbox", "conversation_id"], name="mailbox_msg_conv_idx"),
            models.Index(fields=["company_profile", "received_at"], name="mailbox_msg_company_idx"),
        ]

    def save(self, *args, **kwargs):
        if self.mailbox_id and self.company_profile_id != self.mailbox.company_profile_id:
            raise ValidationError("A message must belong to its mailbox's own company.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"message {self.pk} of mailbox {self.mailbox_id}"


# MAIL-1.3B: purely technical, one row per mailbox. Holds where an incremental read stopped and
# why the last attempt failed -- never a message, a token or any personal data. Reading a mailbox
# never changes MailboxConnection.status (a transient failure must not deactivate a mailbox);
# only this row records the outcome.
class MailboxSyncState(models.Model):
    mailbox = models.OneToOneField(
        MailboxConnection, on_delete=models.CASCADE, related_name="sync_state",
    )
    # Graph delta link/token when an incremental read is used (opaque, never parsed here).
    cursor = models.TextField(blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    # A short error CODE (e.g. "rate_limited"), never a Graph message, address or subject.
    last_error_code = models.CharField(max_length=40, blank=True, default="")
    last_error_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"sync state of mailbox {self.mailbox_id}"


# MAIL-1.3D-C2-B3: where each file kept from an imported Outlook message comes from. The file itself
# is an ordinary CorrespondenceAttachment (evidence of the correspondence, never a Document); this
# row only records its provenance and protects against duplicates: one original .eml per message,
# one row per Graph attachment of a message, one row per distinct content (SHA-256) in a
# correspondence. Deleting the CorrespondenceAttachment removes its row, so it can be added again.
class MailboxImportedFile(models.Model):
    class Kind(models.TextChoices):
        ORIGINAL_EML = "original_eml", "Original message (.eml)"
        ATTACHMENT = "attachment", "Attachment"

    mailbox_message = models.ForeignKey(
        MailboxMessage, on_delete=models.CASCADE, related_name="imported_files",
    )
    correspondence = models.ForeignKey(
        "project_management.Correspondence", on_delete=models.CASCADE,
        related_name="mailbox_imported_files",
    )
    correspondence_attachment = models.OneToOneField(
        "project_management.CorrespondenceAttachment", on_delete=models.CASCADE,
        related_name="mailbox_source",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Graph's own attachment id ("" for the original .eml); technical, never sent to a client.
    graph_attachment_id = models.CharField(max_length=512, blank=True, default="")
    file_name = models.CharField(max_length=500)
    file_size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    content_type = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="mailbox_imported_files",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["mailbox_message"], condition=Q(kind="original_eml"),
                name="mailbox_imported_file_one_eml_per_message",
            ),
            models.UniqueConstraint(
                fields=["mailbox_message", "graph_attachment_id"], condition=Q(kind="attachment"),
                name="mailbox_imported_file_one_row_per_graph_attachment",
            ),
            models.UniqueConstraint(
                fields=["correspondence", "sha256"],
                name="mailbox_imported_file_unique_content_per_correspondence",
            ),
        ]
