from datetime import timedelta

from django.db import transaction

from django.utils import timezone

from .models import ChatPresence, ChatPresenceSession


STALE_PRESENCE_TIMEOUT = timedelta(seconds=90)


def _ensure_session_key(session_key):
    session_key = str(session_key or "").strip()
    if not session_key:
        raise ValueError("session_id is required.")
    return session_key


def get_presence_cutoff(reference_time=None):
    reference_time = reference_time or timezone.now()
    return reference_time - STALE_PRESENCE_TIMEOUT


def get_online_user_ids(user_ids, reference_time=None):
    if not user_ids:
        return set()

    return set(
        ChatPresenceSession.objects.filter(
            user_id__in=user_ids,
            closed_at__isnull=True,
            last_heartbeat_at__gte=get_presence_cutoff(reference_time),
        ).values_list("user_id", flat=True)
    )


def sync_user_presence_state(user, reference_time=None):
    reference_time = reference_time or timezone.now()
    is_online = ChatPresenceSession.objects.filter(
        user=user,
        closed_at__isnull=True,
        last_heartbeat_at__gte=get_presence_cutoff(reference_time),
    ).exists()

    ChatPresence.objects.update_or_create(
        user=user,
        defaults={
            "is_online": is_online,
            "last_active_at": reference_time,
        },
    )
    return is_online


@transaction.atomic
def heartbeat_user_presence(user, session_key, reference_time=None):
    session_key = _ensure_session_key(session_key)
    reference_time = reference_time or timezone.now()
    ChatPresenceSession.objects.update_or_create(
        user=user,
        session_key=session_key,
        defaults={
            "last_heartbeat_at": reference_time,
            "closed_at": None,
        },
    )
    sync_user_presence_state(user, reference_time)
    return reference_time


@transaction.atomic
def close_user_presence_session(user, session_key, reference_time=None):
    session_key = _ensure_session_key(session_key)
    reference_time = reference_time or timezone.now()

    ChatPresenceSession.objects.filter(
        user=user,
        session_key=session_key,
        closed_at__isnull=True,
    ).update(
        closed_at=reference_time,
        last_heartbeat_at=reference_time,
    )

    sync_user_presence_state(user, reference_time)
    return reference_time
