"""GED-WF.1: issuing and sending a Transmittal need documents.transmittal_issue in the transmittal's
own project scope. The module-permission lookup is stubbed so every case states exactly which
rights the caller holds (same technique as the withdrawal endpoint tests)."""
from unittest.mock import patch

from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Transmittal
from project_management.permissions import TransmittalPermission
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_transmittal import TransmittalTestBase
from project_management.views.transmittal import TransmittalIssueView, TransmittalSendView


class TransmittalIssuePermissionTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        # GED-SEC.2 (SEC-06): a refusal is a 403 only inside the caller's own scope -- the approver is a member of the project
        from project_management.models import ProjectTeamMembers
        ProjectTeamMembers.objects.get_or_create(project=self.project, team_member=self.approver, defaults={"role": "Member", "project_access": "RW"})

    def call(self, view_class, transmittal, grants, user=None, seen=None):
        def fake_check(perm, caller, module, action, scope, context):
            if seen is not None:
                seen.append((module, action, scope))
            return module == "documents" and action in grants

        request = APIRequestFactory().post("/x/", {}, format="json")
        force_authenticate(request, user=user or self.approver)
        view = view_class.as_view({"post": "create"}, permission_classes=[])
        with patch.object(TransmittalPermission, "_check_permission", fake_check):
            return view(request, transmittal_id=transmittal.pk)

    def draft_with_item(self):
        transmittal = self.create_transmittal()
        add_transmittal_item(
            transmittal, self.make_approved_internal_revision(), "FOR_REVIEW", self.owner
        )
        return transmittal

    def stored(self, transmittal):
        return Transmittal.objects.get(pk=transmittal.pk)

    def test_issue_is_refused_without_transmittal_issue_and_nothing_is_recorded(self):
        for grants in ((), ("transmittal_view",), ("transmittal_update",), ("transmittal_create",),
                       ("update",), ("view",)):
            with self.subTest(grants=grants):
                transmittal = self.draft_with_item()
                response = self.call(TransmittalIssueView, transmittal, grants)
                self.assertEqual(response.status_code, 403)
                stored = self.stored(transmittal)
                self.assertEqual(stored.status, Transmittal.Status.DRAFT)
                self.assertIsNone(stored.issued_at)
                self.assertFalse(stored.events.filter(event_type="ISSUED").exists())

    def test_issue_is_allowed_with_transmittal_issue_or_full_access_in_the_project_scope(self):
        for grants in (("transmittal_issue",), ("full_access",)):
            with self.subTest(grants=grants):
                transmittal = self.draft_with_item()
                seen = []
                response = self.call(TransmittalIssueView, transmittal, grants, seen=seen)
                self.assertEqual(response.status_code, 200, getattr(response, "data", None))
                self.assertEqual(self.stored(transmittal).status, Transmittal.Status.ISSUED)
                self.assertIn(("documents", "transmittal_issue", "project"), seen)
                self.assertNotIn(("documents", "transmittal_issue", "company"), seen)

    def test_send_is_refused_without_transmittal_issue(self):
        transmittal = self.draft_with_item()
        for grants in ((), ("transmittal_view",), ("transmittal_update",), ("update",)):
            with self.subTest(grants=grants):
                response = self.call(TransmittalSendView, transmittal, grants)
                self.assertEqual(response.status_code, 403)
        self.assertFalse(self.stored(transmittal).events.filter(event_type__startswith="SEND").exists())

    def test_an_unknown_transmittal_is_a_404_not_a_permission_oracle(self):
        class Missing:
            pk = 999999

        self.assertEqual(
            self.call(TransmittalIssueView, Missing, ("transmittal_issue",)).status_code, 404
        )
