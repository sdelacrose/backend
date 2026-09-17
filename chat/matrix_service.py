import logging
import time
from typing import Optional
from urllib.parse import urlencode
from uuid import uuid4

import requests
from django.conf import settings

from .metrics import emit_chat_metric
from .models import MatrixMessageMap, MatrixRoomMap


logger = logging.getLogger(__name__)


class MatrixService:
    def __init__(self):
        self.enabled = bool(getattr(settings, "MATRIX_ENABLED", False))
        self.base_url = (getattr(settings, "MATRIX_BASE_URL", "") or "").rstrip("/")
        self.access_token = getattr(settings, "MATRIX_ACCESS_TOKEN", "") or ""
        self.timeout = int(getattr(settings, "MATRIX_REQUEST_TIMEOUT", 10))
        self.retry_attempts = int(getattr(settings, "MATRIX_RETRY_ATTEMPTS", 5))

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _request_with_retry(self, method, url, payload=None, params=None):
        last_response = None
        for attempt in range(self.retry_attempts):
            response = requests.request(
                method,
                url,
                json=payload,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            last_response = response

            if response.status_code != 429:
                return response

            # Prefer Retry-After header when present, fall back to retry_after_ms
            # in body for older servers.
            wait_seconds = 1
            retry_after_header = (response.headers or {}).get("Retry-After")
            if retry_after_header:
                try:
                    wait_seconds = max(1, int(float(retry_after_header)))
                except Exception:
                    wait_seconds = max(1, attempt + 1)
            else:
                try:
                    retry_ms = response.json().get("retry_after_ms")
                    if retry_ms:
                        wait_seconds = max(1, int(retry_ms) // 1000)
                except Exception:
                    wait_seconds = max(1, attempt + 1)

            time.sleep(wait_seconds)

        return last_response

    def redact_event(self, matrix_room_id: str, matrix_event_id: str, reason: str = "") -> bool:
        if not self.enabled:
            return False

        if not matrix_room_id or not matrix_event_id:
            return False

        if not self.base_url or not self.access_token:
            emit_chat_metric("matrix_redact_skipped", reason="missing_config")
            return False

        txn_id = str(uuid4())
        payload = {}
        if reason:
            payload["reason"] = reason

        try:
            response = self._request_with_retry(
                "PUT",
                f"{self.base_url}/_matrix/client/v3/rooms/{matrix_room_id}/redact/{matrix_event_id}/{txn_id}",
                payload,
            )
            if response is None:
                emit_chat_metric("matrix_redact_failed", reason="no_response")
                return False
            response.raise_for_status()
            emit_chat_metric("matrix_redact_success")
            return True
        except Exception:
            logger.exception(
                "Failed to redact Matrix event %s in room %s",
                matrix_event_id,
                matrix_room_id,
            )
            emit_chat_metric("matrix_redact_failed", reason="exception")
            return False

    def fetch_room_messages(self, matrix_room_id: str, batch_size: int = 200, max_events: int = 5000):
        if not self.enabled:
            return []

        if not matrix_room_id:
            return []

        if not self.base_url or not self.access_token:
            emit_chat_metric("matrix_fetch_messages_skipped", reason="missing_config")
            return []

        all_events = []
        from_token = None

        try:
            while len(all_events) < max_events:
                params = {
                    "dir": "b",
                    "limit": min(batch_size, max_events - len(all_events)),
                }
                if from_token:
                    params["from"] = from_token

                response = self._request_with_retry(
                    "GET",
                    f"{self.base_url}/_matrix/client/v3/rooms/{matrix_room_id}/messages",
                    params=params,
                )
                if response is None:
                    emit_chat_metric(
                        "matrix_fetch_messages_failed",
                        room_id=matrix_room_id,
                        reason="no_response",
                    )
                    return []
                response.raise_for_status()
                payload = response.json()
                chunk = payload.get("chunk", [])
                if not chunk:
                    break

                all_events.extend(chunk)
                from_token = payload.get("end")
                if not from_token:
                    break

            emit_chat_metric("matrix_fetch_messages_success", room_id=matrix_room_id, count=len(all_events))
            # Matrix backward pagination returns newest -> oldest.
            return list(reversed(all_events))
        except Exception:
            logger.exception("Failed to fetch messages from Matrix room %s", matrix_room_id)
            emit_chat_metric("matrix_fetch_messages_failed", room_id=matrix_room_id)
            return []

    def _room_type_for_local_room(self, room) -> str:
        if room.is_group:
            return "group"
        if room.is_direct:
            return "direct"
        if room.project_id:
            return "project"
        if room.company_id:
            return "company"
        return "freelancer"

    def ensure_room(self, room) -> Optional[MatrixRoomMap]:
        if not self.enabled:
            return None

        existing = getattr(room, "matrix_room_map", None)
        if existing:
            return existing

        if not self.base_url or not self.access_token:
            logger.warning("Matrix is enabled but MATRIX_BASE_URL or MATRIX_ACCESS_TOKEN is missing")
            emit_chat_metric("matrix_room_sync_skipped", reason="missing_config")
            return None

        payload = {
            "name": room.name,
            "preset": "private_chat",
            "visibility": "private",
        }

        try:
            response = self._request_with_retry(
                "POST",
                f"{self.base_url}/_matrix/client/v3/createRoom",
                payload,
            )
            if response is None:
                emit_chat_metric("matrix_room_sync_failed", reason="no_response")
                return None
            response.raise_for_status()
            room_id = response.json().get("room_id")
            if not room_id:
                emit_chat_metric("matrix_room_sync_failed", reason="missing_room_id")
                return None

            matrix_room_map = MatrixRoomMap.objects.create(
                chat_room=room,
                matrix_room_id=room_id,
                room_type=self._room_type_for_local_room(room),
                state="active",
            )
            emit_chat_metric("matrix_room_sync_success", room_type=matrix_room_map.room_type)
            return matrix_room_map
        except Exception as exc:
            logger.exception("Failed to ensure Matrix room for local room %s", room.id)
            emit_chat_metric("matrix_room_sync_failed", reason="exception")
            return None

    def sync_message(self, message) -> Optional[MatrixMessageMap]:
        if not self.enabled:
            return None

        existing = getattr(message, "matrix_message_map", None)
        if existing:
            return existing

        room_map = self.ensure_room(message.room)
        if not room_map:
            return None

        message_body = (message.content or "").strip()
        message_attachments = getattr(message, "attachments", None) or []
        message_linked_objects = getattr(message, "linked_objects", None) or []
        if not message_body and message_attachments:
            message_body = "Attachment"
        if not message_body and message_linked_objects:
            message_body = "Linked object"

        payload = {
            "msgtype": "m.text",
            "body": message_body,
            "m.sharemundo": {
                "local_message_id": message.id,
                "local_sender_id": message.sender_id,
                "is_deleted": message.is_deleted,
                "message_type": getattr(message, "message_type", "text"),
            },
        }

        if message_attachments:
            payload["m.sharemundo"]["attachments"] = message_attachments

        if message_linked_objects:
            payload["m.sharemundo"]["linked_objects"] = message_linked_objects

        if getattr(message, "message_type", "text") == "voice":
            payload["m.sharemundo"]["voice_note_url"] = getattr(message, "voice_note_url", None)
            payload["m.sharemundo"]["voice_note_duration_ms"] = getattr(
                message,
                "voice_note_duration_ms",
                None,
            )

        replied_to = getattr(message, "replied_to", None)
        if replied_to is not None:
            replied_to_map = getattr(replied_to, "matrix_message_map", None)
            if not replied_to_map:
                replied_to_map = self.sync_message(replied_to)

            if replied_to_map:
                payload["m.relates_to"] = {
                    "m.in_reply_to": {
                        "event_id": replied_to_map.matrix_event_id,
                    }
                }
                payload["m.sharemundo"]["reply_to_local_message_id"] = replied_to.id

        txn_id = str(uuid4())

        try:
            response = self._request_with_retry(
                "PUT",
                f"{self.base_url}/_matrix/client/v3/rooms/{room_map.matrix_room_id}/send/m.room.message/{txn_id}",
                payload,
            )
            if response is None:
                emit_chat_metric("matrix_message_sync_failed", reason="no_response")
                return None
            response.raise_for_status()
            event_id = response.json().get("event_id")
            if not event_id:
                emit_chat_metric("matrix_message_sync_failed", reason="missing_event_id")
                return None

            matrix_message_map = MatrixMessageMap.objects.create(
                message=message,
                matrix_event_id=event_id,
                matrix_room_id=room_map.matrix_room_id,
                event_type="m.room.message",
            )
            emit_chat_metric("matrix_message_sync_success")
            return matrix_message_map
        except Exception:
            logger.exception("Failed to sync message %s to Matrix", message.id)
            emit_chat_metric("matrix_message_sync_failed", reason="exception")
            return None

    def send_reaction(self, matrix_room_id: str, relates_to_event_id: str, key: str) -> Optional[str]:
        if not self.enabled:
            return None

        if not matrix_room_id or not relates_to_event_id or not key:
            return None

        if not self.base_url or not self.access_token:
            emit_chat_metric("matrix_reaction_send_skipped", reason="missing_config")
            return None

        payload = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": relates_to_event_id,
                "key": key,
            },
            "m.sharemundo": {
                "kind": "reaction",
            },
        }
        txn_id = str(uuid4())

        try:
            response = self._request_with_retry(
                "PUT",
                f"{self.base_url}/_matrix/client/v3/rooms/{matrix_room_id}/send/m.reaction/{txn_id}",
                payload,
            )
            if response is None:
                emit_chat_metric("matrix_reaction_send_failed", reason="no_response")
                return None
            response.raise_for_status()
            event_id = response.json().get("event_id")
            if not event_id:
                emit_chat_metric("matrix_reaction_send_failed", reason="missing_event_id")
                return None
            emit_chat_metric("matrix_reaction_send_success")
            return event_id
        except Exception:
            logger.exception("Failed to send reaction in Matrix room %s", matrix_room_id)
            emit_chat_metric("matrix_reaction_send_failed", reason="exception")
            return None

    def edit_message(
        self,
        matrix_room_id: str,
        original_event_id: str,
        updated_content: str,
    ) -> Optional[str]:
        if not self.enabled:
            return None

        if not matrix_room_id or not original_event_id:
            return None

        if not self.base_url or not self.access_token:
            emit_chat_metric("matrix_message_edit_skipped", reason="missing_config")
            return None

        next_body = (updated_content or "").strip()
        if not next_body:
            return None

        payload = {
            "msgtype": "m.text",
            "body": f"* {next_body}",
            "m.new_content": {
                "msgtype": "m.text",
                "body": next_body,
            },
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": original_event_id,
            },
            "m.sharemundo": {
                "kind": "message_edit",
                "is_edited": True,
            },
        }
        txn_id = str(uuid4())

        try:
            response = self._request_with_retry(
                "PUT",
                f"{self.base_url}/_matrix/client/v3/rooms/{matrix_room_id}/send/m.room.message/{txn_id}",
                payload,
            )
            if response is None:
                emit_chat_metric("matrix_message_edit_failed", reason="no_response")
                return None
            response.raise_for_status()
            event_id = response.json().get("event_id")
            if not event_id:
                emit_chat_metric("matrix_message_edit_failed", reason="missing_event_id")
                return None

            emit_chat_metric("matrix_message_edit_success")
            return event_id
        except Exception:
            logger.exception(
                "Failed to edit Matrix message event %s in room %s",
                original_event_id,
                matrix_room_id,
            )
            emit_chat_metric("matrix_message_edit_failed", reason="exception")
            return None


matrix_service = MatrixService()
