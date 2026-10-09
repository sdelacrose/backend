# MAIL-1.2B-CONTRACT: AccessibleMailboxSerializer.sendable_projects is SEND-right based, while
# accessible_projects (READ-based), can_send and can_read keep their exact semantics.
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from userapis.mailbox_access import can_use_mailbox
from userapis.serializers.mailbox import AccessibleMailboxSerializer
from userapis.models import MailboxConnection, MailboxProjectAssignment, MailboxUserGrant
from userapis.test_mailbox import _make_company, _make_project, _make_user


class SendableProjectsContractTests(TestCase):
    def setUp(self):
        self.company = _make_company("Acme")
        self.user = _make_user("sender@example.com", self.company)
        self.project = _make_project(self.company)
        self.other_project = _make_project(self.company)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.request = type("R", (), {"user": self.user})()

    def _shared(self, *, status=MailboxConnection.Status.ACTIVE, email="info@example.com"):
        return MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address=email,
            company_profile=self.company,
            status=status,
        )

    def _grant(self, mailbox, *, can_send, can_read):
        return MailboxUserGrant.objects.create(
            mailbox=mailbox, user=self.user, can_send=can_send, can_read=can_read
        )

    def _row(self, mailbox):
        response = self.client.get("/api/user/mailboxes/accessible")
        self.assertEqual(response.status_code, 200, response.data)
        return next(row for row in response.data if row["id"] == mailbox.id)

    @staticmethod
    def _ids(projects):
        return [project["id"] for project in projects]

    def test_shared_send_only_exposes_assigned_project_but_read_projects_stay_empty(self):
        mailbox = self._shared()
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        self._grant(mailbox, can_send=True, can_read=False)
        row = self._row(mailbox)
        self.assertTrue(row["can_send"])
        self.assertFalse(row["can_read"])
        self.assertEqual(
            row["sendable_projects"],
            [{"id": self.project.id, "name": self.project.project_name}],
        )
        # accessible_projects is untouched: still READ-based, so empty without can_read.
        self.assertEqual(row["accessible_projects"], [])

    def test_shared_read_only_has_no_sendable_project(self):
        mailbox = self._shared()
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        self._grant(mailbox, can_send=False, can_read=True)
        row = self._row(mailbox)
        self.assertFalse(row["can_send"])
        self.assertTrue(row["can_read"])
        self.assertEqual(row["sendable_projects"], [])
        self.assertEqual(self._ids(row["accessible_projects"]), [self.project.id])

    def test_shared_send_and_read_exposes_the_project_in_both_lists(self):
        mailbox = self._shared()
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        self._grant(mailbox, can_send=True, can_read=True)
        row = self._row(mailbox)
        self.assertEqual(self._ids(row["sendable_projects"]), [self.project.id])
        self.assertEqual(self._ids(row["accessible_projects"]), [self.project.id])

    def test_shared_without_any_permission_has_no_projects_and_ungranted_is_not_listed(self):
        mailbox = self._shared()
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        self._grant(mailbox, can_send=False, can_read=False)
        row = self._row(mailbox)
        self.assertFalse(row["can_send"])
        self.assertFalse(row["can_read"])
        self.assertEqual(row["sendable_projects"], [])
        self.assertEqual(row["accessible_projects"], [])
        stranger = self._shared(email="nogrant@example.com")
        MailboxProjectAssignment.objects.create(mailbox=stranger, project=self.project)
        response = self.client.get("/api/user/mailboxes/accessible")
        self.assertNotIn(stranger.id, {r["id"] for r in response.data})

    def test_only_assigned_projects_are_sendable_and_match_can_use_mailbox(self):
        mailbox = self._shared()
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        self._grant(mailbox, can_send=True, can_read=False)
        row = self._row(mailbox)
        self.assertEqual(self._ids(row["sendable_projects"]), [self.project.id])
        self.assertTrue(
            can_use_mailbox(self.user, mailbox, for_sending=True, project=self.project)
        )
        self.assertFalse(
            can_use_mailbox(self.user, mailbox, for_sending=True, project=self.other_project)
        )

    def test_mailbox_without_assignment_has_no_sendable_project(self):
        mailbox = self._shared()
        self._grant(mailbox, can_send=True, can_read=True)
        row = self._row(mailbox)
        self.assertTrue(row["can_send"])
        self.assertEqual(row["sendable_projects"], [])

    def test_other_company_project_cannot_be_assigned_nor_exposed(self):
        foreign_company = _make_company("Other")
        foreign_project = _make_project(foreign_company)
        mailbox = self._shared()
        self._grant(mailbox, can_send=True, can_read=True)
        with self.assertRaises(ValidationError):
            MailboxProjectAssignment.objects.create(mailbox=mailbox, project=foreign_project)
        self.assertNotIn(foreign_project.id, self._ids(self._row(mailbox)["sendable_projects"]))
        # Another company's mailbox never reaches this user's payload.
        foreign_mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.SHARED,
            email_address="foreign@example.com",
            company_profile=foreign_company,
            status=MailboxConnection.Status.ACTIVE,
        )
        MailboxProjectAssignment.objects.create(mailbox=foreign_mailbox, project=foreign_project)
        response = self.client.get("/api/user/mailboxes/accessible")
        self.assertNotIn(foreign_mailbox.id, {r["id"] for r in response.data})

    def test_revoked_mailbox_has_no_sendable_project(self):
        mailbox = self._shared(status=MailboxConnection.Status.REVOKED)
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        self._grant(mailbox, can_send=True, can_read=True)
        row = self._row(mailbox)
        self.assertFalse(row["can_send"])
        self.assertEqual(row["sendable_projects"], [])
        self.assertEqual(row["accessible_projects"], [])

    def test_individual_mailbox_of_the_owner_exposes_its_assigned_project(self):
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="me@example.com",
            company_profile=self.company,
            owner_user=self.user,
            connected_by=self.user,
            status=MailboxConnection.Status.ACTIVE,
        )
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        row = self._row(mailbox)
        self.assertEqual(self._ids(row["sendable_projects"]), [self.project.id])
        self.assertEqual(self._ids(row["accessible_projects"]), [self.project.id])

    def test_individual_mailbox_of_someone_else_is_never_exposed(self):
        colleague = _make_user("colleague@example.com", self.company)
        mailbox = MailboxConnection.objects.create(
            provider=MailboxConnection.Provider.MICROSOFT,
            connection_type=MailboxConnection.ConnectionType.INDIVIDUAL,
            email_address="colleague-box@example.com",
            company_profile=self.company,
            owner_user=colleague,
            connected_by=colleague,
            status=MailboxConnection.Status.ACTIVE,
        )
        MailboxProjectAssignment.objects.create(mailbox=mailbox, project=self.project)
        response = self.client.get("/api/user/mailboxes/accessible")
        self.assertNotIn(mailbox.id, {r["id"] for r in response.data})


    def test_sendable_projects_adds_no_query_per_project(self):
        # Measured on the new field alone: it reads the view's prefetched assignments, so the
        # number of assigned projects must not change its query count. (The pre-existing
        # accessible_projects loop has its own per-project queries and is deliberately untouched.)
        mailbox = self._shared()
        self._grant(mailbox, can_send=True, can_read=False)
        for _ in range(5):
            MailboxProjectAssignment.objects.create(
                mailbox=mailbox, project=_make_project(self.company)
            )

        def count_queries(assigned):
            instance = MailboxConnection.objects.prefetch_related(
                "project_assignments__project"
            ).get(pk=mailbox.pk)
            serializer = AccessibleMailboxSerializer(context={"request": self.request})
            with CaptureQueriesContext(connection) as queries:
                result = serializer.get_sendable_projects(instance)
            self.assertEqual(len(result), assigned)
            return len(queries)

        five = count_queries(5)
        MailboxProjectAssignment.objects.filter(mailbox=mailbox).exclude(
            pk=MailboxProjectAssignment.objects.filter(mailbox=mailbox).first().pk
        ).delete()
        one = count_queries(1)
        self.assertEqual(five, one)
