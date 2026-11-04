"""Document distribution using the existing SMTP/Graph transports and mailbox authorization.

Network calls occur outside the two short claim/result transactions. Immutable transmittal
events hold the actual message, recipient and exact attachment hashes. A successful or uncertain
delivery cannot be repeated by submitting the same action again.
"""
import hashlib
import posixpath
from types import SimpleNamespace
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from project_management.models import Transmittal, TransmittalEvent
from project_management.services.correspondence_send import (
    _deliver_via_graph, _deliver_via_provider, _resolve_mail_sender_user,
    validate_mailbox_for_send, _valid_email,
)
from project_management.services.correspondence_template import unresolved_marker_errors
from userapis.mailbox_send import UNCERTAIN_DELIVERY_MARKER
from userapis.models import MailboxConnection

SEND_TYPES = ('SEND_STARTED', 'SEND_SUCCEEDED', 'SEND_FAILED')

def send_transmittal(transmittal, actor, *, recipient_contact_id=None, mailbox_id=None,
                     subject=None, body=None):
    if transmittal.direction != 'OUT' or transmittal.status != 'ISSUED':
        raise ValidationError('Only an issued outgoing transmittal can be sent.')
    contacts = list(transmittal.external_contacts.order_by('id'))
    if not contacts:
        raise ValidationError({'recipient_contact': 'At least one selected contact is required.'})
    if any(c.is_deleted or c.contact_company_id != transmittal.external_company_id or not _valid_email(c.email) for c in contacts):
        raise ValidationError({'recipient_contact': 'Every selected transmittal contact must belong to the recipient company and have a valid email address.'})
    contact = next((c for c in contacts if c.pk == recipient_contact_id), None) if recipient_contact_id is not None else contacts[0]
    if contact is None:
        raise ValidationError({'recipient_contact': 'The primary recipient must be one of the selected transmittal contacts.'})
    to_email = contact.email.strip()
    seen = {to_email.lower()}
    cc = []
    for other in contacts:
        email = other.email.strip()
        if email.lower() not in seen:
            cc.append(email)
            seen.add(email.lower())
    subject = (subject if subject is not None else transmittal.subject) or transmittal.transmittal_number
    body = body if body is not None else (transmittal.message or '')
    if not isinstance(subject, str) or not isinstance(body, str) or not subject.strip():
        raise ValidationError({'subject': 'A subject is required.'})
    if len(subject) > 500 or len(body) > 20000 or '\n' in subject or '\r' in subject:
        raise ValidationError('Invalid message or subject length.')
    unresolved = unresolved_marker_errors(subject=subject, body=body)
    if unresolved: raise ValidationError(unresolved)
    attachments, evidence = [], []
    for item in transmittal.items.select_related('document_revision__document').order_by('id'):
        revision = item.document_revision
        from project_management.services.document_withdrawal import assert_not_withdrawn
        assert_not_withdrawn(revision.document)
        if not item.evidence_snapshot:
            raise ValidationError('This transmittal has no frozen attachment evidence.')
        frozen = item.evidence_snapshot.get('revision') or {}
        if frozen.get('id') != revision.pk or not frozen.get('file_sha256'):
            raise ValidationError('This transmittal has no matching frozen revision hash.')
        try:
            with revision.file.open('rb') as handle:
                verified_content = handle.read()
        except (OSError, ValueError) as error:
            raise ValidationError('A stored document file could not be read. No message was sent.') from error
        digest = hashlib.sha256(verified_content).hexdigest()
        if not revision.file_sha256 or digest != revision.file_sha256 or digest != frozen.get('file_sha256'):
            raise ValidationError('A stored attachment no longer matches its approved hash.')
        filename = posixpath.basename(frozen.get('file_name') or revision.file_name or revision.file.name)
        attachments.append(SimpleNamespace(file=ContentFile(verified_content, name=filename), file_name=filename, file_size=len(verified_content)))
        evidence.append({'item': item.pk, 'document_revision': revision.pk, 'name': filename, 'sha256': digest})
    if not attachments: raise ValidationError('At least one document is required.')
    mailbox, sender_identity = None, None
    if mailbox_id is not None:
        mailbox = MailboxConnection.objects.filter(pk=mailbox_id).first()
        validate_mailbox_for_send(actor, mailbox, transmittal)
        sender_identity = _resolve_mail_sender_user(mailbox)
        if sender_identity is None: raise ValidationError('This mailbox has no connected Microsoft identity.')
    metadata = {'recipient_contact': contact.pk, 'recipient_contacts': [c.pk for c in contacts],
                'to': to_email, 'cc': cc,
                'from': mailbox.email_address if mailbox else settings.DEFAULT_FROM_EMAIL,
                'subject': subject, 'body': body, 'attachments': evidence,
                'mailbox_id': mailbox_id, 'provider': 'graph' if mailbox else 'smtp'}
    with transaction.atomic():
        Transmittal.objects.select_for_update().get(pk=transmittal.pk)
        latest = transmittal.events.filter(event_type__in=SEND_TYPES).order_by('-id').first()
        if latest and (latest.event_type != 'SEND_FAILED' or (latest.metadata or {}).get('uncertain')):
            raise ValidationError('This transmittal was already sent, is being sent, or has an uncertain delivery result. Check its history before another send.')
        attempt = TransmittalEvent.objects.create(transmittal=transmittal, event_type='SEND_STARTED', actor=actor, metadata=metadata)
    from project_management.services.transmittal_send_proof import (
        proof_enabled, transmittal_send_marker, prepare_send_proof_safely,
    )
    marker = transmittal_send_marker(attempt) if proof_enabled() else f'links-transmittal-{transmittal.pk}-{attempt.pk}'
    try:
        if mailbox:
            message_id = _deliver_via_graph(mailbox=mailbox, sender_user=sender_identity,
                to=to_email, cc=cc, bcc=[], subject=subject, body=body,
                attachments=attachments, correlation_marker=marker)
        else:
            message_id = _deliver_via_provider(from_email=metadata['from'], to=to_email,
                cc=cc, bcc=[], subject=subject, body=body, attachments=attachments)
        result_type, result = 'SEND_SUCCEEDED', {'attempt': attempt.pk, 'provider_message_id': message_id}
    except Exception as error:
        result_type = 'SEND_FAILED'
        result = {'attempt': attempt.pk, 'error': str(error),
                  'uncertain': mailbox is None or UNCERTAIN_DELIVERY_MARKER in str(error)}
    with transaction.atomic():
        Transmittal.objects.select_for_update().get(pk=transmittal.pk)
        event = TransmittalEvent.objects.create(transmittal=transmittal, event_type=result_type,
                                               actor=actor, metadata=result)
    if event.event_type == "SEND_SUCCEEDED":
        prepare_send_proof_safely(event, attachments)
    return event
