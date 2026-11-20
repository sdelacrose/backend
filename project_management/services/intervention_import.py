"""DEMO-01: reviewed CSV import of intervention requests.

Flow: preview (parses + classifies every row, persists a batch, creates NO request) then confirm
(one transaction: creates, for each new request, ONE mother Action Item carrying the request title
and its sub-actions as parent_action_item children, from the row's editable plan -- MOD-02).
Protections:

  * file-level   duplicate rows (same external reference) -- identical ones are skipped, conflicting
                 ones are ALL rejected (nothing is guessed);
  * re-import    an external reference already stored is skipped; if its data changed it is only
                 reported (`source_changed`), never updated automatically in this first scope;
  * double click / concurrent imports  confirm locks the batch then the project row, re-checks every
                 reference under the lock, and unique DB constraints are the last backstop;
  * single generation  each action carries (intervention_request, intervention_action_key), unique
                 (key "mother" for the mother, "s1", "s2", ... for its sub-actions);
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

from django.db import transaction
from django.utils import timezone

from project_management.models import (
    ActionItem,
    ActionTemplate,
    InterventionImportBatch,
    InterventionImportRow,
    InterventionReferenceCounter,
    InterventionRequest,
    Milestone,
    Project,
)

from project_management.services.action_progress import build_sub_actions
from project_management.services.intervention_plan import (
    PlanError,
    clean_plan,
    default_plan,
    save_template_from_plan,
)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
INTERVENTIONS_MILESTONE_NAME = "Interventions"
DEFAULT_ACTION_STATUS = "open"
SEQUENCE_WIDTH = 5

MOTHER_ACTION_KEY = "mother"
MAX_TITLE = 255  # the title is also the name of the mother Action Item (max 255)

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


class PlanIncomplete(Exception):
    """Some new requests have no sub-action plan yet (no company template matched, nothing defined)."""

    def __init__(self, lines):
        super().__init__("Some requests have no sub-action plan.")
        self.lines = lines


class PlanRejected(Exception):
    """A stored plan is no longer acceptable at confirmation (rights, owner, points...)."""

    def __init__(self, line, error):
        super().__init__(error.detail)
        self.line = line
        self.code = error.code
        self.detail = error.detail


class InvalidAssignee(Exception):
    """The chosen action owner is not a member of the project (checked server side)."""


class AssignmentNotAtImport(Exception):
    """An owner was sent to the import: owners are assigned afterwards, from the sheet of each action."""


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
        elif len(fields["title"]) > MAX_TITLE:
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
        plan = default_plan(project, fields["title"]) if outcome == Outcome.NEW else {}
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
                plan=plan,
            )
        )
    InterventionImportRow.objects.bulk_create(to_store)

    batch.summary = {"encoding": encoding, "delimiter": delimiter, "total_rows": len(parsed), "outcomes": outcomes}
    refresh_summary(batch)
    return batch


def refresh_summary(batch):
    """Counts derived from the rows: requests, planned actions (mother + sub-actions), rows still
    waiting for a plan. Called at preview and after every plan edit."""
    new_rows = [r for r in batch.rows.all() if r.outcome == InterventionImportRow.Outcome.NEW]
    planned = [r for r in new_rows if (r.plan or {}).get("sub_actions") or (r.plan or {}).get("no_sub_actions")]
    batch.summary = {
        **batch.summary,
        "requests_to_create": len(new_rows),
        "rows_needing_plan": len(new_rows) - len(planned),
        "actions_to_create": sum(1 + len(r.plan["sub_actions"]) for r in planned),
        "templates_to_create": sum(1 for r in planned if r.plan.get("save_as_template")),
    }
    batch.save(update_fields=["summary"])


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

    The import creates every new action WITHOUT owner. `assignee_id` is kept in the signature only to refuse it
    explicitly (AssignmentNotAtImport): owners are assigned afterwards, from the sheet of each action, under the
    Action Items rules. Existing actions (re-import) are never touched.

    Idempotent: confirming an already confirmed batch (double click) returns its stored result.
    Any unexpected error rolls everything back (no request, no action) and leaves the batch
    previewed, so the confirmation can safely be retried.
    MOD-02: each new request gets ONE mother action (same title) and its sub-actions, from the plan
    stored on its row. Every plan is validated first; a row without a plan raises PlanIncomplete and
    nothing is written. A reusable template is created only when its row asks for it, with the company
    configuration right, and never replaces an existing one.
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

    if assignee_id not in (None, ""):
        raise AssignmentNotAtImport("Owners are assigned after the import, from the sheet of each action.")

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

    plans = {}
    unplanned = [
        r.line_number for r in pending
        if not ((r.plan or {}).get("sub_actions") or (r.plan or {}).get("no_sub_actions"))
    ]
    if unplanned:
        raise PlanIncomplete(unplanned)
    for row in pending:
        try:
            plans[row.pk] = clean_plan(project, row.plan, user=user)
        except PlanError as error:
            raise PlanRejected(row.line_number, error)

    created_requests = created_actions = 0
    templates_created, templates_existing = [], []
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
        plan = plans[row.pk]
        lines = [{**l, "assignee": None} for l in plan["sub_actions"]]
        template = None
        if plan["template_id"]:
            template = ActionTemplate.objects.get(pk=plan["template_id"])
        elif plan["save_as_template"]:
            state, template = save_template_from_plan(project, user, data["title"], plan)
            (templates_created if state == "created" else templates_existing).append(
                {"line": row.line_number, "template_id": template.pk if template else None}
            )
        mother = ActionItem(
            milestone=milestone,
            name=data["title"],
            status=DEFAULT_ACTION_STATUS,
            target_date=intervention.expected_end_date,
            created_by=user,
            intervention_request=intervention,
            intervention_action_key=MOTHER_ACTION_KEY,
            is_bpu=plan["is_bpu"],  # BPU lives on the mother; the sub-actions inherit it
        )
        mother._suppress_action_item_notifications = True  # bulk generation: no notification storm
        mother.save()
        if lines:  # a request confirmed explicitly without sub-action only gets its mother
            build_sub_actions(
                mother, lines, user=user, template=template, intervention_request=intervention,
                default_assignees=[],
            )
        created_actions += 1 + len(lines)
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
        "outcomes": counts,
        "templates_created": templates_created,
        "templates_already_existing": templates_existing,
    }
    batch.save(update_fields=["status", "confirmed_at", "result"])
    return batch, True
