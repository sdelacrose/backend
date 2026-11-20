"""One display/download name for controlled files; storage keys and document titles stay separate."""
import posixpath
import re


def safe_part(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or '')).strip().rstrip('.')


def issue_state_code(value):
    value = str(value or '').split(':', 1)[0].strip()
    aliases = {
        'prel': 'PREL', 'preliminary issue': 'PREL', 'envoi préliminaire': 'PREL',
        'bpe': 'BPE', 'for construction (ifc)': 'BPE', 'bon pour exécution': 'BPE',
    }
    return aliases.get(value.lower(), safe_part(value))


def controlled_file_name(reference, revision, state, original):
    original = posixpath.basename(str(original or '').replace('\\', '/'))
    reference = safe_part(reference)
    if not reference:
        return original or 'document'
    extension = posixpath.splitext(original)[1]
    stem = ' '.join(part for part in (reference, safe_part(revision), issue_state_code(state)) if part)
    return stem + extension


def revision_file_name(document, revision):
    snapshot = (revision.approval_snapshot or {}).get('document') or {}
    from project_management.services.client_surveillance_followup import display_index
    return controlled_file_name(
        snapshot.get('reference') or document.full_document_number,
        display_index(revision),
        snapshot.get('status') or getattr(revision, 'issue_state', None),
        revision.file.name or revision.file_name,
    )
