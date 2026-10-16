# CORR-UX-02B: the register exposes the canonical ready_to_send per row (never inferred from the
# workflow state), reusing services.correspondence_workflow.ready_to_send unchanged.
from project_management.models import Correspondence
from project_management.services.correspondances_register import annotate_ready_to_send
from project_management.services.correspondence_workflow import (
    WorkflowType,
    perform_action,
    ready_to_send,
    start_validation,
)
from project_management.test_correspondence_validation_workflow import WorkflowTestBase


def row_of(corr, *, validation_status, entity_type="CORRESPONDENCE"):
    return {
        "id": corr.pk,
        "entity_type": entity_type,
        "direction": corr.direction,
        "lifecycle_status": corr.status,
        "validation_status": validation_status,
    }


class RegisterReadyToSendTests(WorkflowTestBase):
    def approve(self, corr):
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr

    def test_an_approved_out_without_a_recipient_is_not_ready(self):
        # the real LLDP-COR-00009 situation: signed, DRAFT, no recipient
        corr = self.approve(self.create_out(recipient_company=None))
        self.assertFalse(ready_to_send(corr))
        (row,) = annotate_ready_to_send([row_of(corr, validation_status="APPROVED")])
        self.assertIs(row["ready_to_send"], False)

    def test_an_approved_out_with_a_recipient_is_ready(self):
        corr = self.approve(self.create_out(recipient_company=self.client_company.id))
        self.assertTrue(ready_to_send(corr))
        (row,) = annotate_ready_to_send([row_of(corr, validation_status="APPROVED")])
        self.assertIs(row["ready_to_send"], True)

    def test_not_started_pending_and_rejected_rows_are_never_ready(self):
        draft = self.create_out(recipient_company=self.client_company.id)
        pending = self.create_out(recipient_company=self.client_company.id)
        start_validation(pending, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        rows = annotate_ready_to_send([
            row_of(draft, validation_status=None),
            row_of(pending, validation_status="PENDING_APPROVAL"),
            row_of(pending, validation_status="REJECTED"),
        ])
        self.assertEqual([r["ready_to_send"] for r in rows], [False, False, False])

    def test_a_sent_row_and_other_entities_are_never_ready(self):
        corr = self.approve(self.create_out(recipient_company=self.client_company.id))
        Correspondence.objects.filter(pk=corr.pk).update(status="ISSUED")
        incoming = self.create(
            direction="IN", sender_company=self.client_company.id, external_reference="REF-IN"
        )
        corr.refresh_from_db()
        rows = annotate_ready_to_send([
            row_of(corr, validation_status="APPROVED"),
            row_of(incoming, validation_status=None),
            {**row_of(corr, validation_status="APPROVED", entity_type="TRANSMITTAL")},
        ])
        self.assertEqual([r["ready_to_send"] for r in rows], [False, False, False])

    def test_the_flag_always_equals_the_canonical_rule(self):
        with_recipient = self.approve(self.create_out(recipient_company=self.client_company.id))
        without = self.approve(self.create_out(recipient_company=None, subject="No recipient"))
        rows = annotate_ready_to_send([
            row_of(with_recipient, validation_status="APPROVED"),
            row_of(without, validation_status="APPROVED"),
        ])
        for row, corr in zip(rows, (with_recipient, without)):
            self.assertEqual(row["ready_to_send"], ready_to_send(corr))
