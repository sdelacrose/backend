from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APIRequestFactory, force_authenticate

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.permissions import LeaveRequestPermission
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

    def _create_leave(self, user, has_auto_approval=False):
        request = SimpleNamespace(user=user, query_params={})
        serializer = LeaveRequestSerializer(context={"request": request})
        start_date = timezone.localdate() + timedelta(days=7)

        with patch(
            "human_resource_management.serializers.leave."
            "require_any_permission_multiple_modules",
            return_value=has_auto_approval,
        ):
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

    def test_create_without_approval_still_auto_approves_without_manager(self):
        user = self._create_user_with_staff("leave-owner@example.com")

        leave_request = self._create_leave(user, has_auto_approval=True)

        self.assertEqual(leave_request.status, StatusType.APPROVED.value)
        self.assertIsNone(leave_request.manager)

    def test_freelancer_leave_still_auto_approves_without_manager(self):
        user = self._create_user_with_staff(
            "leave-freelancer@example.com",
            user_type="freelancer",
        )

        leave_request = self._create_leave(user)

        self.assertEqual(leave_request.status, StatusType.APPROVED.value)
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
        view = LeaveRequestViewSet()
        view.request = SimpleNamespace(
            user=self.reviewer,
            query_params={"review_queue": "true"},
        )

        with patch(
            "human_resource_management.views.leave."
            "require_any_permission_multiple_modules",
            return_value=True,
        ) as permission_check:
            leave_ids = set(view.get_queryset().values_list("id", flat=True))

        self.assertEqual(
            leave_ids,
            {self.employee_leave.id, self.own_leave.id},
        )
        permission_check.assert_called_once_with(
            self.reviewer,
            {'leave_request': ['review']},
            'company',
            view.request.query_params,
        )

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

        def has_permission(_user, modules_actions, _scope, _query_params):
            return modules_actions == {'leave_request': ['review']}

        with patch.object(
            LeaveRequestPermission,
            "_check_permission",
            side_effect=lambda _user, _module, action, _scope, _context: (
                action == "review"
            ),
        ), patch(
            "human_resource_management.views.leave."
            "require_any_permission_multiple_modules",
            side_effect=has_permission,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {leave["id"] for leave in response.data},
            {self.own_leave.id, self.employee_leave.id},
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
        permission = LeaveRequestPermission()
        request = SimpleNamespace(
            user=self.reviewer,
            method="PATCH",
            data={"status": StatusType.APPROVED.value},
            query_params={},
        )

        with patch.object(
            permission,
            "_check_permission",
            return_value=True,
        ) as permission_check:
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

        permission_check.assert_called_once_with(
            self.reviewer,
            'leave_request',
            'review',
            'company',
            request.query_params,
        )

    def test_permission_layer_blocks_cross_company_retrieve(self):
        permission = LeaveRequestPermission()
        request = SimpleNamespace(
            user=self.reviewer,
            method="GET",
            query_params={},
        )

        with patch.object(
            permission,
            "_check_permission",
            side_effect=lambda _user, _module, action, _scope, _context: (
                action == "review"
            ),
        ):
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
        serializer = LeaveRequestSerializer(
            self.employee_leave,
            context={"request": self._review_request()},
        )
        with patch(
            "human_resource_management.serializers.leave."
            "require_any_permission_multiple_modules",
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
