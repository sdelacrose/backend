"""DEMO-01: reviewed CSV import of intervention requests.

Flow: preview (parses + classifies every row, persists a batch, creates NO request) then confirm
(one transaction: creates the new requests and their four Action Items). Protections:

  * file-level   duplicate rows (same external reference) -- identical ones are skipped, conflicting
                 ones are ALL rejected (nothing is guessed);
  * re-import    an external reference already stored is skipped; if its data changed it is only
                 reported (`source_changed`), never updated automatically in this first scope;
  * double click / concurrent imports  confirm locks the batch then the project row, re-checks every
                 reference under the lock, and unique DB constraints are the last backstop;
  * single generation  each action carries (intervention_request, intervention_action_key), unique;
  * consistency  the whole confirm is one transaction: all requests and actions, or none;
  * dates        received_date is stored as given and never rewritten; no assignee and no start date
                 is invented; the (required) target date of each action is the request's expected end.

The stable key is the external reference column. A title is never used as an identity.
"""
import csv
import hashlib
import io
import json
import re
import unicodedata
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from project_management.models import (
    ActionItem,
    InterventionImportBatch,
    InterventionImportRow,
    InterventionReferenceCounter,
    InterventionRequest,
    Milestone,
    Project,
)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
INTERVENTIONS_MILESTONE_NAME = "Interventions"
DEFAULT_ACTION_STATUS = "open"
SEQUENCE_WIDTH = 5

# Illustrative model, to be validated by the business owner.
ACTION_TEMPLATES = (
    ("preparation", "Préparation matériel"),
    ("delivery", "Acheminement"),
    ("assembly", "Montage"),
    ("control", "Contrôle"),
)

# canonical field -> accepted header spellings (compared after normalisation)
HEADER_ALIASES = {
    "external_reference": ["external_reference", "reference_externe", "ref_externe", "reference", "id", "numero", "n"],
    "title": ["title", "titre", "objet", "intitule"],
    "received_date": ["received_date", "date_reception", "date_de_reception", "date_reception_initiale"],
    "expected_end_date": ["expected_end_date", "date_fin_attendue", "date_de_fin_attendue", "echeance", "date_fin"],
    "budget": ["budget"],
    "currency": ["currency", "devise"],
    "description": ["description", "commentaire", "details"],
    "source_modified_at": ["source_modified_at", "date_modification_source", "date_modification"],
}
REQUIRED_FIELDS = ("external_reference", "title", "received_date", "expected_end_date")


class ImportFileError(ValueError):
    """The whole file cannot be read (size, encoding, header): nothing is stored."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class BatchNotConfirmable(Exception):
    pass


class InvalidAssignee(Exception):
    """The chosen action owner is not a member of the project (checked server side)."""


def _norm_header(value):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _decode(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


def _detect_delimiter(header_line):
    counts = {d: header_line.count(d) for d in (";", ",", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")


def _parse_date(value):
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=dt_timezone.utc)
        except ValueError:
            continue
    return None


def _parse_budget(value):
    text = str(value or "").strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid_budget")
    if not amount.is_finite() or amount < 0 or amount >= Decimal("1e12"):
        raise ValueError("invalid_budget")
    return amount.quantize(Decimal("0.01"))


def compute_content_hash(fields):
    payload = json.dumps(
        {k: fields.get(k) for k in ("title", "received_date", "expected_end_date", "budget", "currency", "description")},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_csv(raw):
    """Returns (rows, encoding, delimiter). rows: [{"line", "fields", "errors"}]."""
    if len(raw) > MAX_FILE_BYTES:
        raise ImportFileError("file_too_large", "The file exceeds 2 MB.")
    if not raw.strip():
        raise ImportFileError("empty_file", "The file is empty.")
    try:
        text, encoding = _decode(raw)
    except UnicodeDecodeError:
        raise ImportFileError("bad_encoding", "The file is neither UTF-8 nor Windows-1252.")
    lines = text.splitlines()
    delimiter = _detect_delimiter(lines[0] if lines else "")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFileError("empty_file", "The file is empty.")

    normalised = [_norm_header(h) for h in header]
    column_of = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                column_of[field] = normalised.index(alias)
                break
    missing = [f for f in REQUIRED_FIELDS if f not in column_of]
    if missing:
        raise ImportFileError("missing_columns", "Missing required columns: " + ", ".join(missing))

    raw_rows = []
    for cells in reader:
        if not any(str(c).strip() for c in cells):
            continue
        if len(raw_rows) >= MAX_ROWS:
            raise ImportFileError("too_many_rows", f"The file exceeds {MAX_ROWS} rows.")
        raw_rows.append((reader.line_num, cells))

    parsed = []
    for line, cells in raw_rows:

        def cell(name):
            idx = column_of.get(name)
            return cells[idx].strip() if idx is not None and idx < len(cells) else ""

        errors = []
        fields = {
            "external_reference": cell("external_reference"),
            "title": cell("title"),
            "currency": cell("currency")[:10],
            "description": cell("description"),
        }
        if not fields["external_reference"]:
            errors.append("missing_external_reference")
        elif len(fields["external_reference"]) > 255:
            errors.append("external_reference_too_long")
        if not fields["title"]:
            errors.append("missing_title")
        elif len(fields["title"]) > 500:
            errors.append("title_too_long")

        received = _parse_date(cell("received_date"))
        end = _parse_date(cell("expected_end_date"))
        if received is None:
            errors.append("invalid_received_date")
        if end is None:
            errors.append("invalid_expected_end_date")
        if received and end and end < received:
            errors.append("end_before_received")
        fields["received_date"] = received.isoformat() if received else None
        fields["expected_end_date"] = end.isoformat() if end else None

        try:
            budget = _parse_budget(cell("budget"))
        except ValueError:
            budget = None
            errors.append("invalid_budget")
        fields["budget"] = str(budget) if budget is not None else None

        modified_raw = cell("source_modified_at")
        modified = _parse_datetime(modified_raw)
        if modified_raw and modified is None:
            errors.append("invalid_source_modified_at")
        fields["source_modified_at"] = modified.isoformat() if modified else None
        parsed.append({"line": line, "fields": fields, "errors": errors})
    return parsed, encoding, delimiter


@transaction.atomic
def preview_import(*, project, user, file_name, raw, source="csv"):
    """Parses, classifies and stores a PREVIEWED batch. Creates no InterventionRequest."""
    parsed, encoding, delimiter = parse_csv(raw)
    source = (source or "csv").strip()[:50] or "csv"
    Outcome = InterventionImportRow.Outcome

    # file-level duplicates, judged on the external reference only
    by_ref = {}
    for item in parsed:
        if item["fields"]["external_reference"] and not item["errors"]:
            item["fields"]["content_hash"] = compute_content_hash(item["fields"])
            by_ref.setdefault(item["fields"]["external_reference"], []).append(item)

    existing = {
        r.external_reference: r
        for r in InterventionRequest.objects.filter(
            project=project, source=source, external_reference__in=list(by_ref)
        )
    }

    batch = InterventionImportBatch.objects.create(
        project=project,
        created_by=user,
        source=source,
        file_name=(file_name or "")[:255],
        file_hash=hashlib.sha256(raw).hexdigest(),
    )
    outcomes = {}
    to_store = []
    for item in parsed:
        fields = item["fields"]
        ref = fields["external_reference"]
        errors = list(item["errors"])
        existing_request = None
        if errors:
            outcome = Outcome.ERROR
        else:
            group = by_ref[ref]
            hashes = {g["fields"]["content_hash"] for g in group}
            if len(group) > 1 and len(hashes) > 1:
                outcome = Outcome.ERROR
                errors.append("conflicting_duplicate_in_file")
            elif len(group) > 1 and group[0] is not item:
                outcome = Outcome.DUPLICATE_IN_FILE
            elif ref in existing:
                existing_request = existing[ref]
                outcome = (
                    Outcome.ALREADY_IMPORTED
                    if existing_request.content_hash == fields["content_hash"]
                    else Outcome.SOURCE_CHANGED
                )
            else:
                outcome = Outcome.NEW
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        to_store.append(
            InterventionImportRow(
                batch=batch,
                line_number=item["line"],
                external_reference=ref[:255],
                data=fields,
                content_hash=fields.get("content_hash", ""),
                outcome=outcome,
                errors=errors,
                existing_request=existing_request,
            )
        )
    InterventionImportRow.objects.bulk_create(to_store)

    new_count = outcomes.get(Outcome.NEW, 0)
    batch.summary = {
        "encoding": encoding,
        "delimiter": delimiter,
        "total_rows": len(parsed),
        "requests_to_create": new_count,
        "actions_to_create": new_count * len(ACTION_TEMPLATES),
        "outcomes": outcomes,
    }
    batch.save(update_fields=["summary"])
    return batch


def _next_reference(project):
    counter, _ = InterventionReferenceCounter.objects.select_for_update().get_or_create(project=project)
    counter.last_sequence += 1
    counter.save(update_fields=["last_sequence", "updated_at"])
    code = project.project_code or f"P{project.pk}"
    return f"IR-{code}-{counter.last_sequence:0{SEQUENCE_WIDTH}d}"


def _interventions_milestone(project, due_date):
    milestone = Milestone.objects.filter(project=project, name=INTERVENTIONS_MILESTONE_NAME).order_by("id").first()
    if milestone is None:
        milestone = Milestone.objects.create(
            project=project, name=INTERVENTIONS_MILESTONE_NAME, due_date=due_date, status="open"
        )
    return milestone


@transaction.atomic
def confirm_import(*, batch_id, user, assignee_id=None):
    """Creates the requests and actions of a previewed batch, exactly once.

    assignee_id (optional): project team member set as owner of the NEWLY generated actions only.
    Same membership rule as the Action Items API; existing actions are never touched.

    Idempotent: confirming an already confirmed batch (double click) returns its stored result.
    Any unexpected error rolls everything back (no request, no action) and leaves the batch
    previewed, so the confirmation can safely be retried.
    Returns (batch, applied_now).
    """
    Outcome = InterventionImportRow.Outcome
    batch = InterventionImportBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status == InterventionImportBatch.Status.CONFIRMED:
        return batch, False
    if batch.status != InterventionImportBatch.Status.PREVIEWED:
        raise BatchNotConfirmable("The batch can no longer be confirmed.")
    # Serialises every import and reference allocation of this project.
    project = Project.objects.select_for_update().get(pk=batch.project_id)

    assignee = None
    if assignee_id not in (None, ""):
        try:
            member = project.team_members.select_related("team_member").get(team_member_id=int(assignee_id))
        except (ObjectDoesNotExist, ValueError, TypeError):
            raise InvalidAssignee("The selected user is not a member of this project.")
        assignee = member.team_member

    rows = list(batch.rows.select_for_update().order_by("line_number"))
    candidate_refs = [r.external_reference for r in rows if r.outcome == Outcome.NEW]
    existing = {
        r.external_reference: r
        for r in InterventionRequest.objects.filter(
            project=project, source=batch.source, external_reference__in=candidate_refs
        )
    }
    pending = [r for r in rows if r.outcome == Outcome.NEW and r.external_reference not in existing]
    milestone = None
    if pending:
        due = max(datetime.fromisoformat(r.data["expected_end_date"]).date() for r in pending)
        milestone = _interventions_milestone(project, due)

    created_requests = created_actions = 0
    for row in rows:
        if row.outcome != Outcome.NEW:
            continue
        if row.external_reference in existing:
            # created by a concurrent import since the preview: treat as a re-import
            row.existing_request = existing[row.external_reference]
            row.outcome = (
                Outcome.ALREADY_IMPORTED
                if row.existing_request.content_hash == row.content_hash
                else Outcome.SOURCE_CHANGED
            )
            row.save(update_fields=["existing_request", "outcome"])
            continue
        data = row.data
        intervention = InterventionRequest.objects.create(
            project=project,
            company_profile=project.company_profile,
            reference=_next_reference(project),
            title=data["title"],
            received_date=datetime.fromisoformat(data["received_date"]).date(),
            expected_end_date=datetime.fromisoformat(data["expected_end_date"]).date(),
            budget=Decimal(data["budget"]) if data.get("budget") else None,
            currency=data.get("currency", ""),
            description=data.get("description", ""),
            source=batch.source,
            external_reference=row.external_reference,
            source_modified_at=(
                datetime.fromisoformat(data["source_modified_at"]) if data.get("source_modified_at") else None
            ),
            content_hash=row.content_hash,
            import_batch=batch,
            created_by=user,
        )
        for key, label in ACTION_TEMPLATES:
            action = ActionItem(
                milestone=milestone,
                name=f"{intervention.reference} — {label}",
                status=DEFAULT_ACTION_STATUS,
                target_date=intervention.expected_end_date,
                created_by=user,
                intervention_request=intervention,
                intervention_action_key=key,
            )
            # Bulk generation: no per-action notification storm.
            action._suppress_action_item_notifications = True
            action.save()
            if assignee is not None:
                action.assignees.add(assignee)  # same instance: notifications stay suppressed
            created_actions += 1
        row.existing_request = intervention
        row.outcome = Outcome.CREATED
        row.save(update_fields=["existing_request", "outcome"])
        created_requests += 1

    counts = {}
    for row in rows:
        counts[row.outcome] = counts.get(row.outcome, 0) + 1
    batch.status = InterventionImportBatch.Status.CONFIRMED
    batch.confirmed_at = timezone.now()
    batch.result = {
        "requests_created": created_requests,
        "actions_created": created_actions,
        "assignee_id": assignee.pk if assignee else None,
        "outcomes": counts,
    }
    batch.save(update_fields=["status", "confirmed_at", "result"])
    return batch, True
