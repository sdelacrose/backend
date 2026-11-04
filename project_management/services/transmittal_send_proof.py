"""Prepared copies and verified Microsoft originals of accepted transmittal sends.

Disabled until explicitly activated after migration. No mailbox read or resend is
scheduled by preparation; original retrieval is a separate authorized action.
"""
import hashlib
import hmac
import logging
import tempfile
from collections import Counter
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.utils import timezone

from project_management.models import EvidenceBlob, TransmittalEvent, TransmittalSendProof, TransmittalSendProofFile
from userapis.mailbox_read import MailboxGraphReader, MailboxReadError, authorize_mailbox_read
from userapis.models import MailboxConnection

logger = logging.getLogger(__name__)


def proof_enabled():
    return bool(getattr(settings, 'TRANSMITTAL_PROOF_ENABLED', False))


def _limit():
    return min(max(int(getattr(settings, 'TRANSMITTAL_PROOF_MAX_BYTES', 25 * 1024 * 1024)), 1), 100 * 1024 * 1024)


def transmittal_send_marker(attempt):
    value = f'transmittal-send:{attempt.pk}:{attempt.transmittal_id}'.encode('ascii')
    signature = hmac.new(settings.SECRET_KEY.encode('utf8'), value, hashlib.sha256).hexdigest()
    return f'tr.{attempt.pk}.{signature}'


def _intact(blob):
    try:
        with blob.file.open('rb') as stream:
            data = stream.read(_limit() + 1)
        return len(data) == blob.file_size and hashlib.sha256(data).hexdigest() == blob.sha256
    except (OSError, ValueError):
        return False


def _store(proof, kind, name, content, revision_id=None):
    if len(content) > _limit():
        raise ValueError('file_too_large')
    digest = hashlib.sha256(content).hexdigest()
    company_id = proof.event.transmittal.project.company_profile_id
    if not company_id:
        raise ValueError('company_required')
    blob = None
    for _ in range(3):
        blob = EvidenceBlob.objects.filter(company_profile_id=company_id, sha256=digest).first()
        if blob:
            if not _intact(blob):
                raise ValueError('blob_corrupt')
            break
        candidate = EvidenceBlob(company_profile_id=company_id, sha256=digest, file_size=len(content),
                                 content_type='message/rfc822' if kind != 'attachment' else 'application/octet-stream')
        candidate.file.save(digest, ContentFile(content), save=False)
        try:
            with transaction.atomic():
                candidate.save()
            blob = candidate
            break
        except IntegrityError:
            if not EvidenceBlob.objects.filter(file=candidate.file.name).exists():
                candidate.file.storage.delete(candidate.file.name)
        except Exception:
            if not EvidenceBlob.objects.filter(file=candidate.file.name).exists():
                candidate.file.storage.delete(candidate.file.name)
            raise
    if blob is None:
        raise ValueError('storage_conflict')
    with transaction.atomic():
        locked = TransmittalSendProof.objects.select_for_update().get(pk=proof.pk)
        existing = locked.files.filter(kind=kind, source_revision_id=revision_id).first()
        if existing:
            if existing.sha256 != digest or existing.file_size != len(content):
                raise ValueError('proof_file_mismatch')
            return existing
        return TransmittalSendProofFile.objects.create(proof=locked, kind=kind, file_name=str(name)[:500],
            content_type=blob.content_type, file_size=len(content), sha256=digest, blob=blob, source_revision_id=revision_id)


def _finish(proof_id, state, code='', graph_id=''):
    with transaction.atomic():
        proof = TransmittalSendProof.objects.select_for_update().get(pk=proof_id)
        if proof.state == 'ARCHIVED':
            return proof
        proof.state, proof.error_code = state, code
        if graph_id:
            proof.graph_message_id = graph_id
        if state == 'ARCHIVED':
            proof.archived_at = timezone.now()
        proof.save()
        return proof


def prepare_send_proof(event, attachments):
    if not proof_enabled() or event.event_type != 'SEND_SUCCEEDED':
        return None
    attempt = TransmittalEvent.objects.get(pk=(event.metadata or {}).get('attempt'), transmittal=event.transmittal, event_type='SEND_STARTED')
    meta = attempt.metadata or {}
    if meta.get('provider') not in {'smtp', 'graph'}:
        return None
    proof, _ = TransmittalSendProof.objects.get_or_create(event=event, defaults={'attempt': attempt, 'provider': meta['provider']})
    if proof.state == 'ARCHIVED':
        return proof
    expected = meta.get('attachments') or []
    if len(expected) != len(attachments) or not expected:
        raise ValueError('attachment_mismatch')
    message = EmailMultiAlternatives(subject=meta['subject'], body=meta['body'], from_email=meta['from'],
                                     to=[meta['to']], cc=meta.get('cc') or [])
    total = 0
    for item, attachment in zip(expected, attachments):
        with attachment.file.open('rb') as stream:
            content = stream.read(_limit() + 1)
        total += len(content)
        if total > _limit() or hashlib.sha256(content).hexdigest() != item.get('sha256'):
            raise ValueError('attachment_mismatch')
        _store(proof, 'attachment', item['name'], content, item['document_revision'])
        message.attach(item['name'], content)
    prepared = message.message().as_bytes()
    _store(proof, 'prepared', 'links-prepared-message.eml', prepared)
    return _finish(proof.pk, 'ARCHIVED' if proof.provider == 'smtp' else 'PENDING')


def prepare_send_proof_safely(event, attachments):
    if not proof_enabled():
        return None
    try:
        return prepare_send_proof(event, attachments)
    except Exception:
        logger.error('Transmittal proof preparation failed for accepted send event %s; send will not be repeated', event.pk)
        try:
            proof = TransmittalSendProof.objects.filter(event=event).first()
            if proof:
                return _finish(proof.pk, 'FAILED', 'preparation_failed')
        except Exception:
            logger.error('Transmittal proof failure could not be recorded for event %s', event.pk)
        return None


def archive_graph_original(proof_id, *, actor, reader=None):
    """Read only; never send. An injected reader is solely for isolated tests."""
    proof = TransmittalSendProof.objects.select_related('event__transmittal__project', 'attempt').get(pk=proof_id)
    if proof.provider != 'graph' or proof.state == 'ARCHIVED':
        return proof
    meta = proof.attempt.metadata or {}
    transmittal = proof.event.transmittal
    mailbox = MailboxConnection.objects.filter(pk=meta.get('mailbox_id'), company_profile_id=transmittal.project.company_profile_id).first()
    # Reauthorize even when a test reader is injected. No sender credential delegation.
    authorize_mailbox_read(actor, mailbox, project=transmittal.project_id)
    reader = reader or MailboxGraphReader(mailbox, actor=actor, project=transmittal.project_id)
    marker = transmittal_send_marker(proof.attempt)
    try:
        page = reader.list_messages('sentitems', top=20, received_since=proof.attempt.created_at-timedelta(minutes=1),
                                    received_until=proof.event.created_at+timedelta(minutes=5), subject=meta['subject'])
        matches = []
        for candidate in page.messages[:20]:
            identification = reader.get_reply_identification(candidate.graph_id)
            if identification.send_marker and hmac.compare_digest(str(identification.send_marker).strip(), marker):
                matches.append(candidate.graph_id)
        if len(matches) > 1:
            return _finish(proof.pk, 'FAILED', 'ambiguous_copy')
        if not matches or page.next_link:
            return _finish(proof.pk, 'PENDING', 'copy_not_found' if not matches else 'search_incomplete')
        graph_id = matches[0]
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as stream:
            reader.download_message_mime(graph_id, stream, _limit())
            stream.seek(0)
            raw = stream.read(_limit() + 1)
        if not raw or len(raw) > _limit():
            return _finish(proof.pk, 'FAILED', 'message_too_large')
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if message.defects or not hmac.compare_digest(str(message.get('x-links-send', '')).strip(), marker):
            return _finish(proof.pk, 'FAILED', 'marker_mismatch')
        recipients = {address.strip().lower() for _, address in getaddresses(message.get_all('To', []) + message.get_all('Cc', []))}
        expected_recipients = {str(value).strip().lower() for value in [meta['to'], *(meta.get('cc') or [])]}
        if str(message.get('Subject', '')) != meta['subject'] or recipients != expected_recipients:
            return _finish(proof.pk, 'FAILED', 'message_mismatch')
        actual = []
        for part in message.iter_attachments():
            content = part.get_payload(decode=True)
            if content is None:
                return _finish(proof.pk, 'FAILED', 'attachment_mismatch')
            actual.append((part.get_filename() or '', hashlib.sha256(content).hexdigest(), content))
        expected = meta.get('attachments') or []
        if Counter((item['name'], item['sha256']) for item in expected) != Counter((name, digest) for name, digest, _ in actual):
            return _finish(proof.pk, 'FAILED', 'attachment_mismatch')
        _store(proof, 'original', 'microsoft-original-message.eml', raw)
        remaining = list(actual)
        for item in expected:
            index = next(i for i, (name, digest, _) in enumerate(remaining) if name == item['name'] and digest == item['sha256'])
            _, _, content = remaining.pop(index)
            _store(proof, 'attachment', item['name'], content, item['document_revision'])
        return _finish(proof.pk, 'ARCHIVED', graph_id=graph_id)
    except MailboxReadError as error:
        return _finish(proof.pk, 'FAILED', error.code)
    except Exception:
        logger.error('Transmittal original archive failed for proof %s; no send performed', proof.pk)
        return _finish(proof.pk, 'FAILED', 'archive_failed')
