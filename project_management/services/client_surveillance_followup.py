"""LOT 1: automated follow-up of a client return (A/B/C/D) of an INTERNAL document.

ONE business service. It is called by three events and never by a screen click that "closes":
- the explicit confirmation of the retained return -> `confirm_retained_return` (LOT 2: the ONLY place that opens a
                                                   follow-up; recording or importing a return opens nothing);
- the file of the derived revision               -> `provide_file` (explicit filiation return -> revision);
- the internal approval of a revision            -> `on_revision_approved` (trace only: the step is derived);
- a really successful OUT send (SEND_SUCCEEDED)  -> `process_send_event` (the ONLY place that closes).

Rules (the category of the retained return decides):
- A  ACCEPTED               same index, a BPE technical version (index_revision -> the approved index), NO new
                            validation, sent FOR_INFORMATION; APPLICABLE only after the verified BPE send (LOT 3
                            wiring). The BPE file is always uploaded explicitly and may be byte-identical to the PREL one.
- B  ACCEPTED_WITH_COMMENTS next index, BPE, FULL internal validation, sent FOR_INFORMATION; applicable after the
                            verified send, then closed.
- C/D COMMENTED / REJECTED  next index, PREL, FULL internal validation, sent FOR_REVIEW; the verified send closes
                            the old surveillance and the existing opening mechanism (same send) opens the new cycle.
PREL/BPE (`DocumentRevision.issue_state`) and the distribution purpose are two separate data.

Nothing here closes without: the filiation (follow-up row whose derived revision is the sent revision), the right
client, the right purpose, the right issue state, the internal approval when required, and a SEND_SUCCEEDED
(never SEND_FAILED / SEND_STARTED / issued-only). Every effect is idempotent (unique rows + event dedupe), traced
as an immutable ClientSurveillanceEvent, and can be replayed after a failure (`process_send_event` is safe to call
again; failures never undo a send or a return).
"""
import logging

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from project_management.models import (
    ClientSurveillance, ClientSurveillanceEvent, ClientSurveillanceFollowUp, ClientSurveillanceReturn, DocumentRevision,
    Transmittal, TransmittalEvent, TransmittalItem,
)
from project_management.models.client_surveillance_code import ClientSurveillanceCode

logger = logging.getLogger(__name__)

Category = ClientSurveillanceCode.Category
EVENT = ClientSurveillanceEvent.EventType
STATUS = ClientSurveillance.Status
PURPOSE = TransmittalItem.DistributionPurpose

STATE_OF_CATEGORY = {
    Category.ACCEPTED: "BPE", Category.ACCEPTED_WITH_COMMENTS: "BPE",
    Category.COMMENTED: "PREL", Category.REJECTED: "PREL",
}
SAME_INDEX = {Category.ACCEPTED}
APPLICABLE = {Category.ACCEPTED, Category.ACCEPTED_WITH_COMMENTS}
# distribution purpose each issue state must be sent with (BPE never opens a surveillance)
PURPOSE_OF_STATE = {"BPE": PURPOSE.FOR_INFORMATION, "PREL": PURPOSE.FOR_REVIEW}

# derived next business steps
AWAITING_BPE_FILE, AWAITING_FILE = "EN_ATTENTE_FICHIER_BPE", "EN_ATTENTE_FICHIER"
AWAITING_VALIDATION, AWAITING_DISTRIBUTION = "EN_ATTENTE_VALIDATION", "EN_ATTENTE_DIFFUSION"
CLOSED, NEEDS_REVIEW = "CLOTURE", "REVUE_EXPLICITE"


def is_automated_document(document):
    """Internal documents only: the supplier circuit is unchanged by this lot."""
    return getattr(document, "document_origin", None) == "INTERNAL"


def category_of(followup):
    return followup.client_return.category


def _event(surveillance, actor, event_type, data):
    return ClientSurveillanceEvent.objects.create(
        surveillance=surveillance, actor=actor, status=surveillance.status, result=surveillance.result,
        event_type=event_type, data=data,
    )


def _iso(value):
    return value.isoformat() if value else None


def _review_needed(surveillance, actor, reason, data=None):
    """One immutable REVIEW_NEEDED event per (reason, send event / return), never duplicated by a replay."""
    data = {"reason": reason, **(data or {})}
    key = {k: data[k] for k in ("reason", "send_event", "return") if k in data}
    for event in surveillance.events.filter(event_type=EVENT.REVIEW_NEEDED, data__reason=reason):
        if all((event.data or {}).get(k) == v for k, v in key.items()):
            return None
    return _event(surveillance, actor, EVENT.REVIEW_NEEDED, data)


# --- registration of a return ------------------------------------------------------------------------------

def expected_index_for(category, issue):
    """The documentary index the answer to a return is expected to carry: the index of the accepted revision for code
    A, the next official index otherwise (read only: it is only given at internal approval)."""
    source = issue.document_revision
    if category in SAME_INDEX:
        root = source.index_revision if source.index_revision_id else source
        return root.revision_label or None
    from project_management.services.document_revision import expected_next_revision_label
    return expected_next_revision_label(source.document)


RECONFIRM = "reconfirmation_required"


def register_return(record, actor, superseded=None):
    """LOT 2: recording or importing a return opens NO follow-up -- the retained return is confirmed explicitly
    (`confirm_retained_return`). A CORRECTION of the retained return never lets the processing continue on its own:
    - nothing produced yet: the previous confirmation is invalidated (the follow-up waits for an explicit
      RECONFIRMATION of the corrected return; no second follow-up is created, the history is kept as events);
    - a file already produced: explicit review, the produced version is neither deleted nor replaced."""
    if superseded is None:
        return None
    issue = record.client_issue
    if not is_automated_document(issue.document_revision.document):
        return None
    surveillance = ClientSurveillance.objects.select_for_update().filter(client_issue=issue).first()
    if surveillance is None or surveillance.status == STATUS.CLOSED:
        return None  # late / historical: kept as a fact only (RETURN_LATE), nothing is opened or reopened
    old = ClientSurveillanceFollowUp.objects.select_for_update().filter(client_return=superseded).first()
    if old is None:
        return None  # the replaced return was never retained: the new one must be confirmed like any other
    data = {"return": record.pk, "replaced_return": superseded.pk, "followup": old.pk}
    if old.derived_revision_id or old.closed_at:
        old.needs_review = "return_replaced_after_followup"
        old.save(update_fields=["needs_review"])
        _review_needed(surveillance, actor, "return_replaced_after_followup", data)
        return None
    old.needs_review, old.applicable_at = RECONFIRM, None
    old.save(update_fields=["needs_review", "applicable_at"])
    _review_needed(surveillance, actor, RECONFIRM, {**data, "previous_category": superseded.category, "new_category": record.category})
    return None


def retainable_returns(issue):
    """The returns of this distribution that can be retained: categorised, not replaced by a correction, not already
    retained (a retained return owns a follow-up)."""
    return [r for r in ClientSurveillanceReturn.objects.filter(client_issue=issue, category__in=list(STATE_OF_CATEGORY), followup__isnull=True)
            .order_by("received_at", "pk") if not r.corrections.exists()]


def _refuse(code, message):
    raise ValidationError({"detail": message, "code": code})


def confirm_retained_return(issue, actor, return_id=None, automatic=False):
    """LOT 2: the explicit, one-shot business operation "this return is the retained one". One candidate -> no
    selection needed; several -> `return_id` is mandatory; none -> nothing is opened. Refuses a second confirmation,
    a second open follow-up for the same document and client, a closed surveillance and a non internal document.
    The follow-up it opens is the ONLY definitive documentary processing of a return."""
    with transaction.atomic():
        surveillance = ClientSurveillance.objects.select_for_update().filter(client_issue=issue).first()
        if surveillance is None:
            _refuse("no_surveillance", "This distribution has no client surveillance to follow up.")
        if not is_automated_document(issue.document_revision.document):
            _refuse("not_internal", "Only internal documents follow the automated client surveillance.")
        if surveillance.status == STATUS.CLOSED:
            _refuse("surveillance_closed", "This client surveillance is closed: a return can no longer be retained.")
        reconfirm = None
        pending = ClientSurveillanceFollowUp.objects.select_for_update().filter(surveillance=surveillance, closed_at__isnull=True).first()
        if pending is not None:
            if pending.needs_review == RECONFIRM and not pending.derived_revision_id:
                reconfirm = pending  # the retained return was corrected before any file: this call is the reconfirmation
            elif pending.needs_review:
                _refuse("followup_in_review", "This follow-up is blocked by an explicit review (%s): resolve it before retaining a return." % pending.needs_review)
            else:
                _refuse("already_confirmed", "A retained return is already being processed for this surveillance.")
        candidates = retainable_returns(issue)
        if return_id is None:
            if not candidates:
                _refuse("no_return", "There is no categorised return to retain.")
            if len(candidates) > 1:
                _refuse("selection_required", "Several returns can be retained (%s): select the one to retain." % ", ".join(str(c.pk) for c in candidates))
            record = candidates[0]
        else:
            record = ClientSurveillanceReturn.objects.filter(pk=return_id, client_issue=issue).first()
            if record is None:
                _refuse("unknown_return", "This return does not belong to this distribution.")
            if record.corrections.exists():
                _refuse("return_replaced", "This return has been replaced by a correction: retain the corrected return.")
            if ClientSurveillanceFollowUp.objects.filter(client_return=record).exists():
                _refuse("already_confirmed", "This return has already been retained.")
            if record.category not in STATE_OF_CATEGORY:
                _refuse("no_category", "This return has no A/B/C/D category: qualify it before retaining it.")
        document = issue.document_revision.document
        if ClientSurveillanceFollowUp.objects.filter(document=document, client_company_id=issue.client_company_id, closed_at__isnull=True).exclude(
                pk=reconfirm.pk if reconfirm else None).exists():
            _refuse("followup_conflict", "Another client follow-up is already open for this document and this client.")
        expected = expected_index_for(record.category, issue)
        if record.category in SAME_INDEX and not expected:
            _refuse("index_not_approved", "The accepted revision has no approved index to keep for the BPE version.")
        state = STATE_OF_CATEGORY[record.category]
        previous_return = reconfirm.client_return_id if reconfirm else None
        try:
            with transaction.atomic():
                if reconfirm is not None:  # same follow-up, re-pointed: never a second one
                    followup = reconfirm
                    followup.client_return, followup.expected_state, followup.expected_index = record, state, expected
                    followup.needs_review, followup.applicable_at = "", None
                    followup.save(update_fields=["client_return", "expected_state", "expected_index", "needs_review", "applicable_at"])
                else:
                    followup = ClientSurveillanceFollowUp.objects.create(
                        client_return=record, surveillance=surveillance, expected_state=state, expected_index=expected, created_by=actor)
        except IntegrityError:
            _refuse("followup_conflict", "Another client follow-up is already open for this document and this client.")
        _event(surveillance, actor, EVENT.FU_OPENED, {
            "followup": followup.pk, "return": record.pk, "category": record.category, "code": record.code_snapshot,
            "expected_state": state, "expected_index": expected, "same_index": record.category in SAME_INDEX,
            "source_revision": issue.document_revision_id, "retained_by": actor.pk if actor else None,
            "explicit_selection": return_id is not None and not automatic, "candidates": [c.pk for c in candidates],
            "reconfirmed": reconfirm is not None, "previous_return": previous_return,
            # LOT C: an automatic confirmation is traced as such (the actor is the user who recorded the return)
            "automatic": bool(automatic), **({"automatic_reason": "single_unambiguous_return"} if automatic else {}),
        })
        return followup


def auto_retain_return(record, actor):
    """LOT C: record AND retain in one action, only when nothing is ambiguous: the return is categorised, is not a
    correction and is the ONLY categorised return of its distribution (retained or not); the other conditions (internal
    document, open surveillance, no follow-up pending, approved index for code A) are the ones of the explicit
    confirmation, which is called as is. Anything else keeps the explicit confirmation and says why.
    Returns {"retained": bool, "followup": ClientSurveillanceFollowUp|None, "reason": str|None}."""
    def kept(reason):
        return {"retained": False, "followup": None, "reason": reason}
    if record.supersedes_id:
        return kept("correction")  # a corrected return is always confirmed explicitly (see register_return)
    if record.category not in STATE_OF_CATEGORY:
        return kept("no_category")
    issue = record.client_issue
    others = [r for r in ClientSurveillanceReturn.objects.filter(client_issue=issue, category__in=list(STATE_OF_CATEGORY)).exclude(pk=record.pk)
              if not r.corrections.exists()]
    if others:
        return kept("several_returns")
    try:
        followup = confirm_retained_return(issue, actor, record.pk, automatic=True)
    except ValidationError as error:
        data = error.message_dict if hasattr(error, "message_dict") else {}
        return kept((data.get("code") or ["not_retained"])[0])
    return {"retained": True, "followup": followup, "reason": None}


def auto_retain_safely(record, actor):
    """The return is already a fact: a failure here is logged and leaves it to the explicit confirmation."""
    try:
        with transaction.atomic():
            return auto_retain_return(record, actor)
    except Exception:
        logger.exception("Automatic retention failed for return %s", getattr(record, "pk", None))
        return {"retained": False, "followup": None, "reason": "error"}


def register_return_safely(record, actor, superseded=None):
    """The return is already a fact: a failure here is logged and recovered by `ensure_followup`, never raised."""
    try:
        with transaction.atomic():
            return register_return(record, actor, superseded)
    except Exception:
        logger.exception("Client follow-up could not be opened for return %s", getattr(record, "pk", None))
        return None


# --- the file of the derived revision ----------------------------------------------------------------------

def _known_hashes(document):
    from project_management.services.document_revision import _hash_of_stored_version
    hashes = set()
    for revision in DocumentRevision.objects.filter(document=document):
        digest = revision.file_sha256
        if not digest:
            try:
                digest = _hash_of_stored_version(revision)
            except Exception as error:
                raise ValidationError({"file": "A previous file cannot be read to prove that the new file is different."}) from error
        hashes.add(digest)
    return hashes


def replaceable(followup):
    """The derived revision may be corrected only while it is neither validated nor distributed."""
    revision = followup.derived_revision
    if revision is None:
        return True
    if followup.closed_at or revision.revision_label or revision.transmittal_items.exists():
        return False
    from project_management.services.document_workflow import revision_has_active_workflow
    return not revision_has_active_workflow(revision)


def provide_file(followup, actor, uploaded_file, comment="", confirm_bpe=False):
    """Store the physical file that answers the return (BPE technical version of the SAME index for A, next index
    otherwise), with the immutable issue state, and record the filiation. The previous files are never touched and
    never picked automatically: code A needs an explicit upload AND the explicit confirmation of the BPE status."""
    from project_management.services.document_revision import MAJOR, MINOR, add_document_revision, expected_next_revision_label, sha256_of_file
    if uploaded_file is None:
        raise ValidationError({"file": "A file is required."})
    with transaction.atomic():
        locked = ClientSurveillanceFollowUp.objects.select_for_update(of=("self",)).select_related(
            "client_return__client_issue__document_revision__document", "surveillance", "derived_revision").get(pk=followup.pk)
        document = locked.client_return.client_issue.document_revision.document
        if locked.closed_at:
            raise ValidationError({"detail": "This follow-up is closed."})
        if locked.needs_review:
            raise ValidationError({"detail": "This follow-up needs an explicit review first."})
        if locked.client_return.corrections.exists():
            raise ValidationError({"detail": "The return of this follow-up has been corrected."})
        if not is_automated_document(document):
            raise ValidationError({"detail": "Only internal documents follow the automated client surveillance."})
        if not replaceable(locked):
            raise ValidationError({"file": "The provided revision is already validated or distributed and cannot be replaced."})
        same_index = category_of(locked) in SAME_INDEX
        source = locked.client_return.client_issue.document_revision
        index_revision = None
        if same_index:
            # Code A: the client accepted the approved index; the BPE file is a TECHNICAL version of it (index_revision).
            if not confirm_bpe:
                raise ValidationError({"confirm_bpe": "Confirm explicitly that the uploaded file is the BPE version of the accepted index.", "code": "bpe_confirmation_required"})
            index_revision = source.index_revision if source.index_revision_id else source
            if not index_revision.revision_label:
                raise ValidationError({"detail": "The accepted revision has no approved index to keep.", "code": "index_not_approved"})
            expected = index_revision.revision_label
            if locked.expected_index and locked.expected_index != expected:
                raise ValidationError({"detail": "The accepted index (%s) is no longer the one expected by this follow-up (%s)." % (expected, locked.expected_index), "code": "index_conflict"})
        else:
            # The other codes need a modified file, never a previous one -- and a new official index at approval.
            if sha256_of_file(uploaded_file) in _known_hashes(document):
                raise ValidationError({"file": "This file is identical to a previous file of the document. Provide the expected file."})
            expected = expected_next_revision_label(document)
        previous_expected = locked.expected_index
        revision = add_document_revision(
            document, uploaded_file, actor, revision_type=MINOR if same_index else MAJOR,
            comment=(comment or "").strip() or ("Client return %s: %s file" % (locked.client_return.code_snapshot, locked.expected_state)),
            issue_state=locked.expected_state, allow_identical=same_index, index_revision=index_revision,
        )
        locked.expected_index = expected  # not yet reserved: an official index is only given at internal approval
        previous = locked.derived_revision_id
        locked.derived_revision = revision
        locked.save(update_fields=["derived_revision", "expected_index"])
        _event(locked.surveillance, actor, EVENT.FU_FILE, {
            "followup": locked.pk, "return": locked.client_return_id, "revision": revision.pk, "version_number": revision.version_number,
            "issue_state": locked.expected_state, "sha256": revision.file_sha256, "file_name": revision.file_name,
            "replaced_revision": previous,
            "index_revision": index_revision.pk if index_revision else None, "expected_index": expected, "previous_expected_index": previous_expected,
        })
        return revision


# --- internal approval (trace only) --------------------------------------------------------------------------

def on_revision_approved(revision):
    followup = ClientSurveillanceFollowUp.objects.select_for_update().filter(derived_revision=revision).select_related("surveillance").first()
    if followup is None or followup.closed_at:
        return None
    if followup.surveillance.events.filter(event_type=EVENT.FU_VALIDATED, data__revision=revision.pk).exists():
        return None
    if (followup.client_return.category not in SAME_INDEX and followup.expected_index and revision.revision_label
            and revision.revision_label != followup.expected_index):
        # Another version was approved meanwhile: the official index is not the expected one -> explicit review.
        followup.needs_review = "index_conflict"
        followup.save(update_fields=["needs_review"])
        _review_needed(followup.surveillance, None, "index_conflict", {
            "followup": followup.pk, "revision": revision.pk, "expected_index": followup.expected_index, "actual_index": revision.revision_label})
    return _event(followup.surveillance, None, EVENT.FU_VALIDATED, {
        "followup": followup.pk, "revision": revision.pk, "revision_label": revision.revision_label})


def on_revision_approved_safely(revision):
    try:
        with transaction.atomic():
            return on_revision_approved(revision)
    except Exception:
        logger.exception("Client follow-up approval trace failed for revision %s", getattr(revision, "pk", None))
        return None


# --- derived next step ------------------------------------------------------------------------------------------

def next_step(followup):
    """The next real business step, derived from the data. Never stored, never a manual transition."""
    if followup.closed_at:
        return {"code": CLOSED, "detail": None}
    if followup.needs_review:
        return {"code": NEEDS_REVIEW, "detail": followup.needs_review}
    same_index = category_of(followup) in SAME_INDEX
    revision = followup.derived_revision
    if revision is None:
        return {"code": AWAITING_BPE_FILE if same_index else AWAITING_FILE, "detail": None}
    if not same_index and not revision.revision_label:
        from project_management.models import DocumentWorkflow
        workflow = DocumentWorkflow.objects.filter(revision=revision).order_by("-id").first()
        detail = "not_started" if workflow is None else workflow.status.lower()
        return {"code": AWAITING_VALIDATION, "detail": detail}
    failed = TransmittalEvent.objects.filter(
        event_type=TransmittalEvent.EventType.SEND_FAILED, transmittal__items__document_revision=revision,
        transmittal__direction=Transmittal.Direction.OUT).exists()
    return {"code": AWAITING_DISTRIBUTION, "detail": "send_failed" if failed else None}


# --- distribution rules (eligibility + purpose) -----------------------------------------------------------------

def followup_of_revision(revision):
    return ClientSurveillanceFollowUp.objects.filter(derived_revision=revision).select_related("client_return__client_issue").first()


def display_index(revision):
    """The documentary index of a version: its own approved label, or -- for the code A BPE technical version -- the
    index of the revision the client accepted (same index, no label of its own)."""
    if revision.revision_label or not isinstance(revision, DocumentRevision) or revision.pk is None:
        return revision.revision_label
    if revision.index_revision_id:
        return revision.index_revision.revision_label
    followup = followup_of_revision(revision)
    if followup is not None and followup.client_return.category in SAME_INDEX:
        return followup.client_return.client_issue.document_revision.revision_label
    return None


def _index_root(record):
    source = record.client_issue.document_revision
    return source.index_revision if source.index_revision_id else source


def eligibility_exemption(revision, transmittal):
    """Code A only, server side, strictly this revision and this client: the BPE technical version of the SAME index
    needs no new internal validation. Anything else (other client, other revision, another category, a corrected
    return, a closed follow-up, a non PREL/BPE state) gets no exemption."""
    followup = followup_of_revision(revision)
    if followup is None or followup.closed_at or followup.needs_review:
        return False
    record = followup.client_return
    return bool(
        record.category in SAME_INDEX and revision.issue_state == "BPE" and followup.expected_state == "BPE"
        and revision.file and transmittal.direction == Transmittal.Direction.OUT
        and transmittal.external_company_id == record.client_issue.client_company_id
        and not record.corrections.exists()
        and revision.document_id == record.client_issue.document_revision.document_id
        and _index_root(record).pk == revision.index_revision_id)


def assert_followup_sendable(transmittal, revision, distribution_purpose):
    """LOT 3: server side, BEFORE anything is sent (when an item is added, when the transmittal is finalized and again
    right before the send). Only a send addressed to the follow-up's own client and carrying its derived revision is
    constrained here; a distribution to another client stays an ordinary distribution that never closes this follow-up.
    The checks that need the stored bytes (hash, presence of the file) are made by the send itself."""
    followup = followup_of_revision(revision)
    if followup is None or followup.closed_at or transmittal.direction != Transmittal.Direction.OUT:
        return
    record = followup.client_return
    issue = record.client_issue
    if transmittal.external_company_id != issue.client_company_id:
        return

    def refuse(code, message):
        raise ValidationError({"detail": message, "code": code})

    if followup.needs_review:
        refuse("followup_in_review", "This revision answers a client return that is blocked by an explicit review (%s)." % followup.needs_review)
    if record.corrections.exists():
        refuse("reconfirmation_required", "The client return behind this revision was corrected: retain the corrected return first.")
    if revision.document_id != issue.document_revision.document_id:
        refuse("document_mismatch", "This revision does not belong to the document of the client return.")
    if revision.issue_state != followup.expected_state:
        refuse("issue_state_mismatch", "This revision is %s but the client return expects a %s version." % (revision.issue_state or "not qualified", followup.expected_state))
    assert_purpose(revision, distribution_purpose)
    if not revision.file:
        refuse("file_missing", "The physical file of this revision is missing.")
    if record.category in SAME_INDEX:
        if revision.index_revision_id != _index_root(record).pk:
            refuse("index_mismatch", "This BPE version is not attached to the accepted index.")
    else:
        if not revision.revision_label:
            refuse("not_validated", "This revision is not internally approved yet.")
        if followup.expected_index and revision.revision_label != followup.expected_index:
            refuse("index_mismatch", "This revision has index %s but %s was expected." % (revision.revision_label, followup.expected_index))


def assert_purpose(revision, distribution_purpose):
    """A revision born from a client return is sent with the purpose its issue state requires."""
    followup = followup_of_revision(revision)
    if followup is None:
        return
    expected = PURPOSE_OF_STATE[followup.expected_state]
    if distribution_purpose != expected:
        raise ValidationError({"distribution_purpose": "This revision answers a client return (%s): it must be distributed %s." % (
            followup.expected_state, "for information" if expected == PURPOSE.FOR_INFORMATION else "for review"), "code": "wrong_purpose"})


# --- the verified send ------------------------------------------------------------------------------------------

def _is_sendable_event(send_event):
    return (send_event is not None and send_event.event_type == TransmittalEvent.EventType.SEND_SUCCEEDED
            and send_event.transmittal.direction == Transmittal.Direction.OUT)


def _check(followup, item, send_event):
    """-> (None, None) when the send closes the follow-up; ("review", reason) for a mismatch worth an event;
    ("wait", reason) when nothing can be decided yet (the replay will finish it)."""
    record = followup.client_return
    issue = record.client_issue
    transmittal = send_event.transmittal
    revision = item.document_revision
    category = record.category
    if revision.document_id != issue.document_revision.document_id:
        return "review", "document_mismatch"
    if transmittal.external_company_id != issue.client_company_id:
        return "review", "wrong_client"
    if record.corrections.exists():
        return "review", "return_replaced"
    if revision.issue_state != followup.expected_state:
        return "review", "issue_state_mismatch"
    if item.distribution_purpose != PURPOSE_OF_STATE[followup.expected_state]:
        return "review", "wrong_purpose"
    if followup.needs_review:
        return "review", "followup_in_review"
    frozen = (item.evidence_snapshot or {}).get("revision") or {}
    if not revision.file_sha256 or frozen.get("file_sha256") != revision.file_sha256:
        return "review", "hash_mismatch"
    if category not in SAME_INDEX and not revision.revision_label:
        return "review", "not_validated"
    if category in SAME_INDEX and revision.index_revision_id != _index_root(record).pk:
        return "review", "index_mismatch"
    if category not in SAME_INDEX and followup.expected_index and revision.revision_label != followup.expected_index:
        return "review", "index_mismatch"
    if category not in APPLICABLE:  # C/D: the same verified send must have opened the new PREL cycle
        if item.client_issue_id is None or not ClientSurveillance.objects.filter(
                client_issue_id=item.client_issue_id, opening_send_event=send_event).exists():
            return "wait", "new_cycle_not_opened"
    return None, None


def plan_followups(send_event):
    """Read-only (used by the simulation command): what processing this send would do per follow-up."""
    if not _is_sendable_event(send_event):
        return []
    rows = []
    for item in send_event.transmittal.items.select_related("document_revision", "client_issue"):
        followup = ClientSurveillanceFollowUp.objects.filter(derived_revision_id=item.document_revision_id).select_related("client_return__client_issue").first()
        if followup is None:
            continue
        if followup.closing_send_event_id == send_event.pk:
            action, reason = "ALREADY_DONE", None
        elif followup.closed_at:
            action, reason = "ALREADY_CLOSED", None
        else:
            kind, reason = _check(followup, item, send_event)
            action = "CLOSE" if kind is None else ("REVIEW" if kind == "review" else "WAIT")
        rows.append({"followup": followup.pk, "document_revision": item.document_revision_id, "action": action, "reason": reason})
    return rows


@transaction.atomic
def process_send_event(send_event):
    """Close (and mark applicable) every follow-up covered by this really successful send. Idempotent."""
    if not _is_sendable_event(send_event):
        return []
    Transmittal.objects.select_for_update().get(pk=send_event.transmittal_id)
    done = []
    items = list(send_event.transmittal.items.select_related("document_revision", "client_issue").order_by("id"))
    for item in items:
        followup = ClientSurveillanceFollowUp.objects.select_for_update().filter(derived_revision_id=item.document_revision_id).select_related("client_return__client_issue").first()
        if followup is None or followup.closed_at:
            continue
        surveillance = ClientSurveillance.objects.select_for_update().get(pk=followup.surveillance_id)
        kind, reason = _check(followup, item, send_event)
        if kind == "review":
            if _review_needed(surveillance, send_event.actor, reason, {"followup": followup.pk, "send_event": send_event.pk, "item": item.pk}):
                done.append(("REVIEW", followup.pk))
            continue
        if kind == "wait":
            logger.warning("Client follow-up %s not closed yet by send %s: %s", followup.pk, send_event.pk, reason)
            continue
        record = followup.client_return
        category = record.category
        now = timezone.now()
        next_surveillance = None
        if category not in APPLICABLE:
            next_surveillance = ClientSurveillance.objects.get(client_issue_id=item.client_issue_id, opening_send_event=send_event)
        if surveillance.status != STATUS.CLOSED:
            surveillance.status, surveillance.result, surveillance.closed_at = STATUS.CLOSED, category, now
            surveillance.save()
        followup.closing_send_event, followup.closed_at = send_event, now
        if category in APPLICABLE and followup.applicable_at is None:
            followup.applicable_at = send_event.created_at  # B: applicable only after its verified BPE send
        followup.next_surveillance = next_surveillance
        followup.save(update_fields=["closing_send_event", "closed_at", "applicable_at", "next_surveillance"])
        _event(surveillance, send_event.actor, EVENT.AUTO_CLOSED, {
            "followup": followup.pk, "return": record.pk, "category": category, "code": record.code_snapshot,
            "send_event": send_event.pk, "transmittal": send_event.transmittal_id,
            "transmittal_reference": send_event.transmittal.transmittal_number, "revision": item.document_revision_id,
            "issue_state": followup.expected_state, "purpose": item.distribution_purpose,
            "applicable_at": _iso(followup.applicable_at), "next_surveillance": next_surveillance.pk if next_surveillance else None,
            "retained_return": record.pk,
        })
        done.append(("CLOSE", followup.pk))
    return done


def process_send_event_safely(send_event):
    """After SEND_SUCCEEDED (and the opening of new cycles): a failure never undoes the send; the idempotent replay
    (`process_send_event`, or the `process_client_followups` command) finishes the work."""
    try:
        return process_send_event(send_event)
    except Exception:
        logger.exception("Client follow-up processing failed for send event %s; the message stays sent", send_event.pk)
        return None


def ensure_followup(record, actor):
    """Operator recovery (command `process_client_followups --returns`): the operator NAMES the return to retain, which is
    the explicit confirmation. Idempotent: a return that already owns a follow-up is left alone."""
    if ClientSurveillanceFollowUp.objects.filter(client_return=record).exists():
        return None
    issue = ClientSurveillanceReturn.objects.select_related("client_issue__document_revision__document").get(pk=record.pk).client_issue
    return confirm_retained_return(issue, actor, return_id=record.pk)
