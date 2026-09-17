import unittest
from datetime import timedelta

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIClient, APIRequestFactory

from ecpmpapp.license_queries import (
    get_active_license_sql_condition,
    get_active_license_user_ids_by_product_names,
)
from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.signals import (
    notify_on_leave_request_delete,
    notify_on_leave_request_save,
)
from models.constants import FeedbackStatusType
from models.models import (
    CompanyProfile,
    CompanyConfig,
    Feedback,
    FeedbackResponse,
    Permission,
    Role,
    RolePermission,
    Staff,
    User,
    UserOutlookCredentials,
    UserRole,
)
from project_management.models import (
    Document,
    ECPMPConfig,
    ExpenseCard,
    Folder,
    MonthlyTimesheetSubmission,
    ObjectAssessment,
    Project,
    ProjectTeamMembers,
)
from project_management.signals import (
    create_default_folders,
    create_related_objects,
    notify_on_project_save,
    notify_on_folder_save,
    notify_on_document_save,
    notify_on_object_assessment_save,
)
from project_management.signals import notify_expensecard_delete, notify_expensecard_save
from userapis.models import CalendarEvent
from userapis.permissions import CalendarEventPermission
from userapis.serializers import CalendarEventSerializer
from userapis.signals import (
    notify_on_calendar_event_assignees_changed,
    notify_on_calendar_event_delete,
    notify_on_calendar_event_save,
)
from userapis.microsoft_outlook import (
    create_outlook_calendar_event,
    get_access_token,
    get_outlook_event_visibility_data,
    is_outlook_token_valid,
    is_private_outlook_event,
    sync_outlook_events,
)
from userapis.views.microsoft_auth import build_outlook_oauth_url


CALENDAR_TEST_REST_FRAMEWORK = {
    **settings.REST_FRAMEWORK,
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}


class UserSearchViewTests(TestCase):
    def setUp(self):
        self.license_permission_patcher = patch(
            "ecpmpapp.permissions.LicensePermission.has_permission",
            return_value=True,
        )
        self.license_permission_patcher.start()
        self.addCleanup(self.license_permission_patcher.stop)

        self.client = APIClient()
        self.request_user = User.objects.create_user(
            email="requester@example.com",
            password="password",
            first_name="Request",
            last_name="User",
            user_type="company_user",
            user_searchable=True,
        )
        self.request_company = CompanyProfile.objects.create(
            admin_user=self.request_user,
            company_name="Requester Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://requester.example.com",
            company_email_address="requester-co@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.request_user.company = self.request_company
        self.request_user.save(update_fields=["company"])

        self.external_admin = User.objects.create_user(
            email="admin@example.com",
            password="password",
            first_name="External",
            last_name="Admin",
            user_type="company_user",
            user_searchable=True,
            mobile_phone="200",
        )
        self.external_company = CompanyProfile.objects.create(
            admin_user=self.external_admin,
            company_name="Robotics Partners",
            company_address="Street 2",
            company_town="Paris",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://robotics.example.com",
            company_email_address="robotics@example.com",
            company_tax_reference="tax",
            company_iban="iban",
            sharemundo_links_id="linkscid_robotics",
        )
        self.external_admin.company = self.external_company
        self.external_admin.save(update_fields=["company"])

        self.searchable_user = User.objects.create_user(
            email="claire.search@example.com",
            password="password",
            first_name="Claire",
            last_name="Davoine",
            user_type="company_user",
            user_searchable=True,
            company=self.external_company,
            professional_headline="Automation specialist",
            current_position="Senior Consultant",
            qualifications="Robotics and energy systems",
            keywords="automation integration",
            city="Lyon",
            country="France",
            availability_location="Remote",
            spoken_languages=["English", "French"],
            sharemundo_links_id="linksuid_claire_search",
            mobile_phone="100",
        )
        User.objects.create_user(
            email="hidden@example.com",
            password="password",
            first_name="Hidden",
            last_name="Person",
            user_type="company_user",
            user_searchable=False,
            company=self.external_company,
            spoken_languages=["English"],
        )
        self.client.force_authenticate(self.request_user)

    def _result_ids(self, response):
        self.assertEqual(response.status_code, 200)
        return [item["id"] for item in response.data["results"]]

    def test_global_search_returns_matching_public_users(self):
        response = self.client.get("/api/user/user-search", {"global_search": "robotics"})

        self.assertIn(self.searchable_user.id, self._result_ids(response))

    def test_search_alias_returns_matching_public_users(self):
        response = self.client.get("/api/user/user-search", {"search": "automation"})

        self.assertIn(self.searchable_user.id, self._result_ids(response))

    def test_name_filter_matches_first_last_and_full_name(self):
        first_name_response = self.client.get("/api/user/user-search", {"name": "Claire"})
        full_name_response = self.client.get("/api/user/user-search", {"name": "Claire Davoine"})

        self.assertIn(self.searchable_user.id, self._result_ids(first_name_response))
        self.assertIn(self.searchable_user.id, self._result_ids(full_name_response))

    def test_spoken_languages_filter_uses_canonical_language_entries(self):
        response = self.client.get("/api/user/user-search", {"spoken_languages": "French"})

        self.assertIn(self.searchable_user.id, self._result_ids(response))

    def test_search_results_can_be_ordered_by_mobile_phone(self):
        response = self.client.get(
            "/api/user/user-search",
            {"global_search": "example.com", "ordering": "mobile_phone"},
        )

        result_ids = self._result_ids(response)

        self.assertLess(
            result_ids.index(self.searchable_user.id),
            result_ids.index(self.external_admin.id),
        )


class OutlookPrivateEventSyncTests(SimpleTestCase):
    def test_detects_private_outlook_events(self):
        event = {"sensitivity": "private"}

        self.assertTrue(is_private_outlook_event(event))

    def test_public_outlook_event_keeps_visible_fields(self):
        event = {
            "subject": "Weekly Sync",
            "bodyPreview": "Agenda for the meeting",
            "sensitivity": "normal",
            "isOnlineMeeting": True,
            "onlineMeeting": {"joinUrl": "https://example.com/meeting"},
            "attendees": [
                {"emailAddress": {"address": "user@example.com"}},
            ],
        }

        visibility_data = get_outlook_event_visibility_data(event)

        self.assertEqual(visibility_data["name"], "Weekly Sync")
        self.assertEqual(visibility_data["description"], "Agenda for the meeting")
        self.assertEqual(
            visibility_data["meeting_link"],
            "https://example.com/meeting",
        )
        self.assertEqual(len(visibility_data["assignees"]), 1)
        self.assertFalse(visibility_data["is_private"])

    def test_private_outlook_event_keeps_sync_fields_and_sets_private_flag(self):
        event = {
            "subject": "Medical Appointment",
            "bodyPreview": "Confidential details",
            "sensitivity": "private",
            "isOnlineMeeting": True,
            "onlineMeeting": {"joinUrl": "https://example.com/private"},
            "attendees": [
                {"emailAddress": {"address": "private@example.com"}},
            ],
        }

        visibility_data = get_outlook_event_visibility_data(event)

        self.assertEqual(visibility_data["name"], "Medical Appointment")
        self.assertEqual(visibility_data["description"], "Confidential details")
        self.assertEqual(
            visibility_data["meeting_link"],
            "https://example.com/private",
        )
        self.assertEqual(len(visibility_data["assignees"]), 1)
        self.assertTrue(visibility_data["is_private"])

    @patch("userapis.microsoft_outlook.requests.post")
    def test_get_access_token_requests_calendar_write_scope(self, post):
        post.return_value.json.return_value = {"access_token": "token"}

        get_access_token("auth-code")

        requested_scope = post.call_args.kwargs.get("data", {}).get("scope", "")
        self.assertIn("Calendars.ReadWrite", requested_scope)
        self.assertIn("Contacts.Read", requested_scope)
        self.assertIn("offline_access", requested_scope)

    @override_settings(
        MS_CLIENT_ID="client-id",
        MS_REDIRECT_URI="http://localhost:8000/api/user/auth/outlook/callback",
    )
    def test_build_outlook_oauth_url_preserves_redirect_without_project(self):
        user = MagicMock()
        user.id = 582

        url = build_outlook_oauth_url(
            user,
            "energy_consultant_solution",
            redirect_to="ai_assistant",
        )

        self.assertIn("state=582%3Aenergy_consultant_solution%3A%3Aai_assistant", url)


class MicrosoftOutlookAuthCallbackTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="outlook-auth@example.com",
            password="password",
            first_name="Outlook",
            last_name="Auth",
            user_type="company_user",
        )

    @patch("userapis.views.microsoft_auth.sync_outlook_events")
    @patch("userapis.views.microsoft_auth.get_access_token")
    def test_ai_assistant_callback_closes_popup_without_dashboard_redirect(self, get_token, sync_events):
        get_token.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        sync_events.return_value = {"success": True, "message": "Synced"}

        response = self.client.get(
            "/api/user/auth/outlook/callback/",
            {
                "code": "auth-code",
                "state": f"{self.user.id}:energy_consultant_solution::ai_assistant",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"outlook_calendar_connected", response.content)
        self.assertIn(b"window.close()", response.content)
        self.assertNotIn(b"calendar-management", response.content)

    @patch("userapis.microsoft_outlook.requests.post")
    def test_create_outlook_calendar_event_posts_graph_event(self, post):
        user = MagicMock()
        user.outlook_credentials.outlook_access_token = "token"
        user.outlook_credentials.outlook_token_expires = timezone.now() + timedelta(hours=1)
        event = MagicMock()
        event.name = "Project meeting"
        event.description = "Discuss milestone"
        event.start_date = timezone.now()
        event.end_date = event.start_date + timedelta(minutes=30)
        event.assignees.all.return_value = []
        post.return_value.status_code = 201
        post.return_value.json.return_value = {
            "id": "graph-id",
            "uid": "graph-uid",
            "onlineMeeting": {"joinUrl": "https://teams.example/meeting"},
        }

        result = create_outlook_calendar_event(
            user,
            event,
            attendee_emails=["guest@example.com"],
            online_meeting=True,
        )

        self.assertTrue(result["success"])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["subject"], "Project meeting")
        self.assertTrue(payload["isOnlineMeeting"])
        self.assertNotIn("onlineMeetingProvider", payload)
        self.assertEqual(payload["attendees"][0]["emailAddress"]["address"], "guest@example.com")
        self.assertEqual(event.outlook_event_id, "graph-id")
        self.assertEqual(event.meeting_link, "https://teams.example/meeting")
        event.save.assert_called_once_with(update_fields=["outlook_event_id", "meeting_link", "user"])

    @patch("userapis.microsoft_outlook.requests.get")
    @patch("userapis.microsoft_outlook.requests.post")
    def test_create_outlook_calendar_event_fetches_missing_meeting_link(self, post, get):
        user = MagicMock()
        user.outlook_credentials.outlook_access_token = "token"
        user.outlook_credentials.outlook_token_expires = timezone.now() + timedelta(hours=1)
        event = MagicMock()
        event.name = "Project meeting"
        event.description = "Discuss milestone"
        event.start_date = timezone.now()
        event.end_date = event.start_date + timedelta(minutes=30)
        event.assignees.all.return_value = []
        post.return_value.status_code = 201
        post.return_value.json.return_value = {
            "id": "graph-id/with-special+chars",
            "uid": "graph-uid",
        }
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            "id": "graph-id/with-special+chars",
            "isOnlineMeeting": True,
            "onlineMeeting": {"joinUrl": "https://teams.example/meeting"},
        }

        result = create_outlook_calendar_event(
            user,
            event,
            online_meeting=True,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["meeting_link"], "https://teams.example/meeting")
        self.assertEqual(event.outlook_event_id, "graph-id/with-special+chars")
        self.assertIn("graph-id%2Fwith-special%2Bchars", get.call_args.args[0])
        event.save.assert_called_once_with(update_fields=["outlook_event_id", "meeting_link", "user"])


class OutlookEventSyncTests(TestCase):
    @override_settings(MS_CLIENT_ID="client-id", MS_CLIENT_SECRET="client-secret")
    @patch("userapis.microsoft_outlook.requests.post")
    def test_expired_outlook_token_is_refreshed_without_reconnection(self, post):
        user = User.objects.create(
            email="outlook-refresh@example.com",
            last_name="Refresh",
            user_type="company_user",
        )
        credentials = UserOutlookCredentials.objects.create(
            user=user,
            outlook_access_token="expired-token",
            outlook_refresh_token="refresh-token",
            outlook_token_expires=timezone.now() - timedelta(minutes=5),
        )
        post.return_value.ok = True
        post.return_value.json.return_value = {
            "access_token": "refreshed-token",
            "refresh_token": "rotated-refresh-token",
            "expires_in": 3600,
        }

        self.assertTrue(is_outlook_token_valid(user))

        credentials.refresh_from_db()
        self.assertEqual(credentials.outlook_access_token, "refreshed-token")
        self.assertEqual(credentials.outlook_refresh_token, "rotated-refresh-token")
        self.assertGreater(credentials.outlook_token_expires, timezone.now())
        self.assertEqual(
            post.call_args.kwargs["data"]["grant_type"],
            "refresh_token",
        )

    @patch("userapis.microsoft_outlook.reschedule_event")
    @patch("userapis.microsoft_outlook.get_outlook_events")
    def test_sync_matches_created_graph_event_id(self, get_events, reschedule_event):
        user = User.objects.create(
            email="outlook-sync@example.com",
            last_name="Sync",
            user_type="company_user",
        )
        existing = CalendarEvent.objects.create(
            user=user,
            outlook_event_id="graph-id",
            name="Original",
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(minutes=30),
        )
        get_events.return_value.status_code = 200
        get_events.return_value.json.return_value = {
            "value": [
                {
                    "id": "graph-id",
                    "uid": "uid-id",
                    "iCalUId": "ical-id",
                    "subject": "Updated from Outlook",
                    "bodyPreview": "Updated description",
                    "sensitivity": "normal",
                    "isOnlineMeeting": True,
                    "onlineMeeting": {"joinUrl": "https://teams.example/synced"},
                    "start": {"dateTime": "2026-06-24T10:00:00"},
                    "end": {"dateTime": "2026-06-24T10:30:00"},
                    "attendees": [],
                },
            ],
        }

        result = sync_outlook_events(user, "token")

        self.assertTrue(result["success"])
        self.assertEqual(CalendarEvent.objects.filter(user=user).count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.outlook_event_id, "graph-id")
        self.assertEqual(existing.name, "Updated from Outlook")
        self.assertEqual(existing.meeting_link, "https://teams.example/synced")
        reschedule_event.assert_called_with(existing, is_deleted=False)


class ActiveLicenseQueryTests(unittest.TestCase):
    @patch("ecpmpapp.license_queries.connection.cursor")
    def test_active_license_query_uses_any_parameter(self, cursor_factory):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [(10,), (12,)]
        cursor_factory.return_value = cursor

        user_ids = get_active_license_user_ids_by_product_names(
            ["Energy Consultant Solution", "Enterprise Solution"]
        )

        self.assertEqual(user_ids, [10, 12])
        executed_query, executed_params = cursor.execute.call_args.args
        self.assertIn("p.name = ANY(%s)", executed_query)
        self.assertIn("pkg.status = 'Auto Renew'", executed_query)
        self.assertIn("pkg.stripe_subscription_id IS NOT NULL", executed_query)
        self.assertIn("INTERVAL '24 hours'", executed_query)
        self.assertEqual(executed_params[0], ["Energy Consultant Solution", "Enterprise Solution"])

    def test_active_license_condition_has_paid_auto_renew_grace_period(self):
        condition = get_active_license_sql_condition()

        self.assertIn("pkg.expires_at > NOW()", condition)
        self.assertIn("pkg.status = 'Auto Renew'", condition)
        self.assertIn("pkg.is_freetrial = FALSE", condition)
        self.assertIn("pkg.stripe_subscription_id IS NOT NULL", condition)
        self.assertIn("INTERVAL '24 hours'", condition)


@override_settings(REST_FRAMEWORK=CALENDAR_TEST_REST_FRAMEWORK)
class CalendarEventSerializerProjectScopeTests(TestCase):
    def setUp(self):
        from django.db.models.signals import m2m_changed, post_save, pre_delete

        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(create_related_objects, sender=ProjectTeamMembers)
        post_save.disconnect(notify_on_folder_save, sender=Folder)
        post_save.disconnect(notify_on_document_save, sender=Document)
        post_save.disconnect(notify_on_object_assessment_save, sender=ObjectAssessment)
        post_save.disconnect(notify_on_calendar_event_save, sender=CalendarEvent)
        m2m_changed.disconnect(
            notify_on_calendar_event_assignees_changed,
            sender=CalendarEvent.assignees.through,
        )
        pre_delete.disconnect(notify_on_calendar_event_delete, sender=CalendarEvent)
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            create_related_objects,
            sender=ProjectTeamMembers,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_folder_save,
            sender=Folder,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_document_save,
            sender=Document,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_object_assessment_save,
            sender=ObjectAssessment,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_calendar_event_save,
            sender=CalendarEvent,
        )
        self.addCleanup(
            m2m_changed.connect,
            notify_on_calendar_event_assignees_changed,
            sender=CalendarEvent.assignees.through,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_calendar_event_delete,
            sender=CalendarEvent,
        )

        self.factory = APIRequestFactory()
        self.api_client = APIClient()
        self.company_user = User.objects.create_user(
            email="calendar-owner@example.com",
            password="password",
            first_name="Calendar",
            last_name="Owner",
            user_type="company_user",
        )
        self.team_member = User.objects.create_user(
            email="team-member@example.com",
            password="password",
            first_name="Team",
            last_name="Member",
            user_type="company_user",
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com",
            password="password",
            first_name="Out",
            last_name="Sider",
            user_type="company_user",
        )
        self.other_company_user = User.objects.create_user(
            email="other-company-calendar@example.com",
            password="password",
            first_name="Other",
            last_name="Company",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.company_user,
            company_name="Calendar Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="calendar@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_company_user,
            company_name="Other Calendar Co",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="other-calendar@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )

        for user in [self.company_user, self.team_member, self.outsider]:
            user.company = self.company
            user.save(update_fields=["company"])

        self.other_company_user.company = self.other_company
        self.other_company_user.save(update_fields=["company"])

        self.project = Project.objects.create(
            solution="Energy Consultant Solution",
            project_name="Main Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.company_user,
        )
        self.other_project = Project.objects.create(
            solution="Energy Consultant Solution",
            project_name="Other Project",
            contract_value=1000,
            company_profile=self.other_company,
            user=self.other_company_user,
        )

        ProjectTeamMembers.objects.create(
            project=self.project,
            team_member=self.team_member,
            role="Engineer",
        )

        self.project_folder = Folder.objects.create(
            project=self.project,
            name="Project Docs",
        )
        self.other_project_folder = Folder.objects.create(
            project=self.other_project,
            name="Other Project Docs",
        )
        self.project_document = Document.objects.create(
            folder=self.project_folder,
            file=SimpleUploadedFile("project.txt", b"project"),
            file_name="project.txt",
        )
        self.other_project_document = Document.objects.create(
            folder=self.other_project_folder,
            file=SimpleUploadedFile("other.txt", b"other"),
            file_name="other.txt",
        )

        self.project_object_assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Photo",
            record_title="Project Observation",
            file=SimpleUploadedFile("assessment.txt", b"assessment"),
        )
        self.other_project_object_assessment = ObjectAssessment.objects.create(
            project=self.other_project,
            record_type="Photo",
            record_title="Other Observation",
            file=SimpleUploadedFile("other-assessment.txt", b"assessment"),
        )

    def _build_request(self, user):
        request = self.factory.post("/api/user/events/")
        request.user = user
        request.query_params = request.GET
        return request

    def _build_payload(self, **overrides):
        payload = {
            "name": "Project event",
            "start_date": "2026-06-20T10:00:00Z",
            "end_date": "2026-06-20T11:00:00Z",
            "project": self.project.id,
            "documents": [self.project_document.id],
            "object_assessments": [self.project_object_assessment.id],
            "assignees": [self.team_member.id],
        }
        payload.update(overrides)
        return payload

    def test_rejects_project_scope_without_explicit_context_flag(self):
        serializer = CalendarEventSerializer(
            data=self._build_payload(),
            context={"request": self._build_request(self.company_user)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("project", serializer.errors)

    def test_rejects_document_from_other_project(self):
        serializer = CalendarEventSerializer(
            data=self._build_payload(documents=[self.other_project_document.id]),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("documents", serializer.errors)

    def test_rejects_object_assessment_from_other_project(self):
        serializer = CalendarEventSerializer(
            data=self._build_payload(
                object_assessments=[self.other_project_object_assessment.id]
            ),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("object_assessments", serializer.errors)

    def test_rejects_assignee_outside_project_team(self):
        serializer = CalendarEventSerializer(
            data=self._build_payload(assignees=[self.outsider.id]),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("assignees", serializer.errors)

    def test_accepts_valid_project_scoped_payload(self):
        serializer = CalendarEventSerializer(
            data=self._build_payload(),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_overlapping_company_room_booking(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            user=self.company_user,
            created_by=self.company_user,
            name="Existing meeting",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data={
                "name": "Overlapping meeting",
                "room": "Room C",
                "start_date": "2026-06-20T22:15:00Z",
                "end_date": "2026-06-20T23:30:00Z",
            },
            context={"request": self._build_request(self.company_user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.room_warning["room"], "Room C")
        self.assertEqual(
            serializer.room_warning["start"],
            "2026-06-20T21:00:00+00:00",
        )
        self.assertEqual(
            serializer.room_warning["end"],
            "2026-06-20T23:00:00+00:00",
        )
        self.assertIn("booked_by", serializer.room_warning)
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.company_user.id,
        )
        self.assertTrue(serializer.room_warning["is_self_booking"])
        self.assertEqual(serializer.room_warning["event_name"], "Existing meeting")
        self.assertIsNone(serializer.room_warning["project_name"])

    def test_room_warning_marks_other_user_conflict_as_not_self(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            user=self.team_member,
            created_by=self.team_member,
            name="Teammate meeting",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data={
                "name": "Overlapping meeting",
                "room": "Room C",
                "start_date": "2026-06-20T22:15:00Z",
                "end_date": "2026-06-20T23:30:00Z",
            },
            context={"request": self._build_request(self.company_user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(serializer.room_warning["is_self_booking"])
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.team_member.id,
        )

    def test_room_warning_is_not_self_when_user_is_only_assignee(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        existing_event = CalendarEvent.objects.create(
            company_profile=self.company,
            user=self.company_user,
            created_by=self.company_user,
            name="Organizer meeting",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )
        existing_event.assignees.add(self.team_member)

        serializer = CalendarEventSerializer(
            data={
                "name": "Overlapping meeting",
                "room": "Room C",
                "start_date": "2026-06-20T22:15:00Z",
                "end_date": "2026-06-20T23:30:00Z",
            },
            context={"request": self._build_request(self.team_member)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(serializer.room_warning["is_self_booking"])
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.company_user.id,
        )

    def test_allows_non_overlapping_company_room_booking(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            user=self.company_user,
            created_by=self.company_user,
            name="Existing meeting",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data={
                "name": "Later meeting",
                "room": "Room C",
                "start_date": "2026-06-20T23:00:00Z",
                "end_date": "2026-06-20T23:30:00Z",
            },
            context={"request": self._build_request(self.company_user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_overlapping_project_room_booking(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            project=self.project,
            user=self.company_user,
            created_by=self.company_user,
            name="Existing project meeting",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data=self._build_payload(
                room="Room C",
                start_date="2026-06-20T22:15:00Z",
                end_date="2026-06-20T23:30:00Z",
                documents=[],
                object_assessments=[],
                assignees=[],
            ),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.room_warning["room"], "Room C")
        self.assertEqual(
            serializer.room_warning["start"],
            "2026-06-20T21:00:00+00:00",
        )
        self.assertEqual(
            serializer.room_warning["end"],
            "2026-06-20T23:00:00+00:00",
        )
        self.assertIn("booked_by", serializer.room_warning)
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.company_user.id,
        )
        self.assertTrue(serializer.room_warning["is_self_booking"])

    def test_project_event_warns_when_company_calendar_already_booked_same_room(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            user=self.company_user,
            created_by=self.company_user,
            name="Company room booking",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data=self._build_payload(
                room="Room C",
                start_date="2026-06-20T22:15:00Z",
                end_date="2026-06-20T23:30:00Z",
                documents=[],
                object_assessments=[],
                assignees=[],
            ),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.room_warning["room"], "Room C")
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.company_user.id,
        )
        self.assertEqual(serializer.room_warning["event_name"], "Company room booking")
        self.assertIsNone(serializer.room_warning["project_name"])

    def test_company_event_warns_when_project_calendar_already_booked_same_room(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            project=self.project,
            user=self.company_user,
            created_by=self.company_user,
            name="Project room booking",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data={
                "name": "Company overlap",
                "room": "Room C",
                "start_date": "2026-06-20T22:15:00Z",
                "end_date": "2026-06-20T23:30:00Z",
            },
            context={"request": self._build_request(self.company_user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.room_warning["room"], "Room C")
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.company_user.id,
        )
        self.assertEqual(serializer.room_warning["event_name"], "Project room booking")
        self.assertEqual(
            serializer.room_warning["project_name"],
            self.project.project_name,
        )

    def test_project_event_warns_when_existing_project_booking_has_different_stored_company(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.other_company,
            project=self.project,
            user=self.other_company_user,
            created_by=self.other_company_user,
            name="External collaborator booking",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data=self._build_payload(
                room="Room C",
                start_date="2026-06-20T22:15:00Z",
                end_date="2026-06-20T23:30:00Z",
                documents=[],
                object_assessments=[],
                assignees=[],
            ),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.room_warning["room"], "Room C")
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.other_company_user.id,
        )
        self.assertEqual(
            serializer.room_warning["event_name"],
            "External collaborator booking",
        )
        self.assertEqual(
            serializer.room_warning["project_name"],
            self.project.project_name,
        )

    def test_company_event_warns_when_existing_project_booking_has_different_stored_company(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.other_company,
            project=self.project,
            user=self.other_company_user,
            created_by=self.other_company_user,
            name="External collaborator booking",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )

        serializer = CalendarEventSerializer(
            data={
                "name": "Company overlap",
                "room": "Room C",
                "start_date": "2026-06-20T22:15:00Z",
                "end_date": "2026-06-20T23:30:00Z",
            },
            context={"request": self._build_request(self.company_user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.room_warning["room"], "Room C")
        self.assertEqual(
            serializer.room_warning["booked_by_user_id"],
            self.other_company_user.id,
        )
        self.assertEqual(
            serializer.room_warning["event_name"],
            "External collaborator booking",
        )
        self.assertEqual(
            serializer.room_warning["project_name"],
            self.project.project_name,
        )

    def test_project_event_create_uses_project_company_profile(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )

        serializer = CalendarEventSerializer(
            data=self._build_payload(
                room="Room C",
                documents=[],
                object_assessments=[],
                assignees=[],
            ),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        event = serializer.save()

        self.assertEqual(event.project_id, self.project.id)
        self.assertEqual(event.company_profile_id, self.project.company_profile_id)
        self.assertEqual(event.created_by_id, self.company_user.id)

    def test_project_event_uses_company_rooms_instead_of_project_config_rooms(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        ECPMPConfig.objects.create(
            project=self.project,
            rooms=["Project Only Room"],
        )

        serializer = CalendarEventSerializer(
            data=self._build_payload(
                room="Room C",
                documents=[],
                object_assessments=[],
                assignees=[],
            ),
            context={
                "request": self._build_request(self.company_user),
                "allow_project_scope": True,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_company_calendar_endpoint_returns_room_warning_without_saving(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            user=self.company_user,
            created_by=self.company_user,
            name="Existing meeting",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )
        self._grant_company_permissions(
            self.company_user,
            ["calendar_events.create"],
            role_name="Company Calendar Creator API",
        )
        self.api_client.force_authenticate(user=self.company_user)

        response = self.api_client.post(
            "/api/user/events/",
            {
                "name": "Overlapping meeting",
                "room": "Room C",
                "start_date": "2026-06-20T22:15:00Z",
                "end_date": "2026-06-20T23:30:00Z",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["room_warning"][0]["room"], "Room C")
        self.assertIn("booked_by", response.data["room_warning"][0])
        self.assertEqual(
            response.data["room_warning"][0]["booked_by_user_id"],
            self.company_user.id,
        )
        self.assertEqual(
            response.data["room_warning"][0]["event_name"],
            "Existing meeting",
        )
        self.assertIsNone(response.data["room_warning"][0]["project_name"])
        self.assertIn("start", response.data["room_warning"][0])
        self.assertIn("end", response.data["room_warning"][0])
        self.assertEqual(
            CalendarEvent.objects.filter(name="Overlapping meeting").count(),
            0,
        )

    def test_project_calendar_endpoint_returns_room_warning_without_saving(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            rooms=["Room C"],
        )
        CalendarEvent.objects.create(
            company_profile=self.company,
            project=self.project,
            user=self.company_user,
            created_by=self.company_user,
            name="Existing project meeting",
            room="Room C",
            start_date=parse_datetime("2026-06-20T21:00:00Z"),
            end_date=parse_datetime("2026-06-20T23:00:00Z"),
        )
        self._grant_project_permissions(
            self.team_member,
            ["calendar_events.create"],
            role_name="Project Calendar Room Warning API",
        )
        self.api_client.force_authenticate(user=self.team_member)

        response = self.api_client.post(
            "/api/project_management/project-calendar/",
            {
                "name": "Overlapping project meeting",
                "room": "Room C",
                "start_date": "2026-06-20T22:15:00Z",
                "end_date": "2026-06-20T23:30:00Z",
                "project": self.project.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["room_warning"][0]["room"], "Room C")
        self.assertIn("booked_by", response.data["room_warning"][0])
        self.assertEqual(
            response.data["room_warning"][0]["booked_by_user_id"],
            self.company_user.id,
        )
        self.assertEqual(
            response.data["room_warning"][0]["event_name"],
            "Existing project meeting",
        )
        self.assertEqual(
            response.data["room_warning"][0]["project_name"],
            self.project.project_name,
        )
        self.assertIn("start", response.data["room_warning"][0])
        self.assertIn("end", response.data["room_warning"][0])
        self.assertEqual(
            CalendarEvent.objects.filter(name="Overlapping project meeting").count(),
            0,
        )

    def _grant_project_permissions(self, user, permission_keys, role_name="Project Calendar Role"):
        role = Role.objects.create(
            name=role_name,
            company=self.company,
            scope="project",
            type="solution",
            created_by=self.company_user,
        )
        for permission_key in permission_keys:
            permission, _ = Permission.objects.get_or_create(
                key=permission_key,
                defaults={
                    "name": permission_key,
                    "module": permission_key.split(".")[0],
                },
            )
            RolePermission.objects.get_or_create(role=role, permission=permission)

        membership, _ = ProjectTeamMembers.objects.get_or_create(
            project=self.project,
            team_member=user,
            defaults={
                "role": "Engineer",
                "project_access": "RW",
            },
        )
        membership.project_role = role
        membership.save(update_fields=["project_role"])
        return role

    def _grant_company_permissions(self, user, permission_keys, role_name="Company Calendar Role"):
        role = Role.objects.create(
            name=role_name,
            company=self.company,
            scope="company",
            type="solution",
            created_by=self.company_user,
        )
        for permission_key in permission_keys:
            permission, _ = Permission.objects.get_or_create(
                key=permission_key,
                defaults={
                    "name": permission_key,
                    "module": permission_key.split(".")[0],
                },
            )
            RolePermission.objects.get_or_create(role=role, permission=permission)
        UserRole.objects.get_or_create(
            user=user,
            role=role,
            company=self.company,
            assigned_by=self.company_user,
        )
        return role

    def _calendar_event_kwargs(self, **overrides):
        start_date = timezone.now()
        defaults = {
            "name": "Event",
            "start_date": start_date,
            "end_date": start_date + timedelta(hours=1),
            "company_profile": self.company,
            "created_by": self.company_user,
            "user": self.company_user,
        }
        defaults.update(overrides)
        return defaults

    def test_project_scope_create_permission_uses_project_context(self):
        self._grant_project_permissions(
            self.team_member,
            ["calendar_events.create"],
            role_name="Project Calendar Creator",
        )
        request = SimpleNamespace(
            method="POST",
            user=self.team_member,
            data={"project": self.project.id},
            query_params={},
        )
        permission = CalendarEventPermission("create", "project")
        view = type("View", (), {"action": "create"})()

        self.assertTrue(permission.has_permission(request, view))

    def test_project_scope_view_assigned_created_filters_queryset(self):
        self._grant_project_permissions(
            self.team_member,
            ["calendar_events.view_assigned_created"],
            role_name="Project Calendar Viewer",
        )
        assigned_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Assigned event",
                project=self.project,
            )
        )
        assigned_event.assignees.set([self.team_member])
        created_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Created event",
                project=self.project,
                created_by=self.team_member,
                user=self.team_member,
            )
        )
        other_project_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Other event",
                project=self.project,
            )
        )
        foreign_project_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Foreign project event",
                company_profile=self.other_company,
                project=self.other_project,
                created_by=self.other_company_user,
                user=self.other_company_user,
            )
        )
        company_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Company event",
                project=None,
            )
        )

        permission = CalendarEventPermission("view", "project")
        queryset = permission.filter_queryset(
            CalendarEvent.objects.all(),
            self.team_member,
            {"project": self.project},
        )

        self.assertCountEqual(
            list(queryset.values_list("id", flat=True)),
            [assigned_event.id, created_event.id],
        )
        self.assertNotIn(other_project_event.id, queryset.values_list("id", flat=True))
        self.assertNotIn(foreign_project_event.id, queryset.values_list("id", flat=True))
        self.assertNotIn(company_event.id, queryset.values_list("id", flat=True))

    def test_project_scope_ignores_company_project_view_override_for_calendar_events(self):
        self._grant_company_permissions(
            self.team_member,
            ["projects.view_all_access"],
            role_name="Company Project Viewer",
        )
        visible_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Project event without calendar permission",
                project=self.project,
            )
        )

        permission = CalendarEventPermission("view", "project")
        queryset = permission.filter_queryset(
            CalendarEvent.objects.all(),
            self.team_member,
            {"project": self.project},
        )

        self.assertNotIn(visible_event.id, queryset.values_list("id", flat=True))

    def test_company_scope_filter_queryset_excludes_project_events(self):
        self._grant_company_permissions(
            self.company_user,
            ["calendar_events.view"],
            role_name="Company Calendar Viewer",
        )
        company_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Company event",
                project=None,
            )
        )
        project_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Project event",
                project=self.project,
            )
        )

        permission = CalendarEventPermission("view", "company")
        queryset = permission.filter_queryset(
            CalendarEvent.objects.all(),
            self.company_user,
            {},
        )

        self.assertIn(company_event.id, queryset.values_list("id", flat=True))
        self.assertNotIn(project_event.id, queryset.values_list("id", flat=True))

    def test_company_calendar_endpoint_only_returns_company_calendar_events(self):
        self._grant_company_permissions(
            self.company_user,
            ["calendar_events.view"],
            role_name="Company Calendar Endpoint Viewer",
        )
        company_event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Company endpoint event",
                project=None,
            )
        )
        CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Project endpoint event",
                project=self.project,
            )
        )

        self.api_client.force_authenticate(user=self.company_user)
        response = self.api_client.get(
            "/api/user/events/",
            {"all": "true"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [item["id"] for item in response.data["calendar_events"]],
            [company_event.id],
        )

    def test_project_calendar_endpoint_creates_project_event(self):
        self._grant_project_permissions(
            self.team_member,
            ["calendar_events.create"],
            role_name="Project Calendar Creator API",
        )
        self.api_client.force_authenticate(user=self.team_member)

        response = self.api_client.post(
            "/api/project_management/project-calendar/",
            {
                "name": "Created via project endpoint",
                "start_date": "2026-06-20T10:00:00Z",
                "end_date": "2026-06-20T11:00:00Z",
                "project": self.project.id,
                "documents": [self.project_document.id],
                "object_assessments": [self.project_object_assessment.id],
                "assignees": [self.team_member.id],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["project"], self.project.id)
        self.assertEqual(
            [item["id"] for item in response.data["object_assessments"]],
            [self.project_object_assessment.id],
        )
        self.assertTrue(
            CalendarEvent.objects.filter(
                id=response.data["id"],
                project=self.project,
            ).exists()
        )

    def test_project_calendar_endpoint_retrieve_update_delete(self):
        self._grant_project_permissions(
            self.team_member,
            [
                "calendar_events.full_access",
                "calendar_events.view",
                "calendar_events.update",
                "calendar_events.delete",
            ],
            role_name="Project Calendar Full API",
        )
        self.api_client.force_authenticate(user=self.team_member)

        event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Editable project event",
                project=self.project,
                created_by=self.team_member,
                user=self.team_member,
            )
        )
        event.assignees.set([self.team_member])
        event.documents.set([self.project_document])
        event.object_assessments.set([self.project_object_assessment])

        retrieve_response = self.api_client.get(
            f"/api/project_management/project-calendar/{event.id}/"
        )
        self.assertEqual(retrieve_response.status_code, 200, retrieve_response.data)
        self.assertEqual(retrieve_response.data["id"], event.id)
        self.assertEqual(
            [item["id"] for item in retrieve_response.data["object_assessments"]],
            [self.project_object_assessment.id],
        )

        update_response = self.api_client.patch(
            f"/api/project_management/project-calendar/{event.id}/",
            {"name": "Updated project event"},
            format="json",
        )
        self.assertEqual(update_response.status_code, 200, update_response.data)
        self.assertEqual(update_response.data["name"], "Updated project event")

        delete_response = self.api_client.delete(
            f"/api/project_management/project-calendar/{event.id}/"
        )
        self.assertEqual(delete_response.status_code, 204, delete_response.data)
        self.assertFalse(CalendarEvent.objects.filter(id=event.id).exists())

    @patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
    def test_project_calendar_aggregate_includes_calendar_events(self, _mock_license):
        self._grant_project_permissions(
            self.team_member,
            ["calendar_events.view"],
            role_name="Project Calendar Aggregate Viewer",
        )
        event_start = parse_datetime("2026-06-20T10:00:00Z")
        event = CalendarEvent.objects.create(
            **self._calendar_event_kwargs(
                name="Aggregate project event",
                project=self.project,
                created_by=self.team_member,
                user=self.team_member,
                start_date=event_start,
                end_date=event_start + timedelta(hours=1),
            )
        )
        event.assignees.set([self.team_member])

        self.api_client.force_authenticate(user=self.team_member)
        response = self.api_client.get(
            "/api/project_management/project-calendar/events/",
            {
                "project": self.project.id,
                "start_date": "2026-06-20T00:00:00Z",
                "end_date": "2026-06-21T00:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("calendar_events", response.data)
        self.assertEqual(
            [item["id"] for item in response.data["calendar_events"]],
            [event.id],
        )

    @patch("project_management.views.project_calendar.sync_outlook_events")
    @patch("project_management.views.project_calendar.is_outlook_token_valid")
    @patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
    def test_project_calendar_sync_endpoint_scopes_to_project(
        self,
        _mock_license,
        mock_is_outlook_token_valid,
        mock_sync_outlook_events,
    ):
        self._grant_project_permissions(
            self.team_member,
            ["calendar_events.create"],
            role_name="Project Calendar Sync Creator",
        )
        mock_is_outlook_token_valid.return_value = True
        mock_sync_outlook_events.return_value = {
            "success": True,
            "message": "Outlook events synced successfully.",
            "synced_events_count": 3,
        }

        UserOutlookCredentials.objects.update_or_create(
            user=self.team_member,
            defaults={
                "outlook_access_token": "token",
                "outlook_refresh_token": "refresh-token",
                "outlook_token_expires": timezone.now() + timedelta(hours=1),
            },
        )

        self.api_client.force_authenticate(user=self.team_member)
        response = self.api_client.post(
            "/api/project_management/project-calendar/sync-calendar/",
            {"project": self.project.id},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["synced_events_count"], 3)
        mock_sync_outlook_events.assert_called_once_with(
            self.team_member,
            "token",
            project=self.project,
        )


TEST_REST_FRAMEWORK = {
    **settings.REST_FRAMEWORK,
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}


@override_settings(REST_FRAMEWORK=TEST_REST_FRAMEWORK)
class ActionCountersViewTests(TestCase):
    def setUp(self):
        from django.db.models.signals import post_save, pre_delete

        post_save.disconnect(notify_on_leave_request_save, sender=LeaveRequest)
        post_save.disconnect(notify_expensecard_save, sender=ExpenseCard)
        pre_delete.disconnect(notify_on_leave_request_delete, sender=LeaveRequest)
        pre_delete.disconnect(notify_expensecard_delete, sender=ExpenseCard)
        self.addCleanup(
            post_save.connect,
            notify_on_leave_request_save,
            sender=LeaveRequest,
        )
        self.addCleanup(
            post_save.connect,
            notify_expensecard_save,
            sender=ExpenseCard,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_leave_request_delete,
            sender=LeaveRequest,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_expensecard_delete,
            sender=ExpenseCard,
        )

        self.client = APIClient()
        self.reviewer = User.objects.create_user(
            email="reviewer@example.com",
            password="password",
            first_name="Review",
            last_name="User",
            user_type="company_user",
        )
        self.viewer = User.objects.create_user(
            email="viewer@example.com",
            password="password",
            first_name="View",
            last_name="User",
            user_type="company_user",
        )
        self.no_permission_user = User.objects.create_user(
            email="noperms@example.com",
            password="password",
            first_name="No",
            last_name="Perms",
            user_type="company_user",
        )
        self.employee_user = User.objects.create_user(
            email="employee@example.com",
            password="password",
            first_name="Employee",
            last_name="User",
            user_type="company_user",
        )
        self.other_company_user = User.objects.create_user(
            email="other-company@example.com",
            password="password",
            first_name="Other",
            last_name="Company",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Counters Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="counters@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_company_user,
            company_name="Other Co",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="other@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )

        for user in [
            self.reviewer,
            self.viewer,
            self.no_permission_user,
            self.employee_user,
        ]:
            user.company = self.company
            user.save(update_fields=["company"])

        self.reviewer_staff = Staff.objects.create(
            user=self.reviewer,
            salary=1000,
            weekly_hours=40,
            hourly_rate=50,
        )
        self.viewer_staff = Staff.objects.create(
            user=self.viewer,
            salary=1000,
            weekly_hours=40,
            hourly_rate=50,
        )
        self.employee_staff = Staff.objects.create(
            user=self.employee_user,
            salary=1000,
            weekly_hours=40,
            hourly_rate=50,
            supervisor=self.reviewer_staff,
        )

        self._grant_company_permissions(
            self.reviewer,
            [
                "time_booking.review",
                "leave_request.review",
                "expense_booking.review",
            ],
            "Reviewer Role",
        )
        self._grant_company_permissions(
            self.viewer,
            [
                "time_booking.view",
                "leave_request.view",
                "expense_booking.view",
            ],
            "Viewer Role",
        )

        MonthlyTimesheetSubmission.objects.create(
            user=self.employee_user,
            month=6,
            year=2026,
            status=StatusType.PENDING.value,
        )
        MonthlyTimesheetSubmission.objects.create(
            user=self.employee_user,
            month=5,
            year=2026,
            status=StatusType.APPROVED.value,
        )
        MonthlyTimesheetSubmission.objects.create(
            user=self.other_company_user,
            month=6,
            year=2026,
            status=StatusType.PENDING.value,
        )

        LeaveRequest.objects.create(
            employee=self.employee_staff,
            manager=self.reviewer_staff,
            type="Paid",
            start_date="2026-06-10",
            end_date="2026-06-12",
            status=StatusType.PENDING.value,
        )
        LeaveRequest.objects.create(
            employee=self.employee_staff,
            manager=self.reviewer_staff,
            type="Paid",
            start_date="2026-06-15",
            end_date="2026-06-16",
            status=StatusType.APPROVED.value,
        )
        LeaveRequest.objects.create(
            employee=self.reviewer_staff,
            type="Paid",
            start_date="2026-06-20",
            end_date="2026-06-20",
            status=StatusType.PENDING.value,
        )

        ExpenseCard.objects.create(
            company_profile=self.company,
            created_by=self.employee_user,
            name="Pending Expense",
            description="Pending Expense",
            category="travel",
            rate=100,
            quantity=2,
            total=200,
            status=StatusType.PENDING.value,
        )
        ExpenseCard.objects.create(
            company_profile=self.company,
            created_by=self.employee_user,
            name="Approved Expense",
            description="Approved Expense",
            category="travel",
            rate=100,
            quantity=1,
            total=100,
            status=StatusType.APPROVED.value,
        )

        Feedback.objects.create(
            user=self.reviewer,
            company_profile=self.company,
            solution_name="Energy Consultant Solution",
            feedback_type="suggestion",
            description="Open feedback",
            status=FeedbackStatusType.OPEN.value,
        )
        Feedback.objects.create(
            user=self.reviewer,
            company_profile=self.company,
            solution_name="Energy Consultant Solution",
            feedback_type="suggestion",
            description="Closed feedback",
            status=FeedbackStatusType.CLOSED.value,
        )
        Feedback.objects.create(
            user=self.no_permission_user,
            company_profile=self.company,
            solution_name="Energy Consultant Solution",
            feedback_type="incident",
            description="Other open feedback",
            status=FeedbackStatusType.OPEN.value,
        )

    def _grant_company_permissions(self, user, permission_keys, role_name):
        role = Role.objects.create(
            name=role_name,
            company=self.company,
            type="solution",
            scope="company",
        )
        for permission_key in permission_keys:
            permission, _ = Permission.objects.get_or_create(
                key=permission_key,
                defaults={
                    "name": permission_key,
                    "module": permission_key.split(".")[0],
                },
            )
            RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=self.company)

    def test_returns_pending_counts_for_reviewer(self):
        self.client.force_authenticate(user=self.reviewer)

        response = self.client.get(
            "/api/user/action-counters",
            {"solution": "Energy Consultant Solution"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sections"]["time_booking"]["tabs"]["timesheets"], 1)
        self.assertEqual(response.json()["sections"]["time_booking"]["tabs"]["leaves"], 1)
        self.assertEqual(response.json()["sections"]["cost_management"]["tabs"]["expenses_report"], 1)
        self.assertEqual(response.json()["sections"]["feedback"]["tabs"]["feedback"], 1)
        self.assertEqual(response.json()["sidebar"]["time_booking"], 2)
        self.assertEqual(response.json()["sidebar"]["cost_management"], 1)
        self.assertEqual(response.json()["sidebar"]["feedback"], 1)

    def test_returns_pending_counts_for_view_only_user(self):
        self.client.force_authenticate(user=self.viewer)

        response = self.client.get(
            "/api/user/action-counters",
            {"solution": "Energy Consultant Solution"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sections"]["time_booking"]["tabs"]["timesheets"], 1)
        self.assertEqual(response.json()["sections"]["time_booking"]["tabs"]["leaves"], 0)
        self.assertEqual(response.json()["sections"]["cost_management"]["tabs"]["expenses_report"], 1)
        self.assertEqual(response.json()["sections"]["feedback"]["tabs"]["feedback"], 0)

    def test_returns_zero_for_sections_without_permission(self):
        self.client.force_authenticate(user=self.no_permission_user)

        response = self.client.get(
            "/api/user/action-counters",
            {"solution": "Energy Consultant Solution"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sidebar"]["time_booking"], 0)
        self.assertEqual(response.json()["sidebar"]["cost_management"], 0)
        self.assertEqual(response.json()["sidebar"]["feedback"], 1)


class FeedbackOrderingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="feedback-ordering@example.com",
            password="password",
            first_name="Feedback",
            last_name="Owner",
            user_type="company_user",
        )
        self.client.force_authenticate(user=self.user)

    def _create_feedback(self, description, response_count):
        feedback = Feedback.objects.create(
            user=self.user,
            solution_name="Energy Consultant Solution",
            feedback_type="suggestion",
            description=description,
            status=FeedbackStatusType.OPEN.value,
        )
        FeedbackResponse.objects.bulk_create(
            [
                FeedbackResponse(
                    feedback=feedback,
                    response=f"Response {index}",
                    responded_by=self.user,
                )
                for index in range(response_count)
            ]
        )
        return feedback

    def test_reply_count_ordering_is_numeric_and_keeps_no_replies_last(self):
        no_replies = self._create_feedback("No replies", 0)
        one_reply = self._create_feedback("One reply", 1)
        two_replies = self._create_feedback("Two replies", 2)
        ten_replies = self._create_feedback("Ten replies", 10)

        for ordering, expected in (
            (
                "response_sort_count",
                [one_reply.id, two_replies.id, ten_replies.id, no_replies.id],
            ),
            (
                "-response_sort_count",
                [ten_replies.id, two_replies.id, one_reply.id, no_replies.id],
            ),
        ):
            with self.subTest(ordering=ordering):
                response = self.client.get(
                    "/api/user/feedback/",
                    {"ordering": ordering, "page_size": 25},
                )

                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(
                    [row["id"] for row in response.data["results"]],
                    expected,
                )
