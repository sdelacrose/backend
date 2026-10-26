"""MAIL-1.3D-C1: a user's own, explicit, bounded search of ONE of their authorized mailboxes.

This is a lookup, not a synchronisation and not a mail client: nothing is read until the
authenticated user asks, one mailbox and one folder at a time, with a bounded period, at most
MAX_PAGES small pages, and metadata only (no body, no body preview, no attachment). Nothing found
is stored: no MailboxMessage, no sync state, no correspondence. Persisting a message happens only
after a later, explicit selection and confirmation (MAIL-1.3D-C2).

Microsoft Graph, as relied on here (Graph's documented behaviour -- not verified live in this lot):
  - `$filter` on `receivedDateTime` (ge / le), on `from/emailAddress/address` (eq) and
    `contains(subject, ...)` can be combined with `and`;
  - `$orderby receivedDateTime desc` is accepted when `receivedDateTime` leads the `$filter`
    (so a search ALWAYS carries a start date -- never an unbounded read);
  - `$top` and `$skip` page such a result. `$search` is deliberately not used: it cannot be
    combined with `$filter`/`$orderby` and returns by relevance.
  If Graph rejects a combination, the user gets a "narrow your criteria" error, never a fallback to
  a broader read.
Paging is `$skip` with one extra row requested (`$top = page_size + 1`) to know whether another
page exists: no raw Graph next-link is ever handed to the client (it could be altered, e.g. to ask
for a body).

The reference returned with each result is a SIGNED, expiring, user-bound token naming the mailbox
and the Graph item. It proves only that THIS server returned this item to THIS user recently. It is
never an authorization: whoever consumes it (C2) must re-authorize the mailbox read, and re-fetch
the message from Graph, before doing anything.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.core import signing
from django.utils import timezone

from userapis.mailbox_read import MailboxGraphReader, authorize_mailbox_read

logger = logging.getLogger(__name__)

SEARCH_FOLDERS = ("inbox", "sentitems")
DEFAULT_PERIOD = timedelta(days=30)
MAX_PERIOD = timedelta(days=366)
MIN_TEXT_LENGTH = 3
MAX_TEXT_LENGTH = 100
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 25
MAX_PAGES = 4

REFERENCE_SALT = "links.mailbox-search-reference"
REFERENCE_MAX_AGE_SECONDS = 30 * 60


class InvalidSearchReference(Exception):
    """The reference is forged, altered, expired or belongs to someone else."""


@dataclass
class SearchPage:
    folder: str
    page: int
    page_size: int
    has_next: bool
    since: object
    until: object
    results: list = field(default_factory=list)       # [(reference, GraphMessageMeta)]


def build_search_reference(user, mailbox, meta):
    return signing.dumps(
        {"m": mailbox.pk, "u": user.pk, "g": meta.graph_id, "f": meta.folder},
        salt=REFERENCE_SALT, compress=True,
    )


def read_search_reference(token, *, user, max_age=REFERENCE_MAX_AGE_SECONDS):
    """Decode a reference issued to `user`: {"mailbox_id", "graph_id", "folder"}. A signature, age
    and user check ONLY -- the caller must still re-authorize the mailbox and re-read the message."""
    try:
        payload = signing.loads(str(token or ""), salt=REFERENCE_SALT, max_age=max_age)
    except signing.BadSignature:
        raise InvalidSearchReference()
    if not isinstance(payload, dict) or payload.get("u") != getattr(user, "pk", None):
        raise InvalidSearchReference()
    return {"mailbox_id": payload.get("m"), "graph_id": payload.get("g"), "folder": payload.get("f")}


def search_messages(
    user, mailbox, *, folder="inbox", sender=None, subject=None, since=None, until=None,
    page=1, page_size=DEFAULT_PAGE_SIZE, project=None, reader=None,
):
    """One bounded page of metadata matching the user's criteria in ONE mailbox. Re-authorizes
    `user` for READ on the mailbox (and the project, when given) before any request; raises
    MailboxReadError for every refusal or Graph failure (fixed, generic messages)."""
    authorize_mailbox_read(user, mailbox, project=project)
    now = timezone.now()
    until = until or now
    since = since or (until - DEFAULT_PERIOD)
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    page = max(1, min(int(page), MAX_PAGES))
    reader = reader or MailboxGraphReader(mailbox, actor=user, project=project)
    listed = reader.list_messages(
        folder, top=page_size + 1, received_since=since, received_until=until,
        sender=sender or None, subject_contains=subject or None, skip=(page - 1) * page_size,
    )
    messages = listed.messages
    result = SearchPage(
        folder=folder, page=page, page_size=page_size, has_next=len(messages) > page_size,
        since=since, until=until,
    )
    result.results = [(build_search_reference(user, mailbox, m), m) for m in messages[:page_size]]
    # Counts only: the criteria and the messages are personal data and are never logged.
    logger.info(
        "Mailbox search mailbox_id=%s user_id=%s page=%s results=%s",
        mailbox.pk, user.pk, page, len(result.results),
    )
    return result
