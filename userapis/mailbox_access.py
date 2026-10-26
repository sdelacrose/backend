"""MAIL-1.1B section 5: centralized mailbox access-control checks.

Nothing here implements OAuth, sending, or receiving (MAIL-1.1C+'s job) -- these are the
cumulative authorization rules a future endpoint/service must apply before letting a user act
on a MailboxConnection, so that logic is written exactly once rather than re-derived at every
future call site.

Rule numbers below match section 5 of the MAIL-1.1B ticket verbatim.
"""

from userapis.models import MailboxConnection, MailboxProjectAssignment, MailboxUserGrant
from userapis.permissions import MailboxAdminPermission


def can_administer_mailbox(user, mailbox):
    """Rule 1: a user may administer only mailboxes of their own company. Reuses
    MailboxAdminPermission (mail.admin_shared_mailbox, company scope) -- never a parallel
    is_company_admin check."""
    if mailbox is None:
        return False
    return MailboxAdminPermission().is_company_mailbox_admin(user, mailbox.company_profile)


def is_mailbox_assigned_to_project(mailbox, project):
    """Rule 4: for a project-scoped use, the project must actually be assigned to the
    mailbox -- never inferred from company membership alone."""
    if mailbox is None or project is None:
        return False
    return MailboxProjectAssignment.objects.filter(mailbox=mailbox, project=project).exists()


def can_use_mailbox(user, mailbox, *, for_sending, project=None):
    """Rules 2/3/6/7/8/9, the cumulative check: given a mailbox id a caller supplies (rule 6:
    the check always re-derives everything from the mailbox/user themselves, nothing is ever
    trusted from the caller beyond "which mailbox, which user, read or send"), decide whether
    `user` may use `mailbox` right now for the given purpose.

    Rule 7 (this function's own limit, not a flaw): LINKS granting `True` here means the
    LINKS-side authorization checks out -- it is never a guarantee that Microsoft
    Exchange/Google actually honours the resulting request (Send As/Send on Behalf/mailbox
    access are tenant-side configuration this code cannot see or enforce, see the MAIL-1/
    MAIL-1.1A reports). A provider-side refusal is a separate, later failure mode.

    Does NOT check the Correspondances permission itself (rule 5: "Les permissions
    Correspondances existantes restent obligatoires") -- that is the caller's own, additional,
    pre-existing responsibility (e.g. CorrespondencePermission), not duplicated here.
    """
    if mailbox is None or user is None:
        return False
    # Rule 8: a revoked/expired/needs-reconsent connection is never usable, regardless of any
    # grant that might otherwise apply.
    if mailbox.status != MailboxConnection.Status.ACTIVE:
        return False
    if project is not None and not is_mailbox_assigned_to_project(mailbox, project):
        return False
    if mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
        # Rule 9: an individual mailbox is usable only by its own owner -- never by anyone
        # else, including a company administrator (administering it, via
        # can_administer_mailbox above, is a separate capability from using it to send/read).
        return mailbox.owner_user_id == user.id
    # Shared mailbox: rule 3, an explicit MailboxUserGrant is required -- never implied by
    # project membership alone (rule 2).
    grant = MailboxUserGrant.objects.filter(mailbox=mailbox, user=user).first()
    if grant is None:
        return False
    return grant.can_send if for_sending else grant.can_read
