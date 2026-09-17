class OpportunityHistoryReason:
    OPPORTUNITY_CREATED = "opportunity.created"
    OFFER_CREATED = "offer.created"
    OPPORTUNITY_CONVERTED_TO_OFFER = "offer.converted"
    OFFER_ISSUED = "offer.issued"
    OFFER_CONVERTED_TO_PROJECT = "offer.converted_to_project"
    OFFER_DECLINED = "offer.declined"
    RECORD_CLOSED = "record.closed"


LIFECYCLE_REASON_BY_ACTION = {
    "convert_to_offer": OpportunityHistoryReason.OPPORTUNITY_CONVERTED_TO_OFFER,
    "convert_to_project": OpportunityHistoryReason.OFFER_CONVERTED_TO_PROJECT,
    "decline_offer": OpportunityHistoryReason.OFFER_DECLINED,
    "close": OpportunityHistoryReason.RECORD_CLOSED,
}


def prepare_opportunity_history(instance, reason, actor=None):
    instance._change_reason = reason
    if actor is not None:
        instance._history_user = actor


def annotate_latest_opportunity_history(instance, reason, actor=None):
    history = instance.history.order_by("-history_date", "-history_id").first()
    if not history:
        return
    update_fields = {"history_change_reason": reason}
    if actor is not None:
        update_fields["history_user_id"] = actor.pk
    instance.history.filter(history_id=history.history_id).update(**update_fields)
