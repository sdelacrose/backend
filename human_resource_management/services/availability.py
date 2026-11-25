"""LOT RH 6, item 4 -- reusable availability information, built from the ONE
thing this codebase already has: approved `LeaveRequest` rows. Deliberately
NOT a dated-allocation/assignment model: investigation confirmed none exists
anywhere in this codebase (`ProjectTeamMembers` is static company/project
membership with no start/end dates; `resources.resource_allocation`/
`ResourceAllocationPermission` guards no actual model or endpoint -- it is
dead code). Per the lot's own instruction, this stays a minimal, reusable
building block for a future dated-assignment feature to call -- it does not
invent that feature's model, and nothing in this codebase yet calls it from
a project-assignment flow (there is no dated assignment to block or to show
unavailability against today).
"""
from dataclasses import dataclass
from datetime import date

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.utils import is_weekend


@dataclass
class LeaveOverlap:
    leave_request_id: int
    start_date: date
    end_date: date
    is_half_day: bool
    half_day_period: str | None

    def overlaps_half_day(self, candidate_period):
        """True if this approved leave actually conflicts with a candidate
        assignment that itself only claims a specific half-day. A
        date-only candidate (no half-day precision) always conflicts if the
        dates overlap at all -- this codebase has no notion of clock times
        to otherwise distinguish them (see the lot's own instruction not to
        invent 8h-12h/14h-18h hours)."""
        if candidate_period is None or not self.is_half_day:
            return True
        return self.half_day_period == candidate_period


def get_approved_leave_overlaps(staff, start_date, end_date):
    """All of `staff`'s APPROVED leave requests overlapping [start_date,
    end_date] (inclusive). Never pending/rejected -- only an approved leave
    is a real, confirmed unavailability. Returns a list of `LeaveOverlap`,
    ordered by start_date, for the caller to interpret (e.g. decide if a
    specific half-day candidate assignment actually conflicts via
    `LeaveOverlap.overlaps_half_day`)."""
    rows = (
        LeaveRequest.objects.filter(
            employee=staff,
            status=StatusType.APPROVED.value,
            start_date__lte=end_date,
            end_date__gte=start_date,
        )
        .order_by("start_date", "id")
    )
    return [
        LeaveOverlap(
            leave_request_id=r.id,
            start_date=r.start_date,
            end_date=r.end_date,
            is_half_day=r.is_half_day,
            half_day_period=r.half_day_period,
        )
        for r in rows
    ]


def is_staff_available(staff, start_date, end_date, *, half_day_period=None):
    """True iff `staff` has NO approved-leave overlap for the given range
    (optionally scoped to one half-day). A date-only range (half_day_period
    left None) is unavailable if ANY overlapping approved leave exists, full
    or half-day -- this codebase has no clock-time precision to narrow that
    further (per the lot's own instruction, a date-only candidate can only
    ever report a same-day overlap, never a same-half-day one)."""
    overlaps = get_approved_leave_overlaps(staff, start_date, end_date)
    return not any(o.overlaps_half_day(half_day_period) for o in overlaps)
