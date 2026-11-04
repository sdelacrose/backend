"""Reserved workflow-like labels cannot be newly assigned as business status."""
import re

RESERVED_MANUAL_STATUSES = frozenset({"under review", "submitted", "approved", "sent", "issued", "revised"})


def is_reserved_manual_status(value):
    label = str(value or "").split(":", 1)[0].strip().lower()
    return re.sub(r"[\s_-]+", " ", label) in RESERVED_MANUAL_STATUSES
