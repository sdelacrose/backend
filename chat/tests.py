from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIClient, APIRequestFactory
from unittest.mock import patch

from companyprofileapis.models import Opportunity
from models.models import CompanyProfile, User
from project_management import signals as project_signals
from project_management.models import Project, Requirement
from userapis.models import CalendarEvent

from .models import ChatPresence, ChatPresenceSession, ChatRoom, Message, MessageRead
from .object_links import resolve_linked_objects, search_linked_objects
from .serializers import MessageSerializer
from .tasks import send_chat_message_push_notifications
from .views import ChatPresenceViewSet, ChatRoomViewSet


TEST_REST_FRAMEWORK = {
    **settings.REST_FRAMEWORK,
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}


@override_settings(REST_FRAMEWORK=TEST_REST_FRAMEWORK)
class ChatPresenceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.original_chatroom_permissions = ChatRoomViewSet.permission_classes
        self.original_presence_permissions = ChatPresenceViewSet.permission_classes
        ChatRoomViewSet.permission_classes = [IsAuthenticated]
        ChatPresenceViewSet.permission_classes = [IsAuthenticated]
        self.user = User.objects.create_user(
            email="owner@example.com",
            last_name="Owner",
            first_name="Room",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            last_name="Other",
            first_name="User",
        )
        self.third_user = User.objects.create_user(
            email="third@example.com",
            last_name="Third",
            first_name="User",
        )

    def tearDown(self):
        ChatRoomViewSet.permission_classes = self.original_chatroom_permissions
        ChatPresenceViewSet.permission_classes = self.original_presence_permissions

    def test_heartbeat_updates_presence_timestamp(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/chat/presence/heartbeat/",
            {"session_id": "tab-1"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        presence = ChatPresence.objects.get(user=self.user)
        session = ChatPresenceSession.objects.get(
            user=self.user,
            session_key="tab-1",
        )
        self.assertTrue(presence.is_online)
        self.assertIsNone(session.closed_at)
        self.assertGreaterEqual(
            presence.last_active_at,
            timezone.now() - timedelta(seconds=5),
        )

    def test_heartbeat_requires_authentication(self):
        response = self.client.post(
            "/api/chat/presence/heartbeat/",
            {"session_id": "tab-1"},
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    def test_patch_closes_presence_session_and_marks_user_offline(self):
        self.client.force_authenticate(user=self.user)

        self.client.post(
            "/api/chat/presence/heartbeat/",
            {"session_id": "tab-1"},
            format="json",
        )
        response = self.client.patch(
            "/api/chat/presence/heartbeat/",
            {"session_id": "tab-1"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)

        presence = ChatPresence.objects.get(user=self.user)
        session = ChatPresenceSession.objects.get(
            user=self.user,
            session_key="tab-1",
        )
        self.assertFalse(presence.is_online)
        self.assertIsNotNone(session.closed_at)

    def test_chatrooms_list_includes_presence_and_group_online_count(self):
        self.client.force_authenticate(user=self.user)
        now = timezone.now()

        direct_room = ChatRoom.objects.create(
            name="Direct room",
            is_direct=True,
            is_group=False,
            created_by=self.user,
        )
        direct_room.participants.set([self.user, self.other_user])

        group_room = ChatRoom.objects.create(
            name="Group room",
            is_direct=False,
            is_group=True,
            created_by=self.user,
        )
        group_room.participants.set([self.user, self.other_user, self.third_user])

        ChatPresence.objects.create(user=self.user, is_online=True, last_active_at=now)
        ChatPresence.objects.create(user=self.other_user, is_online=True, last_active_at=now)
        ChatPresence.objects.create(
            user=self.third_user,
            is_online=False,
            last_active_at=now,
        )
        ChatPresenceSession.objects.create(
            user=self.other_user,
            session_key="active-tab",
            last_heartbeat_at=now,
        )
        ChatPresenceSession.objects.create(
            user=self.third_user,
            session_key="stale-tab",
            last_heartbeat_at=now - timedelta(minutes=5),
        )

        response = self.client.get("/api/chat/chatrooms/")

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 2)

        direct_payload = next(item for item in results if item["id"] == direct_room.id)
        group_payload = next(item for item in results if item["id"] == group_room.id)

        other_participant = next(
            participant
            for participant in direct_payload["participants"]
            if participant["id"] == self.other_user.id
        )
        third_participant = next(
            participant
            for participant in group_payload["participants"]
            if participant["id"] == self.third_user.id
        )

        self.assertEqual(direct_payload["online_count"], 0)
        self.assertTrue(other_participant["presence"]["is_online"])
        self.assertEqual(group_payload["online_count"], 1)
        self.assertFalse(third_participant["presence"]["is_online"])

    def test_chatrooms_list_marks_online_user_with_active_calendar_event_as_busy(self):
        self.client.force_authenticate(user=self.user)
        now = timezone.now()

        direct_room = ChatRoom.objects.create(
            name="Direct room",
            is_direct=True,
            is_group=False,
            created_by=self.user,
        )
        direct_room.participants.set([self.user, self.other_user])

        ChatPresence.objects.create(user=self.other_user, is_online=True, last_active_at=now)
        ChatPresenceSession.objects.create(
            user=self.other_user,
            session_key="active-tab",
            last_heartbeat_at=now,
        )
        CalendarEvent.objects.create(
            name="Busy block",
            start_date=now - timedelta(minutes=30),
            end_date=now + timedelta(minutes=30),
            user=self.other_user,
        )

        response = self.client.get("/api/chat/chatrooms/")

        self.assertEqual(response.status_code, 200)
        direct_payload = next(
            item for item in response.json()["results"] if item["id"] == direct_room.id
        )
        other_participant = next(
            participant
            for participant in direct_payload["participants"]
            if participant["id"] == self.other_user.id
        )

        self.assertTrue(other_participant["presence"]["is_online"])
        self.assertTrue(other_participant["presence"]["has_active_calendar_event"])

    def test_stale_open_session_is_treated_as_offline(self):
        self.client.force_authenticate(user=self.user)
        now = timezone.now()

        direct_room = ChatRoom.objects.create(
            name="Direct room",
            is_direct=True,
            is_group=False,
            created_by=self.user,
        )
        direct_room.participants.set([self.user, self.other_user])

        ChatPresence.objects.create(
            user=self.other_user,
            is_online=True,
            last_active_at=now,
        )
        ChatPresenceSession.objects.create(
            user=self.other_user,
            session_key="stale-tab",
            last_heartbeat_at=now - timedelta(minutes=5),
        )

        response = self.client.get("/api/chat/chatrooms/")

        self.assertEqual(response.status_code, 200)
        direct_payload = next(
            item for item in response.json()["results"] if item["id"] == direct_room.id
        )
        other_participant = next(
            participant
            for participant in direct_payload["participants"]
            if participant["id"] == self.other_user.id
        )

        self.assertFalse(other_participant["presence"]["is_online"])

    def test_unread_count_returns_count_without_serializing_rooms(self):
        self.client.force_authenticate(user=self.user)

        direct_room = ChatRoom.objects.create(
            name="Direct room",
            is_direct=True,
            is_group=False,
            created_by=self.user,
        )
        direct_room.participants.set([self.user, self.other_user])

        inaccessible_room = ChatRoom.objects.create(
            name="Other room",
            is_direct=True,
            is_group=False,
            created_by=self.other_user,
        )
        inaccessible_room.participants.set([self.other_user, self.third_user])

        Message.objects.create(
            room=direct_room,
            sender=self.other_user,
            content="Unread message",
        )
        read_message = Message.objects.create(
            room=direct_room,
            sender=self.other_user,
            content="Read message",
        )
        Message.objects.create(
            room=direct_room,
            sender=self.user,
            content="Own message",
        )
        Message.objects.create(
            room=inaccessible_room,
            sender=self.other_user,
            content="Hidden message",
        )
        MessageRead.objects.create(user=self.user, message=read_message)

        response = self.client.get("/api/chat/chatrooms/unread_count/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"unread_count": 1})


@override_settings(REST_FRAMEWORK=TEST_REST_FRAMEWORK)
class ChatRoomHeaderPreviewTests(TestCase):
    def setUp(self):
        self.matrix_room_patcher = patch(
            "chat.signals.matrix_service.ensure_room",
            return_value=None,
        )
        self.matrix_room_patcher.start()
        self.client = APIClient()
        self.original_chatroom_permissions = ChatRoomViewSet.permission_classes
        ChatRoomViewSet.permission_classes = [IsAuthenticated]
        self.user = User.objects.create_user(
            email="preview-owner@example.com",
            first_name="Preview",
            last_name="Owner",
        )
        self.other_user = User.objects.create_user(
            email="preview-other@example.com",
            first_name="Other",
            last_name="Person",
            user_image="https://example.com/avatar.png",
        )
        self.third_user = User.objects.create_user(
            email="preview-third@example.com",
            first_name="Third",
            last_name="Person",
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        ChatRoomViewSet.permission_classes = self.original_chatroom_permissions
        self.matrix_room_patcher.stop()

    def create_direct_room(self, name):
        room = ChatRoom.objects.create(
            name=name,
            is_direct=True,
            created_by=self.user,
        )
        room.participants.set([self.user, self.other_user])
        return room

    def test_preview_returns_latest_five_unread_rooms_by_message_date(self):
        now = timezone.now()
        expected_room_ids = []

        for index in range(6):
            room = self.create_direct_room(f"Room {index}")
            message = Message.objects.create(
                room=room,
                sender=self.other_user,
                content=f"Message {index}",
            )
            message_time = now - timedelta(days=6 - index)
            Message.objects.filter(id=message.id).update(created_at=message_time)
            ChatRoom.objects.filter(id=room.id).update(
                updated_at=now + timedelta(days=6 - index),
            )
            expected_room_ids.append(room.id)

        inaccessible_room = ChatRoom.objects.create(
            name="Inaccessible",
            is_direct=True,
            created_by=self.other_user,
        )
        inaccessible_room.participants.set([self.other_user, self.third_user])
        Message.objects.create(
            room=inaccessible_room,
            sender=self.other_user,
            content="Hidden",
        )

        scoped_room = ChatRoom.objects.create(name="Company room")
        scoped_room.participants.set([self.user, self.other_user])
        Message.objects.create(
            room=scoped_room,
            sender=self.other_user,
            content="Scoped",
        )

        response = self.client.get("/api/chat/chatrooms/header-preview/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "unread")
        self.assertEqual(
            [room["id"] for room in response.json()["results"]],
            list(reversed(expected_room_ids[-5:])),
        )

    def test_preview_falls_back_to_latest_rooms_when_none_are_unread(self):
        now = timezone.now()
        expected_room_ids = []

        for index in range(6):
            room = self.create_direct_room(f"Read room {index}")
            message = Message.objects.create(
                room=room,
                sender=self.other_user,
                content=f"Read message {index}",
            )
            Message.objects.filter(id=message.id).update(
                created_at=now - timedelta(hours=6 - index),
            )
            MessageRead.objects.create(user=self.user, message=message)
            expected_room_ids.append(room.id)

        response = self.client.get("/api/chat/chatrooms/header-preview/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "recent")
        self.assertEqual(
            [room["id"] for room in response.json()["results"]],
            list(reversed(expected_room_ids[-5:])),
        )
        self.assertTrue(
            all(room["unread_count"] == 0 for room in response.json()["results"])
        )

    def test_preview_fills_remaining_slots_with_recent_read_rooms(self):
        now = timezone.now()
        unread_room = self.create_direct_room("Unread room")
        Message.objects.create(
            room=unread_room,
            sender=self.other_user,
            content="Unread message",
        )
        read_room_ids = []

        for index in range(5):
            room = self.create_direct_room(f"Read room {index}")
            message = Message.objects.create(
                room=room,
                sender=self.other_user,
                content=f"Read message {index}",
            )
            Message.objects.filter(id=message.id).update(
                created_at=now - timedelta(hours=5 - index),
            )
            MessageRead.objects.create(user=self.user, message=message)
            read_room_ids.append(room.id)

        response = self.client.get("/api/chat/chatrooms/header-preview/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "unread")
        self.assertEqual(len(response.json()["results"]), 5)
        self.assertEqual(response.json()["results"][0]["id"], unread_room.id)
        self.assertEqual(
            [room["id"] for room in response.json()["results"][1:]],
            list(reversed(read_room_ids[-4:])),
        )

    def test_preview_returns_compact_direct_and_group_metadata(self):
        direct_room = self.create_direct_room("Internal direct name")
        direct_message = Message.objects.create(
            room=direct_room,
            sender=self.other_user,
            content="Direct preview",
        )
        MessageRead.objects.create(user=self.user, message=direct_message)

        group_room = ChatRoom.objects.create(
            name="Delivery Team",
            is_group=True,
            created_by=self.user,
        )
        group_room.participants.set(
            [self.user, self.other_user, self.third_user],
        )
        Message.objects.create(
            room=group_room,
            sender=self.third_user,
            content="",
            message_type=Message.TYPE_VOICE,
            attachments=[{"url": "https://example.com/file.pdf"}],
            linked_objects=[{"type_label": "Document"}],
        )

        response = self.client.get("/api/chat/chatrooms/header-preview/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "unread")
        self.assertEqual(len(payload["results"]), 2)
        group_payload = payload["results"][0]
        self.assertEqual(group_payload["room_type"], "group")
        self.assertEqual(group_payload["display_name"], "Delivery Team")
        self.assertIsNone(group_payload["avatar_url"])
        self.assertEqual(group_payload["unread_count"], 1)
        self.assertEqual(group_payload["last_message"]["message_type"], "voice")
        self.assertEqual(group_payload["last_message"]["attachment_count"], 1)
        self.assertEqual(
            group_payload["last_message"]["linked_object_type_label"],
            "Document",
        )

        MessageRead.objects.create(
            user=self.user,
            message=group_room.messages.get(),
        )
        response = self.client.get("/api/chat/chatrooms/header-preview/")
        direct_payload = next(
            room for room in response.json()["results"] if room["id"] == direct_room.id
        )
        self.assertEqual(direct_payload["room_type"], "direct")
        self.assertEqual(direct_payload["display_name"], "Other Person")
        self.assertEqual(
            direct_payload["avatar_url"],
            "https://example.com/avatar.png",
        )

    def test_mark_room_read_is_idempotent_and_user_specific(self):
        group_room = ChatRoom.objects.create(
            name="Read group",
            is_group=True,
            created_by=self.user,
        )
        group_room.participants.set(
            [self.user, self.other_user, self.third_user],
        )
        first_message = Message.objects.create(
            room=group_room,
            sender=self.other_user,
            content="First unread",
        )
        second_message = Message.objects.create(
            room=group_room,
            sender=self.third_user,
            content="Second unread",
        )
        Message.objects.create(
            room=group_room,
            sender=self.user,
            content="Own message",
        )

        response = self.client.patch(
            f"/api/chat/chatrooms/{group_room.id}/mark-read/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["marked_count"], 2)
        self.assertEqual(response.json()["unread_count"], 0)
        self.assertEqual(response.json()["total_unread_count"], 0)
        self.assertEqual(
            MessageRead.objects.filter(
                user=self.user,
                message__in=[first_message, second_message],
            ).count(),
            2,
        )
        self.assertFalse(
            MessageRead.objects.filter(
                user=self.third_user,
                message=first_message,
            ).exists()
        )

        response = self.client.patch(
            f"/api/chat/chatrooms/{group_room.id}/mark-read/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["marked_count"], 0)

    def test_mark_all_read_only_updates_accessible_direct_and_group_rooms(self):
        direct_room = self.create_direct_room("Direct unread")
        direct_message = Message.objects.create(
            room=direct_room,
            sender=self.other_user,
            content="Direct unread",
        )
        group_room = ChatRoom.objects.create(
            name="Group unread",
            is_group=True,
            created_by=self.user,
        )
        group_room.participants.set(
            [self.user, self.other_user, self.third_user],
        )
        group_message = Message.objects.create(
            room=group_room,
            sender=self.third_user,
            content="Group unread",
        )
        inaccessible_room = ChatRoom.objects.create(
            name="Inaccessible",
            is_direct=True,
            created_by=self.other_user,
        )
        inaccessible_room.participants.set([self.other_user, self.third_user])
        inaccessible_message = Message.objects.create(
            room=inaccessible_room,
            sender=self.third_user,
            content="Hidden unread",
        )

        response = self.client.patch(
            "/api/chat/chatrooms/mark-all-read/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["marked_count"], 2)
        self.assertEqual(response.json()["total_unread_count"], 0)
        self.assertEqual(
            MessageRead.objects.filter(
                user=self.user,
                message__in=[direct_message, group_message],
            ).count(),
            2,
        )
        self.assertFalse(
            MessageRead.objects.filter(
                user=self.user,
                message=inaccessible_message,
            ).exists()
        )

    def test_mark_read_rejects_inaccessible_room(self):
        room = ChatRoom.objects.create(
            name="Other direct room",
            is_direct=True,
            created_by=self.other_user,
        )
        room.participants.set([self.other_user, self.third_user])

        response = self.client.patch(
            f"/api/chat/chatrooms/{room.id}/mark-read/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 404)


@override_settings(REST_FRAMEWORK=TEST_REST_FRAMEWORK)
class OpenDirectChatTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.original_chatroom_permissions = ChatRoomViewSet.permission_classes
        ChatRoomViewSet.permission_classes = [IsAuthenticated]

        self.company_owner = User.objects.create_user(
            email="owner@example.com",
            password="password",
            first_name="Owner",
            last_name="User",
            user_type="company_user",
        )
        self.company_member = User.objects.create_user(
            email="member@example.com",
            password="password",
            first_name="Member",
            last_name="User",
            user_type="company_user",
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com",
            password="password",
            first_name="Out",
            last_name="Sider",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.company_owner,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[self.company_owner.id, self.company_member.id]
        ).update(company=self.company)
        self.company_owner.refresh_from_db()
        self.company_member.refresh_from_db()

        self.existing_direct_room = ChatRoom.objects.create(
            name="Existing direct room",
            is_direct=True,
            is_group=False,
            created_by=self.company_owner,
        )
        self.existing_direct_room.participants.set(
            [self.company_owner, self.company_member]
        )

    def tearDown(self):
        ChatRoomViewSet.permission_classes = self.original_chatroom_permissions

    def test_open_direct_chat_returns_existing_room(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.post(
            "/api/chat/chatrooms/open-direct-chat/",
            {"participant_id": self.company_member.id},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload, {"id": self.existing_direct_room.id})

    @patch("chat.serializers.matrix_service.ensure_room")
    def test_open_direct_chat_creates_room_when_missing(self, _mock_ensure_room):
        self.client.force_authenticate(user=self.company_owner)
        company_other = User.objects.create_user(
            email="other@example.com",
            password="password",
            first_name="Other",
            last_name="User",
            user_type="company_user",
            company=self.company,
        )

        response = self.client.post(
            "/api/chat/chatrooms/open-direct-chat/",
            {"participant_id": company_other.id},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertIn("id", payload)

        room = ChatRoom.objects.get(id=payload["id"])
        self.assertTrue(room.is_direct)
        participant_ids = set(room.participants.values_list("id", flat=True))
        self.assertEqual(participant_ids, {self.company_owner.id, company_other.id})

    def test_open_direct_chat_rejects_self_chat(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.post(
            "/api/chat/chatrooms/open-direct-chat/",
            {"participant_id": self.company_owner.id},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_open_direct_chat_rejects_unconnected_users(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.post(
            "/api/chat/chatrooms/open-direct-chat/",
            {"participant_id": self.outsider.id},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_open_direct_chat_requires_participant_id(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.post(
            "/api/chat/chatrooms/open-direct-chat/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400)


class ChatMessageMentionSerializerTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.company_owner = User.objects.create_user(
            email="owner@example.com",
            password="password",
            first_name="Owner",
            last_name="User",
            user_type="company_user",
        )
        self.company_member = User.objects.create_user(
            email="member@example.com",
            password="password",
            first_name="Member",
            last_name="User",
            user_type="company_user",
        )
        self.company_other = User.objects.create_user(
            email="other@example.com",
            password="password",
            first_name="Other",
            last_name="User",
            user_type="company_user",
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com",
            password="password",
            first_name="Out",
            last_name="Sider",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.company_owner,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[
                self.company_owner.id,
                self.company_member.id,
                self.company_other.id,
            ]
        ).update(company=self.company)
        self.company_owner.refresh_from_db()
        self.company_member.refresh_from_db()
        self.company_other.refresh_from_db()

        self.company_room = ChatRoom.objects.create(
            name="Company chat",
            company=self.company,
            is_direct=False,
            is_group=False,
            created_by=self.company_owner,
        )
        self.group_room = ChatRoom.objects.create(
            name="Group room",
            is_direct=False,
            is_group=True,
            created_by=self.company_owner,
        )
        self.group_room.participants.set(
            [self.company_owner, self.company_member, self.company_other]
        )
        self.direct_room = ChatRoom.objects.create(
            name="Direct room",
            is_direct=True,
            is_group=False,
            created_by=self.company_owner,
        )
        self.direct_room.participants.set([self.company_owner, self.company_member])

    def _build_request(self, user, method="post"):
        request = getattr(self.factory, method)("/api/chat/messages/", {}, format="json")
        request.user = user
        return request

    @patch("chat.serializers.matrix_service.sync_message")
    def test_company_chat_message_mentions_are_normalized(self, _mock_sync):
        serializer = MessageSerializer(
            data={
                "room": self.company_room.id,
                "content": "Hello @Member User",
                "mentions": [{"user_id": self.company_member.id}],
            },
            context={"request": self._build_request(self.company_owner)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        message = serializer.save()

        self.assertEqual(
            message.mentions,
            [
                {
                    "user_id": self.company_member.id,
                    "name": self.company_member.full_name,
                    "user_image": self.company_member.user_image or None,
                }
            ],
        )

    @patch("chat.serializers.matrix_service.sync_message")
    def test_group_chat_message_mentions_are_normalized(self, _mock_sync):
        serializer = MessageSerializer(
            data={
                "room": self.group_room.id,
                "content": "Hello @Other User",
                "mentions": [{"user_id": self.company_other.id}],
            },
            context={"request": self._build_request(self.company_owner)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        message = serializer.save()

        self.assertEqual(
            message.mentions,
            [
                {
                    "user_id": self.company_other.id,
                    "name": self.company_other.full_name,
                    "user_image": self.company_other.user_image or None,
                }
            ],
        )

    def test_group_chat_rejects_mentions_for_non_participants(self):
        serializer = MessageSerializer(
            data={
                "room": self.group_room.id,
                "content": "Hello @Out Sider",
                "mentions": [{"user_id": self.outsider.id}],
            },
            context={"request": self._build_request(self.company_owner)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("mentions", serializer.errors)

    def test_direct_chat_rejects_mentions(self):
        serializer = MessageSerializer(
            data={
                "room": self.direct_room.id,
                "content": "Hello @Member User",
                "mentions": [{"user_id": self.company_member.id}],
            },
            context={"request": self._build_request(self.company_owner)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("mentions", serializer.errors)

    def test_partial_update_revalidates_mentions_against_existing_room(self):
        message = Message.objects.create(
            room=self.group_room,
            sender=self.company_owner,
            content="Hello",
            mentions=[],
        )

        serializer = MessageSerializer(
            message,
            data={
                "content": "Hello @Member User",
                "mentions": [{"user_id": self.company_member.id}],
            },
            partial=True,
            context={"request": self._build_request(self.company_owner, method="patch")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated_message = serializer.save()
        self.assertEqual(
            updated_message.mentions,
            [
                {
                    "user_id": self.company_member.id,
                    "name": self.company_member.full_name,
                    "user_image": self.company_member.user_image or None,
                }
            ],
        )


@override_settings(REST_FRAMEWORK=TEST_REST_FRAMEWORK)
class ChatMentionCandidatesApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.original_chatroom_permissions = ChatRoomViewSet.permission_classes
        ChatRoomViewSet.permission_classes = [IsAuthenticated]

        self.company_owner = User.objects.create_user(
            email="owner@example.com",
            password="password",
            first_name="Owner",
            last_name="User",
            user_type="company_user",
        )
        self.company_member = User.objects.create_user(
            email="member@example.com",
            password="password",
            first_name="Member",
            last_name="User",
            user_type="company_user",
        )
        self.company_other = User.objects.create_user(
            email="other@example.com",
            password="password",
            first_name="Other",
            last_name="Person",
            user_type="company_user",
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com",
            password="password",
            first_name="Out",
            last_name="Sider",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.company_owner,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[
                self.company_owner.id,
                self.company_member.id,
                self.company_other.id,
            ]
        ).update(company=self.company)
        self.company_owner.refresh_from_db()
        self.company_member.refresh_from_db()
        self.company_other.refresh_from_db()

        self.company_room = ChatRoom.objects.create(
            name="Company chat",
            company=self.company,
            is_direct=False,
            is_group=False,
            created_by=self.company_owner,
        )
        self.group_room = ChatRoom.objects.create(
            name="Group room",
            is_direct=False,
            is_group=True,
            created_by=self.company_owner,
        )
        self.group_room.participants.set(
            [self.company_owner, self.company_member, self.company_other]
        )
        self.direct_room = ChatRoom.objects.create(
            name="Direct room",
            is_direct=True,
            is_group=False,
            created_by=self.company_owner,
        )
        self.direct_room.participants.set([self.company_owner, self.company_member])

    def tearDown(self):
        ChatRoomViewSet.permission_classes = self.original_chatroom_permissions

    def test_company_chat_mention_candidates_return_company_users_only(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.company_room.id}/mention-candidates/",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            {item["user_id"] for item in payload},
            {self.company_owner.id, self.company_member.id, self.company_other.id},
        )

    def test_company_chat_mention_candidates_support_search(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.company_room.id}/mention-candidates/?search=Other",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["user_id"], self.company_other.id)

    def test_group_chat_mention_candidates_return_group_participants_only(self):
        self.client.force_authenticate(user=self.company_member)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.group_room.id}/mention-candidates/",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            {item["user_id"] for item in payload},
            {self.company_owner.id, self.company_member.id, self.company_other.id},
        )

    def test_direct_chat_mention_candidates_are_rejected(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.direct_room.id}/mention-candidates/",
        )

        self.assertEqual(response.status_code, 400)

    def test_inaccessible_room_returns_not_found(self):
        self.client.force_authenticate(user=self.outsider)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.company_room.id}/mention-candidates/",
        )

        self.assertEqual(response.status_code, 404)


@override_settings(REST_FRAMEWORK=TEST_REST_FRAMEWORK)
class ChatMessageSearchApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company_owner = User.objects.create_user(
            email="owner-search@example.com",
            password="password",
            first_name="Owner",
            last_name="Search",
            user_type="company_user",
        )
        self.company_member = User.objects.create_user(
            email="member-search@example.com",
            password="password",
            first_name="Member",
            last_name="Search",
            user_type="company_user",
        )
        self.company_other = User.objects.create_user(
            email="other-search@example.com",
            password="password",
            first_name="Other",
            last_name="Search",
            user_type="company_user",
        )
        self.outsider = User.objects.create_user(
            email="outsider-search@example.com",
            password="password",
            first_name="Outside",
            last_name="User",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.company_owner,
            company_name="Acme Search",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[
                self.company_owner.id,
                self.company_member.id,
                self.company_other.id,
            ]
        ).update(company=self.company)
        self.company_owner.refresh_from_db()
        self.company_member.refresh_from_db()
        self.company_other.refresh_from_db()

        self.company_room = ChatRoom.objects.create(
            name="Company Search Room",
            company=self.company,
            is_direct=False,
            is_group=False,
            created_by=self.company_owner,
        )
        self.direct_room = ChatRoom.objects.create(
            name="Direct Search Room",
            is_direct=True,
            is_group=False,
            created_by=self.company_owner,
        )
        self.direct_room.participants.set([self.company_owner, self.company_member])
        self.group_room = ChatRoom.objects.create(
            name="Group Search Room",
            is_direct=False,
            is_group=True,
            created_by=self.company_owner,
        )
        self.group_room.participants.set(
            [self.company_owner, self.company_member, self.company_other]
        )

        self.company_message = Message.objects.create(
            room=self.company_room,
            sender=self.company_owner,
            content="Budget keyword in company chat",
        )
        self.direct_message = Message.objects.create(
            room=self.direct_room,
            sender=self.company_member,
            content="Budget keyword in direct chat",
        )
        self.group_message = Message.objects.create(
            room=self.group_room,
            sender=self.company_other,
            content="Budget keyword in group chat",
        )
        self.deleted_group_message = Message.objects.create(
            room=self.group_room,
            sender=self.company_other,
            content="Budget keyword deleted",
            is_deleted=True,
        )

    def test_global_search_includes_company_direct_and_group_messages(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get("/api/chat/messages/search/?q=budget")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 3)
        self.assertEqual(
            {item["message_id"] for item in payload["results"]},
            {
                self.company_message.id,
                self.direct_message.id,
                self.group_message.id,
            },
        )
        self.assertEqual(
            {item["room_type"] for item in payload["results"]},
            {"company", "direct", "group"},
        )

    def test_global_search_excludes_deleted_messages(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get("/api/chat/messages/search/?q=deleted")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["results"], [])

    def test_global_search_respects_access_control(self):
        self.client.force_authenticate(user=self.outsider)

        response = self.client.get("/api/chat/messages/search/?q=budget")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["results"], [])

    def test_global_search_supports_pagination(self):
        self.client.force_authenticate(user=self.company_owner)

        for index in range(3):
            Message.objects.create(
                room=self.company_room,
                sender=self.company_owner,
                content=f"Budget keyword pagination {index}",
            )

        response = self.client.get("/api/chat/messages/search/?q=budget&page_size=2")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 6)
        self.assertEqual(len(payload["results"]), 2)
        self.assertIsNotNone(payload["next"])

    def test_room_scoped_search_returns_only_selected_room_messages(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.group_room.id}/search-messages/?q=budget"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["message_id"], self.group_message.id)

    def test_room_scoped_search_blocks_inaccessible_rooms(self):
        self.client.force_authenticate(user=self.outsider)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.group_room.id}/search-messages/?q=budget"
        )

        self.assertEqual(response.status_code, 404)


@override_settings(REST_FRAMEWORK=TEST_REST_FRAMEWORK)
class ChatMessageContextApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company_owner = User.objects.create_user(
            email="owner-context@example.com",
            password="password",
            first_name="Owner",
            last_name="Context",
            user_type="company_user",
        )
        self.company_member = User.objects.create_user(
            email="member-context@example.com",
            password="password",
            first_name="Member",
            last_name="Context",
            user_type="company_user",
        )
        self.outsider = User.objects.create_user(
            email="outsider-context@example.com",
            password="password",
            first_name="Outside",
            last_name="Context",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.company_owner,
            company_name="Acme Context",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[self.company_owner.id, self.company_member.id]
        ).update(company=self.company)
        self.company_owner.refresh_from_db()
        self.company_member.refresh_from_db()

        self.group_room = ChatRoom.objects.create(
            name="Group Context Room",
            is_direct=False,
            is_group=True,
            created_by=self.company_owner,
        )
        self.group_room.participants.set([self.company_owner, self.company_member])

        self.other_group_room = ChatRoom.objects.create(
            name="Other Group Context Room",
            is_direct=False,
            is_group=True,
            created_by=self.company_owner,
        )
        self.other_group_room.participants.set([self.company_owner, self.company_member])

        self.context_messages = [
            Message.objects.create(
                room=self.group_room,
                sender=self.company_owner if index % 2 == 0 else self.company_member,
                content=f"Context message {index}",
            )
            for index in range(6)
        ]
        self.anchor_message = self.context_messages[3]
        self.other_room_message = Message.objects.create(
            room=self.other_group_room,
            sender=self.company_owner,
            content="Other room message",
        )

    def test_message_context_returns_before_anchor_and_after(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.group_room.id}/message-context/?message_id={self.anchor_message.id}&before_limit=2&after_limit=2"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["room_id"], self.group_room.id)
        self.assertEqual(payload["anchor_message_id"], self.anchor_message.id)
        self.assertEqual(
            [item["id"] for item in payload["before"]],
            [self.context_messages[1].id, self.context_messages[2].id],
        )
        self.assertEqual(payload["anchor"]["id"], self.anchor_message.id)
        self.assertEqual(
            [item["id"] for item in payload["after"]],
            [self.context_messages[4].id, self.context_messages[5].id],
        )
        self.assertTrue(payload["has_older"])
        self.assertFalse(payload["has_newer"])

    def test_message_context_blocks_inaccessible_room(self):
        self.client.force_authenticate(user=self.outsider)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.group_room.id}/message-context/?message_id={self.anchor_message.id}"
        )

        self.assertEqual(response.status_code, 404)

    def test_message_context_rejects_room_mismatch(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.group_room.id}/message-context/?message_id={self.other_room_message.id}"
        )

        self.assertEqual(response.status_code, 400)

    def test_message_context_supports_custom_window_sizes(self):
        self.client.force_authenticate(user=self.company_owner)

        response = self.client.get(
            f"/api/chat/chatrooms/{self.group_room.id}/message-context/?message_id={self.anchor_message.id}&before_limit=1&after_limit=1"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["before"]), 1)
        self.assertEqual(len(payload["after"]), 1)
        self.assertEqual(payload["before"][0]["id"], self.context_messages[2].id)
        self.assertEqual(payload["after"][0]["id"], self.context_messages[4].id)


class ChatLinkedObjectsTests(TestCase):
    def setUp(self):
        project_signals.post_save.disconnect(project_signals.create_default_folders, sender=Project)
        self.project_notifications_patcher = patch(
            "project_management.signals.create_notification_with_recipients"
        )
        self.project_notifications_patcher.start()
        self.opportunity_notifications_patcher = patch(
            "companyprofileapis.signals.create_notification_with_recipients"
        )
        self.opportunity_notifications_patcher.start()
        self.notification_preference_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        self.notification_preference_patcher.start()
        self.matrix_room_patcher = patch(
            "chat.signals.matrix_service.ensure_room",
            return_value=None,
        )
        self.matrix_room_patcher.start()

        self.company_owner = User.objects.create_user(
            email="linked-owner@example.com",
            password="password",
            first_name="Linked",
            last_name="Owner",
            user_type="company_user",
        )
        self.company_member = User.objects.create_user(
            email="linked-member@example.com",
            password="password",
            first_name="Linked",
            last_name="Member",
            user_type="company_user",
        )
        self.other_company_user = User.objects.create_user(
            email="linked-other@example.com",
            password="password",
            first_name="Other",
            last_name="Company",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.company_owner,
            company_name="Acme Linked",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_company_user,
            company_name="Other Linked",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="999",
            company_mob_no="888",
            company_web_address="https://other.example.com",
            company_email_address="other@example.com",
            company_tax_reference="other-tax",
            company_iban="other-iban",
        )

        User.objects.filter(id__in=[self.company_owner.id, self.company_member.id]).update(
            company=self.company
        )
        self.company_owner.refresh_from_db()
        self.company_member.refresh_from_db()
        self.other_company_user.refresh_from_db()

        self.company_project = Project.objects.create(
            project_name="Alpha Project",
            contract_value=Decimal("1000.00"),
            company_profile=self.company,
            user=self.company_owner,
            created_by=self.company_owner,
        )
        self.other_project = Project.objects.create(
            project_name="Beta Project",
            contract_value=Decimal("2000.00"),
            company_profile=self.other_company,
            user=self.other_company_user,
            created_by=self.other_company_user,
        )
        self.company_room = ChatRoom.objects.create(
            name="Company linked room",
            company=self.company,
            is_direct=False,
            is_group=False,
            created_by=self.company_owner,
        )
        self.project_room = ChatRoom.objects.create(
            name="Project linked room",
            project=self.company_project,
            is_direct=False,
            is_group=False,
            created_by=self.company_owner,
        )
        self.company_project.team_members.create(
            role="Engineer",
            team_member=self.company_member,
            project_access="RW",
        )

        self.company_opportunity = Opportunity.objects.create(
            created_by=self.company_owner,
            company_profile=self.company,
            title="Solar retrofit",
            details="Retrofit opportunity for plant A",
            type_of_service="Consulting",
        )
        Opportunity.objects.create(
            created_by=self.other_company_user,
            company_profile=self.other_company,
            title="Other company opportunity",
            details="Should not leak into search",
            type_of_service="Audit",
        )

        self.root_requirement = Requirement.objects.create(
            project=self.company_project,
            name="Safety requirement",
            object_number="REQ-001",
            skill="safety",
            sort_order=1,
        )
        self.child_requirement = Requirement.objects.create(
            project=self.company_project,
            parent=self.root_requirement,
            name="Pump isolation",
            object_number="REQ-001-A",
            skill="pump",
            sort_order=1,
        )
        Requirement.objects.create(
            project=self.other_project,
            name="Foreign requirement",
            object_number="REQ-999",
            skill="other",
            sort_order=1,
        )

    def tearDown(self):
        self.matrix_room_patcher.stop()
        self.notification_preference_patcher.stop()
        self.opportunity_notifications_patcher.stop()
        self.project_notifications_patcher.stop()
        project_signals.post_save.connect(project_signals.create_default_folders, sender=Project)

    @patch("chat.object_links.require_any_permission_multiple_modules")
    def test_company_room_search_returns_company_opportunities_only(self, mock_permissions):
        mock_permissions.return_value = True

        results = search_linked_objects(
            self.company_owner,
            self.company_room,
            query="solar",
            object_types=["opportunity"],
            limit=10,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["object_type"], "opportunity")
        self.assertEqual(results[0]["object_id"], self.company_opportunity.id)
        self.assertEqual(results[0]["target_url"], f"/opportunities?id={self.company_opportunity.id}")

    @patch("chat.object_links.require_any_permission_multiple_modules")
    def test_project_room_search_excludes_opportunities(self, mock_permissions):
        mock_permissions.return_value = True

        results = search_linked_objects(
            self.company_owner,
            self.project_room,
            query="solar",
            object_types=["opportunity"],
            limit=10,
        )

        self.assertEqual(results, [])

    def test_requirement_search_matches_sequence_in_project_scope(self):
        results = search_linked_objects(
            self.company_member,
            self.project_room,
            query="1.1",
            object_types=["requirement"],
            limit=10,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["object_type"], "requirement")
        self.assertEqual(results[0]["object_id"], self.child_requirement.id)
        self.assertEqual(results[0]["sequence"], "1.1")
        self.assertEqual(
            results[0]["target_url"],
            f"/project-system-requirements/technical-requirements?id={self.child_requirement.id}&project_id={self.company_project.id}",
        )

    def test_requirement_search_is_scoped_to_room_project(self):
        results = search_linked_objects(
            self.company_member,
            self.project_room,
            query="Foreign",
            object_types=["requirement"],
            limit=10,
        )

        self.assertEqual(results, [])

    def test_company_room_requirement_search_includes_connected_project(self):
        self.other_project.team_members.create(
            role="Consultant",
            team_member=self.company_member,
            project_access="RW",
        )

        results = search_linked_objects(
            self.company_member,
            self.company_room,
            query="Foreign",
            object_types=["requirement"],
            limit=10,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["object_type"], "requirement")
        self.assertEqual(results[0]["title"], "Foreign requirement")

    def test_company_room_requirement_search_excludes_unconnected_external_project(self):
        results = search_linked_objects(
            self.company_member,
            self.company_room,
            query="Foreign",
            object_types=["requirement"],
            limit=10,
        )

        self.assertEqual(results, [])

    def test_resolve_linked_objects_includes_requirement_metadata(self):
        cards = resolve_linked_objects(
            [
                {
                    "object_type": "requirement",
                    "object_id": self.child_requirement.id,
                }
            ],
            self.company_member,
        )

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["object_type"], "requirement")
        self.assertEqual(cards[0]["sequence"], "1.1")
        self.assertEqual(cards[0]["object_number"], "REQ-001-A")
        self.assertEqual(cards[0]["project_name"], self.company_project.project_name)


@override_settings(
    REST_FRAMEWORK=TEST_REST_FRAMEWORK,
    CHAT_PUSH_NOTIFICATIONS_ENABLED=True,
)
class ChatMentionPushNotificationTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(
            email="sender@example.com",
            password="password",
            first_name="Danish",
            last_name="User",
            user_type="company_user",
        )
        self.mentioned_user = User.objects.create_user(
            email="mentioned@example.com",
            password="password",
            first_name="Mentioned",
            last_name="User",
            user_type="company_user",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="password",
            first_name="Other",
            last_name="User",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.sender,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[self.sender.id, self.mentioned_user.id, self.other_user.id]
        ).update(company=self.company)
        self.sender.refresh_from_db()
        self.mentioned_user.refresh_from_db()
        self.other_user.refresh_from_db()

        self.company_room = ChatRoom.objects.create(
            name="Company chat",
            company=self.company,
            is_direct=False,
            is_group=False,
            created_by=self.sender,
        )

    @patch("chat.tasks.send_chat_push_notifications")
    def test_mentioned_user_gets_mention_specific_push_text(self, mock_send_push):
        from .models import ChatPushDevice

        ChatPushDevice.objects.create(user=self.mentioned_user, token="mentioned-token")
        ChatPushDevice.objects.create(user=self.other_user, token="other-token")

        message = Message.objects.create(
            room=self.company_room,
            sender=self.sender,
            content="Please check this",
            mentions=[
                {
                    "user_id": self.mentioned_user.id,
                    "name": self.mentioned_user.full_name,
                    "user_image": self.mentioned_user.user_image or None,
                }
            ],
        )

        mock_send_push.return_value = []

        send_chat_message_push_notifications.apply(args=[message.id])

        self.assertEqual(mock_send_push.call_count, 2)

        calls_by_token = {
            call.kwargs["tokens"][0]: call.kwargs
            for call in mock_send_push.call_args_list
        }

        mentioned_call = calls_by_token["mentioned-token"]
        other_call = calls_by_token["other-token"]

        self.assertEqual(
            mentioned_call["body"],
            "Danish User mentioned you: Please check this",
        )
        self.assertEqual(
            mentioned_call["data_payload"]["is_mention"],
            "true",
        )
        self.assertEqual(other_call["body"], "Please check this")
        self.assertEqual(other_call["data_payload"]["is_mention"], "false")
