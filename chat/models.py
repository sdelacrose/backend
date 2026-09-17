from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from project_management.models import Project
from models.models import User, CompanyProfile
from ecpmpapp.constants import ChatStatusType

class ChatRoom(models.Model):
    """A chat room tied to a company or project"""
    name = models.CharField(max_length=255)
    company = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name='company_chats', null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="project_chats", null=True, blank=True)
    freelancer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='freelancer_chats', null=True, blank=True)

    is_direct = models.BooleanField(default=False)
    is_group = models.BooleanField(default=False)
    is_closed = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_chat_rooms",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_chat_rooms",
    )
    participants = models.ManyToManyField(User,blank=True,related_name='direct_chatrooms')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

class Message(models.Model):
    """Message model for saving chat messages"""
    TYPE_TEXT = "text"
    TYPE_VOICE = "voice"
    MESSAGE_TYPE_CHOICES = [
        (TYPE_TEXT, "Text"),
        (TYPE_VOICE, "Voice"),
    ]

    status_choices = [
        ('sent', 'Sent'),
        ('read', 'Read'),
        ('delivered', 'Delivered'),
    ]
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    message_type = models.CharField(
        max_length=16,
        choices=MESSAGE_TYPE_CHOICES,
        default=TYPE_TEXT,
    )
    voice_note_url = models.CharField(max_length=2048, null=True, blank=True)
    voice_note_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    attachments = models.JSONField(default=list, blank=True)
    linked_objects = models.JSONField(default=list, blank=True)
    mentions = models.JSONField(default=list, blank=True)
    status = models.CharField(choices=status_choices, default=ChatStatusType.DELIVERED.value)
    replied_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
    )
    is_deleted = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deleted_chat_messages",
    )
    read_by = models.ManyToManyField(User, through="MessageRead", related_name="read_messages", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.sender} - {self.content[:50]}"
    
class MessageRead(models.Model):
    """Through model to store read timestamps for each user"""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    message = models.ForeignKey(Message, on_delete=models.CASCADE)
    read_at = models.DateTimeField(auto_now=True)
    class Meta:
        
        unique_together = ("user", "message")
        indexes = [
            models.Index(fields=["user", "message"]),
        ]


class MatrixUserMap(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="matrix_user_map",
    )
    matrix_user_id = models.CharField(max_length=255, unique=True)
    is_provisioned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user_id} -> {self.matrix_user_id}"


class MatrixRoomMap(models.Model):
    ROOM_TYPE_CHOICES = [
        ("direct", "Direct"),
        ("group", "Group"),
        ("company", "Company"),
        ("project", "Project"),
        ("freelancer", "Freelancer"),
    ]

    chat_room = models.OneToOneField(
        ChatRoom,
        on_delete=models.CASCADE,
        related_name="matrix_room_map",
    )
    matrix_room_id = models.CharField(max_length=255, unique=True)
    room_type = models.CharField(max_length=32, choices=ROOM_TYPE_CHOICES)
    state = models.CharField(max_length=32, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.chat_room_id} -> {self.matrix_room_id}"


class MatrixMessageMap(models.Model):
    message = models.OneToOneField(
        Message,
        on_delete=models.CASCADE,
        related_name="matrix_message_map",
    )
    matrix_event_id = models.CharField(max_length=255, unique=True)
    matrix_room_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=64, default="m.room.message")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.message_id} -> {self.matrix_event_id}"


class MessageReaction(models.Model):
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="reactions",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="chat_reactions",
    )
    key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("message", "user", "key")
        indexes = [
            models.Index(fields=["message", "key"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.message_id}:{self.key}:{self.user_id}"


class MatrixReactionMap(models.Model):
    reaction = models.OneToOneField(
        MessageReaction,
        on_delete=models.CASCADE,
        related_name="matrix_reaction_map",
    )
    matrix_event_id = models.CharField(max_length=255, unique=True)
    matrix_room_id = models.CharField(max_length=255)
    relates_to_event_id = models.CharField(max_length=255)
    key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.reaction_id} -> {self.matrix_event_id}"


class ChatPushDevice(models.Model):
    PLATFORM_WEB = "web"
    PLATFORM_CHOICES = [
        (PLATFORM_WEB, "Web"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="chat_push_devices",
    )
    token = models.CharField(max_length=512)
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES, default=PLATFORM_WEB)
    user_agent = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "token"], name="chat_push_device_user_token_unique"),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["token"]),
            models.Index(fields=["updated_at"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.platform}:{self.token[:24]}"


class ChatPresence(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="chat_presence",
    )
    is_online = models.BooleanField(default=False)
    last_active_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["is_online"]),
            models.Index(fields=["last_active_at"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.last_active_at.isoformat()}"


class ChatPresenceSession(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="chat_presence_sessions",
    )
    session_key = models.CharField(max_length=128)
    last_heartbeat_at = models.DateTimeField(default=timezone.now)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "session_key"],
                name="chat_presence_session_user_key_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "closed_at"]),
            models.Index(fields=["session_key"]),
            models.Index(fields=["last_heartbeat_at"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.session_key}:{self.closed_at is None}"
