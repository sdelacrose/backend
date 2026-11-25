"""OPTION B1 / LOT 1: the LOGICAL document version, on top of the physical DocumentRevision rows.

A document revision row is either an ordinary revision (`index_revision` is NULL) or a TECHNICAL BPE version of an
accepted index (`index_revision` points to the approved, non technical revision it belongs to). Rule:
- one ordinary revision = one LOGICAL version (V1 / A, V2 / B, ...); its number is the RANK of the ordinary revisions
  of the document (ordered by their physical `version_number`, which is never modified);
- a technical BPE version belongs to the logical version of its source: it never creates a logical version.

Three distinct notions (the physical one stays what every existing rule is built on):
- latest PHYSICAL revision / latest FILE: the newest row, technical or not (`get_latest_physical_revision`): drives the
  current file, the locking of the file and the numbering of the next physical version;
- CURRENT LOGICAL revision: the newest ordinary row (`get_current_logical_revision`): drives the internal approval
  status and the official revision label of the document;
- LOGICAL revision of a given row (`logical_revision_of`).
Nothing here writes anything.
"""
from django.db.models import Count, Subquery

from project_management.models import DocumentRevision


def is_technical(revision):
    return revision.index_revision_id is not None


def logical_revision_of(revision):
    """The ordinary revision a row belongs to: itself, or the source of a technical BPE version."""
    if revision is None:
        return None
    return revision.index_revision if is_technical(revision) else revision


def get_latest_physical_revision(document):
    """Newest stored row of the document (the latest file), technical versions included."""
    return DocumentRevision.objects.filter(document=document).order_by("-created_at", "-id").first()


def get_current_logical_revision(document):
    """Newest ORDINARY revision (technical BPE versions excluded): the one whose workflow and label describe the document."""
    return DocumentRevision.objects.filter(document=document, index_revision__isnull=True).order_by("-created_at", "-id").first()


def logical_version_number(revision):
    """Rank of the logical revision of `revision` among the ordinary revisions of its document (1-based); None for no row."""
    logical = logical_revision_of(revision)
    if logical is None:
        return None
    return DocumentRevision.objects.filter(
        document_id=logical.document_id, index_revision__isnull=True, version_number__lte=logical.version_number,
    ).count()


def logical_info(revisions):
    """{revision id: (logical revision id, logical version number)} for ALL the rows of ONE document (no query)."""
    rows = list(revisions)
    ordinary = sorted((r for r in rows if not is_technical(r)), key=lambda r: (r.version_number, r.pk))
    rank = {r.pk: number for number, r in enumerate(ordinary, start=1)}
    info = {}
    for row in rows:
        logical_id = row.index_revision_id if is_technical(row) else row.pk
        info[row.pk] = (logical_id, rank.get(logical_id))
    return info


def logical_annotations(document_ref):
    """Subqueries for a Document queryset: (newest ordinary revision queryset, count of ordinary revisions).
    The count is also the logical number of the CURRENT logical revision (it is the newest one)."""
    ordinary = DocumentRevision.objects.filter(document=document_ref, index_revision__isnull=True)
    count = ordinary.order_by().values("document").annotate(n=Count("pk")).values("n")[:1]
    return ordinary.order_by("-created_at", "-id"), Subquery(count)
