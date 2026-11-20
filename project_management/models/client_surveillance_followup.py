"""LOT 1 (client surveillance automation): the explicit filiation from ONE client return to the revision (or
technical version) that follows from it, and to the send that closes the processing.

One return can produce at most ONE follow-up (OneToOne), and one revision can belong to at most ONE follow-up
(OneToOne): two concurrent follow-ups for the same return, or two returns claiming the same revision, are
impossible at database level. The row carries facts only; the next business step is always derived from the
real data (revision, workflow, send events), never stored.

Kinds are the category of the return: ACCEPTED keeps the same index (a BPE technical version), the three others
need the next index. `expected_state` is the immutable issue state (PREL/BPE) the derived revision must carry.
Closing facts (`closing_send_event`, `closed_at`, `applicable_at`, `next_surveillance`) are written once by the
service, from a verified SEND_SUCCEEDED, and never reopened.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ClientSurveillanceFollowUp(models.Model):
    class State(models.TextChoices):
        PREL = "PREL", "PREL"
        BPE = "BPE", "BPE"

    # Redundant on purpose (0257), immutable, derived from the return when not given: they make "one open follow-up per
    # document and client company" a database guarantee, without serialising the other clients of the document.
    document = models.ForeignKey("Document", on_delete=models.PROTECT, related_name="client_followups")
    client_company = models.ForeignKey("ContactCompany", on_delete=models.PROTECT, related_name="client_followups")
    # The documentary index this follow-up expects its derived revision to carry (null = not planned / legacy).
    expected_index = models.CharField(max_length=20, null=True, blank=True)
    client_return = models.OneToOneField("ClientSurveillanceReturn", on_delete=models.PROTECT, related_name="followup")
    surveillance = models.ForeignKey("ClientSurveillance", on_delete=models.PROTECT, related_name="followups")
    expected_state = models.CharField(max_length=4, choices=State.choices)
    derived_revision = models.OneToOneField("DocumentRevision", on_delete=models.PROTECT, null=True, blank=True, related_name="client_followup")
    closing_send_event = models.OneToOneField("TransmittalEvent", on_delete=models.PROTECT, null=True, blank=True, related_name="closed_client_followup")
    # C/D: the surveillance of the new PREL cycle that the same verified send opened.
    next_surveillance = models.OneToOneField("ClientSurveillance", on_delete=models.PROTECT, null=True, blank=True, related_name="opened_by_followup")
    applicable_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    # Set when the automatic path cannot decide safely (wrong client, wrong purpose, replaced return...).
    needs_review = models.CharField(max_length=40, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "client_company"], condition=Q(closed_at__isnull=True),
                name="client_followup_one_open_per_document_client",
            ),
            # one automated follow-up in progress per surveillance (a second return never starts a competing one)
            models.UniqueConstraint(fields=["surveillance"], condition=Q(closed_at__isnull=True), name="client_followup_one_open_per_surveillance"),
            models.CheckConstraint(check=Q(closed_at__isnull=True, closing_send_event__isnull=True) | Q(closed_at__isnull=False, closing_send_event__isnull=False), name="client_followup_closure_is_complete"),
            models.CheckConstraint(check=Q(derived_revision__isnull=False) | Q(closed_at__isnull=True), name="client_followup_closed_has_revision"),
        ]

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self._state.adding or (update_fields and "derived_revision" in update_fields):
            issue = self.client_return.client_issue
            document_id = issue.document_revision.document_id
            if self._state.adding:
                if self.document_id is None:
                    self.document_id = document_id
                if self.client_company_id is None:
                    self.client_company_id = issue.client_company_id
                if self.document_id != document_id or self.client_company_id != issue.client_company_id:
                    raise ValidationError("The follow-up must belong to the document and client company of its return.")
            if self.derived_revision_id and self.derived_revision.document_id != self.document_id:
                raise ValidationError("The derived revision must belong to the document of the follow-up.")
        return super().save(*args, **kwargs)
