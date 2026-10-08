import logging
from datetime import timedelta, timezone as datetime_timezone
from urllib.parse import quote

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from .models import CalendarEvent
from ecpmpapp.utils import can_assign_calendar_event, reschedule_event
from ecpmpapp.constants import (
    MS_GRAPH_USER_CALENDAR_EVENTS_URL,
    MS_GRAPH_USER_CONTACTS_URL,
    MS_OAUTH_TOKEN_URL,
    MS_OUTLOOK_CALENDAR_SCOPES,
)
from models.models import UserOutlookCredentials
from project_management.utils import get_sharemundo_user
from userapis.token_encryption import ENCRYPTION_PREFIX, TokenCryptoError, encrypt_token


PRIVATE_OUTLOOK_EVENT_TITLE = "Private"
logger = logging.getLogger(__name__)


def get_outlook_event_id(event):
    return event.get("uid") or event.get("iCalUId") or event.get("id")


def get_outlook_event_ids(event):
    event_ids = [event.get("id"), event.get("uid"), event.get("iCalUId")]
    return list(dict.fromkeys(event_id for event_id in event_ids if event_id))


def get_created_outlook_event_id(event):
    return event.get("id") or get_outlook_event_id(event)


def get_outlook_meeting_link(event):
    online_meeting = event.get("onlineMeeting") or {}
    return online_meeting.get("joinUrl") or event.get("onlineMeetingUrl")


def is_private_outlook_event(event):
    """Return True when Microsoft Graph marks the event as private."""
    sensitivity = str(event.get("sensitivity") or "").strip().lower()
    return sensitivity == "private"


def get_outlook_event_visibility_data(event):
    """Return event sync data while preserving privacy for response-time masking."""

    meeting_link = None
    if event.get("isOnlineMeeting", False):
        meeting_link = get_outlook_meeting_link(event)

    attendees = event.get("attendees", [])

    return {
        "name": event.get("subject", "No Title"),
        "description": event.get("bodyPreview") or "",
        "meeting_link": meeting_link,
        "assignees": attendees,
        "is_private": is_private_outlook_event(event),
    }

def get_access_token(auth_code):
    """Exchange auth code for access token."""
    token_url = MS_OAUTH_TOKEN_URL
    data = {
        "client_id": settings.MS_CLIENT_ID,
        "client_secret": settings.MS_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": settings.MS_REDIRECT_URI,
        "code": auth_code,
        "scope": MS_OUTLOOK_CALENDAR_SCOPES,
    }
    response = requests.post(token_url, data=data)
    return response.json()

def get_outlook_events(access_token):
    """
    Fetch events from Microsoft Outlook Calendar.

    :param access_token: OAuth token for Microsoft Graph API
    :return: List of events from the Outlook calendar
    """
    
    url = MS_GRAPH_USER_CALENDAR_EVENTS_URL

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    response = requests.get(url, headers=headers)
    return response


def get_outlook_contacts(access_token):
    """Fetch the current user's Outlook contacts for attendee resolution."""
    return requests.get(
        MS_GRAPH_USER_CONTACTS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        params={
            "$select": "id,displayName,givenName,surname,emailAddresses",
            "$top": 100,
        },
        timeout=10,
    )


def can_assign_project_calendar_event(project, assignee):
    if not project or not assignee:
        return False
    if project.user_id == assignee.id:
        return True
    return project.team_members.filter(team_member=assignee).exists()


def get_outlook_event(access_token, event_id):
    """Fetch a single Microsoft Graph calendar event by Graph event id."""
    url = f"{MS_GRAPH_USER_CALENDAR_EVENTS_URL}/{quote(event_id, safe='')}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    return requests.get(url, headers=headers)


def _graph_datetime(value):
    return value.astimezone(datetime_timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def create_outlook_calendar_event(user, calendar_event, attendee_emails=None, online_meeting=False):
    """Create a matching Outlook calendar event for an existing Sharemundo calendar event."""
    if not is_outlook_token_valid(user):
        return {
            "success": False,
            "requires_reconnect": True,
            "error": "Outlook calendar is not connected or the token has expired. Please reconnect Outlook calendar access.",
        }

    attendee_emails = attendee_emails or []
    attendees_by_email = {}
    for assignee in calendar_event.assignees.all():
        if assignee.email:
            attendees_by_email[assignee.email.lower()] = {
                "emailAddress": {
                    "address": assignee.email,
                    "name": assignee.full_name or assignee.email,
                },
                "type": "required",
            }
    for email in attendee_emails:
        email = str(email or "").strip()
        if not email:
            continue
        attendees_by_email[email.lower()] = {
            "emailAddress": {
                "address": email,
                "name": email,
            },
            "type": "required",
        }

    graph_payload = {
        "subject": calendar_event.name,
        "body": {
            "contentType": "Text",
            "content": calendar_event.description or "",
        },
        "start": {
            "dateTime": _graph_datetime(calendar_event.start_date),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": _graph_datetime(calendar_event.end_date),
            "timeZone": "UTC",
        },
        "attendees": list(attendees_by_email.values()),
    }
    if online_meeting:
        graph_payload["isOnlineMeeting"] = True

    response = requests.post(
        MS_GRAPH_USER_CALENDAR_EVENTS_URL,
        headers={
            "Authorization": f"Bearer {user.outlook_credentials.outlook_access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=graph_payload,
    )
    if response.status_code not in {200, 201}:
        try:
            error_payload = response.json().get("error", response.json())
        except ValueError:
            error_payload = {"message": response.text}
        return {
            "success": False,
            "status_code": response.status_code,
            "error": error_payload,
        }

    created_event = response.json()
    calendar_event.outlook_event_id = get_created_outlook_event_id(created_event)
    meeting_link = get_outlook_meeting_link(created_event)
    if online_meeting and not meeting_link and calendar_event.outlook_event_id:
        event_response = get_outlook_event(
            user.outlook_credentials.outlook_access_token,
            calendar_event.outlook_event_id,
        )
        if event_response.status_code == 200:
            try:
                meeting_link = get_outlook_meeting_link(event_response.json())
            except ValueError:
                meeting_link = None
    calendar_event.meeting_link = meeting_link or calendar_event.meeting_link
    calendar_event.user = user
    calendar_event.save(update_fields=["outlook_event_id", "meeting_link", "user"])
    return {
        "success": True,
        "outlook_event_id": calendar_event.outlook_event_id,
        "meeting_link": calendar_event.meeting_link,
    }


def sync_outlook_events(user, access_token, project=None):
    """Sync Outlook events and close missing events with error handling."""
    
    outlook_response = get_outlook_events(access_token)
    if outlook_response.status_code != 200:
        try:
            error_info = outlook_response.json().get("error", {})
            error_code = error_info.get("code", "UnknownError")
            error_message = error_info.get("message", "An unknown error occurred.")
        except ValueError:
            error_code = "InvalidResponse"
            error_message = "Received an invalid response from Outlook API."


        return {
            "success": False,
            "error": {
                "code": error_code,
                "message": error_message
            }
        }

    try:
        fetched_events = outlook_response.json().get("value", [])
    except ValueError:
        return {
            "success": False,
            "error": {
                "code": "InvalidJSON",
                "message": "Failed to parse JSON from Outlook API."
            }
        }

    fetched_event_ids = set()
    for event in fetched_events:
        fetched_event_ids.update(get_outlook_event_ids(event))

    for event in fetched_events:
        outlook_event_id = get_outlook_event_id(event)
        outlook_event_ids = get_outlook_event_ids(event)
        if not outlook_event_id or not outlook_event_ids:
            continue
        visibility_data = get_outlook_event_visibility_data(event)

        # Parse datetime strings from Outlook
        start_date = parse_datetime(event.get("start", {}).get("dateTime"))
        end_date = parse_datetime(event.get("end", {}).get("dateTime"))

        # Ensure timezone awareness
        if start_date.tzinfo is None:
            start_date = timezone.make_aware(start_date)
        if end_date.tzinfo is None:
            end_date = timezone.make_aware(end_date)
        
        # Get attendees from MS event
        assignees = []
        for attendee in visibility_data["assignees"]:
            email = attendee.get("emailAddress", {}).get("address")
            if email:
                try:
                    sharemundo_user = get_sharemundo_user(email)
                    if not sharemundo_user:
                        continue

                    if project:
                        if can_assign_project_calendar_event(project, sharemundo_user):
                            assignees.append(sharemundo_user)
                    elif can_assign_calendar_event(user, sharemundo_user):
                        assignees.append(sharemundo_user)
                except Exception as e:
                    print(f"Error processing attendee {email}: {e}")

        defaults = {
            "user": user,
            "name": visibility_data["name"],
            "description": visibility_data["description"],
            "is_private": visibility_data["is_private"],
            "start_date": start_date,
            "end_date": end_date,
            "project": project,
            "company_profile": user.company if not user.is_freelancer else None,
            "freelancer": user if user.is_freelancer else None,
            "meeting_link": visibility_data["meeting_link"],
        }
        calendar_event_query = CalendarEvent.objects.filter(
            user=user,
            outlook_event_id__in=outlook_event_ids,
        )
        if project:
            calendar_event_query = calendar_event_query.filter(project=project)
        else:
            calendar_event_query = calendar_event_query.filter(project__isnull=True)

        calendar_event = calendar_event_query.first()
        if calendar_event:
            for field, value in defaults.items():
                setattr(calendar_event, field, value)
            calendar_event.save(update_fields=[*defaults.keys()])
        else:
            calendar_event = CalendarEvent.objects.create(
                outlook_event_id=outlook_event_id,
                **defaults,
            )
        calendar_event.assignees.set(assignees)
        reschedule_event(calendar_event, is_deleted=False)


    closed_events = CalendarEvent.objects.filter(
        user=user,
        outlook_event_id__isnull=False,
    ).exclude(
        outlook_event_id__in=fetched_event_ids
    )
    if project:
        closed_events = closed_events.filter(project=project)
    else:
        closed_events = closed_events.filter(project__isnull=True)
    for event in closed_events:
        reschedule_event(event, is_deleted=True)
        event.delete()

    return {
        "success": True,
        "message": "Outlook events synced successfully.",
        "synced_events_count": len(fetched_events)
    }

def refresh_outlook_access_token(user):
    """Refresh an expired Microsoft access token without forcing reconnection."""
    if not hasattr(user, "outlook_credentials"):
        return None
    credentials = user.outlook_credentials
    if not credentials.outlook_refresh_token:
        return None

    try:
        response = requests.post(
            MS_OAUTH_TOKEN_URL,
            data={
                "client_id": settings.MS_CLIENT_ID,
                "client_secret": settings.MS_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": credentials.outlook_refresh_token,
                "scope": MS_OUTLOOK_CALENDAR_SCOPES,
            },
            timeout=10,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        logger.warning(
            "Unable to refresh Outlook access token for user_id=%s",
            getattr(user, "id", None),
            exc_info=True,
        )
        return None

    access_token = payload.get("access_token") if response.ok else None
    if not access_token:
        logger.info(
            "Outlook token refresh was rejected for user_id=%s error=%s",
            getattr(user, "id", None),
            payload.get("error"),
        )
        return None

    new_refresh_token = payload.get("refresh_token") or credentials.outlook_refresh_token

    # MAIL-1.1C2-S section 3 fix: this refresh path is shared by Calendar, Contacts, and now
    # Mail (all three sit on the same UserOutlookCredentials row). It used to assign the raw
    # Microsoft response straight onto the instance and rely entirely on the global,
    # OFF-by-default MS_TOKEN_ENCRYPTION_WRITE_ENABLED flag (signals.py pre_save) to encrypt it
    # on save. store_mail_tokens() (mailbox_oauth.py) always encrypts a brand-new Mail token
    # regardless of that flag -- so without this fix, the very first refresh after a Mail
    # connection silently overwrote that already-encrypted value with plaintext, with no error
    # and no trace, defeating the "équivalente" protection this checkpoint asks to verify.
    #
    # Fix is scoped to preserve whatever protection level this specific credential already
    # has, rather than unconditionally forcing encryption here: read the RAW (pre-decryption)
    # column value -- bypassing the instance's own post_init-decrypted in-memory attribute --
    # and only take over explicit encryption when that raw value already carries
    # ENCRYPTION_PREFIX. A credential that was never encrypted (the legacy
    # OutlookEventSyncTests case, still exercised with no key configured at all) keeps writing
    # exactly as before, so an environment with write-encryption never enabled and no Mail
    # connection ever made is completely unaffected.
    raw_access_before = (
        UserOutlookCredentials.objects.filter(pk=credentials.pk)
        .values_list("outlook_access_token", flat=True)
        .first()
        or ""
    )
    if raw_access_before.startswith(ENCRYPTION_PREFIX):
        try:
            encrypted_access = encrypt_token(access_token)
            encrypted_refresh = (
                encrypt_token(new_refresh_token) if new_refresh_token else new_refresh_token
            )
        except TokenCryptoError:
            # Fail closed, same as every other encryption failure in this lot: never
            # downgrade an already-protected credential to plaintext. The stale, still-
            # encrypted row is left untouched rather than overwritten.
            logger.warning(
                "Outlook token refresh succeeded for user_id=%s but could not be stored: "
                "token encryption is not configured.",
                getattr(user, "id", None),
            )
            return None
        credentials.outlook_access_token = encrypted_access
        credentials.outlook_refresh_token = encrypted_refresh
    else:
        credentials.outlook_access_token = access_token
        credentials.outlook_refresh_token = new_refresh_token
    credentials.outlook_token_expires = timezone.now() + timedelta(
        seconds=int(payload.get("expires_in") or 3600)
    )
    credentials.save(
        update_fields=[
            "outlook_access_token",
            "outlook_refresh_token",
            "outlook_token_expires",
        ]
    )
    if raw_access_before.startswith(ENCRYPTION_PREFIX):
        # Mirrors signals.py's own stash-then-restore pattern for the identical problem:
        # the instance just wrote ciphertext to the DB, but any caller that keeps using
        # this same, already-loaded `credentials` object afterward (it is not re-fetched,
        # so post_init never re-fires) must still see a usable plaintext token, not the
        # ciphertext that was only ever meant for the SQL write.
        credentials.outlook_access_token = access_token
        credentials.outlook_refresh_token = new_refresh_token
    return access_token


def get_valid_outlook_access_token(user):
    """Return a current access token, refreshing it when possible."""
    if not hasattr(user, "outlook_credentials"):
        return None
    credentials = user.outlook_credentials
    expires_at = credentials.outlook_token_expires
    if (
        credentials.outlook_access_token
        and expires_at
        and timezone.now() < expires_at - timedelta(seconds=60)
    ):
        return credentials.outlook_access_token
    return refresh_outlook_access_token(user)


def is_outlook_token_valid(user):
    """Return whether Outlook access is currently usable or refreshable."""
    return bool(get_valid_outlook_access_token(user))
