from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest, LeaveRequestAttachment
from human_resource_management.permissions import (
    LeaveRequestPermission,
    can_view_onboarding_instance,
    can_view_staff_compensation,
    can_view_staff_document,
    can_view_staff_sst_dossier,
)
from project_management.models import Project
from project_management.signals import create_default_folders, notify_on_project_save
from project_management.test_document_revision import NoNotificationsMixin
from human_resource_management.serializers.leave import LeaveRequestSerializer
from human_resource_management.signals import notify_on_leave_request_save
from human_resource_management.utils import (
    get_leave_reviewers,
    send_request_status_email,
)
from human_resource_management.views.leave import LeaveRequestViewSet
from models.models import (
    CompanyProfile,
    Permission,
    Role,
    RolePermission,
    Staff,
    User,
    UserRole,
)


class LeaveRequestCreationTests(TestCase):
    def _create_user_with_staff(self, email, user_type="company_user"):
        user = User.objects.create_user(
            email=email,
            password="password",
            first_name="Leave",
            last_name="Requester",
            user_type=user_type,
        )
        Staff.objects.create(
            user=user,
            salary=1000,
            annual_leaves_allowed=20,
            weekly_hours=40,
        )
        return user

    def _create_leave(self, user):
        # LOT RH4 closure (see LeaveRequestSerializer.create's own docstring):
        # every creation path always starts PENDING -- the auto-approval
        # mechanism this helper used to toggle via `has_auto_approval` (by
        # mocking `require_any_permission_multiple_modules`, a name
        # `serializers.leave` no longer even imports, since create() no
        # longer calls it) was removed entirely by that lot. Nothing is
        # mocked here any more because nothing in create() reads a
        # permission flag to decide approval status.
        request = SimpleNamespace(user=user, query_params={})
        serializer = LeaveRequestSerializer(context={"request": request})
        start_date = timezone.localdate() + timedelta(days=7)

        return serializer.create({
            "type": "annual",
            "start_date": start_date,
            "end_date": start_date,
            "reason": "Planned leave",
        })

    def test_company_user_leave_is_pending_without_manager(self):
        user = self._create_user_with_staff("leave-user@example.com")

        leave_request = self._create_leave(user)

        self.assertEqual(leave_request.status, StatusType.PENDING.value)
        self.assertIsNone(leave_request.manager)
        self.assertNotIn("manager", LeaveRequestSerializer.Meta.fields)

    def test_create_always_starts_pending_even_without_explicit_status(self):
        # LOT RH4 closure, validated rule: creation is closed off from ever
        # setting anything but pending -- a client-supplied `status` (or,
        # historically, an auto-approval permission flag) is always
        # ignored/stripped. This replaces the old
        # `test_create_without_approval_still_auto_approves_without_manager`,
        # which asserted the OPPOSITE (APPROVED), an expectation LOT RH4
        # closure deliberately superseded.
        user = self._create_user_with_staff("leave-owner@example.com")

        leave_request = self._create_leave(user)

        self.assertEqual(leave_request.status, StatusType.PENDING.value)
        self.assertIsNone(leave_request.manager)

    def test_freelancer_leave_also_always_starts_pending(self):
        # Same LOT RH4 closure rule applies uniformly to freelancers --
        # replaces the old `test_freelancer_leave_still_auto_approves_
        # without_manager`, which asserted the now-superseded APPROVED
        # behaviour.
        user = self._create_user_with_staff(
            "leave-freelancer@example.com",
            user_type="freelancer",
        )

        leave_request = self._create_leave(user)

        self.assertEqual(leave_request.status, StatusType.PENDING.value)
        self.assertIsNone(leave_request.manager)


class LeaveRequestReviewTests(TestCase):
    def setUp(self):
        post_save.disconnect(notify_on_leave_request_save, sender=LeaveRequest)
        self.addCleanup(
            post_save.connect,
            notify_on_leave_request_save,
            sender=LeaveRequest,
        )

        self.company, self.reviewer = self._create_company(
            "Review Company",
            "leave-reviewer@example.com",
        )
        self.employee = self._create_company_user(
            "leave-employee@example.com",
            self.company,
        )
        self.other_company, self.other_company_user = self._create_company(
            "Other Company",
            "other-company-user@example.com",
        )

        self.reviewer_staff = Staff.objects.create(
            user=self.reviewer,
            salary=1000,
            weekly_hours=40,
        )
        self.employee_staff = Staff.objects.create(
            user=self.employee,
            salary=1000,
            weekly_hours=40,
        )
        self.other_company_staff = Staff.objects.create(
            user=self.other_company_user,
            salary=1000,
            weekly_hours=40,
        )

        start_date = timezone.localdate() + timedelta(days=7)
        self.own_leave = LeaveRequest.objects.create(
            employee=self.reviewer_staff,
            type="Paid",
            start_date=start_date,
            end_date=start_date,
        )
        self.employee_leave = LeaveRequest.objects.create(
            employee=self.employee_staff,
            type="Paid",
            start_date=start_date,
            end_date=start_date,
        )
        self.cross_company_leave = LeaveRequest.objects.create(
            employee=self.other_company_staff,
            type="Paid",
            start_date=start_date,
            end_date=start_date,
        )

    def _create_company(self, company_name, admin_email):
        admin_user = User.objects.create_user(
            email=admin_email,
            password="password",
            first_name="Leave",
            last_name="Reviewer",
            user_type="company_user",
        )
        company = CompanyProfile.objects.create(
            admin_user=admin_user,
            company_name=company_name,
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=admin_email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        admin_user.company = company
        admin_user.save(update_fields=["company"])
        return company, admin_user

    @staticmethod
    def _create_company_user(email, company):
        return User.objects.create_user(
            email=email,
            password="password",
            first_name="Leave",
            last_name="Employee",
            user_type="company_user",
            company=company,
        )

    def _review_request(self, user=None):
        return SimpleNamespace(
            user=user or self.reviewer,
            query_params={},
        )

    @staticmethod
    def _grant_review_permission(user, role_name):
        permission, _ = Permission.objects.get_or_create(
            key="leave_request.review",
            defaults={
                "name": "Review Leave Request",
                "module": "leave_request",
            },
        )
        role = Role.objects.create(
            name=role_name,
            company=user.company,
            type="solution",
            scope="company",
            is_active=True,
        )
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=user.company)

    def test_review_queue_returns_same_company_leaves_including_own(self):
        # The review_queue branch of get_queryset() resolves access via
        # `has_cumulative_leave_review_permission` (a real, cumulative
        # roles-union DB query -- LOT RH9), not via a mockable
        # `require_any_permission_multiple_modules({'leave_request':
        # ['review']}, ...)` call, which this branch never makes. Granting
        # the real permission (same helper every other passing review test
        # in this file already uses) exercises the actual, validated
        # cross-company isolation logic instead of a stale mock.
        self._grant_review_permission(self.reviewer, "Review Queue Test Reviewer")
        view = LeaveRequestViewSet()
        view.request = SimpleNamespace(
            user=self.reviewer,
            query_params={"review_queue": "true"},
        )

        leave_ids = set(view.get_queryset().values_list("id", flat=True))

        self.assertEqual(
            leave_ids,
            {self.employee_leave.id, self.own_leave.id},
        )
        self.assertNotIn(self.cross_company_leave.id, leave_ids)

    def test_review_queue_is_empty_without_review_permission(self):
        view = LeaveRequestViewSet()
        view.request = SimpleNamespace(
            user=self.reviewer,
            query_params={"review_queue": "1"},
        )

        with patch(
            "human_resource_management.views.leave."
            "require_any_permission_multiple_modules",
            return_value=False,
        ):
            self.assertFalse(view.get_queryset().exists())

    def test_personal_leave_list_still_returns_only_requesters_leaves(self):
        view = LeaveRequestViewSet()
        view.request = SimpleNamespace(
            user=self.reviewer,
            query_params={},
        )

        def has_permission(_user, modules_actions, _scope, _query_params):
            return modules_actions == {'leave_request': ['view_created']}

        with patch(
            "human_resource_management.views.leave."
            "require_any_permission_multiple_modules",
            side_effect=has_permission,
        ):
            leave_ids = set(view.get_queryset().values_list("id", flat=True))

        self.assertEqual(leave_ids, {self.own_leave.id})

    def test_calendar_denies_user_without_leave_view_permission(self):
        request = APIRequestFactory().get(
            "/leave-requests/calendar/",
            {
                "range_start": timezone.localdate().isoformat(),
                "range_end": (
                    timezone.localdate() + timedelta(days=14)
                ).isoformat(),
            },
        )
        force_authenticate(request, user=self.reviewer)
        view = LeaveRequestViewSet.as_view({"get": "calendar"})

        with patch.object(
            LeaveRequestPermission,
            "_check_permission",
            return_value=False,
        ), patch(
            "human_resource_management.views.leave."
            "require_any_permission_multiple_modules",
            return_value=False,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 403)

    def test_leave_queryset_is_empty_without_visibility_permission(self):
        view = LeaveRequestViewSet()
        view.request = SimpleNamespace(
            user=self.reviewer,
            query_params={},
        )

        with patch(
            "human_resource_management.views.leave."
            "require_any_permission_multiple_modules",
            return_value=False,
        ):
            self.assertFalse(view.get_queryset().exists())

    def test_calendar_reviewer_sees_only_same_company_leaves(self):
        # Review-based access is now decided entirely by
        # `has_cumulative_leave_review_permission` (a real DB query), both
        # in `LeaveRequestPermission._check_view_permission` (gates the
        # request) and in the `calendar` action itself (decides
        # `include_type`) -- `_check_permission(..., 'review', ...)` is
        # never called for this permission any more, so mocking it has no
        # effect. Granting the permission for real exercises the actual,
        # validated company-scoped visibility.
        self._grant_review_permission(self.reviewer, "Calendar Test Reviewer")
        # The calendar action deliberately only ever returns status=approved
        # rows ("a pending request is never shown as a confirmed
        # unavailability" -- see LeaveRequestViewSet.calendar's own
        # docstring) -- these fixtures default to pending (see setUp), so
        # without approving them first the calendar legitimately returns
        # nothing, independent of permissions. A second, previously-masked
        # fixture gap uncovered once the stale permission mock above was
        # fixed; approving them here (not in setUp, to avoid changing what
        # OTHER tests in this class see) is the minimal correction.
        for leave in (self.own_leave, self.employee_leave):
            leave.status = StatusType.APPROVED.value
            leave.save(update_fields=["status"])
        start_date = timezone.localdate()
        request = APIRequestFactory().get(
            "/leave-requests/calendar/",
            {
                "range_start": start_date.isoformat(),
                "range_end": (start_date + timedelta(days=14)).isoformat(),
            },
        )
        force_authenticate(request, user=self.reviewer)
        view = LeaveRequestViewSet.as_view({"get": "calendar"})

        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {leave["id"] for leave in response.data},
            {self.own_leave.id, self.employee_leave.id},
        )
        self.assertNotIn(
            self.cross_company_leave.id,
            {leave["id"] for leave in response.data},
        )

    def test_reviewer_resolver_returns_only_active_same_company_reviewers(self):
        second_reviewer = self._create_company_user(
            "second-reviewer@example.com",
            self.company,
        )
        inactive_reviewer = self._create_company_user(
            "inactive-reviewer@example.com",
            self.company,
        )
        inactive_reviewer.is_active = False
        inactive_reviewer.save(update_fields=["is_active"])

        self._grant_review_permission(self.reviewer, "Primary HR Reviewer")
        self._grant_review_permission(second_reviewer, "Secondary HR Reviewer")
        self._grant_review_permission(inactive_reviewer, "Inactive HR Reviewer")
        self._grant_review_permission(self.employee, "Requester HR Reviewer")
        self._grant_review_permission(
            self.other_company_user,
            "Other Company HR Reviewer",
        )

        reviewer_ids = set(
            get_leave_reviewers(self.employee).values_list("id", flat=True)
        )

        self.assertEqual(
            reviewer_ids,
            {self.reviewer.id, second_reviewer.id},
        )

    def test_submission_notifies_all_resolved_reviewers_on_both_channels(self):
        reviewers = {self.reviewer, self.employee}

        with patch(
            "human_resource_management.signals.get_leave_reviewers",
            return_value=reviewers,
        ), patch(
            "human_resource_management.signals."
            "create_notification_with_recipients",
        ) as create_notification, patch(
            "human_resource_management.signals."
            "send_request_notification_email",
        ) as send_email, self.captureOnCommitCallbacks(execute=True):
            notify_on_leave_request_save(
                sender=LeaveRequest,
                instance=self.employee_leave,
                created=True,
            )

        create_notification.assert_called_once()
        notification_kwargs = create_notification.call_args.kwargs
        self.assertEqual(notification_kwargs["recipients"], {self.reviewer})
        self.assertEqual(notification_kwargs["notification_type"], "leave_request")
        self.assertEqual(notification_kwargs.get("action_type"), "create")
        self.assertFalse(notification_kwargs.get("bypass_preference_check", False))
        send_email.assert_called_once_with(
            self.employee,
            self.reviewer,
            self.employee_leave,
        )

    def test_decision_notifies_employee_on_both_channels(self):
        self.employee_leave.status = StatusType.APPROVED.value

        with patch(
            "human_resource_management.signals.get_current_user",
            return_value=self.reviewer,
        ), patch.object(
            self.employee_leave.tracker,
            "changed",
            return_value={"status": StatusType.PENDING.value},
        ), patch(
            "human_resource_management.signals."
            "create_notification_with_recipients",
        ) as create_notification, patch(
            "human_resource_management.signals.send_request_status_email",
        ) as send_email, self.captureOnCommitCallbacks(execute=True):
            notify_on_leave_request_save(
                sender=LeaveRequest,
                instance=self.employee_leave,
                created=False,
            )

        create_notification.assert_called_once()
        self.assertEqual(
            create_notification.call_args.kwargs["recipients"],
            {self.employee},
        )
        self.assertEqual(
            create_notification.call_args.kwargs["notification_type"],
            "leave_request",
        )
        self.assertFalse(
            create_notification.call_args.kwargs.get(
                "bypass_preference_check",
                False,
            )
        )
        send_email.assert_called_once_with(
            self.employee,
            self.reviewer,
            self.employee_leave,
            StatusType.APPROVED.value,
        )

    def test_status_email_uses_employee_preference_and_recipient(self):
        with patch(
            "human_resource_management.utils.get_user_email_preferences",
            return_value=True,
        ) as get_preferences, patch(
            "human_resource_management.utils.send_mail",
        ) as send_mail:
            send_request_status_email(
                self.employee,
                self.reviewer,
                self.employee_leave,
                StatusType.APPROVED.value,
            )

        get_preferences.assert_called_once_with(self.employee.id)
        self.assertEqual(
            send_mail.call_args.kwargs["recipient_list"],
            [self.employee.email],
        )

    def test_permission_layer_requires_review_and_blocks_self_review(self):
        # `_check_update_permission`'s status-change branch calls
        # `has_cumulative_leave_review_permission(user)` directly for a
        # review action -- it never calls `self._check_permission(...,
        # 'review', ...)` (that method is only used for full_access/
        # create/create_without_approval/update, none of which apply to a
        # status-bearing PATCH). Mocking `_check_permission` therefore has
        # no bearing on this path; granting real review permission exercises
        # the actual, validated self-review and cross-company blocks.
        self._grant_review_permission(self.reviewer, "Object Permission Test Reviewer")
        permission = LeaveRequestPermission()
        request = SimpleNamespace(
            user=self.reviewer,
            method="PATCH",
            data={"status": StatusType.APPROVED.value},
            query_params={},
        )

        self.assertTrue(
            permission.has_object_permission(
                request,
                SimpleNamespace(),
                self.employee_leave,
            )
        )
        self.assertFalse(
            permission.has_object_permission(
                request,
                SimpleNamespace(),
                self.own_leave,
            )
        )
        self.assertFalse(
            permission.has_object_permission(
                request,
                SimpleNamespace(),
                self.cross_company_leave,
            )
        )

    def test_permission_layer_blocks_cross_company_retrieve(self):
        # Same root cause as above: GET routes through `_check_view_
        # permission`, which resolves review-based visibility via
        # `has_cumulative_leave_review_permission`, not a patchable
        # `_check_permission(..., 'review', ...)` call.
        self._grant_review_permission(self.reviewer, "Cross Company Retrieve Test Reviewer")
        permission = LeaveRequestPermission()
        request = SimpleNamespace(
            user=self.reviewer,
            method="GET",
            query_params={},
        )

        self.assertTrue(
            permission.has_object_permission(
                request,
                SimpleNamespace(),
                self.employee_leave,
            )
        )
        self.assertFalse(
            permission.has_object_permission(
                request,
                SimpleNamespace(),
                self.cross_company_leave,
            )
        )

    def test_reviewer_can_approve_another_users_pending_leave(self):
        self._grant_review_permission(
            self.reviewer,
            "Custom Leave Reviewer",
        )
        serializer = LeaveRequestSerializer(
            self.employee_leave,
            context={"request": self._review_request()},
        )

        serializer.update(
            self.employee_leave,
            {"status": StatusType.APPROVED.value},
        )

        self.employee_leave.refresh_from_db()
        self.assertEqual(
            self.employee_leave.status,
            StatusType.APPROVED.value,
        )

    def test_company_administrator_without_review_permission_cannot_review(self):
        company_admin_role = Role.objects.create(
            name="Company Administrator",
            company=self.company,
            type="solution",
            scope="company",
            is_active=True,
        )
        UserRole.objects.create(
            user=self.reviewer,
            role=company_admin_role,
            company=self.company,
        )
        serializer = LeaveRequestSerializer(
            self.employee_leave,
            context={"request": self._review_request()},
        )

        with self.assertRaises(serializers.ValidationError):
            serializer.update(
                self.employee_leave,
                {"status": StatusType.APPROVED.value},
            )

    def test_serializer_rejects_self_and_cross_company_review(self):
        for leave_request in (self.own_leave, self.cross_company_leave):
            serializer = LeaveRequestSerializer(
                leave_request,
                context={"request": self._review_request()},
            )
            with self.assertRaises(serializers.ValidationError):
                serializer.update(
                    leave_request,
                    {"status": StatusType.APPROVED.value},
                )

    def test_serializer_rejects_missing_permission_invalid_and_repeat_review(self):
        # The review-permission check in LeaveRequestSerializer.update() now
        # calls `has_cumulative_leave_review_permission` directly (LOT RH9's
        # cumulative-role union, see permissions.py) -- never
        # `require_any_permission_multiple_modules`, which `serializers.leave`
        # no longer even imports since this path stopped using it. The
        # reviewer here was never granted `leave_request.review` in setUp(),
        # so patching the real, current function to False makes the
        # "missing permission" case explicit and self-documenting rather
        # than relying on an implicit absence of a grant.
        serializer = LeaveRequestSerializer(
            self.employee_leave,
            context={"request": self._review_request()},
        )
        with patch(
            "human_resource_management.serializers.leave."
            "has_cumulative_leave_review_permission",
            return_value=False,
        ), self.assertRaises(serializers.ValidationError):
            serializer.update(
                self.employee_leave,
                {"status": StatusType.APPROVED.value},
            )

        with self.assertRaises(serializers.ValidationError):
            serializer.update(
                self.employee_leave,
                {"status": StatusType.PENDING.value},
            )

        self.employee_leave.status = StatusType.APPROVED.value
        self.employee_leave.save(update_fields=["status"])
        with self.assertRaises(serializers.ValidationError):
            serializer.update(
                self.employee_leave,
                {"status": StatusType.REJECTED.value},
            )


class _NoGrants:
    """Stands in for a related manager that holds no row."""

    def filter(self, **kwargs):
        return self

    def exists(self):
        return False


class HrSecurityTests(NoNotificationsMixin, TestCase):
    """GIT-01C.3G-C: the HR confidentiality rules that must hold for every company, role and export."""

    KPI_URL = "/api/human_resource_management/staff/export_staff_project_kpis/"

    def setUp(self):
        self.silence_notifications()
        for signal_receiver in (create_default_folders, notify_on_project_save):
            post_save.disconnect(signal_receiver, sender=Project)
            self.addCleanup(post_save.connect, signal_receiver, sender=Project)
        post_save.disconnect(notify_on_leave_request_save, sender=LeaveRequest)
        self.addCleanup(post_save.connect, notify_on_leave_request_save, sender=LeaveRequest)
        # The global product-license check queries tables absent from the test DB.
        license_patch = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        license_patch.start()
        self.addCleanup(license_patch.stop)

        self.company_a, self.admin_a = self._company("Security A", "sec-a-admin@links-local.invalid")
        self.company_b, self.admin_b = self._company("Security B", "sec-b-admin@links-local.invalid")
        self.employee = self._member("sec-employee@links-local.invalid", self.company_a)
        self.colleague = self._member("sec-colleague@links-local.invalid", self.company_a)
        self.manager = self._member("sec-manager@links-local.invalid", self.company_a, "staff_management.full_access")
        self.hr = self._member(
            "sec-hr@links-local.invalid", self.company_a,
            "leave_request.review", "staff_management.view_documents", "staff_management.view_compensation",
            "staff_management.view_sst_dossier", "staff_management.onboarding_view_all",
        )
        self.outsider = self._member(
            "sec-outsider@links-local.invalid", self.company_b,
            "leave_request.review", "staff_management.view", "staff_management.view_documents",
            "staff_management.view_compensation", "staff_management.view_sst_dossier",
            "staff_management.onboarding_view_all", "staff_management.offboarding_view_all",
        )
        self.reader = self._member("sec-reader@links-local.invalid", self.company_a, "staff_management.view")
        for user in (self.employee, self.colleague, self.manager, self.hr, self.outsider, self.reader):
            Staff.objects.create(user=user, salary=1000, weekly_hours=40)

        self.project_a = Project.objects.create(
            project_name="Security Project A", contract_value=1, company_profile=self.company_a,
            created_by=self.admin_a,
        )
        self.project_b = Project.objects.create(
            project_name="Security Project B", contract_value=1, company_profile=self.company_b,
            created_by=self.admin_b,
        )
        start = timezone.localdate() + timedelta(days=7)
        self.leave = LeaveRequest.objects.create(
            employee=self.employee.staff, type="Paid", start_date=start, end_date=start,
        )

    @staticmethod
    def _company(name, admin_email):
        admin = User.objects.create_user(
            email=admin_email, password="x", first_name="S", last_name="A", user_type="company_user"
        )
        company = CompanyProfile.objects.create(
            admin_user=admin, company_name=name, company_address="1", company_town="T",
            company_tell_no="0", company_mob_no="1", company_web_address="https://x.invalid",
            company_email_address=admin_email, company_tax_reference="T", company_iban="I",
        )
        admin.company = company
        admin.save(update_fields=["company"])
        return company, admin

    @staticmethod
    def _member(email, company, *permission_keys):
        user = User.objects.create_user(
            email=email, password="x", first_name="S", last_name="U", user_type="company_user", company=company,
        )
        if permission_keys:
            role = Role.objects.create(
                name=f"Security {email}", company=company, type="solution", scope="company", is_active=True,
            )
            for key in permission_keys:
                permission, _ = Permission.objects.get_or_create(
                    key=key, defaults={"name": key, "module": key.split(".")[0], "description": "test"}
                )
                RolePermission.objects.create(role=role, permission=permission)
            UserRole.objects.create(user=user, role=role, company=company)
        return user

    def _as(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    # --- export of a project's staff KPIs ------------------------------------------------

    def test_a_permitted_user_exports_the_kpis_of_a_project_of_their_company(self):
        response = self._as(self.reader).get(self.KPI_URL, {"project_id": self.project_a.id})
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheet", response["Content-Type"])

    def test_a_permitted_user_cannot_export_the_kpis_of_another_companys_project(self):
        response = self._as(self.reader).get(self.KPI_URL, {"project_id": self.project_b.id})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("spreadsheet", response["Content-Type"])

    def test_a_user_without_the_hr_permission_cannot_export_the_kpis(self):
        response = self._as(self.colleague).get(self.KPI_URL, {"project_id": self.project_a.id})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("spreadsheet", response["Content-Type"])

    def test_an_unknown_project_discloses_nothing_and_looks_like_another_companys(self):
        unknown = self._as(self.reader).get(self.KPI_URL, {"project_id": 99999999})
        foreign = self._as(self.reader).get(self.KPI_URL, {"project_id": self.project_b.id})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual((unknown.status_code, unknown.json()), (foreign.status_code, foreign.json()))

    # --- review of a leave request (permission layer) -------------------------------------

    def _may_review(self, user, leave):
        request = SimpleNamespace(
            user=user, method="PATCH", data={"status": StatusType.APPROVED.value}, query_params={},
        )
        return LeaveRequestPermission().has_object_permission(request, SimpleNamespace(), leave)

    def test_only_a_real_hr_reviewer_may_validate_a_leave_request(self):
        self.assertFalse(self._may_review(self.colleague, self.leave))   # ordinary employee
        self.assertFalse(self._may_review(self.manager, self.leave))     # Company Manager without the HR role
        self.assertFalse(self._may_review(self.employee, self.leave))    # never the employee's own request
        self.assertTrue(self._may_review(self.hr, self.leave))           # HR of the same company
        self.assertFalse(self._may_review(self.outsider, self.leave))    # HR of another company

    # --- confidential supporting document of a leave request ------------------------------

    def test_only_the_employee_and_hr_reach_a_leave_justificatif(self):
        attachment = LeaveRequestAttachment.objects.create(
            leave_request=self.leave, file="", file_name="justificatif.pdf", file_size=1,
            uploaded_by=self.employee,
        )
        url = (
            f"/api/human_resource_management/leave-requests/{self.leave.id}"
            f"/attachments/{attachment.id}/download"
        )
        for user in (self.colleague, self.manager, self.outsider):
            self.assertEqual(self._as(user).get(url).status_code, 403, user.email)
        # Allowed users pass the permission gate (the test row has no stored file, hence 404, not 403).
        for user in (self.employee, self.hr):
            self.assertEqual(self._as(user).get(url).status_code, 404, user.email)

    # --- HR file, compensation, SST and (on|off)boarding ----------------------------------

    def test_hr_documents_stay_inside_the_company_and_the_granted_roles(self):
        document = SimpleNamespace(
            category=SimpleNamespace(is_sst=False), company_profile=self.company_a,
            staff_id=self.employee.staff.id, staff=self.employee.staff, grants=_NoGrants(),
        )
        self.assertTrue(can_view_staff_document(self.employee, document))
        self.assertTrue(can_view_staff_document(self.hr, document))
        self.assertFalse(can_view_staff_document(self.colleague, document))
        self.assertFalse(can_view_staff_document(self.manager, document))
        self.assertFalse(can_view_staff_document(self.outsider, document))   # holds the right, in another company

    def test_compensation_is_visible_only_to_the_employee_and_the_granted_roles(self):
        staff = self.employee.staff
        self.assertTrue(can_view_staff_compensation(self.employee, staff))
        self.assertTrue(can_view_staff_compensation(self.hr, staff))
        self.assertFalse(can_view_staff_compensation(self.colleague, staff))
        self.assertFalse(can_view_staff_compensation(self.manager, staff))
        self.assertFalse(can_view_staff_compensation(self.outsider, staff))

    def test_the_sst_dossier_is_limited_to_the_employee_and_the_sst_rights(self):
        staff = self.employee.staff
        self.assertTrue(can_view_staff_sst_dossier(self.employee, staff))
        self.assertTrue(can_view_staff_sst_dossier(self.hr, staff))
        self.assertFalse(can_view_staff_sst_dossier(self.colleague, staff))
        self.assertFalse(can_view_staff_sst_dossier(self.manager, staff))
        self.assertFalse(can_view_staff_sst_dossier(self.outsider, staff))

    def test_onboarding_and_offboarding_files_follow_their_own_permissions(self):
        def instance(kind):
            return SimpleNamespace(
                staff=self.employee.staff, company_profile_id=self.company_a.id,
                process_kind=kind, step_instances=_NoGrants(),
            )
        onboarding, offboarding = instance("onboarding"), instance("offboarding")
        self.assertTrue(can_view_onboarding_instance(self.employee, onboarding))
        self.assertTrue(can_view_onboarding_instance(self.hr, onboarding))
        self.assertFalse(can_view_onboarding_instance(self.hr, offboarding))      # onboarding right is not offboarding
        self.assertFalse(can_view_onboarding_instance(self.colleague, onboarding))
        self.assertFalse(can_view_onboarding_instance(self.manager, offboarding))
        self.assertFalse(can_view_onboarding_instance(self.outsider, onboarding))
        self.assertFalse(can_view_onboarding_instance(self.outsider, offboarding))
