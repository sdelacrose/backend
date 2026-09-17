from django.utils import timezone

from companyprofileapis.models import Opportunity


class OpportunityLifecycleAction:
    CREATE_DIRECT_OFFER = "create_direct_offer"
    CONVERT_TO_OFFER = "convert_to_offer"
    CONVERT_TO_PROJECT = "convert_to_project"
    DECLINE_OFFER = "decline_offer"
    CLOSE = "close"
    SEND = "send"


FULLY_EDITABLE_OFFER_STATUSES = frozenset(
    {
        Opportunity.Status.DRAFT,
        Opportunity.Status.CONVERTED,
        Opportunity.Status.ISSUED,
    }
)
SENDABLE_OFFER_STATUSES = frozenset(
    {
        *FULLY_EDITABLE_OFFER_STATUSES,
        Opportunity.Status.CONVERTED_TO_PROJECT,
    }
)
TERMINAL_STATUSES = frozenset(
    {
        Opportunity.Status.CLOSED,
        Opportunity.Status.DECLINED,
    }
)


class OpportunityLifecycleError(ValueError):
    pass


def target_status_for_action(current_status, action):
    if action == OpportunityLifecycleAction.CONVERT_TO_OFFER:
        if current_status != Opportunity.Status.OPEN:
            raise OpportunityLifecycleError(
                "Only an open opportunity can be converted to an offer."
            )
        return Opportunity.Status.CONVERTED

    if action == OpportunityLifecycleAction.CONVERT_TO_PROJECT:
        if current_status not in FULLY_EDITABLE_OFFER_STATUSES:
            raise OpportunityLifecycleError(
                "Only a draft, converted, or issued offer can be converted to a project."
            )
        return Opportunity.Status.CONVERTED_TO_PROJECT

    if action == OpportunityLifecycleAction.DECLINE_OFFER:
        if current_status not in FULLY_EDITABLE_OFFER_STATUSES:
            raise OpportunityLifecycleError(
                "Only a draft, converted, or issued offer can be declined."
            )
        return Opportunity.Status.DECLINED

    if action == OpportunityLifecycleAction.CLOSE:
        if current_status not in {
            Opportunity.Status.OPEN,
            *FULLY_EDITABLE_OFFER_STATUSES,
        }:
            raise OpportunityLifecycleError(
                "Only an open opportunity or editable offer can be closed."
            )
        return Opportunity.Status.CLOSED

    if action == OpportunityLifecycleAction.SEND:
        if current_status not in SENDABLE_OFFER_STATUSES:
            raise OpportunityLifecycleError(
                "Only an active or project-converted offer can be sent."
            )
        if current_status == Opportunity.Status.CONVERTED_TO_PROJECT:
            return Opportunity.Status.CONVERTED_TO_PROJECT
        return Opportunity.Status.ISSUED

    raise OpportunityLifecycleError("Unsupported opportunity lifecycle action.")


def apply_offer_send_state(opportunity, sent_on=None):
    """
    Apply only the persisted date/status portion of Send.

    Ticket 04 is responsible for calling this inside the same transaction as PDF
    regeneration and package creation so a generation failure rolls everything back.
    """
    target_status, sent_date = get_offer_send_state(opportunity, sent_on)
    update_fields = []

    if opportunity.sent_date != sent_date:
        opportunity.sent_date = sent_date
        update_fields.append("sent_date")

    if opportunity.status != target_status:
        opportunity.status = target_status
        update_fields.append("status")

    if update_fields:
        update_fields.append("updated_at")
        opportunity.save(update_fields=update_fields)

    return opportunity


def get_offer_send_state(opportunity, sent_on=None):
    target_status = target_status_for_action(
        opportunity.status,
        OpportunityLifecycleAction.SEND,
    )
    sent_date = opportunity.sent_date or sent_on or timezone.localdate()
    return target_status, sent_date
