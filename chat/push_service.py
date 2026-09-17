import base64
import json
import logging

from django.conf import settings

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except Exception:  # pragma: no cover - handled by runtime checks
    firebase_admin = None
    credentials = None
    messaging = None


logger = logging.getLogger(__name__)
FIREBASE_APP_NAME = "chat-push"


def _ensure_firebase_modules_loaded():
    global firebase_admin, credentials, messaging

    if firebase_admin is not None and credentials is not None and messaging is not None:
        return True

    try:
        import firebase_admin as firebase_admin_module
        from firebase_admin import credentials as credentials_module
        from firebase_admin import messaging as messaging_module

        firebase_admin = firebase_admin_module
        credentials = credentials_module
        messaging = messaging_module
        return True
    except Exception:
        logger.warning("firebase-admin package is not available for chat push")
        return False


def _feature_enabled():
    return bool(getattr(settings, "CHAT_PUSH_NOTIFICATIONS_ENABLED", False))


def _load_service_account_info():
    json_raw = (getattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "") or "").strip()
    if json_raw:
        return json.loads(json_raw)

    json_b64 = (getattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON_BASE64", "") or "").strip()
    if json_b64:
        decoded = base64.b64decode(json_b64).decode("utf-8")
        return json.loads(decoded)

    project_id = (getattr(settings, "FIREBASE_PROJECT_ID", "") or "").strip()
    client_email = (getattr(settings, "FIREBASE_CLIENT_EMAIL", "") or "").strip()
    private_key = getattr(settings, "FIREBASE_PRIVATE_KEY", "") or ""
    if project_id and client_email and private_key:
        return {
            "type": "service_account",
            "project_id": project_id,
            "client_email": client_email,
            "private_key": private_key,
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    return None


def get_firebase_app():
    if not _feature_enabled():
        return None

    if not _ensure_firebase_modules_loaded():
        return None

    try:
        return firebase_admin.get_app(FIREBASE_APP_NAME)
    except Exception:
        pass

    service_account_info = _load_service_account_info()
    if not service_account_info:
        logger.warning("Chat push is enabled but Firebase credentials are missing")
        return None

    try:
        credential = credentials.Certificate(service_account_info)
        return firebase_admin.initialize_app(credential, name=FIREBASE_APP_NAME)
    except Exception:
        logger.exception("Failed to initialize Firebase app for chat push")
        return None


def _chunked(items, chunk_size):
    for index in range(0, len(items), chunk_size):
        yield items[index : index + chunk_size]


def _is_invalid_token_error(exc):
    error_text = str(exc or "").lower()
    return (
        "registration-token-not-registered" in error_text
        or "invalid-registration-token" in error_text
        or "unregistered" in error_text
        or "notregistered" in error_text
    )


def send_chat_push_notifications(tokens, title, body, data_payload, link=None, icon=None, badge=None):
    app = get_firebase_app()
    if not app or messaging is None:
        return []

    normalized_tokens = list({str(token).strip() for token in tokens if str(token).strip()})
    if not normalized_tokens:
        return []

    clean_payload = {
        str(key): str(value)
        for key, value in (data_payload or {}).items()
        if value is not None
    }

    invalid_tokens = []

    for token_chunk in _chunked(normalized_tokens, 500):
        notification_link = str(link or "")
        payload_type = clean_payload.get("type") or ""
        if payload_type == "chat_reaction":
            notification_tag = (
                f"chat-reaction-{clean_payload.get('message_id', '')}-"
                f"{clean_payload.get('reactor_id', '')}-"
                f"{clean_payload.get('emoji', '')}"
            )
        elif clean_payload.get("message_id"):
            notification_tag = f"chat-{clean_payload.get('message_id', '')}"
        else:
            notification_tag = None

        webpush_notification = messaging.WebpushNotification(
            title=title,
            body=body,
            icon=icon,
            badge=badge,
            data={"link": notification_link} if notification_link else None,
            tag=notification_tag,
            renotify=False,
        )

        message = messaging.MulticastMessage(
            tokens=token_chunk,
            data=clean_payload,
            webpush=messaging.WebpushConfig(
                notification=webpush_notification,
                fcm_options=messaging.WebpushFCMOptions(link=link) if link else None,
                headers={"Urgency": "high"},
            ),
        )

        try:
            result = messaging.send_each_for_multicast(message, app=app)
        except Exception:
            logger.exception("Failed sending chat push batch")
            continue

        logger.info(
            "Chat push Firebase batch completed",
            extra={
                "token_count": len(token_chunk),
                "success_count": int(getattr(result, "success_count", 0)),
                "failure_count": int(getattr(result, "failure_count", 0)),
            },
        )

        for index, response in enumerate(result.responses):
            if response.success:
                continue
            logger.warning(
                "Chat push token failed",
                extra={
                    "token_suffix": token_chunk[index][-12:],
                    "exception": str(getattr(response, "exception", "")),
                },
            )
            if _is_invalid_token_error(getattr(response, "exception", None)):
                invalid_tokens.append(token_chunk[index])

    return invalid_tokens
