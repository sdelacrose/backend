"""MAIL-1.4-B: keeping the proof of an OUT send -- the original message (.eml) found in Sent Items and
every attachment really sent, each with its size and SHA-256 (see models/correspondence_send_proof.py).

`archive_send_proof(proof_id)` is an internal service: nothing calls it automatically (no task, no
schedule, no API in this lot) and it NEVER sends anything -- it only reads the mailbox, with the
existing read-only Graph client, and writes LINKS rows and files. The send it archives is never
touched: its event, its result and the correspondence's status are unchanged whatever happens here.

What it does, in order, stopping at the first thing it cannot trust:
  1. the proof must belong to a successful Microsoft 365 send, and nobody else may be running it;
  2. the Sent Items copy of THAT send is found (`identify_sent_message`) and its HMAC marker is
     checked again against the event, so a similar message can never be taken for it;
  3. the original message is downloaded, checked (`validate_eml`) and kept;
  4. each attachment of the copy is downloaded within the existing size/name/content limits, kept
     as an EvidenceBlob (one per company and SHA-256, never overwritten) and listed as a proof file;
  5. the attachments kept are matched to the ones the event says were sent (`attachments_snapshot`):
     by SHA-256 when the event has it, otherwise -- an event from before MAIL-1.4-A -- only by name and
     real size, which is a REDUCED level of verification, reported as such and never presented as
     full. A name alone never proves anything, and an ambiguous association is never "verified".
  6. the final state is decided here, stricter than the model's own check: ARCHIVED needs a valid
     .eml and every expected attachment kept, matched without ambiguity and verified.

Each file is committed on its own, so a run that stops half way is resumed where it stopped; a file
already kept is never downloaded or written again. Files written by a step that then fails are
removed; a blob that already existed is never touched.
"""

import logging
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field

from django.core.files import File
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from project_management.models import (
    CorrespondenceAttachment,
    CorrespondenceSendEvent,
    CorrespondenceSendProof,
    CorrespondenceSendProofFile,
    EvidenceBlob,
)
from userapis import mailbox_preview as limits
from userapis.mailbox_correlation import identify_sent_message, verify_send_marker
from userapis.mailbox_import import TEMP_FILE_MEMORY, _eml_limit
from userapis.mailbox_import_files import (
    FileRefused,
    InvalidEml,
    check_content,
    check_name_and_type,
    clean_file_name,
    eml_file_name,
    validate_eml,
)
from userapis.mailbox_read import MailboxGraphReader, MailboxReadError

logger = logging.getLogger(__name__)

State = CorrespondenceSendProof.State
Kind = CorrespondenceSendProofFile.Kind

# Namespace of this service's PostgreSQL advisory locks (first key; the proof id is the second).
ADVISORY_LOCK_NAMESPACE = 13041

# Technical, non-sensitive codes kept in CorrespondenceSendProof.error_code.
REDUCED_VERIFICATION = "reduced_verification"     # ARCHIVED, but sent attachments matched without a SHA-256
COPY_NOT_FOUND = "copy_not_found"
COPY_GONE = "copy_gone"
MARKER_MISMATCH = "marker_mismatch"
BLOB_CORRUPT = "blob_corrupt"
INVALID_EML = "invalid_eml"
EML_TOO_LARGE = "eml_too_large"
ATTACHMENT_MISSING = "attachment_missing"
ATTACHMENT_MISMATCH = "attachment_hash_mismatch"
ATTACHMENT_AMBIGUOUS = "attachment_ambiguous"
ATTACHMENT_UNEXPECTED = "unexpected_attachment"
ATTACHMENT_NOT_ALLOWED = "attachment_not_allowed"
ATTACHMENT_TOO_LARGE = "file_too_large"
ATTACHMENTS_TRUNCATED = "attachments_truncated"
UNEXPECTED_ERROR = "unexpected_error"

# Which problem is reported when several apply (the first of these that happened).
_PARTIAL_PRIORITY = (
    ATTACHMENT_MISMATCH, ATTACHMENT_AMBIGUOUS, ATTACHMENT_NOT_ALLOWED, ATTACHMENT_TOO_LARGE,
    ATTACHMENT_MISSING, ATTACHMENT_UNEXPECTED, ATTACHMENTS_TRUNCATED,
)


@dataclass
class ArchiveResult:
    """What a run did: the state and counts only -- never a name, an address or an identifier."""
    proof_id: int
    outcome: str            # archived | partial | pending | failed | busy | already_archived | not_applicable
    state: str = ""
    error_code: str = ""
    verification: str = "none"       # full | reduced | none
    files_stored: int = 0
    blobs_reused: int = 0
    problems: list = field(default_factory=list)


# --- matching what was sent against what Sent Items holds ---------------------------------------

@dataclass(frozen=True)
class Fact:
    """One attachment of the Sent Items copy, as really received."""
    graph_id: str
    name: str
    size: object        # real size; None when it was never downloaded (refused before)
    sha256: object      # None when it was never downloaded
    kept: bool          # stored as a proof file
    refusal: str = ""   # why it was not kept


def _same_name(a, b):
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def match_attachments(expected, facts):
    """Pair the attachments the event says were sent with the ones found, never by name alone.

    `expected`: the snapshot entries ({"id", "file_name", "file_size", optional "sha256"}).
    `facts`: Fact rows. Returns (pairs, missing, unmatched_facts, problems, reduced) where `pairs` is
    a list of (expected entry, Fact, level) with level "full" (same SHA-256) or "reduced" (same name
    and same real size, the event having no SHA-256); `problems` lists the codes of every divergence.
    An expected entry that cannot be paired without guessing is never paired, and is reported."""
    expected = [dict(e) for e in expected]
    free_exp = list(range(len(expected)))
    free_fact = list(range(len(facts)))
    pairs, problems = [], []

    # 1. identical SHA-256 (the name only breaks ties between identical contents).
    for j in list(free_fact):
        fact = facts[j]
        if not fact.sha256:
            continue
        candidates = [i for i in free_exp if expected[i].get("sha256") == fact.sha256]
        if not candidates:
            continue
        chosen = next((i for i in candidates if _same_name(expected[i].get("file_name"), fact.name)), candidates[0])
        pairs.append((expected[chosen], fact, "full"))
        free_exp.remove(chosen)
        free_fact.remove(j)

    # 1b. an attachment refused before any download (blocked name, too large) has no content to compare:
    #     it stands for the entry of the same name, and its refusal is reported on its own.
    for j in [j for j in free_fact if not facts[j].sha256]:
        same_name = [i for i in free_exp if _same_name(expected[i].get("file_name"), facts[j].name)]
        if same_name:
            pairs.append((expected[same_name[0]], facts[j], "refused"))
            free_exp.remove(same_name[0])
            free_fact.remove(j)

    # 2. an entry WITH a SHA-256 that meets a same-named attachment of another content is a
    #    divergence (the attachment found is still kept as information).
    for i in [i for i in free_exp if expected[i].get("sha256")]:
        same_name = [j for j in free_fact if _same_name(expected[i].get("file_name"), facts[j].name)]
        if same_name:
            free_exp.remove(i)
            free_fact.remove(same_name[0])
            problems.append(ATTACHMENT_MISMATCH)

    # 3. an entry WITHOUT a SHA-256 (sent before MAIL-1.4-A): same name and same real size, and only
    #    when exactly one entry and exactly one attachment fit each other; otherwise nothing is
    #    guessed.
    reduced = False
    for i in [i for i in free_exp if not expected[i].get("sha256")]:
        e = expected[i]
        fits = [
            j for j in free_fact
            if _same_name(e.get("file_name"), facts[j].name) and facts[j].size is not None
            and facts[j].size == e.get("file_size")
        ]
        rivals = [
            k for k in free_exp
            if not expected[k].get("sha256") and _same_name(expected[k].get("file_name"), e.get("file_name"))
            and expected[k].get("file_size") == e.get("file_size")
        ]
        if len(fits) == 1 and len(rivals) == 1:
            pairs.append((e, facts[fits[0]], "reduced"))
            free_exp.remove(i)
            free_fact.remove(fits[0])
            reduced = True
        elif fits:
            problems.append(ATTACHMENT_AMBIGUOUS)
        else:
            same_name = [j for j in free_fact if _same_name(e.get("file_name"), facts[j].name)]
            if same_name:                       # the same name with another size: not the same file
                free_exp.remove(i)
                free_fact.remove(same_name[0])
                problems.append(ATTACHMENT_MISMATCH)

    missing = [expected[i] for i in free_exp]
    unmatched = [facts[j] for j in free_fact]
    if missing:
        problems.append(ATTACHMENT_MISSING)
    if unmatched:
        problems.append(ATTACHMENT_UNEXPECTED)
    return pairs, missing, unmatched, problems, reduced


# --- the lock ----------------------------------------------------------------------------------

def _try_lock(proof_id):
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, proof_id])
        return bool(cursor.fetchone()[0])


def _unlock(proof_id):
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, proof_id])


# --- storing one file ----------------------------------------------------------------------------

class _Stop(Exception):
    """A run that cannot go on: carries the final state and code."""

    def __init__(self, state, code):
        super().__init__(code)
        self.state, self.code = state, code


def _hash_stored(blob):
    """(size, sha256) of what the blob's file really holds, or None when it cannot be read."""
    import hashlib

    digest, size = hashlib.sha256(), 0
    try:
        with blob.file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                size += len(chunk)
                digest.update(chunk)
    except Exception:  # noqa: BLE001 -- missing, unreadable: the blob is not trustworthy
        return None
    return size, digest.hexdigest()


def _blob_is_intact(blob, size, sha256):
    if blob.sha256 != sha256 or blob.file_size != size:
        return False
    stored = _hash_stored(blob)
    return stored is not None and stored == (size, sha256)


def _store_file(proof, company_id, *, kind, name, content_type, fh, size, sha256, graph_id="", source=None):
    """Keep ONE downloaded file: reuse the company's blob of this content when it is intact (never
    overwritten, never replaced), else write a new one; then add the proof file row. Commits on its
    own. Returns True when an existing blob was reused. Raises _Stop("FAILED", "blob_corrupt") when
    the blob already there is not what its hash says."""
    for attempt in (1, 2):
        blob = EvidenceBlob.objects.filter(company_profile_id=company_id, sha256=sha256).first()
        written = None
        try:
            if blob is not None:
                if not _blob_is_intact(blob, size, sha256):
                    raise _Stop(State.FAILED, BLOB_CORRUPT)
                reused = True
            else:
                reused = False
                blob = EvidenceBlob(
                    company_profile_id=company_id, sha256=sha256, file_size=size,
                    content_type=str(content_type or "")[:255],
                )
                fh.seek(0)
                blob.file.save(sha256, File(fh), save=False)        # the write to storage happens here
                written = (blob.file.storage, blob.file.name)
            with transaction.atomic():
                if written is not None:
                    blob.save()
                CorrespondenceSendProofFile.objects.create(
                    proof=proof, kind=kind, file_name=name, content_type=str(content_type or "")[:255],
                    file_size=size, sha256=sha256, graph_attachment_id=graph_id,
                    source_attachment=source, blob=blob,
                )
            return reused
        except IntegrityError:
            # another process stored the same blob or the same file first: undo only OUR write, look again
            _remove_written(written)
            if attempt == 2:
                raise
            continue
        except Exception:
            _remove_written(written)
            raise
    return False


def _remove_written(written):
    """Delete the file THIS step wrote, unless a blob row now owns that exact name."""
    if written is None:
        return
    storage, name = written
    try:
        if not EvidenceBlob.objects.filter(file=name).exists():
            storage.delete(name)
    except Exception:  # noqa: BLE001 -- a failed delete must not hide the original error
        logger.warning("Could not remove a file written by a failed send proof step")


def _temp_file(stack):
    return stack.enter_context(tempfile.SpooledTemporaryFile(max_size=TEMP_FILE_MEMORY))


def _provisional_source(event, expected, used, name, size, sha256):
    """The CorrespondenceAttachment this file was probably sent from (information only, the proof
    never depends on it): same SHA-256, else same name and size -- and only if it still exists."""
    for entry in expected:
        if entry.get("id") in used:
            continue
        if entry.get("sha256") and entry.get("sha256") == sha256:
            pass
        elif not entry.get("sha256") and _same_name(entry.get("file_name"), name) and entry.get("file_size") == size:
            pass
        else:
            continue
        used.add(entry.get("id"))
        return CorrespondenceAttachment.objects.filter(
            pk=entry.get("id"), correspondence_id=event.correspondence_id
        ).first()
    return None


# --- the service ---------------------------------------------------------------------------------

def _finish(proof, state, code=""):
    proof.state = state
    proof.error_code = code
    if state == State.ARCHIVED:
        proof.archived_at = timezone.now()
        proof.next_attempt_at = None        # nothing left to retry (MAIL-1.4-C scheduling)
    # only these columns: the link to the Sent Items copy may have been made through another
    # instance of the proof meanwhile, and a whole-row save would write the old value back
    proof.save(update_fields=["state", "error_code", "archived_at", "next_attempt_at", "updated_at"])


def archive_send_proof(proof_id, *, actor=None, reader=None):
    """Keep the proof of the send `proof_id` stands for. See the module docstring. `actor` is the
    person whose right to READ the mailbox is checked (default: whoever sent -- who must still be
    allowed to read it, sending and reading being two different rights); `reader` is for tests."""
    proof = CorrespondenceSendProof.objects.select_related(
        "event", "event__mailbox", "event__sender", "event__correspondence", "mailbox_message",
    ).filter(pk=proof_id).first()
    if proof is None:
        return ArchiveResult(proof_id, "not_applicable", error_code="no_such_proof")
    event = proof.event
    if (
        event.result != CorrespondenceSendEvent.Result.SUCCESS
        or event.provider != "graph" or event.mailbox_id is None
    ):
        return ArchiveResult(proof_id, "not_applicable", state=proof.state, error_code="not_a_graph_send")
    if proof.state == State.ARCHIVED:
        return ArchiveResult(proof_id, "already_archived", state=proof.state, verification=_level(proof))
    if not _try_lock(proof.pk):
        return ArchiveResult(proof_id, "busy", state=proof.state)
    try:
        proof.refresh_from_db()
        if proof.state == State.ARCHIVED:                       # finished by the run that held the lock
            return ArchiveResult(proof_id, "already_archived", state=proof.state, verification=_level(proof))
        return _run(proof, event, actor, reader)
    finally:
        _unlock(proof.pk)


def _level(proof):
    if proof.state != State.ARCHIVED:
        return "none"
    return "reduced" if proof.error_code == REDUCED_VERIFICATION else "full"


def _run(proof, event, actor, reader):
    mailbox = event.mailbox
    result = ArchiveResult(proof.pk, "failed")
    proof.attempts = min(proof.attempts + 1, 32767)
    proof.save(update_fields=["attempts", "updated_at"])
    try:
        actor = actor or event.sender
        if reader is None:
            if actor is None:
                raise MailboxReadError("not_authorized")
            reader = MailboxGraphReader(mailbox, actor=actor)       # re-authorizes the READ right
        return _archive(proof, event, mailbox, actor, reader, result)
    except _Stop as stop:
        _finish(proof, stop.state, stop.code)
        return _result(result, proof, "failed" if stop.state == State.FAILED else "pending")
    except MailboxReadError as exc:
        _finish(proof, State.FAILED, exc.code)
        return _result(result, proof, "failed")
    except Exception:  # noqa: BLE001 -- the send stands whatever happens here; nothing but the id is logged
        logger.error("Send proof %s could not be archived", proof.pk, exc_info=False)
        _finish(proof, State.FAILED, UNEXPECTED_ERROR)
        return _result(result, proof, "failed")


def _result(result, proof, outcome):
    result.outcome, result.state, result.error_code = outcome, proof.state, proof.error_code
    result.verification = _level(proof)
    return result


def _archive(proof, event, mailbox, actor, reader, result):
    # 2. THIS send's copy, authenticated by its marker
    try:
        row = identify_sent_message(event, actor=actor, reader=reader)
    except MailboxReadError as exc:
        if exc.code == "not_found":
            raise _Stop(State.PENDING, COPY_NOT_FOUND)
        raise
    if row is None:
        raise _Stop(State.PENDING, COPY_NOT_FOUND)
    graph_id = row.graph_message_id
    identification = reader.get_reply_identification(graph_id)
    if not verify_send_marker(identification.send_marker, event):
        raise _Stop(State.FAILED, MARKER_MISMATCH)
    proof.refresh_from_db()
    if proof.mailbox_message_id != row.pk:
        raise _Stop(State.FAILED, MARKER_MISMATCH)               # linked to a different copy: never overwritten

    company_id = mailbox.company_profile_id
    kept = {f.graph_attachment_id or f.kind: f for f in proof.files.all()}
    total = sum(f.file_size for f in kept.values())
    expected = [dict(e) for e in (event.attachments_snapshot or [])]
    used_sources = set()

    with ExitStack() as stack:
        # 3. the original message
        if Kind.EML not in kept:
            fh = _temp_file(stack)
            cap = min(_eml_limit(), limits.MAX_TOTAL_BYTES - total)
            try:
                size, digest = reader.download_message_mime(graph_id, fh, cap)
            except MailboxReadError as exc:
                if exc.code == "content_too_large":
                    raise _Stop(State.FAILED, EML_TOO_LARGE)
                if exc.code == "not_found":
                    raise _Stop(State.PENDING, COPY_GONE)
                raise
            try:
                if size <= 0:
                    raise InvalidEml()
                validate_eml(fh)
            except InvalidEml:
                raise _Stop(State.FAILED, INVALID_EML)
            reused = _store_file(
                proof, company_id, kind=Kind.EML, name=eml_file_name(row.subject or event.subject),
                content_type="message/rfc822", fh=fh, size=size, sha256=digest,
            )
            result.files_stored += 1
            result.blobs_reused += int(reused)
            total += size
        # 4. the attachments of the copy
        listing = reader.list_attachments(graph_id)
        facts, refusals = [], []
        count = len([k for k in kept if k != Kind.EML])
        for meta in listing.attachments:
            already = kept.get(meta.graph_id)
            if already is not None:
                facts.append(Fact(meta.graph_id, already.file_name, already.file_size, already.sha256, True))
                _note_source(used_sources, already)
                continue
            fact = _archive_one(
                proof, event, company_id, reader, graph_id, meta, expected, used_sources, stack,
                total, count, result,
            )
            facts.append(fact)
            if fact.kept:
                total += fact.size
                count += 1
            else:
                refusals.append(fact.refusal)

    # 5/6. what was sent against what was kept, and the final state
    pairs, missing, unmatched, problems, reduced = match_attachments(expected, facts)
    problems = list(problems)
    for refusal in refusals:
        problems.append(refusal)
    if listing.truncated:
        problems.append(ATTACHMENTS_TRUNCATED)
    result.problems = [p for p in _PARTIAL_PRIORITY if p in problems]
    if result.problems:
        _finish(proof, State.PARTIAL, result.problems[0])
        return _result(result, proof, "partial")
    _finish(proof, State.ARCHIVED, REDUCED_VERIFICATION if reduced else "")
    return _result(result, proof, "archived")


def _note_source(used, kept_file):
    if kept_file.source_attachment_id:
        used.add(kept_file.source_attachment_id)


def _archive_one(proof, event, company_id, reader, graph_id, meta, expected, used_sources, stack, total, count, result):
    """Download and keep one attachment of the copy; a Fact says what happened. A refused file is not
    kept and is reported; a technical failure stops the run (and what was kept stays kept)."""
    declared = meta.size if isinstance(meta.size, int) and meta.size >= 0 else None
    if meta.kind != "file" or meta.is_inline:
        return Fact(meta.graph_id, meta.name, None, None, False, ATTACHMENT_NOT_ALLOWED)
    try:
        name = check_name_and_type(meta.name, meta.content_type)
    except FileRefused:
        return Fact(meta.graph_id, meta.name, None, None, False, ATTACHMENT_NOT_ALLOWED)
    if count >= limits.MAX_FILES or (declared is not None and declared > limits.MAX_FILE_BYTES):
        return Fact(meta.graph_id, name, declared, None, False, ATTACHMENT_TOO_LARGE)
    fh = _temp_file(stack)
    cap = min(limits.MAX_FILE_BYTES, limits.MAX_TOTAL_BYTES - total)
    try:
        size, digest = reader.download_attachment(graph_id, meta.graph_id, fh, cap)
    except MailboxReadError as exc:
        if exc.code == "content_too_large":
            return Fact(meta.graph_id, name, declared, None, False, ATTACHMENT_TOO_LARGE)
        if exc.code == "not_found":
            return Fact(meta.graph_id, name, declared, None, False, ATTACHMENT_MISSING)
        raise
    try:
        check_content(fh, size)                      # what was really received: not empty, not a program
    except FileRefused:
        return Fact(meta.graph_id, name, size, digest, False, ATTACHMENT_NOT_ALLOWED)
    source = _provisional_source(event, expected, used_sources, name, size, digest)
    reused = _store_file(
        proof, company_id, kind=Kind.ATTACHMENT, name=name, content_type=meta.content_type, fh=fh,
        size=size, sha256=digest, graph_id=meta.graph_id, source=source,
    )
    result.files_stored += 1
    result.blobs_reused += int(reused)
    return Fact(meta.graph_id, name, size, digest, True)
