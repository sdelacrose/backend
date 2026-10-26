# MAIL-1.2C: an authorized administrator can create a personal (individual) Microsoft 365
# mailbox for a user of their own company, through the existing create endpoint. The owner
# connects it with the existing OAuth action; nothing is connected, granted or sent here.
from django.test import TestCase
from rest_framework.test import APIClient

from userapis.models import MailboxConnection, MailboxProjectAssignment, MailboxUserGrant
from userapis.test_mailbox import _grant_mail_admin, _make_company, _make_project, _make_user

URL = "/api/user/mailbox-connections/"


class PersonalMailboxCreateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = _make_company("Acme")
        self.admin = _make_user("admin@example.com", self.company)
        _grant_mail_admin(self.admin, self.company)
        self.owner = _make_user("owner@example.com", self.company)
        self.other_member = _make_user("member@example.com", self.company)
        self.foreign_company = _make_company("Other")
        self.foreign_user = _make_user("foreign@example.com", self.foreign_company)

    def create(self, **extra):
        payload = {
            "provider": "microsoft",
            "email_address": "owner.mail@example.com",
            "display_name": "Owner Mail",
            "connection_type": "individual",
            "owner_user": self.owner.id,
            **extra,
        }
        return self.client.post(URL, payload, format="json")

    def test_admin_creates_a_personal_mailbox_pending_for_a_valid_owner(self):
        self.client.force_authenticate(self.admin)
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        mailbox = MailboxConnection.objects.get(pk=response.data["id"])
        self.assertEqual(mailbox.connection_type, "individual")
        self.assertEqual(mailbox.owner_user_id, self.owner.id)
        self.assertEqual(mailbox.company_profile_id, self.company.id)
        # Never active without the owner's own OAuth; nobody is the delegate yet.
        self.assertEqual(mailbox.status, "pending")
        self.assertIsNone(mailbox.connected_by_id)
        # No implicit sharing of read/send rights, no project assignment.
        self.assertFalse(MailboxUserGrant.objects.filter(mailbox=mailbox).exists())
        self.assertFalse(MailboxProjectAssignment.objects.filter(mailbox=mailbox).exists())
        self.assertEqual(response.data["owner_user_email"], "owner@example.com")

    def test_an_owner_is_required_for_a_personal_mailbox(self):
        self.client.force_authenticate(self.admin)
        for extra in ({"owner_user": None}, {"owner_user": ""}):
            response = self.create(**extra)
            self.assertEqual(response.status_code, 400, extra)
            self.assertIn("owner_user", response.data)
        payload = {
            "provider": "microsoft", "email_address": "x@example.com", "display_name": "X",
            "connection_type": "individual",
        }
        self.assertEqual(self.client.post(URL, payload, format="json").status_code, 400)
        self.assertEqual(MailboxConnection.objects.count(), 0)

    def test_a_user_of_another_company_cannot_be_the_owner(self):
        self.client.force_authenticate(self.admin)
        response = self.create(owner_user=self.foreign_user.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("owner_user", response.data)
        # Same answer as an unknown id: no way to probe which users exist elsewhere.
        unknown = self.create(owner_user=999999)
        self.assertEqual(
            [error.code for error in unknown.data["owner_user"]],
            [error.code for error in response.data["owner_user"]],
        )
        self.assertEqual(response.data["owner_user"][0].code, "does_not_exist")
        self.assertEqual(MailboxConnection.objects.count(), 0)

    def test_an_inactive_user_cannot_be_the_owner(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.create().status_code, 400)

    def test_an_unauthorized_user_cannot_create_any_mailbox(self):
        for user in (self.other_member, self.owner):
            self.client.force_authenticate(user)
            self.assertEqual(self.create().status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.create().status_code, (401, 403))
        self.assertEqual(MailboxConnection.objects.count(), 0)

    def test_a_duplicate_address_is_a_field_error_whatever_the_type(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.create().status_code, 201)
        response = self.create(email_address="OWNER.mail@example.com", owner_user=self.other_member.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("email_address", response.data)
        shared = self.client.post(
            URL,
            {"provider": "microsoft", "email_address": "owner.mail@example.com", "display_name": "S"},
            format="json",
        )
        self.assertEqual(shared.status_code, 400)
        self.assertEqual(MailboxConnection.objects.count(), 1)

    def test_the_shared_payload_is_unchanged_and_ignores_an_owner(self):
        self.client.force_authenticate(self.admin)
        plain = self.client.post(
            URL,
            {"provider": "microsoft", "email_address": "info@example.com", "display_name": "Info"},
            format="json",
        )
        self.assertEqual(plain.status_code, 201, plain.data)
        self.assertEqual(plain.data["connection_type"], "shared")
        self.assertIsNone(plain.data["owner_user"])
        with_owner = self.client.post(
            URL,
            {
                "provider": "microsoft", "email_address": "info2@example.com",
                "display_name": "Info2", "connection_type": "shared",
                "owner_user": self.owner.id,
            },
            format="json",
        )
        self.assertEqual(with_owner.status_code, 201, with_owner.data)
        self.assertEqual(with_owner.data["connection_type"], "shared")
        self.assertIsNone(with_owner.data["owner_user"])

    def test_only_the_owner_can_start_the_oauth_connection(self):
        self.client.force_authenticate(self.admin)
        mailbox_id = self.create().data["id"]
        url = f"{URL}{mailbox_id}/connect-oauth/"
        # The creating administrator is not the owner: the existing owner-only rule applies.
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_authenticate(self.other_member)
        self.assertIn(self.client.post(url).status_code, (403, 404))
        self.client.force_authenticate(self.owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("oauth_url", response.data)
        mailbox = MailboxConnection.objects.get(pk=mailbox_id)
        self.assertEqual(mailbox.status, "pending")  # starting OAuth activates nothing

    def test_the_owner_finds_it_in_my_mailboxes_and_nobody_else_does(self):
        self.client.force_authenticate(self.admin)
        mailbox_id = self.create().data["id"]
        self.client.force_authenticate(self.owner)
        rows = self.client.get("/api/user/mailboxes/accessible").data
        row = next(r for r in rows if r["id"] == mailbox_id)
        self.assertEqual(row["status"], "pending")
        self.assertFalse(row["can_send"])  # not connected yet: never a sender
        for user in (self.admin, self.other_member):
            self.client.force_authenticate(user)
            ids = {r["id"] for r in self.client.get("/api/user/mailboxes/accessible").data}
            self.assertNotIn(mailbox_id, ids)

    def test_once_connected_it_is_a_sender_only_for_its_owner_and_assigned_projects(self):
        project = _make_project(self.company)
        other_project = _make_project(self.company)
        self.client.force_authenticate(self.admin)
        mailbox_id = self.create().data["id"]
        MailboxProjectAssignment.objects.create(mailbox_id=mailbox_id, project=project)
        # The owner's own OAuth callback is what activates it (simulated: no network here).
        MailboxConnection.objects.filter(pk=mailbox_id).update(
            status="active", connected_by=self.owner
        )
        self.client.force_authenticate(self.owner)
        row = next(
            r for r in self.client.get("/api/user/mailboxes/accessible").data
            if r["id"] == mailbox_id
        )
        self.assertTrue(row["can_send"])
        self.assertEqual([p["id"] for p in row["sendable_projects"]], [project.id])
        self.assertNotIn(other_project.id, [p["id"] for p in row["sendable_projects"]])
        # The admin who created it gets no read/send right on it.
        self.client.force_authenticate(self.admin)
        self.assertNotIn(
            mailbox_id, {r["id"] for r in self.client.get("/api/user/mailboxes/accessible").data}
        )

    def test_a_foreign_admin_cannot_reach_or_administer_the_mailbox(self):
        self.client.force_authenticate(self.admin)
        mailbox_id = self.create().data["id"]
        foreign_admin = _make_user("foreign.admin@example.com", self.foreign_company)
        _grant_mail_admin(foreign_admin, self.foreign_company)
        self.client.force_authenticate(foreign_admin)
        self.assertEqual(self.client.get(f"{URL}{mailbox_id}/").status_code, 404)
        self.assertEqual(self.client.post(f"{URL}{mailbox_id}/connect-oauth/").status_code, 404)
