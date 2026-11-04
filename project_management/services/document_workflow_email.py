"""Document workflow email, using the server mail backend and existing opt-in."""
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.validators import validate_email
from django.utils.html import format_html

from userapis.utils import get_user_email_preferences

logger = logging.getLogger(__name__)

LABELS = {
    "French": {"version": "Version de travail", "reason": "Commentaire", "open": "Consulter le document dans LINKS", "login": "Connectez-vous à LINKS pour accéder à la fiche documentaire."},
    "German": {"version": "Arbeitsversion", "reason": "Kommentar", "open": "Dokument in LINKS öffnen", "login": "Melden Sie sich bei LINKS an, um das Dokument aufzurufen."},
    "English": {"version": "Working version", "reason": "Comment", "open": "Open the document in LINKS", "login": "Sign in to LINKS to access the document."},
}


def send_workflow_email(workflow, status, recipient, message):
    # Bell assignments remain mandatory; the existing email opt-in is independent.
    if not get_user_email_preferences(recipient.pk):
        return False
    try:
        validate_email(recipient.email)
        base = str(settings.FRONTEND_URL).rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid frontend URL configuration")
        document = workflow.revision.document
        url = base + "/company-documents/" + str(document.pk)
        labels = LABELS.get(recipient.preferred_language, LABELS["English"])
        version = str(workflow.revision.version_number)
        reason = ""
        if status in {"REJECTED", "CANCELLED"}:
            action = "REJECT" if status == "REJECTED" else "STOP"
            event = workflow.events.filter(action=action).order_by("-created_at", "-pk").first()
            reason = event.comment if event else ""
        plain = message + "\n\n" + labels["version"] + ": " + version
        if reason:
            plain += "\n\n" + labels["reason"] + ": " + reason
        plain += "\n\n" + labels["open"] + ": " + url + "\n" + labels["login"]
        html = format_html(
            '<p>{}</p><p>{}: {}</p>{}<p><a href="{}">{}</a></p><p>{}</p>',
            message, labels["version"], version,
            format_html('<p>{}: {}</p>', labels["reason"], reason) if reason else "",
            url, labels["open"], labels["login"],
        )
        # A provider problem after commit must not turn an already recorded signature
        # into an apparent failure. Do not automatically retry an uncertain delivery.
        connection = get_connection(timeout=getattr(settings, "EMAIL_TIMEOUT", None) or 10)
        email = EmailMultiAlternatives(
            subject=" ".join(("LINKS — " + message).split())[:250],
            body=plain, from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient.email], connection=connection,
        )
        email.attach_alternative(html, "text/html")
        sent = email.send(fail_silently=False)
        if sent != 1:
            raise RuntimeError("Mail backend did not confirm sending")
        return True
    except (ValidationError, ValueError):
        logger.warning("Document workflow email skipped: invalid address or frontend configuration (workflow=%s, recipient=%s)", workflow.pk, recipient.pk)
    except Exception:
        logger.error("Document workflow email failed after commit (workflow=%s, recipient=%s); workflow and bell notification remain recorded", workflow.pk, recipient.pk)
    return False
