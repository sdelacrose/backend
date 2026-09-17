import io
import zipfile
from datetime import timedelta
from decimal import Decimal
from django.db.models.signals import post_save, pre_delete, m2m_changed
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
import pandas as pd
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from unittest.mock import Mock, patch

from models.models import (
    CompanyProfile,
    CompanyConfig,
    GlobalConfig,
    GlobalPublicHoliday,
    NotificationSeen,
    Permission,
    Role,
    RolePermission,
    Staff,
    SupplierPaymentMethod,
    User,
    UserRole,
)
from project_management.models import ActionItem, ActionItemComment, ActionItemDependency, CompanyPublicHoliday, ContactCompany, ContactUser, Document, ECPMPConfig, ExpenseCard, Folder, Invoice, InvoiceLineItem, PurchaseOrder, TimesheetActivity, TimesheetEntry, MonthlyTimesheetSubmission, MonthlyTimesheetFeedback, MonthlyTimesheetActivityDocumentLink, TimeCard, Project, ProjectClient, ProjectExternalUsers, ProjectPhase, Milestone, ObjectAssessment, Observation, PartsListConfig, ProjectMatrixConfig, ProjectTeamMembers, Requirement, SiteReport, SolutionDemand, MatrixComment, PartPurchaseIntent, AvailableProductPart
from project_management.serializers.action_item import ActionItemSerializer
from project_management.serializers.billing import InvoiceSerializer
from companyprofileapis.serializers.company import CompanyConfigSerializer
from project_management.serializers.contact import (
    ContactCompanySerializer,
    ContactUserSerializer,
)
from project_management.serializers.matrix import (
    BillOfMaterialPartSerializer,
    MatrixCommentSerializer,
    PartsListConfigSerializer,
    ProjectMatrixConfigSerializer,
    RequirementSerializer,
    SolutionDemandSerializer,
)
from project_management.matrix_comment_mentions import (
    get_matrix_mention_candidate_queryset,
    get_matrix_mention_candidates,
)
from project_management.serializers.purchase_order import PurchaseOrderSerializer
from project_management.serializers.project import ProjectSerializer
from project_management.serializers.project_phase import ProjectPhaseSerializer
from project_management.serializers.public_holiday import CompanyPublicHolidaySerializer
from project_management.serializers.site_reporting import SiteReportSerializer
from project_management.serializers.time_expense import ExpenseCardSerializer, TimesheetWeekSaveSerializer, TimesheetActivityDraftCreateSerializer, TimesheetCopyPreviousWeekSerializer, TimesheetPlannedActivitiesSerializer, _build_monthly_timesheet_preview, _month_period_bounds, _get_week_dates_in_month, _get_week_starts_for_month, _get_submission_locked_dates_for_week, _get_expected_daily_hours
from project_management.permissions import (
    ExpenseBookingPermission,
    MilestonePermission,
    PurchaseOrderPermission,
)
from project_management.purchase_order_access import (
    filter_purchase_orders_to_created_or_project_visibility,
    filter_purchase_orders_to_created_visibility,
    filter_purchase_orders_to_owner_scope,
    filter_purchase_orders_to_project_visibility,
)
from project_management.services.public_holiday_sync import (
    sync_company_public_holidays_from_global,
)
from project_management.signals import (
    action_item_assignees_post_save_operations,
    action_item_post_save_operations,
    create_default_folders,
    create_related_objects,
    milestone_post_save_operations,
    notify_on_object_assessment_delete,
    notify_on_object_assessment_save,
    notify_on_milestone_save,
    notify_on_purchase_order_delete,
    notify_on_purchase_order_save,
    notify_on_project_save,
    notify_timecard_delete,
)
from project_management.views.billing import InvoiceViewSet
from project_management.views.action_item_comment import _notify_new_comment
from project_management.views.action_item import ActionItemViewSet
from project_management.filters import ContactCompanyFilter
from project_management.views.contact import ContactCompanyViewSet
from project_management.views.time_expense import TimesheetWeekViewSet, MonthlyTimesheetViewSet
from project_management.views.public_holiday import CompanyPublicHolidayViewSet
from project_management.views.purchase_order import PurchaseOrderViewSet
from project_management.views.matrix import MatrixCommentViewSet
from project_management.utils import get_project_id_from_request
from ecpmpapp.constants import StatusType
from ecpmpapp.utils import get_actor_name
from human_resource_management.models import LeaveRequest
from human_resource_management.signals import (
    notify_on_leave_request_delete,
    notify_on_leave_request_save,
)
from project_management.signals import notify_expensecard_save
from userapis.models import CalendarEvent
from userapis.signals import (
    notify_on_calendar_event_assignees_changed,
    notify_on_calendar_event_delete,
    notify_on_calendar_event_save,
)
from project_management.utils import (
    INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
    INVOICE_EXPORT_INCLUDE_MODE_WITH_TIMESHEETS,
    INVOICE_EXPORT_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
    create_pdf_invoice_by_bill,
    create_invoice_export_package,
)


class ContactCompanyRoleTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="company-role@example.com",
            password="password",
            first_name="Config",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Role Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="role@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        GlobalConfig.objects.create(
            company_profile=self.company,
            company_roles=["Global Role"],
        )
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            company_roles=["Company Role #1", "Company Role #2", "Company Role #3"],
        )

    def test_serializer_persists_company_role(self):
        request = self.factory.post("/api/project_management/contact-company/")
        request.user = self.user
        serializer = ContactCompanySerializer(
            data={
                "company_name": "Acme GmbH",
                "company_role": "Company Role #2",
                "contact_email": "contact@acme.test",
            },
            context={"request": request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save()

        self.assertEqual(instance.company_role, "Company Role #2")
        self.assertEqual(instance.company_profile, self.company)

    def test_serializer_uses_company_config_as_role_source(self):
        request = self.factory.post("/api/project_management/contact-company/")
        request.user = self.user
        serializer = ContactCompanySerializer(
            data={
                "company_name": "Config Scoped GmbH",
                "company_role": "Company Role #1",
                "contact_email": "config@example.com",
            },
            context={"request": request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_serializer_rejects_invalid_company_role(self):
        request = self.factory.post("/api/project_management/contact-company/")
        request.user = self.user
        serializer = ContactCompanySerializer(
            data={
                "company_name": "Invalid Role GmbH",
                "company_role": "Not In Config",
                "contact_email": "invalid-role@example.com",
            },
            context={"request": request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("company_role", serializer.errors)

    def test_company_config_cannot_remove_role_in_use(self):
        ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Existing Role",
            company_role="Company Role #1",
            contact_email="existing@example.com",
        )
        config = CompanyConfig.objects.get(
            company_profile=self.company,
            solution="Energy Consultant Solution",
        )
        serializer = CompanyConfigSerializer(
            config,
            data={"company_roles": ["Company Role #2", "Company Role #3"]},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("company_roles", serializer.errors)

    def test_filter_supports_multiple_company_roles(self):
        role_one = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Role One",
            company_role="Company Role #1",
            contact_email="one@example.com",
        )
        ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Role Two",
            company_role="Company Role #2",
            contact_email="two@example.com",
        )
        role_three = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Role Three",
            company_role="Company Role #3",
            contact_email="three@example.com",
        )

        filtered = ContactCompanyFilter(
            data={"company_role": "Company Role #1,Company Role #3"},
            queryset=ContactCompany.objects.all(),
        ).qs

        self.assertQuerySetEqual(
            filtered.order_by("id"),
            [role_one.id, role_three.id],
            lambda item: item.id,
        )


class ContactProjectAssignmentTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="contact-projects@example.com",
            password="password",
            first_name="Contact",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Contact Projects Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="contact-projects@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Contact Project",
            contract_value=1000,
            company_profile=self.company,
            created_by=self.user,
        )
        self.contact_company_one = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="First Contact Company",
            contact_email="first-company@example.com",
        )
        self.contact_company_two = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Second Contact Company",
            contact_email="second-company@example.com",
        )
        self.contact_user_one = ContactUser.objects.create(
            company_profile=self.company,
            name="First",
            surname="Contact",
            email="first-contact@example.com",
        )
        self.contact_user_two = ContactUser.objects.create(
            company_profile=self.company,
            name="Second",
            surname="Contact",
            email="second-contact@example.com",
        )

    def get_request(self, path):
        request = self.factory.patch(path, {}, format="json")
        request.user = self.user
        return request

    def test_company_project_update_preserves_other_contact_links(self):
        ProjectExternalUsers.objects.create(
            project=self.project,
            contact_company=self.contact_company_one,
            role="architect",
        )
        preserved_company_link = ProjectExternalUsers.objects.create(
            project=self.project,
            contact_company=self.contact_company_two,
            role="architect",
        )
        preserved_user_link = ProjectExternalUsers.objects.create(
            project=self.project,
            contact_user=self.contact_user_one,
            role="architect",
        )
        serializer = ContactCompanySerializer(
            self.contact_company_one,
            data={"projects": []},
            partial=True,
            context={
                "request": self.get_request(
                    f"/api/project_management/contact-company/{self.contact_company_one.id}/"
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.assertFalse(
            ProjectExternalUsers.objects.filter(
                contact_company=self.contact_company_one
            ).exists()
        )
        self.assertTrue(
            ProjectExternalUsers.objects.filter(pk=preserved_company_link.pk).exists()
        )
        self.assertTrue(
            ProjectExternalUsers.objects.filter(pk=preserved_user_link.pk).exists()
        )

    def test_user_project_update_preserves_other_contact_links(self):
        ProjectExternalUsers.objects.create(
            project=self.project,
            contact_user=self.contact_user_one,
            role="architect",
        )
        preserved_user_link = ProjectExternalUsers.objects.create(
            project=self.project,
            contact_user=self.contact_user_two,
            role="architect",
        )
        preserved_company_link = ProjectExternalUsers.objects.create(
            project=self.project,
            contact_company=self.contact_company_one,
            role="architect",
        )
        serializer = ContactUserSerializer(
            self.contact_user_one,
            data={"projects": []},
            partial=True,
            context={
                "request": self.get_request(
                    f"/api/project_management/contact-user/{self.contact_user_one.id}/"
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.assertFalse(
            ProjectExternalUsers.objects.filter(
                contact_user=self.contact_user_one
            ).exists()
        )
        self.assertTrue(
            ProjectExternalUsers.objects.filter(pk=preserved_user_link.pk).exists()
        )
        self.assertTrue(
            ProjectExternalUsers.objects.filter(pk=preserved_company_link.pk).exists()
        )


class SiteReportPartialUpdateTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(create_related_objects, sender=ProjectTeamMembers)
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            create_related_objects,
            sender=ProjectTeamMembers,
        )

        self.factory = APIRequestFactory()
        self.observer_user = User.objects.create_user(
            email="site-observer@example.com",
            password="password",
            first_name="Site",
            last_name="Observer",
            user_type="company_user",
        )
        self.present_user = User.objects.create_user(
            email="site-present@example.com",
            password="password",
            first_name="Site",
            last_name="Present",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.observer_user,
            company_name="Site Report Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="site@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.observer_user.company = self.company
        self.observer_user.save(update_fields=["company"])
        self.present_user.company = self.company
        self.present_user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Site Report Project",
            contract_value=1000,
            company_profile=self.company,
            created_by=self.observer_user,
        )
        self.observer = ProjectTeamMembers.objects.create(
            project=self.project,
            role="observer",
            team_member=self.observer_user,
        )
        self.present_member = ProjectTeamMembers.objects.create(
            project=self.project,
            role="contractor",
            team_member=self.present_user,
        )
        self.site_report = SiteReport.objects.create(
            project=self.project,
            observer=self.observer,
            created_by=self.observer_user,
            name="Daily report",
        )
        self.site_report.present.add(self.present_member)
        self.site_report.distribution.add(self.present_member)
        self.site_report.assignees.add(
            self.observer_user,
            self.present_user,
        )
        self.observation = Observation.objects.create(
            site_report=self.site_report,
            contractor=self.present_member,
            observation="Existing observation",
        )

    @patch(
        "project_management.serializers.site_reporting.create_pdf_invoice_by_site_report"
    )
    def test_date_patch_preserves_omitted_relationships(self, create_pdf_mock):
        request = self.factory.patch(
            f"/api/project_management/site-report/{self.site_report.id}/",
            {},
            format="json",
        )
        request.user = self.observer_user
        new_date = timezone.now() + timedelta(days=1)
        serializer = SiteReportSerializer(
            self.site_report,
            data={"date_time": new_date},
            partial=True,
            context={"request": request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.assertQuerySetEqual(
            self.site_report.present.all(),
            [self.present_member],
            ordered=False,
        )
        self.assertQuerySetEqual(
            self.site_report.distribution.all(),
            [self.present_member],
            ordered=False,
        )
        self.assertTrue(
            Observation.objects.filter(pk=self.observation.pk).exists()
        )
        self.assertQuerySetEqual(
            self.site_report.assignees.all(),
            [self.observer_user, self.present_user],
            ordered=False,
        )
        create_pdf_mock.assert_called_once()


class ProjectMatrixSerializerTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(notify_on_object_assessment_save, sender=ObjectAssessment)
        pre_delete.disconnect(notify_on_object_assessment_delete, sender=ObjectAssessment)
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_object_assessment_save,
            sender=ObjectAssessment,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_object_assessment_delete,
            sender=ObjectAssessment,
        )

        self.user = User.objects.create_user(
            email="matrix@example.com",
            password="password",
            first_name="Matrix",
            last_name="User",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Matrix Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="matrix@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Matrix Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.other_project = Project.objects.create(
            project_name="Other Matrix Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        ECPMPConfig.objects.create(
            project=self.project,
            skills=["push", "move"],
            status_options=["action only:#000000"],
            demand_status_options=[
                "demand only:#123456",
                "in progress:#fff3ad",
            ],
            requirement_status_options=["requirement only:#654321"],
            bom_status_options=["bom only:#abcdef"],
        )

    def test_config_rejects_mandatory_columns_as_optional(self):
        serializer = ProjectMatrixConfigSerializer(
            data={
                "project": self.project.id,
                "demand_columns": [
                    {"key": "sequence", "label": "Sequence", "order": 0},
                ],
                "requirement_columns": [],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("demand_columns", serializer.errors)

    def test_config_rejects_status_as_optional_column(self):
        serializer = ProjectMatrixConfigSerializer(
            data={
                "project": self.project.id,
                "demand_columns": [
                    {"key": "status", "label": "Status", "order": 0},
                ],
                "requirement_columns": [
                    {"key": "status", "label": "Status", "order": 0},
                ],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("demand_columns", serializer.errors)
        self.assertIn("requirement_columns", serializer.errors)

    def test_solution_demand_status_is_optional(self):
        serializer = SolutionDemandSerializer(
            data={
                "project": self.project.id,
                "subject": "Demand",
                "demand_number": "D-1",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(serializer.save().status)

    def test_solution_demand_normalizes_configured_status(self):
        serializer = SolutionDemandSerializer(
            data={
                "project": self.project.id,
                "subject": "Demand",
                "demand_number": "D-1",
                "status": "in progress:#fff3ad",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.save().status, "in progress")

    def test_solution_demand_rejects_requirement_status(self):
        serializer = SolutionDemandSerializer(
            data={
                "project": self.project.id,
                "subject": "Demand",
                "status": "requirement only",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("status", serializer.errors)

    def test_requirement_accepts_only_requirement_statuses(self):
        serializer = RequirementSerializer(
            data={
                "project": self.project.id,
                "name": "Requirement",
                "status": "requirement only:#654321",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.save().status, "requirement only")

    def test_requirement_rejects_invalid_status(self):
        serializer = RequirementSerializer(
            data={
                "project": self.project.id,
                "name": "Requirement",
                "object_number": "R-1",
                "status": "blocked",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("status", serializer.errors)

    def test_requirement_allows_unchanged_removed_status(self):
        requirement = Requirement.objects.create(
            project=self.project,
            name="Requirement",
            object_number="R-1",
            status="legacy",
        )
        serializer = RequirementSerializer(
            requirement,
            data={"status": "legacy"},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.save().status, "legacy")

    def test_bom_status_is_optional_and_uses_bom_configuration(self):
        optional_serializer = BillOfMaterialPartSerializer(
            data={
                "project": self.project.id,
                "name": "Optional Status Part",
            }
        )
        self.assertTrue(optional_serializer.is_valid(), optional_serializer.errors)
        self.assertIsNone(optional_serializer.save().status)

        configured_serializer = BillOfMaterialPartSerializer(
            data={
                "project": self.project.id,
                "name": "Configured Status Part",
                "status": "bom only:#abcdef",
            }
        )
        self.assertTrue(configured_serializer.is_valid(), configured_serializer.errors)
        self.assertEqual(configured_serializer.save().status, "bom only")

        invalid_serializer = BillOfMaterialPartSerializer(
            data={
                "project": self.project.id,
                "name": "Invalid Status Part",
                "status": "demand only",
            }
        )
        self.assertFalse(invalid_serializer.is_valid())
        self.assertIn("status", invalid_serializer.errors)

    def test_parts_config_reserves_bom_status_column(self):
        serializer = PartsListConfigSerializer(
            data={
                "project": self.project.id,
                "available_product_columns": [],
                "bom_columns": [
                    {"key": "status", "label": "Status", "order": 0},
                ],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("bom_columns", serializer.errors)

    def test_project_parts_config_allows_owning_company_on_partial_update(self):
        config = PartsListConfig.objects.create(
            project=self.project,
            company_profile=self.company,
        )
        serializer = PartsListConfigSerializer(
            config,
            data={"bom_columns": []},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_solution_demand_hierarchy_rejects_level_eleven(self):
        parent = None
        for index in range(10):
            serializer = SolutionDemandSerializer(
                data={
                    "project": self.project.id,
                    "parent": parent.id if parent else None,
                    "subject": f"Demand {index}",
                    "demand_number": str(index),
                }
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            parent = serializer.save()

        serializer = SolutionDemandSerializer(
            data={
                "project": self.project.id,
                "parent": parent.id,
                "subject": "Too deep",
                "demand_number": "11",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("parent", serializer.errors)

    def test_solution_demand_rejects_object_assessment_from_other_project(self):
        assessment = ObjectAssessment.objects.create(
            project=self.other_project,
            record_type="Photo",
            record_title="Other photo",
            file=SimpleUploadedFile("photo.txt", b"content"),
        )
        serializer = SolutionDemandSerializer(
            data={
                "project": self.project.id,
                "subject": "Demand",
                "demand_number": "D-1",
                "object_assessments": [assessment.id],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("object_assessments", serializer.errors)

    def test_requirement_rejects_demand_from_other_project(self):
        other_demand = SolutionDemand.objects.create(
            project=self.other_project,
            subject="Other demand",
            demand_number="OD-1",
        )
        serializer = RequirementSerializer(
            data={
                "project": self.project.id,
                "name": "Requirement",
                "object_number": "R-1",
                "skill": "push",
                "solution_demands": [other_demand.id],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("solution_demands", serializer.errors)

    def test_requirement_skill_must_match_project_options(self):
        serializer = RequirementSerializer(
            data={
                "project": self.project.id,
                "name": "Requirement",
                "object_number": "R-1",
                "skill": "turn",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("skill", serializer.errors)


class ActionItemCommentNotificationTests(TestCase):
    def setUp(self):
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        m2m_changed.disconnect(
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )
        self.addCleanup(
            m2m_changed.connect,
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )

        self.actor = self._create_user("actor@example.com", "Actor")
        self.viewer = self._create_user("viewer@example.com", "Viewer")
        self.no_permission_user = self._create_user("noperms@example.com", "NoPerms")

        self.company = CompanyProfile.objects.create(
            admin_user=self.actor,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[self.actor.id, self.viewer.id, self.no_permission_user.id]
        ).update(company=self.company)
        self.actor.refresh_from_db()
        self.viewer.refresh_from_db()
        self.no_permission_user.refresh_from_db()

        action_item_view = Permission.objects.create(
            key="action_item.view",
            name="View action items",
            module="action_item",
        )
        viewer_role = Role.objects.create(
            name="Action item viewer",
            company=self.company,
            type="solution",
            scope="company",
        )
        RolePermission.objects.create(role=viewer_role, permission=action_item_view)
        UserRole.objects.create(
            user=self.viewer,
            role=viewer_role,
            company=self.company,
        )

        self.action_item = ActionItem.objects.create(
            company_profile=self.company,
            name="Review drawings",
            target_date=timezone.now().date(),
            status="open",
            created_by=self.actor,
        )

    def test_comment_notifies_other_users_with_action_item_view_permission(self):
        comment = ActionItemComment.objects.create(
            action_item=self.action_item,
            sender=self.actor,
            content="Please review this.",
        )

        _notify_new_comment(comment, self.actor)

        self.assertTrue(
            NotificationSeen.objects.filter(user=self.viewer).exists()
        )
        self.assertFalse(
            NotificationSeen.objects.filter(user=self.actor).exists()
        )
        self.assertFalse(
            NotificationSeen.objects.filter(user=self.no_permission_user).exists()
        )

    def test_comment_notifies_secondary_multi_assignee(self):
        self.action_item.assignees.set([self.actor, self.viewer])

        comment = ActionItemComment.objects.create(
            action_item=self.action_item,
            sender=self.actor,
            content="Need follow-up.",
        )

        _notify_new_comment(comment, self.actor)

        self.assertTrue(
            NotificationSeen.objects.filter(user=self.viewer).exists()
        )

    def _create_user(self, email, last_name):
        return User.objects.create_user(
            email=email,
            password="password",
            first_name=last_name,
            last_name=last_name,
            user_type="company_user",
        )


class ActionItemAssignedSubActionVisibilityTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        m2m_changed.disconnect(
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )
        self.addCleanup(
            m2m_changed.connect,
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )
        post_save.disconnect(create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        post_save.disconnect(milestone_post_save_operations, sender=Milestone)
        self.addCleanup(
            post_save.connect,
            milestone_post_save_operations,
            sender=Milestone,
        )
        post_save.disconnect(notify_on_milestone_save, sender=Milestone)
        self.addCleanup(
            post_save.connect,
            notify_on_milestone_save,
            sender=Milestone,
        )

        self.john = self._create_user("john@example.com", "John")
        self.alice = self._create_user("alice@example.com", "Alice")
        self.bob = self._create_user("bob@example.com", "Bob")

        self.company = CompanyProfile.objects.create(
            admin_user=self.john,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(id__in=[self.john.id, self.alice.id, self.bob.id]).update(
            company=self.company
        )
        self.john.refresh_from_db()
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()

    def _create_user(self, email, last_name):
        return User.objects.create_user(
            email=email,
            password="password",
            first_name=last_name,
            last_name=last_name,
            user_type="company_user",
        )

    def _assigned_created_only_permission(self, expected_scope):
        def side_effect(user, modules, scope, context):
            return (
                user == self.alice
                and modules == {"action_item": ["view_assigned_created"]}
                and scope == expected_scope
            )

        return side_effect

    def _broad_company_permission(self, user, modules, scope, context):
        return (
            user == self.alice
            and modules == {"action_item": ["view", "full_access"]}
            and scope == "company"
        )

    def _serialize_list_response(self, path, query_params, user):
        request = self.factory.get(path, query_params, format="json")
        force_authenticate(request, user=user)

        view = ActionItemViewSet()
        view.action_map = {"get": "list"}
        view.action = "list"
        view.args = ()
        view.kwargs = {}
        view.format_kwarg = None
        view.request = view.initialize_request(request)

        queryset = view.filter_queryset(view.get_queryset())
        serializer = view.get_serializer(queryset, many=True)
        return serializer.data

    @patch("project_management.views.action_item.require_any_permission_multiple_modules")
    def test_broad_company_queryset_includes_all_sub_action_items(
        self, permission_mock
    ):
        permission_mock.side_effect = self._broad_company_permission

        parent = ActionItem.objects.create(
            company_profile=self.company,
            created_by=self.john,
            name="Parent task",
            target_date=timezone.now().date(),
            status="open",
        )
        alice_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Alice child",
            target_date=timezone.now().date(),
            status="open",
        )
        bob_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Bob child",
            target_date=timezone.now().date(),
            status="open",
        )
        parent.assignees.set([self.john])
        alice_child.assignees.set([self.alice])
        bob_child.assignees.set([self.bob])

        request = self.factory.get(
            "/api/project_management/action_items/",
            {"company_profile": self.company.id},
            format="json",
        )
        force_authenticate(request, user=self.alice)
        view = ActionItemViewSet()
        view.action_map = {"get": "list"}
        view.action = "list"
        view.args = ()
        view.kwargs = {}
        view.format_kwarg = None
        view.request = view.initialize_request(request)

        visible_ids = set(view.get_queryset().values_list("id", flat=True))

        self.assertEqual(
            visible_ids,
            {parent.id, alice_child.id, bob_child.id},
        )

    @patch("project_management.views.action_item.require_any_permission_multiple_modules")
    def test_company_scope_returns_parent_with_only_visible_sub_action_items(
        self, permission_mock
    ):
        permission_mock.side_effect = self._assigned_created_only_permission(
            "company"
        )

        parent = ActionItem.objects.create(
            company_profile=self.company,
            created_by=self.john,
            name="Parent task",
            target_date=timezone.now().date(),
            status="open",
        )
        alice_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Alice child",
            target_date=timezone.now().date(),
            status="open",
        )
        bob_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Bob child",
            target_date=timezone.now().date(),
            status="open",
        )
        parent.assignees.set([self.john])
        alice_child.assignees.set([self.alice])
        bob_child.assignees.set([self.bob])

        data = self._serialize_list_response(
            "/api/project_management/action_items/",
            {"company_profile": self.company.id},
            self.alice,
        )

        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], parent.id)
        self.assertEqual(
            [item["id"] for item in data[0]["sub_action_items"]],
            [alice_child.id],
        )

    @patch("project_management.views.action_item.require_any_permission_multiple_modules")
    @patch("project_management.permissions.ActionItemPermission._check_permission")
    def test_company_scope_sub_action_endpoint_hides_sibling_sub_actions(
        self, permission_check_mock, permission_mock
    ):
        permission_mock.side_effect = self._assigned_created_only_permission(
            "company"
        )
        permission_check_mock.side_effect = (
            lambda user, module, action, scope, context: (
                user == self.alice
                and module == "action_item"
                and action == "view_assigned_created"
                and scope == "company"
            )
        )

        parent = ActionItem.objects.create(
            company_profile=self.company,
            created_by=self.john,
            name="Parent task",
            target_date=timezone.now().date(),
            status="open",
        )
        alice_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Alice child",
            target_date=timezone.now().date(),
            status="open",
        )
        bob_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Bob child",
            target_date=timezone.now().date(),
            status="open",
        )
        parent.assignees.set([self.john])
        alice_child.assignees.set([self.alice])
        bob_child.assignees.set([self.bob])

        view = ActionItemViewSet.as_view({"get": "sub_action_items"})
        request = self.factory.get(
            f"/api/project_management/action_items/{parent.id}/sub-action-items/",
            {"company_profile": self.company.id},
            format="json",
        )
        force_authenticate(request, user=self.alice)
        response = view(request, pk=parent.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [alice_child.id],
        )

    @patch("project_management.views.action_item.require_any_permission_multiple_modules")
    def test_project_scope_returns_parent_with_only_visible_sub_action_items(
        self, permission_mock
    ):
        permission_mock.side_effect = self._assigned_created_only_permission(
            "project"
        )

        project = Project.objects.create(
            solution="Enterprise Solution",
            project_name="Project A",
            contract_value=1000,
            company_profile=self.company,
            user=self.john,
        )
        milestone = Milestone.objects.create(
            project=project,
            name="Milestone 1",
            status="open",
            due_date=timezone.now().date(),
        )
        parent = ActionItem.objects.create(
            milestone=milestone,
            created_by=self.john,
            name="Parent project task",
            target_date=timezone.now().date(),
            status="open",
        )
        alice_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Alice project child",
            target_date=timezone.now().date(),
            status="open",
        )
        bob_child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Bob project child",
            target_date=timezone.now().date(),
            status="open",
        )
        parent.assignees.set([self.john])
        alice_child.assignees.set([self.alice])
        bob_child.assignees.set([self.bob])

        data = self._serialize_list_response(
            "/api/project_management/action_items/",
            {"project": project.id},
            self.alice,
        )

        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], parent.id)
        self.assertEqual(
            [item["id"] for item in data[0]["sub_action_items"]],
            [alice_child.id],
        )

    @patch("project_management.views.action_item.require_any_permission_multiple_modules")
    @patch("project_management.permissions.ActionItemPermission._check_permission")
    def test_company_scope_secondary_multi_assignee_can_view_parent_and_child(
        self, permission_check_mock, permission_mock
    ):
        permission_mock.side_effect = (
            lambda user, modules, scope, context: (
                user == self.bob
                and modules == {"action_item": ["view_assigned_created"]}
                and scope == "company"
            )
        )
        permission_check_mock.side_effect = (
            lambda user, module, action, scope, context: (
                user == self.bob
                and module == "action_item"
                and action == "view_assigned_created"
                and scope == "company"
            )
        )

        parent = ActionItem.objects.create(
            company_profile=self.company,
            created_by=self.john,
            name="Parent task",
            target_date=timezone.now().date(),
            status="open",
        )
        child = ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.john,
            name="Shared child",
            target_date=timezone.now().date(),
            status="open",
        )
        parent.assignees.set([self.john])
        child.assignees.set([self.alice, self.bob])

        data = self._serialize_list_response(
            "/api/project_management/action_items/",
            {"company_profile": self.company.id},
            self.bob,
        )

        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], parent.id)
        self.assertEqual(
            [item["id"] for item in data[0]["sub_action_items"]],
            [child.id],
        )

        view = ActionItemViewSet.as_view({"get": "sub_action_items"})
        request = self.factory.get(
            f"/api/project_management/action_items/{parent.id}/sub-action-items/",
            {"company_profile": self.company.id},
            format="json",
        )
        force_authenticate(request, user=self.bob)
        response = view(request, pk=parent.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [child.id],
        )


class ActionItemMultiAssigneeSerializerTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        m2m_changed.disconnect(
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )
        self.addCleanup(
            m2m_changed.connect,
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )

        self.owner = self._create_user("owner@example.com", "Owner")
        self.user_one = self._create_user("one@example.com", "One")
        self.user_two = self._create_user("two@example.com", "Two")

        self.company = CompanyProfile.objects.create(
            admin_user=self.owner,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[self.owner.id, self.user_one.id, self.user_two.id]
        ).update(company=self.company)
        self.owner.refresh_from_db()
        self.user_one.refresh_from_db()
        self.user_two.refresh_from_db()

    def _create_user(self, email, last_name):
        return User.objects.create_user(
            email=email,
            password="password",
            first_name=last_name,
            last_name=last_name,
            user_type="company_user",
        )

    def _request(self):
        request = self.factory.post(
            "/api/project_management/action_items/",
            {},
            format="json",
        )
        request.user = self.owner
        return request

    @patch("project_management.serializers.action_item.get_valid_choices")
    def test_create_accepts_multi_assignees(
        self, valid_choices_mock
    ):
        valid_choices_mock.side_effect = lambda key, *args, **kwargs: {
            "status_options": ["open:#0057B7"],
            "reminder_settings": ["none"],
            "priority_levels": ["low"],
        }.get(key, [])

        serializer = ActionItemSerializer(
            data={
                "company_profile": self.company.id,
                "name": "Multi assignee item",
                "target_date": str(timezone.now().date()),
                "status": "open",
                "reminder": "none",
                "priority_level": "low",
                "assignees": [self.user_one.id, self.user_two.id],
            },
            context={"request": self._request(), "user": self.owner},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        item = serializer.save()

        self.assertEqual(
            sorted(item.assignees.values_list("id", flat=True)),
            sorted([self.user_one.id, self.user_two.id]),
        )

        data = ActionItemSerializer(
            item,
            context={"request": self._request(), "user": self.owner},
        ).data
        self.assertEqual(
            sorted(assignee["id"] for assignee in data["assignees"]),
            sorted([self.user_one.id, self.user_two.id]),
        )

    @patch("project_management.serializers.action_item.get_valid_choices")
    def test_create_leaves_omitted_forecast_date_empty(
        self, valid_choices_mock
    ):
        valid_choices_mock.side_effect = lambda key, *args, **kwargs: {
            "status_options": ["open:#0057B7"],
            "reminder_settings": ["none"],
            "priority_levels": ["low"],
        }.get(key, [])
        target_date = str(timezone.now().date())

        serializer = ActionItemSerializer(
            data={
                "company_profile": self.company.id,
                "name": "Action item without forecast",
                "target_date": target_date,
                "status": "open",
                "reminder": "none",
                "priority_level": "low",
                "assignees": [self.user_one.id],
            },
            context={"request": self._request(), "user": self.owner},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        item = serializer.save()

        self.assertIsNone(item.forecast_date)

    @patch("project_management.serializers.action_item.get_valid_choices")
    def test_create_rejects_legacy_assignee_payload(
        self, valid_choices_mock
    ):
        valid_choices_mock.side_effect = lambda key, *args, **kwargs: {
            "status_options": ["open:#0057B7"],
            "reminder_settings": ["none"],
            "priority_levels": ["low"],
        }.get(key, [])

        serializer = ActionItemSerializer(
            data={
                "company_profile": self.company.id,
                "name": "Legacy assignee payload",
                "target_date": str(timezone.now().date()),
                "status": "open",
                "reminder": "none",
                "priority_level": "low",
                "assignee": self.user_one.id,
            },
            context={"request": self._request(), "user": self.owner},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("assignee", serializer.errors)

    @patch("project_management.serializers.action_item.get_valid_choices")
    def test_create_rejects_sub_action_item_under_closed_parent(
        self, valid_choices_mock
    ):
        valid_choices_mock.side_effect = lambda key, *args, **kwargs: {
            "status_options": ["open:#0057B7"],
            "reminder_settings": ["none"],
            "priority_levels": ["low"],
        }.get(key, [])
        target_date = timezone.now().date()
        parent = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.owner,
            created_by=self.owner,
            name="Closed parent",
            target_date=target_date,
            status="closed",
        )
        parent.assignees.set([self.owner])

        serializer = ActionItemSerializer(
            data={
                "parent_action_item": parent.id,
                "name": "New child",
                "target_date": str(target_date),
                "status": "open",
                "reminder": "none",
                "priority_level": "low",
                "assignees": [self.user_one.id],
            },
            context={"request": self._request(), "user": self.owner},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("parent_action_item", serializer.errors)


class ActionItemMultiAssigneeNotificationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.owner = User.objects.create_user(
            email="notify-owner@example.com",
            password="password",
            first_name="Notify",
            last_name="Owner",
            user_type="company_user",
        )
        self.user_one = User.objects.create_user(
            email="notify-one@example.com",
            password="password",
            first_name="Notify",
            last_name="One",
            user_type="company_user",
        )
        self.user_two = User.objects.create_user(
            email="notify-two@example.com",
            password="password",
            first_name="Notify",
            last_name="Two",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.owner,
            company_name="Acme",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(
            id__in=[self.owner.id, self.user_one.id, self.user_two.id]
        ).update(company=self.company)
        self.owner.refresh_from_db()
        self.user_one.refresh_from_db()
        self.user_two.refresh_from_db()

    def _request(self):
        request = self.factory.post(
            "/api/project_management/action_items/",
            {},
            format="json",
        )
        request.user = self.owner
        return request

    def _create_action_item(self, assignees):
        action_item = ActionItem.objects.create(
            company_profile=self.company,
            name="Notification item",
            target_date=timezone.now().date(),
            status="open",
            reminder="none",
            priority_level="low",
            created_by=self.owner,
        )
        action_item.assignees.set(assignees)
        return action_item

    @staticmethod
    def _assignee_update_calls(create_notification_mock):
        return [
            call
            for call in create_notification_mock.call_args_list
            if call.kwargs.get("action_type") == "update"
            and "fields: Assignees" in " ".join(call.kwargs.get("message", []))
        ]

    @staticmethod
    def _run_action_item_notification_task(task_name, *args):
        from project_management import tasks

        return getattr(tasks, task_name)(*args)

    @patch("project_management.signals.get_current_user")
    @patch(
        "project_management.services.action_item_notifications."
        "create_notification_with_recipients"
    )
    @patch("project_management.serializers.action_item.get_valid_choices")
    def test_create_notifies_all_multi_assignees(
        self, valid_choices_mock, create_notification_mock, current_user_mock
    ):
        valid_choices_mock.side_effect = lambda key, *args, **kwargs: {
            "status_options": ["open:#0057B7"],
            "reminder_settings": ["none"],
            "priority_levels": ["low"],
        }.get(key, [])
        current_user_mock.return_value = self.owner

        serializer = ActionItemSerializer(
            data={
                "company_profile": self.company.id,
                "name": "Notification item",
                "target_date": str(timezone.now().date()),
                "status": "open",
                "reminder": "none",
                "priority_level": "low",
                "assignees": [self.user_one.id, self.user_two.id],
            },
            context={"request": self._request(), "user": self.owner},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with patch(
            "project_management.signals._enqueue_action_item_notification",
            side_effect=self._run_action_item_notification_task,
        ), self.captureOnCommitCallbacks(execute=True):
            serializer.save()

        assignee_calls = [
            call
            for call in create_notification_mock.call_args_list
            if call.kwargs.get("action_type") == "create"
            and call.kwargs.get("message") == [
                'You have been assigned Action Item "Notification item" by Notify Owner.',
            ]
        ]

        self.assertTrue(assignee_calls)
        self.assertEqual(
            assignee_calls[0].kwargs["recipients"],
            {self.user_one, self.user_two},
        )

    @patch("project_management.signals.get_current_user")
    @patch(
        "project_management.services.action_item_notifications."
        "create_notification_with_recipients"
    )
    def test_update_does_not_notify_when_assignees_are_unchanged(
        self, create_notification_mock, current_user_mock
    ):
        current_user_mock.return_value = self.owner
        action_item = self._create_action_item([self.user_one, self.user_two])
        create_notification_mock.reset_mock()

        serializer = ActionItemSerializer(
            action_item,
            data={
                "name": "Updated notification item",
                "assignees": [self.user_two.id, self.user_one.id],
            },
            partial=True,
            context={"request": self._request(), "user": self.owner},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with patch(
            "project_management.signals._enqueue_action_item_notification",
            side_effect=self._run_action_item_notification_task,
        ), self.captureOnCommitCallbacks(execute=True):
            serializer.save()

        self.assertFalse(self._assignee_update_calls(create_notification_mock))
        self.assertEqual(
            set(action_item.assignees.values_list("id", flat=True)),
            {self.user_one.id, self.user_two.id},
        )

    @patch("project_management.signals.get_current_user")
    @patch(
        "project_management.services.action_item_notifications."
        "create_notification_with_recipients"
    )
    def test_update_notifies_when_assignees_are_removed(
        self, create_notification_mock, current_user_mock
    ):
        current_user_mock.return_value = self.owner
        action_item = self._create_action_item([self.user_one, self.user_two])
        create_notification_mock.reset_mock()

        serializer = ActionItemSerializer(
            action_item,
            data={"assignees": [self.user_one.id]},
            partial=True,
            context={"request": self._request(), "user": self.owner},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with patch(
            "project_management.signals._enqueue_action_item_notification",
            side_effect=self._run_action_item_notification_task,
        ), self.captureOnCommitCallbacks(execute=True):
            serializer.save()

        self.assertTrue(self._assignee_update_calls(create_notification_mock))
        self.assertEqual(
            set(action_item.assignees.values_list("id", flat=True)),
            {self.user_one.id},
        )


class ActionItemRouteTests(TestCase):
    def test_duplicate_endpoint_is_not_registered(self):
        response = APIClient().post(
            "/api/project_management/action_items/1/duplicate/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class CompanyPublicHolidaySerializerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="holiday-owner@example.com",
            password="password",
            first_name="Holiday",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Holiday Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])

    def test_rejects_duplicate_holiday_for_same_company_and_date(self):
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 1).date(),
            name="Labour Day",
            created_by=self.user,
        )

        serializer = CompanyPublicHolidaySerializer(
            data={
                "company_profile": self.company.id,
                "date": "2026-05-01",
                "name": "Workers Day",
                "source": "manual",
                "created_by": self.user.id,
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            serializer.errors["date"][0],
            "A public holiday already exists for this company on this date.",
        )

    def test_allows_same_date_for_different_company(self):
        other_user = User.objects.create_user(
            email="holiday-owner-2@example.com",
            password="password",
            first_name="Other",
            last_name="Owner",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_user,
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
        other_user.company = other_company
        other_user.save(update_fields=["company"])

        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 1).date(),
            name="Labour Day",
            created_by=self.user,
        )

        serializer = CompanyPublicHolidaySerializer(
            data={
                "company_profile": other_company.id,
                "date": "2026-05-01",
                "name": "Labour Day",
                "source": "manual",
                "created_by": other_user.id,
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


class StrictMonthlyTimesheetHelperTests(TestCase):
    def test_month_period_bounds_returns_exact_calendar_range(self):
        period_start, period_end = _month_period_bounds(2026, 3)

        self.assertEqual(period_start, timezone.datetime(2026, 3, 1).date())
        self.assertEqual(period_end, timezone.datetime(2026, 3, 31).date())

    def test_week_starts_for_month_include_first_and_last_overlapping_weeks(self):
        week_starts = _get_week_starts_for_month(2026, 3)

        self.assertEqual(
            week_starts,
            [
                timezone.datetime(2026, 2, 23).date(),
                timezone.datetime(2026, 3, 2).date(),
                timezone.datetime(2026, 3, 9).date(),
                timezone.datetime(2026, 3, 16).date(),
                timezone.datetime(2026, 3, 23).date(),
                timezone.datetime(2026, 3, 30).date(),
            ],
        )

    def test_week_dates_in_month_returns_only_in_month_days(self):
        week_start = timezone.datetime(2026, 3, 30).date()
        week_end = week_start + timezone.timedelta(days=6)

        self.assertEqual(
            _get_week_dates_in_month(week_start, week_end, 2026, 3),
            [
                timezone.datetime(2026, 3, 30).date(),
                timezone.datetime(2026, 3, 31).date(),
            ],
        )

    def test_submission_locked_dates_for_week_follow_submission_month_only(self):
        user = User.objects.create_user(
            email="strict-month-lock@example.com",
            password="password",
            first_name="Strict",
            last_name="Month",
            user_type="company_user",
        )
        submission = MonthlyTimesheetSubmission(
            user=user,
            month=3,
            year=2026,
            status="pending",
        )
        week_start = timezone.datetime(2026, 3, 30).date()
        week_end = week_start + timezone.timedelta(days=6)

        self.assertEqual(
            _get_submission_locked_dates_for_week(submission, week_start, week_end),
            [
                timezone.datetime(2026, 3, 30).date(),
                timezone.datetime(2026, 3, 31).date(),
            ],
        )

    def test_expected_daily_hours_uses_staff_weekly_hours_divided_by_five(self):
        user = User.objects.create_user(
            email="daily-hours@example.com",
            password="password",
            first_name="Daily",
            last_name="Hours",
            user_type="company_user",
        )
        Staff.objects.create(
            user=user,
            salary=1000,
            weekly_hours=37.5,
        )

        self.assertEqual(_get_expected_daily_hours(user), 7.5)

    def test_expected_daily_hours_falls_back_to_eight_when_staff_missing(self):
        user = User.objects.create_user(
            email="daily-hours-fallback@example.com",
            password="password",
            first_name="Fallback",
            last_name="Hours",
            user_type="company_user",
        )

        self.assertEqual(_get_expected_daily_hours(user), 8.0)


class CompanyPublicHolidayViewSetTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.viewer = User.objects.create_user(
            email="holiday-viewer@example.com",
            password="password",
            first_name="Holiday",
            last_name="Viewer",
            user_type="company_user",
        )
        self.manager = User.objects.create_user(
            email="holiday-manager@example.com",
            password="password",
            first_name="Holiday",
            last_name="Manager",
            user_type="company_user",
        )
        self.other_user = User.objects.create_user(
            email="holiday-other@example.com",
            password="password",
            first_name="Holiday",
            last_name="Other",
            user_type="company_user",
        )

        self.company = CompanyProfile.objects.create(
            admin_user=self.manager,
            company_name="Holiday View Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.other_company = CompanyProfile.objects.create(
            admin_user=self.other_user,
            company_name="Other Holiday Co",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="other@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )

        self.viewer.company = self.company
        self.viewer.save(update_fields=["company"])
        self.manager.company = self.company
        self.manager.save(update_fields=["company"])
        self.other_user.company = self.other_company
        self.other_user.save(update_fields=["company"])

        self._grant_company_permissions(
            self.viewer,
            ["company_configurations.view"],
            "Holiday Viewer Role",
            self.company,
        )
        self._grant_company_permissions(
            self.manager,
            ["company_configurations.full_access"],
            "Holiday Manager Role",
            self.company,
        )

    def _grant_company_permissions(self, user, permission_keys, role_name, company):
        role = Role.objects.create(
            name=role_name,
            company=company,
            type="solution",
            scope="company",
        )
        for permission_key in permission_keys:
            permission = Permission.objects.create(
                key=permission_key,
                name=permission_key,
                module=permission_key.split(".")[0],
            )
            RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=company)

    def _build_request(self, path, user, method="get", data=None):
        request_factory = getattr(self.factory, method.lower())
        request = request_factory(path, data=data or {}, format="json")
        force_authenticate(request, user=user)
        return request

    def test_list_is_scoped_to_request_users_company(self):
        own_holiday = CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 1, 1).date(),
            name="New Year",
            created_by=self.manager,
        )
        CompanyPublicHoliday.objects.create(
            company_profile=self.other_company,
            date=timezone.datetime(2026, 1, 1).date(),
            name="Other New Year",
            created_by=self.other_user,
        )

        view = CompanyPublicHolidayViewSet.as_view({"get": "list"})
        request = self._build_request(
            "/api/project_management/company-public-holidays/",
            self.viewer,
            method="get",
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], own_holiday.id)

    def test_create_uses_request_users_company_and_created_by(self):
        view = CompanyPublicHolidayViewSet.as_view({"post": "create"})
        request = self._build_request(
            "/api/project_management/company-public-holidays/",
            self.manager,
            method="post",
            data={
                "date": "2026-05-01",
                "name": "Labour Day",
                "source": "manual",
            },
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        holiday = CompanyPublicHoliday.objects.get()
        self.assertEqual(holiday.company_profile_id, self.company.id)
        self.assertEqual(holiday.created_by_id, self.manager.id)

    def test_view_only_user_cannot_create(self):
        view = CompanyPublicHolidayViewSet.as_view({"post": "create"})
        request = self._build_request(
            "/api/project_management/company-public-holidays/",
            self.viewer,
            method="post",
            data={
                "date": "2026-05-01",
                "name": "Labour Day",
                "source": "manual",
            },
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_synced_holiday_can_be_updated_and_deleted_locally(self):
        synced_holiday = CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 1).date(),
            name="Admin Labour Day",
            source="global_sync",
            synced_global_public_holiday_id=123,
            created_by=self.manager,
        )

        update_view = CompanyPublicHolidayViewSet.as_view({"patch": "partial_update"})
        update_request = self._build_request(
            f"/api/project_management/company-public-holidays/{synced_holiday.id}/",
            self.manager,
            method="patch",
            data={"name": "Changed Name"},
        )
        update_response = update_view(update_request, pk=synced_holiday.id)

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        synced_holiday.refresh_from_db()
        self.assertEqual(synced_holiday.name, "Changed Name")

        delete_view = CompanyPublicHolidayViewSet.as_view({"delete": "destroy"})
        delete_request = self._build_request(
            f"/api/project_management/company-public-holidays/{synced_holiday.id}/",
            self.manager,
            method="delete",
        )
        delete_response = delete_view(delete_request, pk=synced_holiday.id)

        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            CompanyPublicHoliday.objects.filter(pk=synced_holiday.id).exists()
        )

    @patch("project_management.services.public_holiday.requests.get")
    def test_import_adds_national_rows_and_skips_existing_dates(self, mock_get):
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 1).date(),
            name="Existing Labour Day",
            created_by=self.manager,
        )
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(
                return_value=[
                    {
                        "date": "2026-05-01",
                        "localName": "Labour Day",
                        "name": "Labour Day",
                        "global": True,
                    },
                    {
                        "date": "2026-12-25",
                        "localName": "Christmas Day",
                        "name": "Christmas Day",
                        "global": True,
                    },
                    {
                        "date": "2026-08-15",
                        "localName": "Regional Holiday",
                        "name": "Regional Holiday",
                        "global": False,
                    },
                ]
            ),
        )

        view = CompanyPublicHolidayViewSet.as_view({"post": "import_holidays"})
        request = self._build_request(
            "/api/project_management/company-public-holidays/import/",
            self.manager,
            method="post",
            data={"country_code": "DE", "year": 2026},
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_received"], 3)
        self.assertEqual(response.data["added_count"], 1)
        self.assertEqual(response.data["skipped_existing_count"], 1)
        self.assertEqual(response.data["skipped_non_national_count"], 1)
        self.assertEqual(response.data["skipped_invalid_count"], 0)
        self.assertTrue(
            CompanyPublicHoliday.objects.filter(
                company_profile=self.company,
                date=timezone.datetime(2026, 12, 25).date(),
                source="imported",
                country_code="DE",
            ).exists()
        )


class CompanyPublicHolidayGlobalSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="sync-owner@example.com",
            password="password",
            first_name="Sync",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Sync Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hello@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
    def test_sync_creates_and_updates_admin_controlled_public_holidays(self):
        global_holiday = GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 1).date(),
            name="Labour Day",
            created_by=self.user,
        )

        summary = sync_company_public_holidays_from_global(
            company_profile=self.company,
            acting_user=self.user,
        )

        self.assertEqual(summary["synced_count"], 1)
        synced_holiday = CompanyPublicHoliday.objects.get(
            company_profile=self.company,
            synced_global_public_holiday_id=global_holiday.id,
        )
        self.assertEqual(synced_holiday.source, "global_sync")
        self.assertEqual(synced_holiday.name, "Labour Day")

        global_holiday.name = "International Workers Day"
        global_holiday.save(update_fields=["name"])

        summary = sync_company_public_holidays_from_global(
            company_profile=self.company,
            acting_user=self.user,
        )

        self.assertEqual(summary["synced_count"], 1)
        synced_holiday = CompanyPublicHoliday.objects.get(
            company_profile=self.company,
            synced_global_public_holiday_id=global_holiday.id,
        )
        self.assertEqual(synced_holiday.name, "International Workers Day")

    def test_sync_replaces_local_holidays_with_admin_holidays(self):
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 1).date(),
            name="Local Labour Day",
            source="manual",
            created_by=self.user,
        )
        GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 1).date(),
            name="Admin Labour Day",
            created_by=self.user,
        )
        GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 12, 25).date(),
            name="Christmas Day",
            created_by=self.user,
        )

        summary = sync_company_public_holidays_from_global(
            company_profile=self.company,
            acting_user=self.user,
        )

        self.assertEqual(summary["synced_count"], 2)
        self.assertEqual(summary["removed_count"], 1)
        self.assertFalse(
            CompanyPublicHoliday.objects.filter(
                company_profile=self.company,
                date=timezone.datetime(2026, 5, 1).date(),
                source="manual",
            ).exists()
        )
        self.assertTrue(
            CompanyPublicHoliday.objects.filter(
                company_profile=self.company,
                date=timezone.datetime(2026, 12, 25).date(),
                source="global_sync",
            ).exists()
        )

    def test_preserve_sync_keeps_local_holidays_and_refreshes_admin_holidays(self):
        local_date = timezone.datetime(2026, 5, 1).date()
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=local_date,
            name="Local Labour Day",
            source="manual",
            created_by=self.user,
        )
        GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=local_date,
            name="Admin Labour Day",
            created_by=self.user,
        )
        GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 12, 25).date(),
            name="Christmas Day",
            created_by=self.user,
        )

        summary = sync_company_public_holidays_from_global(
            company_profile=self.company,
            acting_user=self.user,
            preserve_local=True,
        )

        self.assertEqual(summary["synced_count"], 1)
        self.assertEqual(summary["removed_count"], 0)
        self.assertTrue(
            CompanyPublicHoliday.objects.filter(
                company_profile=self.company,
                date=local_date,
                name="Local Labour Day",
                source="manual",
            ).exists()
        )
        self.assertTrue(
            CompanyPublicHoliday.objects.filter(
                company_profile=self.company,
                date=timezone.datetime(2026, 12, 25).date(),
                source="global_sync",
            ).exists()
        )


class InvoiceCreditNoteReplacementTests(TestCase):
    def setUp(self):
        notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        notification_patcher.start()
        self.addCleanup(notification_patcher.stop)

        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="billing@example.com",
            password="password",
            first_name="Bill",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Acme Billing",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="billing@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])

        CompanyConfig.objects.create(
            solution="Energy Consultant Solution",
            company_profile=self.company,
            invoice_status=["open", "due", "overdue", "paid", "issued"],
        )

        self.recipient = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Client Co",
        )

    def _build_request(self, data=None, method="post"):
        request_factory = getattr(self.factory, method)
        request = request_factory("/api/project_management/billing-invoice/", data or {}, format="json")
        request.user = self.user
        return request

    def _create_invoice(self, amount=500):
        today = timezone.now().date()
        return Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=today,
            due_date=today,
            status="open",
            grand_total=amount,
            vat_rate=0,
            document_type=Invoice.DocumentType.INVOICE,
            created_by=self.user,
            reference_number="INV-202604-00001",
        )

    @patch("project_management.utils.HTML")
    @patch(
        "project_management.utils.render_to_string",
        return_value="<html><body>Due date</body></html>",
    )
    def test_generated_invoice_and_credit_note_use_reference_as_public_filename(
        self,
        _mock_render,
        mock_html,
    ):
        mock_html.return_value.write_pdf.side_effect = (
            lambda output: output.write(b"%PDF-1.4 generated billing document")
        )
        root_folder = Folder.objects.create(
            company_profile=self.company,
            name=f"{self.company.id}_company_root_folder",
        )
        Folder.objects.create(
            company_profile=self.company,
            parent=root_folder,
            name="invoices",
        )
        invoice = self._create_invoice()
        credit_note = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            status="open",
            grand_total=-100,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=invoice,
            created_by=self.user,
            reference_number="CN-202604-00001",
        )

        for billing_document, expected_file_name in (
            (invoice, "INV-202604-00001.pdf"),
            (credit_note, "credit-note-CN-202604-00001.pdf"),
        ):
            with self.subTest(document_type=billing_document.document_type):
                document, action = create_pdf_invoice_by_bill(
                    billing_document,
                    self.user,
                )

                self.assertEqual(action, "created")
                self.assertEqual(document.file_name, expected_file_name)
                self.assertNotIn("company_root_folder", document.file_name)

                updated_document, update_action = create_pdf_invoice_by_bill(
                    billing_document,
                    self.user,
                )
                self.assertEqual(update_action, "updated")
                self.assertEqual(updated_document.pk, document.pk)
                self.assertEqual(updated_document.file_name, expected_file_name)

                package_document, _ = create_invoice_export_package(
                    billing_document,
                    include_mode=INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
                    request_user=self.user,
                )
                with package_document.file.open("rb") as package_file:
                    with zipfile.ZipFile(
                        io.BytesIO(package_file.read()),
                        "r",
                    ) as archive:
                        self.assertEqual(archive.namelist(), [expected_file_name])

    def test_billing_pdf_template_uses_due_date_label(self):
        from django.template.loader import render_to_string

        rendered_html = render_to_string(
            "billing_invoice.html",
            {
                "issue_date": "15-09-2026",
                "due_date": "30-09-2026",
                "document_label": "Invoice",
            },
        )

        self.assertIn("Due date: <strong>30-09-2026</strong>", rendered_html)
        self.assertNotIn("Date due:", rendered_html)

    @patch(
        "ecpmpapp.utils.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(InvoiceViewSet, "get_permissions", return_value=[])
    def test_net_total_ordering_keeps_credit_notes_last_in_both_directions(
        self,
        _permissions,
        _permission_lookup,
    ):
        low_total = self._create_invoice(amount=100)
        high_total = self._create_invoice(amount=500)
        high_total.reference_number = "INV-202604-00002"
        high_total.save(update_fields=["reference_number"])
        credit_note = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=-50,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=low_total,
            created_by=self.user,
            reference_number="CN-202604-00001",
        )

        for ordering, expected in (
            (
                "net_total_after_credit_notes",
                [low_total.id, high_total.id, credit_note.id],
            ),
            (
                "-net_total_after_credit_notes",
                [high_total.id, low_total.id, credit_note.id],
            ),
        ):
            with self.subTest(ordering=ordering):
                request = self.factory.get(
                    "/api/project_management/billing-invoice/",
                    {"ordering": ordering, "page_size": 25},
                )
                force_authenticate(request, user=self.user)
                response = InvoiceViewSet.as_view({"get": "list"})(request)

                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(
                    [row["id"] for row in response.data["results"]],
                    expected,
                )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_partial_status_update_preserves_omitted_line_items(
        self,
        mock_create_pdf,
    ):
        invoice = self._create_invoice()
        line_item = InvoiceLineItem.objects.create(
            invoice=invoice,
            description="Existing item",
            quantity=1,
            price=500,
            vat_amount=0,
        )
        serializer = InvoiceSerializer(
            invoice,
            data={"status": "due"},
            partial=True,
            context={"request": self._build_request(method="patch")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "due")
        self.assertTrue(
            InvoiceLineItem.objects.filter(pk=line_item.pk, invoice=invoice).exists()
        )
        mock_create_pdf.assert_called_once()

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_partial_credit_note_update_accepts_documents_without_line_items(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=500)
        credit_note = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=-100,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=source_invoice,
            created_by=self.user,
            reference_number="CN-202604-00004",
        )
        serializer = InvoiceSerializer(
            credit_note,
            data={
                "grand_total": -100,
                "notes": "Updated credit note",
                "documents": [],
                "status": "open",
            },
            partial=True,
            context={"request": self._build_request(method="patch")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated_credit_note = serializer.save()
        updated_credit_note.refresh_from_db()

        self.assertEqual(updated_credit_note.notes, "Updated credit note")
        self.assertEqual(updated_credit_note.line_items.count(), 0)
        self.assertTrue(mock_create_pdf.called)

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_credit_note_create_does_not_generate_replacement_invoice_for_positive_balance(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=500)
        serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -200,
                "notes": "Partial credit",
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        credit_note = serializer.save()

        source_invoice.refresh_from_db()

        self.assertEqual(source_invoice.status, "open")
        self.assertEqual(credit_note.status, "open")
        self.assertEqual(credit_note.source_invoice_id, source_invoice.id)
        self.assertFalse(
            Invoice.objects.filter(replaced_invoice=source_invoice).exists()
        )
        self.assertEqual(mock_create_pdf.call_count, 2)

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_credit_note_create_with_zero_remaining_balance_does_not_create_replacement(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=500)
        serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -500,
                "notes": "Full credit",
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        credit_note = serializer.save()

        source_invoice.refresh_from_db()

        self.assertEqual(source_invoice.status, "open")
        self.assertEqual(credit_note.status, "open")
        self.assertFalse(
            Invoice.objects.filter(replaced_invoice=source_invoice).exists()
        )
        self.assertEqual(mock_create_pdf.call_count, 2)

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_additional_credit_note_updates_net_total_without_replacement_invoice(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=750)

        first_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -650,
                "notes": "First partial credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(first_serializer.is_valid(), first_serializer.errors)
        first_serializer.save()

        self.assertFalse(
            Invoice.objects.filter(replaced_invoice=source_invoice).exists()
        )

        second_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -50,
                "notes": "Second partial credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(second_serializer.is_valid(), second_serializer.errors)
        second_credit_note = second_serializer.save()

        source_invoice.refresh_from_db()
        source_data = InvoiceSerializer(source_invoice).data

        self.assertEqual(source_invoice.status, "open")
        self.assertEqual(second_credit_note.status, "open")
        self.assertEqual(source_invoice.credit_notes.count(), 2)
        self.assertEqual(source_data["total_credit_notes_amount"], -700.0)
        self.assertEqual(source_data["net_total_after_credit_notes"], 50.0)
        self.assertFalse(
            Invoice.objects.filter(replaced_invoice=source_invoice).exists()
        )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_credit_notes_reference_original_invoice_in_linked_documents(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=750)

        first_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -650,
                "notes": "First partial credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(first_serializer.is_valid(), first_serializer.errors)
        first_credit_note = first_serializer.save()

        second_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -50,
                "notes": "Second partial credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(second_serializer.is_valid(), second_serializer.errors)
        second_credit_note = second_serializer.save()

        source_data = InvoiceSerializer(source_invoice).data
        first_credit_note_data = InvoiceSerializer(first_credit_note).data
        second_credit_note_data = InvoiceSerializer(second_credit_note).data

        source_linked_ids = {
            item["id"] for item in source_data["linked_documents"]
        }
        first_credit_note_linked_ids = {
            item["id"] for item in first_credit_note_data["linked_documents"]
        }
        second_credit_note_linked_ids = {
            item["id"] for item in second_credit_note_data["linked_documents"]
        }

        self.assertIn(first_credit_note.id, source_linked_ids)
        self.assertIn(second_credit_note.id, source_linked_ids)
        self.assertIn(source_invoice.id, first_credit_note_linked_ids)
        self.assertIn(source_invoice.id, second_credit_note_linked_ids)
        self.assertFalse(
            Invoice.objects.filter(replaced_invoice=source_invoice).exists()
        )
        self.assertFalse(
            any(
                item.get("relation_type") == "replacement_invoice"
                for item in source_data["linked_documents"]
            )
        )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_additional_credit_note_still_does_not_create_replacement_when_remaining_becomes_zero(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=500)

        first_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -400,
                "notes": "First partial credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(first_serializer.is_valid(), first_serializer.errors)
        first_serializer.save()

        self.assertFalse(
            Invoice.objects.filter(replaced_invoice=source_invoice).exists()
        )

        second_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -100,
                "notes": "Final credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(second_serializer.is_valid(), second_serializer.errors)
        second_credit_note = second_serializer.save()

        source_invoice.refresh_from_db()
        source_data = InvoiceSerializer(source_invoice).data

        self.assertEqual(source_invoice.status, "open")
        self.assertEqual(second_credit_note.status, "open")
        self.assertEqual(source_invoice.credit_notes.count(), 2)
        self.assertEqual(source_data["net_total_after_credit_notes"], 0.0)
        self.assertFalse(
            Invoice.objects.filter(replaced_invoice=source_invoice).exists()
        )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_partial_credit_note_on_118_creates_no_replacement_invoice(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=118)
        serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -18,
                "notes": "Partial credit",
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        credit_note = serializer.save()

        source_invoice.refresh_from_db()
        source_data = InvoiceSerializer(source_invoice).data

        self.assertEqual(credit_note.status, "open")
        self.assertEqual(source_invoice.status, "open")
        self.assertEqual(credit_note.source_invoice_id, source_invoice.id)
        self.assertEqual(source_data["net_total_after_credit_notes"], 100.0)
        self.assertEqual(
            Invoice.objects.filter(replaced_invoice=source_invoice).count(),
            0,
        )
        self.assertEqual(
            Invoice.objects.filter(
                company_profile=self.company,
                document_type=Invoice.DocumentType.INVOICE,
            ).count(),
            1,
        )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_credit_note_is_linked_to_source_invoice_purchase_order(
        self,
        mock_create_pdf,
    ):
        purchase_order = PurchaseOrder.objects.create(
            company_profile=self.company,
            created_by=self.user,
            reference="PO-CN-LINK-1",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Pending Invoice",
            amount_excl_vat=500,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=500,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        source_invoice = self._create_invoice(amount=118)
        source_invoice.purchase_order = purchase_order
        source_invoice.save(update_fields=["purchase_order", "updated_at"])

        serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -18,
                "notes": "PO linked credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        credit_note = serializer.save()

        self.assertEqual(credit_note.purchase_order_id, purchase_order.id)
        linked = PurchaseOrderSerializer(
            purchase_order,
            context={"request": self._build_request(method="get")},
        ).data["linked_invoices"]
        linked_ids = {item["id"] for item in linked}
        self.assertIn(source_invoice.id, linked_ids)
        self.assertIn(credit_note.id, linked_ids)
        credit_note_row = next(item for item in linked if item["id"] == credit_note.id)
        self.assertEqual(credit_note_row["document_type"], Invoice.DocumentType.CREDIT_NOTE)
        self.assertEqual(credit_note_row["grand_total"], -18.0)

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_credit_note_does_not_free_po_remaining_invoice_capacity(
        self,
        mock_create_pdf,
    ):
        from project_management.purchase_order_invoice_status import (
            get_linked_invoice_total,
        )

        purchase_order = PurchaseOrder.objects.create(
            company_profile=self.company,
            created_by=self.user,
            reference="PO-CN-BUDGET-1",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Pending Invoice",
            amount_excl_vat=1000,
            vat_rate=10,
            vat_amount=100,
            amount_incl_vat=1100,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        source_invoice = self._create_invoice(amount=784)
        source_invoice.purchase_order = purchase_order
        source_invoice.save(update_fields=["purchase_order", "updated_at"])

        credit_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -500,
                "notes": "Should not free PO capacity",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(credit_serializer.is_valid(), credit_serializer.errors)
        credit_serializer.save()

        # PO capacity is invoices-only; CN does not reduce used amount.
        self.assertEqual(float(get_linked_invoice_total(purchase_order)), 784.0)

        limit_serializer = InvoiceSerializer(context={"request": self._build_request()})
        # Remaining capacity should be 1100 - 784 = 316.
        limit_serializer._validate_invoice_against_po_limit(
            instance=None,
            purchase_order=purchase_order,
            grand_total=316,
        )
        with self.assertRaises(ValidationError):
            limit_serializer._validate_invoice_against_po_limit(
                instance=None,
                purchase_order=purchase_order,
                grand_total=317,
            )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_open_credit_note_can_be_manually_set_to_issued(
        self,
        mock_create_pdf,
    ):
        source_invoice = self._create_invoice(amount=500)
        create_serializer = InvoiceSerializer(
            data={
                "document_type": Invoice.DocumentType.CREDIT_NOTE,
                "source_invoice_id": source_invoice.id,
                "grand_total": -100,
                "notes": "Open credit",
            },
            context={"request": self._build_request()},
        )
        self.assertTrue(create_serializer.is_valid(), create_serializer.errors)
        credit_note = create_serializer.save()
        self.assertEqual(credit_note.status, "open")

        update_serializer = InvoiceSerializer(
            credit_note,
            data={"status": "issued"},
            partial=True,
            context={"request": self._build_request(method="patch")},
        )
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        updated = update_serializer.save()
        self.assertEqual(updated.status, "issued")

        source_invoice.refresh_from_db()
        self.assertEqual(source_invoice.status, "open")

    def test_issued_credit_note_cannot_be_edited(self):
        source_invoice = self._create_invoice(amount=500)
        credit_note = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="issued",
            grand_total=-100,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=source_invoice,
            created_by=self.user,
            reference_number="CN-202604-00002",
        )

        serializer = InvoiceSerializer(
            credit_note,
            data={"notes": "Changed"},
            partial=True,
            context={"request": self._build_request(method="patch")},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn(
            "Issued credit notes cannot be edited after creation.",
            serializer.errors["non_field_errors"],
        )

    def test_legacy_paid_credit_note_cannot_be_edited(self):
        source_invoice = self._create_invoice(amount=500)
        credit_note = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="paid",
            grand_total=-100,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=source_invoice,
            created_by=self.user,
            reference_number="CN-202604-00003",
        )

        serializer = InvoiceSerializer(
            credit_note,
            data={"notes": "Changed"},
            partial=True,
            context={"request": self._build_request(method="patch")},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn(
            "Issued credit notes cannot be edited after creation.",
            serializer.errors["non_field_errors"],
        )

    def test_credit_note_with_replacement_cannot_be_deleted(self):
        source_invoice = self._create_invoice(amount=500)
        credit_note = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="paid",
            grand_total=-100,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=source_invoice,
            created_by=self.user,
            reference_number="CN-202604-00002",
        )
        Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="due",
            grand_total=400,
            vat_rate=0,
            document_type=Invoice.DocumentType.INVOICE,
            replaced_invoice=source_invoice,
            carried_forward_amount=400,
            created_by=self.user,
            reference_number="INV-202604-00003",
        )

        view = InvoiceViewSet()
        view.request = self._build_request(method="delete")

        with self.assertRaises(ValidationError):
            view.perform_destroy(credit_note)

    def test_original_invoice_with_credit_note_chain_cannot_be_deleted(self):
        source_invoice = self._create_invoice(amount=500)
        Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="paid",
            grand_total=-100,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=source_invoice,
            created_by=self.user,
            reference_number="CN-202604-00002",
        )

        view = InvoiceViewSet()
        view.request = self._build_request(method="delete")

        with self.assertRaises(ValidationError) as exc:
            view.perform_destroy(source_invoice)

        self.assertIn(
            "This invoice cannot be deleted because it has linked credit notes or a replacement invoice.",
            str(exc.exception),
        )

    def test_replacement_invoice_cannot_be_deleted(self):
        source_invoice = self._create_invoice(amount=500)
        replacement_invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="due",
            grand_total=400,
            vat_rate=0,
            document_type=Invoice.DocumentType.INVOICE,
            replaced_invoice=source_invoice,
            carried_forward_amount=400,
            created_by=self.user,
            reference_number="INV-202604-00003",
        )

        view = InvoiceViewSet()
        view.request = self._build_request(method="delete")

        with self.assertRaises(ValidationError) as exc:
            view.perform_destroy(replacement_invoice)

        self.assertIn(
            "Replacement invoices that are part of a credit note adjustment chain cannot be deleted.",
            str(exc.exception),
        )


class TimesheetWeekSaveSerializerTests(TestCase):
    def setUp(self):
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(notify_on_leave_request_save, sender=LeaveRequest)
        pre_delete.disconnect(notify_on_leave_request_delete, sender=LeaveRequest)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_leave_request_save,
            sender=LeaveRequest,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_leave_request_delete,
            sender=LeaveRequest,
        )
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="timesheet@example.com",
            password="password",
            first_name="Time",
            last_name="Sheet",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Acme Time",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="timesheet@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.manager_user = User.objects.create_user(
            email="manager@example.com",
            password="password",
            first_name="Manager",
            last_name="User",
            user_type="company_user",
        )
        self.manager_user.company = self.company
        self.manager_user.save(update_fields=["company"])
        self.manager_staff = Staff.objects.create(
            user=self.manager_user,
            salary=1000,
            weekly_hours=40,
        )
        self.staff = Staff.objects.create(
            user=self.user,
            supervisor=self.manager_staff,
            salary=1000,
            weekly_hours=37.5,
        )

        self.company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Internal admin",
            target_date=timezone.now().date(),
            status="open",
        )
        self.project = Project.objects.create(
            project_name="PO Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    role="Project Member",
                    team_member=self.user,
                )
            ]
        )
        purchase_order_permission, _ = Permission.objects.get_or_create(
            key="purchase_order.view_project",
            defaults={
                "name": "View Project Purchase Orders",
                "module": "purchase_order",
            },
        )
        self.purchase_order_role = Role.objects.create(
            name="Timesheet Project PO Viewer",
            company=self.company,
            type="solution",
            scope="company",
            is_default=False,
            is_active=True,
            created_by=self.user,
        )
        RolePermission.objects.create(
            role=self.purchase_order_role,
            permission=purchase_order_permission,
        )
        UserRole.objects.create(
            user=self.user,
            role=self.purchase_order_role,
            company=self.company,
            assigned_by=self.user,
        )
        self.other_project = Project.objects.create(
            project_name="Other PO Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.project_milestone = Milestone.objects.create(
            project=self.project,
            name="Implementation Phase",
            due_date=timezone.now().date(),
        )
        self.other_project_milestone = Milestone.objects.create(
            project=self.other_project,
            name="Other Implementation Phase",
            due_date=timezone.now().date(),
        )
        self.project_action_item = ActionItem.objects.create(
            milestone=self.project_milestone,
            assignee=self.user,
            created_by=self.user,
            name="Send customer specs",
            target_date=timezone.now().date(),
            status="open",
        )
        self.other_project_action_item = ActionItem.objects.create(
            milestone=self.other_project_milestone,
            assignee=self.user,
            created_by=self.user,
            name="Other project task",
            target_date=timezone.now().date(),
            status="open",
        )
        self.purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=100,
            vat_rate=19,
            vat_amount=19,
            amount_incl_vat=119,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        self.change_notice_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="CHN-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=50,
            change_amount_excl_vat=50,
            vat_rate=19,
            vat_amount=9.5,
            amount_incl_vat=59.5,
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
            source_po=self.purchase_order,
        )

    def _build_request(self, data=None, method="put"):
        request_factory = getattr(self.factory, method)
        request = request_factory(
            "/api/project_management/timesheet-weeks/save/",
            data or {},
            format="json",
        )
        request.user = self.user
        return request

    def _sync_global_holidays(self):
        return sync_company_public_holidays_from_global(
            company_profile=self.company,
            acting_user=self.user,
        )

    def test_week_save_creates_activity_and_entries(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "is_invoiceable": True,
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 4},
                            {"entry_date": "2026-05-06", "hours": 3.5},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity = TimesheetActivity.objects.get(user=self.user)
        self.assertEqual(activity.source_type, "company_action_item")
        self.assertEqual(activity.activity_code, str(self.company_action_item.id))
        self.assertEqual(activity.activity_name, "Internal admin")
        self.assertTrue(activity.is_invoiceable)
        self.assertEqual(activity.entries.count(), 2)
        self.assertTrue(
            TimesheetEntry.objects.filter(
                activity=activity,
                entry_date="2026-05-05",
                hours=4,
            ).exists()
        )

    def test_week_save_creates_project_purchase_order_activity(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": self.purchase_order.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 4},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity = TimesheetActivity.objects.get(user=self.user)
        self.assertEqual(activity.source_type, "project_purchase_order")
        self.assertEqual(activity.project_id, self.project.id)
        self.assertEqual(activity.purchase_order_id, self.purchase_order.id)
        self.assertEqual(activity.activity_name, "PO Project / PO-001")

    def test_week_save_preserves_existing_po_activity_after_visibility_is_lost(self):
        foreign_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.manager_user,
            reference="PO-FOREIGN",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=100,
            vat_rate=19,
            vat_amount=19,
            amount_incl_vat=119,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        week_start = timezone.datetime(2026, 5, 4).date()
        week_end = week_start + timezone.timedelta(days=6)
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=foreign_purchase_order,
            activity_code=str(foreign_purchase_order.id),
            activity_name=f"PO Project / {foreign_purchase_order.reference}",
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=2,
        )
        view_created_permission, _ = Permission.objects.get_or_create(
            key="purchase_order.view_created",
            defaults={
                "name": "View Created Purchase Orders",
                "module": "purchase_order",
            },
        )
        RolePermission.objects.filter(role=self.purchase_order_role).delete()
        RolePermission.objects.create(
            role=self.purchase_order_role,
            permission=view_created_permission,
        )

        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": week_start.isoformat(),
                "activities": [
                    {
                        "id": activity.id,
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": foreign_purchase_order.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 3},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity.refresh_from_db()
        self.assertEqual(activity.purchase_order_id, foreign_purchase_order.id)
        self.assertEqual(activity.entries.get(entry_date="2026-05-05").hours, 3)

        another_foreign_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.manager_user,
            reference="PO-FOREIGN-2",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=100,
            vat_rate=19,
            vat_amount=19,
            amount_incl_vat=119,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        changed_link_serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": week_start.isoformat(),
                "activities": [
                    {
                        "id": activity.id,
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": another_foreign_purchase_order.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 3},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(changed_link_serializer.is_valid())
        self.assertIn("purchase_order", changed_link_serializer.errors["activities"][0])

    def test_week_save_creates_project_purchase_order_activity_with_phase_and_action_item_context(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": self.purchase_order.id,
                        "project_milestone": self.project_milestone.id,
                        "project_action_item": self.project_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 4},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity = TimesheetActivity.objects.get(user=self.user)
        self.assertEqual(activity.source_type, "project_purchase_order")
        self.assertEqual(activity.project_id, self.project.id)
        self.assertEqual(activity.purchase_order_id, self.purchase_order.id)
        self.assertEqual(activity.project_milestone_id, self.project_milestone.id)
        self.assertEqual(activity.project_action_item_id, self.project_action_item.id)

    def test_week_save_updates_existing_activity_target_by_id(self):
        week_start = timezone.datetime(2026, 5, 4).date()
        week_end = week_start + timezone.timedelta(days=6)
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
            is_invoiceable=False,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=2,
        )

        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": week_start.isoformat(),
                "activities": [
                    {
                        "id": activity.id,
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": self.purchase_order.id,
                        "project_milestone": self.project_milestone.id,
                        "project_action_item": self.project_action_item.id,
                        "is_invoiceable": False,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 3},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity.refresh_from_db()
        self.assertEqual(TimesheetActivity.objects.filter(user=self.user).count(), 1)
        self.assertEqual(activity.source_type, "project_purchase_order")
        self.assertIsNone(activity.company_action_item_id)
        self.assertEqual(activity.project_id, self.project.id)
        self.assertEqual(activity.purchase_order_id, self.purchase_order.id)
        self.assertEqual(activity.project_milestone_id, self.project_milestone.id)
        self.assertEqual(activity.project_action_item_id, self.project_action_item.id)
        self.assertFalse(activity.is_invoiceable)
        self.assertEqual(activity.entries.get(entry_date="2026-05-05").hours, 3)

    def test_week_save_rejects_edit_to_duplicate_activity_target(self):
        week_start = timezone.datetime(2026, 5, 4).date()
        week_end = week_start + timezone.timedelta(days=6)
        first_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.user,
            source_type="project",
            project=self.project,
            activity_code=str(self.project.id),
            activity_name=self.project.project_name,
        )
        second_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.user,
            source_type="project_phase",
            project_milestone=self.project_milestone,
            activity_code=str(self.project_milestone.id),
            activity_name=f"{self.project.project_name} / {self.project_milestone.name}",
        )

        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": week_start.isoformat(),
                "activities": [
                    {
                        "id": first_activity.id,
                        "source_type": "project",
                        "project": self.project.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 2},
                        ],
                    },
                    {
                        "id": second_activity.id,
                        "source_type": "project",
                        "project": self.project.id,
                        "entries": [
                            {"entry_date": "2026-05-06", "hours": 3},
                        ],
                    },
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("Duplicate activity target", str(serializer.errors))

    def test_week_save_allows_distinct_project_purchase_order_rows_with_different_purchase_orders(self):
        second_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-002",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=120,
            vat_rate=19,
            vat_amount=22.8,
            amount_incl_vat=142.8,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": self.purchase_order.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 2},
                        ],
                    },
                    {
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": second_purchase_order.id,
                        "entries": [
                            {"entry_date": "2026-05-06", "hours": 3},
                        ],
                    },
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_week_save_allows_distinct_project_purchase_order_rows_with_same_po_and_different_context(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": self.purchase_order.id,
                        "project_milestone": self.project_milestone.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 2},
                        ],
                    },
                    {
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": self.purchase_order.id,
                        "project_milestone": self.project_milestone.id,
                        "project_action_item": self.project_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-06", "hours": 3},
                        ],
                    },
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activities = list(
            TimesheetActivity.objects.filter(user=self.user).order_by("id")
        )
        self.assertEqual(len(activities), 2)
        self.assertEqual(activities[0].purchase_order_id, self.purchase_order.id)
        self.assertEqual(activities[0].project_milestone_id, self.project_milestone.id)
        self.assertIsNone(activities[0].project_action_item_id)
        self.assertEqual(activities[1].purchase_order_id, self.purchase_order.id)
        self.assertEqual(activities[1].project_milestone_id, self.project_milestone.id)
        self.assertEqual(activities[1].project_action_item_id, self.project_action_item.id)

    def test_week_save_creates_project_phase_activity_with_project_context(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "project_phase",
                        "project": self.project.id,
                        "project_milestone": self.project_milestone.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 4},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity = TimesheetActivity.objects.get(user=self.user)
        self.assertEqual(activity.source_type, "project_phase")
        self.assertIsNone(activity.project_id)
        self.assertEqual(activity.project_milestone_id, self.project_milestone.id)
        self.assertEqual(activity.activity_name, "PO Project / Implementation Phase")

    def test_week_save_creates_project_action_item_activity_with_project_context(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "project_action_item",
                        "project": self.project.id,
                        "project_action_item": self.project_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 4},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity = TimesheetActivity.objects.get(user=self.user)
        self.assertEqual(activity.source_type, "project_action_item")
        self.assertIsNone(activity.project_id)
        self.assertEqual(activity.project_action_item_id, self.project_action_item.id)
        self.assertEqual(activity.activity_name, "PO Project / Send customer specs")

    def test_week_save_rejects_project_purchase_order_with_phase_from_other_project(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "project_purchase_order",
                        "project": self.project.id,
                        "purchase_order": self.purchase_order.id,
                        "project_milestone": self.other_project_milestone.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 4},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("activities", serializer.errors)

    def test_week_save_rejects_negative_cell_hours(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": -1},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("activities", serializer.errors)

    def test_week_save_rejects_daily_totals_above_twenty_four_hours(self):
        second_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Another task",
            target_date=timezone.now().date(),
            status="open",
        )
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 12.5},
                        ],
                    },
                    {
                        "source_type": "company_action_item",
                        "company_action_item": second_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 12},
                        ],
                    },
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("day_errors", serializer.errors)

    def test_week_save_allows_daily_total_above_contractual_hours_when_below_twenty_four(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 8.5},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_week_save_allows_daily_total_below_contractual_hours(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 6},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_week_payload_includes_weekday_leave_locks_and_daily_hours(self):
        LeaveRequest.objects.create(
            employee=self.staff,
            manager=self.manager_staff,
            type="paid",
            start_date=timezone.datetime(2026, 5, 5).date(),
            end_date=timezone.datetime(2026, 5, 6).date(),
            status="approved",
        )

        view = TimesheetWeekViewSet()
        payload = view._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(payload["locked_dates"], ["2026-05-05", "2026-05-06"])
        self.assertEqual(
            payload["locked_day_details"],
            [
                {
                    "date": "2026-05-05",
                    "hours": 7.5,
                    "lock_type": "leave",
                    "leave_type": "paid",
                    "reason": None,
                },
                {
                    "date": "2026-05-06",
                    "hours": 7.5,
                    "lock_type": "leave",
                    "leave_type": "paid",
                    "reason": None,
                },
            ],
        )
        daily_totals = {item["date"]: item["hours"] for item in payload["daily_totals"]}
        self.assertEqual(daily_totals["2026-05-05"], 7.5)
        self.assertEqual(daily_totals["2026-05-06"], 7.5)
        self.assertEqual(payload["week_total_hours"], 15.0)
        self.assertEqual(payload["expected_daily_hours"], 7.5)
        self.assertEqual(payload["daily_warning_map"]["2026-05-05"], "balanced")
        self.assertEqual(payload["daily_warning_map"]["2026-05-06"], "balanced")
        self.assertEqual(payload["under_allocated_days_count"], 3)
        self.assertEqual(payload["over_allocated_days_count"], 0)

    def test_week_payload_skips_weekend_days_for_leave_locking(self):
        LeaveRequest.objects.create(
            employee=self.staff,
            manager=self.manager_staff,
            type="casual",
            start_date=timezone.datetime(2026, 5, 8).date(),
            end_date=timezone.datetime(2026, 5, 11).date(),
            status="approved",
        )

        view = TimesheetWeekViewSet()
        payload = view._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(payload["locked_dates"], ["2026-05-08"])
        daily_totals = {item["date"]: item["hours"] for item in payload["daily_totals"]}
        self.assertEqual(daily_totals["2026-05-08"], 7.5)
        self.assertEqual(daily_totals["2026-05-10"], 0.0)

    def test_week_save_rejects_entry_on_locked_leave_day(self):
        LeaveRequest.objects.create(
            employee=self.staff,
            manager=self.manager_staff,
            type="sick",
            start_date=timezone.datetime(2026, 5, 5).date(),
            end_date=timezone.datetime(2026, 5, 5).date(),
            status="approved",
        )

        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 2},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("activities", serializer.errors)

    def test_week_payload_includes_weekday_public_holiday_lock_and_hours(self):
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Liberation Day",
            created_by=self.user,
        )

        view = TimesheetWeekViewSet()
        payload = view._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(payload["locked_dates"], ["2026-05-05"])
        self.assertEqual(
            payload["locked_day_details"],
            [
                {
                    "date": "2026-05-05",
                    "hours": 7.5,
                    "lock_type": "public_holiday",
                    "holiday_name": "Liberation Day",
                    "reason": "Liberation Day",
                }
            ],
        )
        daily_totals = {item["date"]: item["hours"] for item in payload["daily_totals"]}
        self.assertEqual(daily_totals["2026-05-05"], 7.5)
        self.assertEqual(payload["week_total_hours"], 7.5)

    def test_week_payload_skips_weekend_public_holidays(self):
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 10).date(),
            name="Weekend Holiday",
            created_by=self.user,
        )

        view = TimesheetWeekViewSet()
        payload = view._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(payload["locked_dates"], [])
        daily_totals = {item["date"]: item["hours"] for item in payload["daily_totals"]}
        self.assertEqual(daily_totals["2026-05-10"], 0.0)
        self.assertNotIn("2026-05-10", payload["daily_warning_map"])

    def test_week_payload_includes_under_and_over_warning_states_for_weekdays_only(self):
        second_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Second task",
            target_date=timezone.now().date(),
            status="open",
        )
        week_start = timezone.datetime(2026, 5, 4).date()
        week_end = week_start + timezone.timedelta(days=6)
        first_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            source_type="company_action_item",
            activity_code=str(self.company_action_item.id),
            activity_name="Internal admin",
            company_action_item=self.company_action_item,
            is_invoiceable=True,
        )
        second_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            source_type="company_action_item",
            activity_code=str(second_action_item.id),
            activity_name="Second task",
            company_action_item=second_action_item,
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=first_activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=6,
        )
        TimesheetEntry.objects.create(
            activity=first_activity,
            entry_date=timezone.datetime(2026, 5, 6).date(),
            hours=7.5,
        )
        TimesheetEntry.objects.create(
            activity=first_activity,
            entry_date=timezone.datetime(2026, 5, 10).date(),
            hours=10,
        )
        TimesheetEntry.objects.create(
            activity=second_activity,
            entry_date=timezone.datetime(2026, 5, 7).date(),
            hours=8.5,
        )

        payload = TimesheetWeekViewSet()._build_week_payload(self.user, week_start)

        self.assertEqual(payload["expected_daily_hours"], 7.5)
        self.assertEqual(
            payload["daily_warning_map"],
            {
                "2026-05-04": "under",
                "2026-05-05": "under",
                "2026-05-06": "balanced",
                "2026-05-07": "over",
                "2026-05-08": "under",
            },
        )
        self.assertEqual(payload["under_allocated_days_count"], 3)
        self.assertEqual(payload["over_allocated_days_count"], 1)

    def test_public_holiday_overrides_existing_entry_hours_in_week_payload(self):
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=10,
        )
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Override Day",
            created_by=self.user,
        )

        view = TimesheetWeekViewSet()
        payload = view._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        daily_totals = {item["date"]: item["hours"] for item in payload["daily_totals"]}
        self.assertEqual(daily_totals["2026-05-05"], 7.5)
        self.assertEqual(payload["activities"][0]["entries"], [])

    def test_public_holiday_takes_precedence_over_leave_lock(self):
        LeaveRequest.objects.create(
            employee=self.staff,
            manager=self.manager_staff,
            type="paid",
            start_date=timezone.datetime(2026, 5, 5).date(),
            end_date=timezone.datetime(2026, 5, 5).date(),
            status="approved",
        )
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Holiday Wins",
            created_by=self.user,
        )

        view = TimesheetWeekViewSet()
        payload = view._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(
            payload["locked_day_details"][0]["lock_type"],
            "public_holiday",
        )
        self.assertEqual(
            payload["locked_day_details"][0]["holiday_name"],
            "Holiday Wins",
        )

    def test_week_save_rejects_entry_on_locked_public_holiday(self):
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Liberation Day",
            created_by=self.user,
        )

        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 2},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("activities", serializer.errors)

    def test_global_synced_holiday_locks_week_payload(self):
        global_holiday = GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Admin Holiday",
            created_by=self.user,
        )
        self._sync_global_holidays()

        payload = TimesheetWeekViewSet()._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(payload["locked_dates"], ["2026-05-05"])
        self.assertEqual(
            payload["locked_day_details"][0]["holiday_name"],
            "Admin Holiday",
        )
        synced_holiday = CompanyPublicHoliday.objects.get(
            company_profile=self.company,
            synced_global_public_holiday_id=global_holiday.id,
        )
        self.assertEqual(synced_holiday.source, "global_sync")

    def test_removing_global_holiday_restores_effective_logged_hours(self):
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=10,
        )
        global_holiday = GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Admin Override Day",
            created_by=self.user,
        )

        self._sync_global_holidays()
        locked_payload = TimesheetWeekViewSet()._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )
        locked_daily_totals = {
            item["date"]: item["hours"] for item in locked_payload["daily_totals"]
        }
        self.assertEqual(locked_daily_totals["2026-05-05"], 7.5)
        self.assertEqual(locked_payload["activities"][0]["entries"], [])

        global_holiday.delete()
        self._sync_global_holidays()

        unlocked_payload = TimesheetWeekViewSet()._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )
        unlocked_daily_totals = {
            item["date"]: item["hours"]
            for item in unlocked_payload["daily_totals"]
        }
        self.assertEqual(unlocked_payload["locked_dates"], [])
        self.assertEqual(unlocked_daily_totals["2026-05-05"], 10.0)
        self.assertEqual(len(unlocked_payload["activities"][0]["entries"]), 1)
        self.assertEqual(
            unlocked_payload["activities"][0]["entries"][0]["entry_date"],
            "2026-05-05",
        )
        self.assertEqual(
            unlocked_payload["activities"][0]["entries"][0]["hours"],
            10.0,
        )

    def test_global_synced_holiday_takes_precedence_over_leave(self):
        LeaveRequest.objects.create(
            employee=self.staff,
            manager=self.manager_staff,
            type="paid",
            start_date=timezone.datetime(2026, 5, 5).date(),
            end_date=timezone.datetime(2026, 5, 5).date(),
            status="approved",
        )
        GlobalPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Admin Public Holiday",
            created_by=self.user,
        )

        self._sync_global_holidays()

        payload = TimesheetWeekViewSet()._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(
            payload["locked_day_details"][0]["lock_type"],
            "public_holiday",
        )
        self.assertEqual(
            payload["locked_day_details"][0]["holiday_name"],
            "Admin Public Holiday",
        )

    def test_timesheet_activity_defaults_to_invoiceable(self):
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )

        self.assertTrue(activity.is_invoiceable)

    def test_week_save_persists_non_invoiceable_activity(self):
        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": "2026-05-04",
                "activities": [
                    {
                        "source_type": "company_action_item",
                        "is_invoiceable": False,
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-05-05", "hours": 4},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(user=self.user)

        activity = TimesheetActivity.objects.get(user=self.user)
        self.assertFalse(activity.is_invoiceable)

    def test_week_payload_includes_invoiceable_state(self):
        TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
            is_invoiceable=False,
        )

        payload = TimesheetWeekViewSet()._build_week_payload(
            self.user,
            timezone.datetime(2026, 5, 4).date(),
        )

        self.assertEqual(len(payload["activities"]), 1)
        self.assertFalse(payload["activities"][0]["is_invoiceable"])

    def test_partial_month_submission_locks_only_overlapping_month_dates_in_week_payload(self):
        week_start = timezone.datetime(2026, 3, 30).date()
        week_end = week_start + timezone.timedelta(days=6)
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 3, 31).date(),
            hours=5,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 4, 1).date(),
            hours=6,
        )
        MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=3,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )

        payload = TimesheetWeekViewSet()._build_week_payload(self.user, week_start)

        self.assertTrue(payload["can_edit"])
        self.assertEqual(payload["locked_dates"], ["2026-03-30", "2026-03-31"])
        self.assertEqual(
            payload["submissions"],
            [
                {
                    "id": MonthlyTimesheetSubmission.objects.get(
                        user=self.user,
                        month=3,
                        year=2026,
                    ).id,
                    "month": 3,
                    "year": 2026,
                    "status": "pending",
                    "submitted_at": MonthlyTimesheetSubmission.objects.get(
                        user=self.user,
                        month=3,
                        year=2026,
                    ).submitted_at,
                    "reviewed_at": None,
                    "reviewed_by": None,
                }
            ],
        )
        self.assertEqual(len(payload["activities"][0]["entries"]), 2)
        daily_totals = {item["date"]: item["hours"] for item in payload["daily_totals"]}
        self.assertEqual(daily_totals["2026-03-31"], 5.0)
        self.assertEqual(daily_totals["2026-04-01"], 6.0)

    def test_week_save_allows_unlocked_adjacent_month_changes_while_preserving_locked_month_entries(self):
        week_start = timezone.datetime(2026, 3, 30).date()
        week_end = week_start + timezone.timedelta(days=6)
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        locked_entry = TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 3, 31).date(),
            hours=5,
        )
        editable_entry = TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 4, 1).date(),
            hours=6,
        )
        MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=3,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
        )

        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": week_start.isoformat(),
                "activities": [
                    {
                        "id": activity.id,
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"id": locked_entry.id, "entry_date": "2026-03-31", "hours": 5},
                            {"id": editable_entry.id, "entry_date": "2026-04-01", "hours": 8},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_week_save_rejects_changing_locked_month_entry_in_partial_week(self):
        week_start = timezone.datetime(2026, 3, 30).date()
        week_end = week_start + timezone.timedelta(days=6)
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 3, 31).date(),
            hours=5,
        )
        MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=3,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )

        serializer = TimesheetWeekSaveSerializer(
            data={
                "week_start_date": week_start.isoformat(),
                "activities": [
                    {
                        "id": activity.id,
                        "source_type": "company_action_item",
                        "company_action_item": self.company_action_item.id,
                        "entries": [
                            {"entry_date": "2026-03-31", "hours": 4},
                        ],
                    }
                ],
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("activities", serializer.errors)


class TimesheetWeekDraftActivitySerializerTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(milestone_post_save_operations, sender=Milestone)
        post_save.disconnect(notify_on_milestone_save, sender=Milestone)
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        post_save.disconnect(notify_on_purchase_order_save, sender=PurchaseOrder)
        pre_delete.disconnect(notify_on_purchase_order_delete, sender=PurchaseOrder)
        post_save.disconnect(notify_on_calendar_event_save, sender=CalendarEvent)
        pre_delete.disconnect(notify_on_calendar_event_delete, sender=CalendarEvent)
        m2m_changed.disconnect(
            notify_on_calendar_event_assignees_changed,
            sender=CalendarEvent.assignees.through,
        )
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            milestone_post_save_operations,
            sender=Milestone,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_milestone_save,
            sender=Milestone,
        )
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_purchase_order_save,
            sender=PurchaseOrder,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_purchase_order_delete,
            sender=PurchaseOrder,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_calendar_event_save,
            sender=CalendarEvent,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_calendar_event_delete,
            sender=CalendarEvent,
        )
        self.addCleanup(
            m2m_changed.connect,
            notify_on_calendar_event_assignees_changed,
            sender=CalendarEvent.assignees.through,
        )
        self.user = User.objects.create_user(
            email="timesheet-draft@example.com",
            password="password",
            first_name="Draft",
            last_name="User",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Acme Draft",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="draft@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        purchase_order_role = Role.objects.create(
            name="Timesheet Purchase Order Viewer",
            company=self.company,
            type="solution",
            scope="company",
            is_default=False,
            is_active=True,
            created_by=self.user,
        )
        purchase_order_permission, _ = Permission.objects.get_or_create(
            key="purchase_order.view_project",
            defaults={
                "name": "View Project Purchase Orders",
                "module": "purchase_order",
            },
        )
        RolePermission.objects.create(
            role=purchase_order_role,
            permission=purchase_order_permission,
        )
        UserRole.objects.create(
            user=self.user,
            role=purchase_order_role,
            company=self.company,
            assigned_by=self.user,
        )

        self.project = Project.objects.create(
            project_name="Draft Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    role="Project Member",
                    team_member=self.user,
                )
            ]
        )
        self.milestone = Milestone.objects.create(
            project=self.project,
            name="Phase One",
            due_date=timezone.now().date(),
        )
        self.company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Company Task",
            target_date=timezone.now().date(),
            status="open",
        )
        self.second_company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Company Task 2",
            target_date=timezone.now().date(),
            status="open",
        )
        self.project_action_item = ActionItem.objects.create(
            milestone=self.milestone,
            assignee=self.user,
            created_by=self.user,
            name="Project Task",
            target_date=timezone.now().date(),
            status="open",
        )
        self.purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-100",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=100,
            vat_rate=19,
            vat_amount=19,
            amount_incl_vat=119,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        self.other_project = Project.objects.create(
            project_name="Other Draft Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.other_milestone = Milestone.objects.create(
            project=self.other_project,
            name="Other Phase",
            due_date=timezone.now().date(),
        )
        self.other_project_action_item = ActionItem.objects.create(
            milestone=self.other_milestone,
            assignee=self.user,
            created_by=self.user,
            name="Other Project Task",
            target_date=timezone.now().date(),
            status="open",
        )
        self.other_project_purchase_order = PurchaseOrder.objects.create(
            project=self.other_project,
            created_by=self.user,
            reference="PO-OTHER",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=100,
            vat_rate=19,
            vat_amount=19,
            amount_incl_vat=119,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        self.change_notice_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="CHN-100",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=50,
            change_amount_excl_vat=50,
            vat_rate=19,
            vat_amount=9.5,
            amount_incl_vat=59.5,
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
            source_po=self.purchase_order,
        )
        self.closed_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-CLOSED",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Closed",
            amount_excl_vat=100,
            vat_rate=19,
            vat_amount=19,
            amount_incl_vat=119,
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        self.assigned_calendar_event = CalendarEvent.objects.create(
            company_profile=self.company,
            name="Assigned Event",
            start_date=timezone.now(),
            end_date=timezone.now() + timezone.timedelta(hours=1),
            created_by=self.user,
        )
        self.assigned_calendar_event.assignees.add(self.user)
        self.unassigned_calendar_event = CalendarEvent.objects.create(
            company_profile=self.company,
            name="Unassigned Event",
            start_date=timezone.now(),
            end_date=timezone.now() + timezone.timedelta(hours=1),
            created_by=self.user,
        )

    def _build_request(self, path="/api/project_management/timesheet-weeks/activities/", data=None):
        request = self.factory.post(path, data or {}, format="json")
        request.user = self.user
        return request

    def _build_get_request(self, path, params=None):
        request = self.factory.get(path, params or {}, format="json")
        force_authenticate(request, user=self.user)
        return request

    def test_add_activity_serializer_builds_project_payload(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project",
                "project": self.project.id,
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payload = serializer.build_activity_payload()

        self.assertIsNone(payload["id"])
        self.assertEqual(payload["source_type"], "project")
        self.assertEqual(payload["project"], self.project.id)
        self.assertEqual(payload["activity_code"], str(self.project.id))
        self.assertEqual(payload["activity_name"], self.project.project_name)
        self.assertTrue(payload["is_invoiceable"])
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["target"]["id"], self.project.id)

    def test_add_activity_serializer_builds_project_purchase_order_payload(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_purchase_order",
                "project": self.project.id,
                "purchase_order": self.purchase_order.id,
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payload = serializer.build_activity_payload()

        self.assertEqual(payload["source_type"], "project_purchase_order")
        self.assertEqual(payload["project"], self.project.id)
        self.assertEqual(payload["purchase_order"], self.purchase_order.id)
        self.assertEqual(payload["activity_name"], "Draft Project / PO-100")
        self.assertEqual(payload["target"]["id"], self.purchase_order.id)
        self.assertEqual(payload["target"]["name"], "PO-100")

    def test_add_activity_serializer_rejects_purchase_order_without_permission(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_purchase_order",
                "project": self.project.id,
                "purchase_order": self.purchase_order.id,
            },
            context={"request": self._build_request()},
        )

        with patch.object(
            PurchaseOrderPermission,
            "can_select_project_purchase_orders",
            return_value=False,
        ):
            self.assertFalse(serializer.is_valid())

        self.assertIn("purchase_order", serializer.errors)

    def test_add_activity_serializer_rejects_purchase_order_for_non_team_member(self):
        ProjectTeamMembers.objects.filter(
            project=self.project,
            team_member=self.user,
        ).delete()
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_purchase_order",
                "project": self.project.id,
                "purchase_order": self.purchase_order.id,
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("purchase_order", serializer.errors)

    def test_add_activity_serializer_allows_project_phase_with_project_context(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_phase",
                "project": self.project.id,
                "project_milestone": self.milestone.id,
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payload = serializer.build_activity_payload()

        self.assertEqual(payload["source_type"], "project_phase")
        self.assertEqual(payload["project"], self.project.id)
        self.assertEqual(payload["project_milestone"], self.milestone.id)
        self.assertIsNone(payload["activity_subtitle"])
        self.assertEqual(payload["target"]["id"], self.milestone.id)
        self.assertEqual(payload["target"]["name"], self.milestone.name)

    def test_add_activity_serializer_allows_project_action_item_with_project_context(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_action_item",
                "project": self.project.id,
                "project_action_item": self.project_action_item.id,
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payload = serializer.build_activity_payload()

        self.assertEqual(payload["source_type"], "project_action_item")
        self.assertEqual(payload["project"], self.project.id)
        self.assertEqual(payload["project_action_item"], self.project_action_item.id)
        self.assertEqual(payload["activity_subtitle"], "Phase: Phase One")
        self.assertEqual(payload["target"]["id"], self.project_action_item.id)
        self.assertEqual(payload["target"]["name"], self.project_action_item.name)

    def test_add_activity_serializer_allows_project_purchase_order_with_phase_and_action_item_context(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_purchase_order",
                "project": self.project.id,
                "purchase_order": self.purchase_order.id,
                "project_milestone": self.milestone.id,
                "project_action_item": self.project_action_item.id,
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payload = serializer.build_activity_payload()

        self.assertEqual(payload["source_type"], "project_purchase_order")
        self.assertEqual(payload["project"], self.project.id)
        self.assertEqual(payload["purchase_order"], self.purchase_order.id)
        self.assertEqual(payload["project_milestone"], self.milestone.id)
        self.assertEqual(payload["project_action_item"], self.project_action_item.id)
        self.assertEqual(
            payload["activity_subtitle"],
            "Phase: Phase One · Action Item: Project Task",
        )

    def test_add_activity_serializer_rejects_project_purchase_order_action_item_from_other_project(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_purchase_order",
                "project": self.project.id,
                "purchase_order": self.purchase_order.id,
                "project_action_item": self.other_project_action_item.id,
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("project_action_item", serializer.errors)

    def test_add_activity_serializer_rejects_project_action_item_phase_mismatch(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_action_item",
                "project": self.project.id,
                "project_milestone": self.other_milestone.id,
                "project_action_item": self.project_action_item.id,
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("project_action_item", serializer.errors)

    def test_add_activity_serializer_rejects_purchase_order_from_other_project(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_purchase_order",
                "project": self.project.id,
                "purchase_order": self.other_project_purchase_order.id,
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("purchase_order", serializer.errors)

    def test_add_activity_serializer_rejects_change_notice_purchase_order(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project_purchase_order",
                "project": self.project.id,
                "purchase_order": self.change_notice_purchase_order.id,
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("purchase_order", serializer.errors)

    def test_add_activity_serializer_rejects_unassigned_calendar_event(self):
        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "company_calendar_event",
                "company_calendar_event": self.unassigned_calendar_event.id,
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("company_calendar_event", serializer.errors)

    def test_copy_previous_week_returns_unique_activities_without_hours(self):
        previous_week_start = timezone.datetime(2026, 5, 4).date()
        previous_week_end = previous_week_start + timezone.timedelta(days=6)
        current_week_start = timezone.datetime(2026, 5, 11).date()
        current_week_end = current_week_start + timezone.timedelta(days=6)

        duplicate_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=previous_week_start,
            week_end_date=previous_week_end,
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=duplicate_activity,
            entry_date=previous_week_start,
            hours=4,
        )
        unique_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=previous_week_start,
            week_end_date=previous_week_end,
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.second_company_action_item,
            activity_code=str(self.second_company_action_item.id),
            activity_name=self.second_company_action_item.name,
            is_invoiceable=False,
        )
        TimesheetEntry.objects.create(
            activity=unique_activity,
            entry_date=previous_week_start + timezone.timedelta(days=1),
            hours=3.5,
        )
        TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=current_week_start,
            week_end_date=current_week_end,
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )

        serializer = TimesheetCopyPreviousWeekSerializer(
            data={"week_start_date": current_week_start.isoformat()},
            context={"request": self._build_request("/api/project_management/timesheet-weeks/copy-previous-week/")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payloads = serializer.build_activity_payloads()

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["company_action_item"], self.second_company_action_item.id)
        self.assertFalse(payloads[0]["is_invoiceable"])
        self.assertEqual(payloads[0]["entries"], [])

    def test_copy_previous_week_preserves_project_activity_context(self):
        previous_week_start = timezone.datetime(2026, 5, 4).date()
        previous_week_end = previous_week_start + timezone.timedelta(days=6)
        current_week_start = timezone.datetime(2026, 5, 11).date()

        activity_specs = [
            {
                "source_type": "project",
                "project": self.project,
            },
            {
                "source_type": "project_phase",
                "project": self.project,
                "project_milestone": self.milestone,
            },
            {
                "source_type": "project_action_item",
                "project": self.project,
                "project_milestone": self.milestone,
                "project_action_item": self.project_action_item,
            },
            {
                "source_type": "project_purchase_order",
                "project": self.project,
                "purchase_order": self.purchase_order,
            },
            {
                "source_type": "project_purchase_order",
                "project": self.project,
                "purchase_order": self.purchase_order,
                "project_milestone": self.milestone,
            },
            {
                "source_type": "project_purchase_order",
                "project": self.project,
                "purchase_order": self.purchase_order,
                "project_action_item": self.project_action_item,
            },
            {
                "source_type": "project_purchase_order",
                "project": self.project,
                "purchase_order": self.purchase_order,
                "project_milestone": self.milestone,
                "project_action_item": self.project_action_item,
            },
        ]

        for activity_spec in activity_specs:
            target = (
                activity_spec.get("purchase_order")
                or activity_spec.get("project_action_item")
                or activity_spec.get("project_milestone")
                or activity_spec["project"]
            )
            TimesheetActivity.objects.create(
                user=self.user,
                week_start_date=previous_week_start,
                week_end_date=previous_week_end,
                created_by=self.user,
                activity_code=str(target.id),
                activity_name=str(target),
                **activity_spec,
            )

        serializer = TimesheetCopyPreviousWeekSerializer(
            data={"week_start_date": current_week_start.isoformat()},
            context={
                "request": self._build_request(
                    "/api/project_management/timesheet-weeks/copy-previous-week/"
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payloads = serializer.build_activity_payloads()
        copied_context = {
            (
                payload["source_type"],
                payload["project"],
                payload["purchase_order"],
                payload["project_milestone"],
                payload["project_action_item"],
            )
            for payload in payloads
        }
        expected_context = {
            (
                activity_spec["source_type"],
                activity_spec["project"].id,
                activity_spec.get("purchase_order").id
                if activity_spec.get("purchase_order")
                else None,
                activity_spec.get("project_milestone").id
                if activity_spec.get("project_milestone")
                else None,
                activity_spec.get("project_action_item").id
                if activity_spec.get("project_action_item")
                else None,
            )
            for activity_spec in activity_specs
        }

        self.assertEqual(copied_context, expected_context)
        self.assertTrue(all(payload["entries"] == [] for payload in payloads))

    def test_planned_activities_return_direct_project_activities(self):
        serializer = TimesheetPlannedActivitiesSerializer(
            data={
                "week_start_date": "2026-05-11",
                "project_ids": [self.project.id],
            },
            context={"request": self._build_request("/api/project_management/timesheet-weeks/planned-activities/")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payloads = serializer.build_activity_payloads()

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["source_type"], "project")
        self.assertEqual(payloads[0]["project"], self.project.id)
        self.assertTrue(payloads[0]["is_invoiceable"])
        self.assertEqual(payloads[0]["entries"], [])

    def test_planned_activities_return_project_purchase_order_rows(self):
        serializer = TimesheetPlannedActivitiesSerializer(
            data={
                "week_start_date": "2026-05-11",
                "project_id": self.project.id,
                "purchase_order_ids": [self.purchase_order.id],
            },
            context={"request": self._build_request("/api/project_management/timesheet-weeks/planned-activities/")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payloads = serializer.build_activity_payloads()

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["source_type"], "project_purchase_order")
        self.assertEqual(payloads[0]["project"], self.project.id)
        self.assertEqual(payloads[0]["purchase_order"], self.purchase_order.id)
        self.assertEqual(payloads[0]["activity_name"], "Draft Project / PO-100")

    def test_planned_activities_reject_ineligible_purchase_orders(self):
        serializer = TimesheetPlannedActivitiesSerializer(
            data={
                "week_start_date": "2026-05-11",
                "project_id": self.project.id,
                "purchase_order_ids": [
                    self.purchase_order.id,
                    self.change_notice_purchase_order.id,
                    self.closed_purchase_order.id,
                ],
            },
            context={"request": self._build_request("/api/project_management/timesheet-weeks/planned-activities/")},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("purchase_order_ids", serializer.errors)

    def test_planned_activities_skip_existing_project_purchase_order_signature(self):
        current_week_start = timezone.datetime(2026, 5, 11).date()
        current_week_end = current_week_start + timezone.timedelta(days=6)
        TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=current_week_start,
            week_end_date=current_week_end,
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=self.purchase_order,
            activity_code=str(self.purchase_order.id),
            activity_name="Draft Project / PO-100",
            is_invoiceable=True,
        )

        serializer = TimesheetPlannedActivitiesSerializer(
            data={
                "week_start_date": current_week_start.isoformat(),
                "project_id": self.project.id,
                "purchase_order_ids": [self.purchase_order.id],
            },
            context={"request": self._build_request("/api/project_management/timesheet-weeks/planned-activities/")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        payloads = serializer.build_activity_payloads()
        self.assertEqual(payloads, [])

    def test_planned_activity_purchase_orders_view_returns_only_eligible_purchase_orders(self):
        view = TimesheetWeekViewSet.as_view({"get": "planned_activity_purchase_orders"})
        request = self._build_get_request(
            "/api/project_management/timesheet-weeks/planned-activity-purchase-orders/",
            {"project_id": self.project.id},
        )

        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["project_id"], self.project.id)
        self.assertEqual(
            [item["id"] for item in response.data["purchase_orders"]],
            [self.purchase_order.id],
        )

    def test_add_activity_allowed_when_week_is_only_partially_locked_by_month_submission(self):
        MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=3,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )

        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-03-30",
                "source_type": "project",
                "project": self.project.id,
            },
            context={"request": self._build_request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_add_activity_rejected_when_all_week_dates_are_locked_by_month_submission(self):
        MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=5,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
        )

        serializer = TimesheetActivityDraftCreateSerializer(
            data={
                "week_start_date": "2026-05-11",
                "source_type": "project",
                "project": self.project.id,
            },
            context={"request": self._build_request()},
        )

        self.assertFalse(serializer.is_valid())


class MonthlyTimesheetWorkflowTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.submitter = User.objects.create_user(
            email="submitter@example.com",
            password="password",
            first_name="Monthly",
            last_name="Submitter",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="reviewer@example.com",
            password="password",
            first_name="Monthly",
            last_name="Reviewer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Acme Monthly",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="monthly@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.submitter.company = self.company
        self.submitter.save(update_fields=["company"])
        self.reviewer.company = self.company
        self.reviewer.save(update_fields=["company"])
        Staff.objects.create(
            user=self.submitter,
            salary=1000,
            weekly_hours=40,
            hourly_rate=70,
        )

        self._grant_company_permissions(
            self.submitter,
            ["time_booking.view_created", "time_booking.update_created", "time_booking.create"],
            "Submitter Role",
        )
        self._grant_company_permissions(
            self.reviewer,
            ["time_booking.review"],
            "Reviewer Role",
        )

        self.company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.submitter,
            created_by=self.reviewer,
            name="Monthly Admin Task",
            target_date=timezone.now().date(),
            status="open",
        )
        week_start = timezone.datetime(2026, 5, 4).date()
        week_end = week_start + timezone.timedelta(days=6)
        activity = TimesheetActivity.objects.create(
            user=self.submitter,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.submitter,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        TimesheetEntry.objects.create(activity=activity, entry_date=timezone.datetime(2026, 5, 5).date(), hours=4)
        TimesheetEntry.objects.create(activity=activity, entry_date=timezone.datetime(2026, 5, 6).date(), hours=3.5)

    def _grant_company_permissions(self, user, permission_keys, role_name):
        role = Role.objects.create(
            name=role_name,
            company=self.company,
            type="solution",
            scope="company",
        )
        for permission_key in permission_keys:
            permission = Permission.objects.create(
                key=permission_key,
                name=permission_key,
                module=permission_key.split(".")[0],
            )
            RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=self.company)

    def _build_request(self, path, user, method="get", data=None):
        request_factory = getattr(self.factory, method.lower())
        request = request_factory(path, data=data or {}, format="json")
        request.user = user
        return request

    def test_monthly_preview_returns_grouped_month_hours_for_submitter(self):
        view = MonthlyTimesheetViewSet.as_view({"get": "preview"})
        request = self._build_request(
            "/api/project_management/monthly-timesheets/preview/?month=5&year=2026",
            self.submitter,
            method="get",
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["month"], 5)
        self.assertEqual(response.data["year"], 2026)
        self.assertEqual(response.data["month_total_hours"], 7.5)
        self.assertEqual(len(response.data["weeks"]), 5)

    def test_submit_creates_pending_monthly_submission(self):
        view = MonthlyTimesheetViewSet.as_view({"post": "submit"})
        request = self._build_request(
            "/api/project_management/monthly-timesheets/submit/",
            self.submitter,
            method="post",
            data={"month": 5, "year": 2026},
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        submission = MonthlyTimesheetSubmission.objects.get(user=self.submitter, month=5, year=2026)
        self.assertEqual(submission.status, "pending")
        self.assertIsNotNone(submission.submitted_at)

    def test_reviewer_queue_lists_company_pending_submissions(self):
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )
        view = MonthlyTimesheetViewSet.as_view({"get": "list"})
        request = self._build_request(
            "/api/project_management/monthly-timesheets/?scope=review",
            self.reviewer,
            method="get",
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], submission.id)

    def test_reviewer_queue_filters_by_scope_and_invoiced_state(self):
        company_submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
        )
        project_submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=6,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
        )

        project = Project.objects.create(project_name="Monthly Project", created_by=self.reviewer)
        project_activity = TimesheetActivity.objects.create(
            user=self.submitter,
            week_start_date=timezone.datetime(2026, 6, 1).date(),
            week_end_date=timezone.datetime(2026, 6, 7).date(),
            created_by=self.submitter,
            source_type="project",
            project=project,
            activity_code=str(project.id),
            activity_name=project.project_name,
        )
        TimesheetEntry.objects.create(
            activity=project_activity,
            entry_date=timezone.datetime(2026, 6, 2).date(),
            hours=6,
        )

        invoice = Invoice.objects.create(
            company_profile=self.company,
            created_by=self.reviewer,
            status="paid",
            reference_number="INV-202605-00001",
            document_type="invoice",
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            grand_total=100,
            company_currency_amount=100,
            conversion_rate=1,
            vat_rate=0,
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            description="Monthly invoice",
            quantity=1,
            price=100,
            vat_amount=0,
            monthly_timesheet_submission=company_submission,
        )

        view = MonthlyTimesheetViewSet.as_view({"get": "list"})

        company_request = self._build_request(
            "/api/project_management/monthly-timesheets/?scope=review&submission_scope=company",
            self.reviewer,
            method="get",
        )
        company_response = view(company_request)
        self.assertEqual(company_response.status_code, status.HTTP_200_OK)
        self.assertEqual(company_response.data["count"], 1)
        self.assertEqual(company_response.data["results"][0]["id"], company_submission.id)
        self.assertTrue(company_response.data["results"][0]["is_invoiced"])

        project_request = self._build_request(
            "/api/project_management/monthly-timesheets/?scope=review&submission_scope=project&is_invoiced=false",
            self.reviewer,
            method="get",
        )
        project_response = view(project_request)
        self.assertEqual(project_response.status_code, status.HTTP_200_OK)
        self.assertEqual(project_response.data["count"], 1)
        self.assertEqual(project_response.data["results"][0]["id"], project_submission.id)
        self.assertFalse(project_response.data["results"][0]["is_invoiced"])

    def test_approve_generates_approved_timecards(self):
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )
        view = MonthlyTimesheetViewSet.as_view({"post": "approve"})
        request = self._build_request(
            f"/api/project_management/monthly-timesheets/{submission.id}/approve/",
            self.reviewer,
            method="post",
        )
        response = view(request, pk=submission.id)

        submission.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(submission.status, "approved")
        self.assertEqual(submission.reviewed_by_id, self.reviewer.id)
        generated_cards = TimeCard.objects.filter(timesheet_submission=submission).order_by("date")
        self.assertEqual(generated_cards.count(), 2)
        self.assertTrue(all(card.is_generated_from_timesheet for card in generated_cards))
        self.assertTrue(all(card.status == StatusType.APPROVED.value for card in generated_cards))
        self.assertEqual(generated_cards.first().company_profile_id, self.company.id)
        self.assertEqual(generated_cards.first().action_item_id, self.company_action_item.id)

    def test_approve_generates_timecards_only_for_strict_month_overlap_dates(self):
        first_overlap_start = timezone.datetime(2026, 2, 23).date()
        last_overlap_start = timezone.datetime(2026, 3, 30).date()

        first_overlap_activity = TimesheetActivity.objects.create(
            user=self.submitter,
            week_start_date=first_overlap_start,
            week_end_date=first_overlap_start + timezone.timedelta(days=6),
            created_by=self.submitter,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=first_overlap_activity,
            entry_date=timezone.datetime(2026, 2, 27).date(),
            hours=4,
        )
        TimesheetEntry.objects.create(
            activity=first_overlap_activity,
            entry_date=timezone.datetime(2026, 3, 1).date(),
            hours=5,
        )

        last_overlap_activity = TimesheetActivity.objects.create(
            user=self.submitter,
            week_start_date=last_overlap_start,
            week_end_date=last_overlap_start + timezone.timedelta(days=6),
            created_by=self.submitter,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=last_overlap_activity,
            entry_date=timezone.datetime(2026, 3, 31).date(),
            hours=7,
        )
        TimesheetEntry.objects.create(
            activity=last_overlap_activity,
            entry_date=timezone.datetime(2026, 4, 1).date(),
            hours=8,
        )

        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=3,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )

        response = MonthlyTimesheetViewSet.as_view({"post": "approve"})(
            self._build_request(
                f"/api/project_management/monthly-timesheets/{submission.id}/approve/",
                self.reviewer,
                method="post",
            ),
            pk=submission.id,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        generated_cards = TimeCard.objects.filter(
            timesheet_submission=submission,
        ).order_by("date", "id")
        self.assertEqual(
            [card.date for card in generated_cards],
            [
                timezone.datetime(2026, 3, 1).date(),
                timezone.datetime(2026, 3, 31).date(),
            ],
        )

        self.assertEqual(
            [card.hours for card in generated_cards],
            [5.0, 7.0],
        )

    def test_reject_creates_feedback_entry(self):
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )
        view = MonthlyTimesheetViewSet.as_view({"post": "reject"})
        request = self._build_request(
            f"/api/project_management/monthly-timesheets/{submission.id}/reject/",
            self.reviewer,
            method="post",
            data={"comment": "Please correct the allocation."},
        )
        response = view(request, pk=submission.id)

        submission.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(submission.status, "rejected")
        feedback = MonthlyTimesheetFeedback.objects.get(submission=submission)
        self.assertEqual(feedback.feedback_type, "rejection")
        self.assertEqual(feedback.comment, "Please correct the allocation.")

    def test_week_lock_checks_do_not_match_unrelated_cross_year_submissions(self):
        MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=1,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )
        view = TimesheetWeekViewSet.as_view({"get": "list"})
        request = self._build_request(
            "/api/project_management/timesheet-weeks/?week_start=2026-12-28",
            self.submitter,
            method="get",
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["can_edit"])
        self.assertIsNone(response.data["submission"])


class MonthlyTimesheetNotificationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.submitter = User.objects.create_user(
            email="notification-submitter@example.com",
            password="password",
            first_name="Notify",
            last_name="Submitter",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="notification-reviewer@example.com",
            password="password",
            first_name="Notify",
            last_name="Reviewer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Notify Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="notify@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.submitter.company = self.company
        self.submitter.save(update_fields=["company"])
        self.reviewer.company = self.company
        self.reviewer.save(update_fields=["company"])
        Staff.objects.create(
            user=self.submitter,
            salary=1000,
            weekly_hours=40,
            hourly_rate=70,
        )

        self._grant_company_permissions(
            self.submitter,
            ["time_booking.view_created", "time_booking.update_created", "time_booking.create"],
            "Notification Submitter Role",
        )
        self._grant_company_permissions(
            self.reviewer,
            ["time_booking.review"],
            "Notification Reviewer Role",
        )

        company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.submitter,
            created_by=self.reviewer,
            name="Notify Task",
            target_date=timezone.now().date(),
            status="open",
        )
        week_start = timezone.datetime(2026, 5, 4).date()
        week_end = week_start + timezone.timedelta(days=6)
        activity = TimesheetActivity.objects.create(
            user=self.submitter,
            week_start_date=week_start,
            week_end_date=week_end,
            created_by=self.submitter,
            source_type="company_action_item",
            company_action_item=company_action_item,
            activity_code=str(company_action_item.id),
            activity_name=company_action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=4,
        )

    def _grant_company_permissions(self, user, permission_keys, role_name):
        role = Role.objects.create(
            name=role_name,
            company=self.company,
            type="solution",
            scope="company",
        )
        for permission_key in permission_keys:
            permission = Permission.objects.create(
                key=permission_key,
                name=permission_key,
                module=permission_key.split(".")[0],
            )
            RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=self.company)

    def _build_request(self, method, path, user, data=None):
        request_factory = getattr(self.factory, method.lower())
        request = request_factory(path, data=data or {}, format="json")
        force_authenticate(request, user=user)
        return request

    def test_submit_creates_notification_for_reviewer(self):
        view = MonthlyTimesheetViewSet.as_view({"post": "submit"})
        request = self._build_request(
            "post",
            "/api/project_management/monthly-timesheets/submit/",
            self.submitter,
            {"month": 5, "year": 2026},
        )

        response = view(request)
        submission = MonthlyTimesheetSubmission.objects.get(
            user=self.submitter,
            month=5,
            year=2026,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        notification_seen = NotificationSeen.objects.filter(
            user=self.reviewer,
            notification__object_id=submission.id,
            notification__content_type__model="monthlytimesheetsubmission",
        ).select_related("notification").first()
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f'Monthly Timesheet "05/2026" from {get_actor_name(self.submitter)} requires your approval.',
        )

    def test_approve_creates_notification_for_submitter(self):
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )
        view = MonthlyTimesheetViewSet.as_view({"post": "approve"})
        request = self._build_request(
            "post",
            f"/api/project_management/monthly-timesheets/{submission.id}/approve/",
            self.reviewer,
        )

        response = view(request, pk=submission.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        notification_seen = NotificationSeen.objects.filter(
            user=self.submitter,
            notification__object_id=submission.id,
            notification__content_type__model="monthlytimesheetsubmission",
        ).select_related("notification").first()
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f'Monthly Timesheet "05/2026" was approved by {get_actor_name(self.reviewer)}.',
        )

    def test_reject_creates_notification_for_submitter(self):
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )
        view = MonthlyTimesheetViewSet.as_view({"post": "reject"})
        request = self._build_request(
            "post",
            f"/api/project_management/monthly-timesheets/{submission.id}/reject/",
            self.reviewer,
            {"comment": "Please fix hours."},
        )

        response = view(request, pk=submission.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        notification_seen = NotificationSeen.objects.filter(
            user=self.submitter,
            notification__object_id=submission.id,
            notification__content_type__model="monthlytimesheetsubmission",
        ).select_related("notification").first()
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f'Monthly Timesheet "05/2026" was rejected by {get_actor_name(self.reviewer)}.',
        )

    def test_resubmit_creates_same_notification_for_reviewer(self):
        MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="rejected",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.reviewer,
        )
        view = MonthlyTimesheetViewSet.as_view({"post": "submit"})
        request = self._build_request(
            "post",
            "/api/project_management/monthly-timesheets/submit/",
            self.submitter,
            {"month": 5, "year": 2026, "comment": "Updated and resubmitted."},
        )

        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        notification_seen = NotificationSeen.objects.filter(
            user=self.reviewer,
            notification__object_id=MonthlyTimesheetSubmission.objects.get(
                user=self.submitter,
                month=5,
                year=2026,
            ).id,
            notification__content_type__model="monthlytimesheetsubmission",
        ).order_by("-notification__created_at").select_related("notification").first()
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f'Monthly Timesheet "05/2026" from {get_actor_name(self.submitter)} requires your approval.',
        )


class ExpenseBookingReviewPermissionTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            email="expense-creator@example.com",
            password="password",
            first_name="Expense",
            last_name="Creator",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="expense-reviewer@example.com",
            password="password",
            first_name="Expense",
            last_name="Reviewer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.creator,
            company_name="Expense Review Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="expense-review@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        for user in [self.creator, self.reviewer]:
            user.company = self.company
            user.save(update_fields=["company"])

        self.expense_card = ExpenseCard.objects.create(
            company_profile=self.company,
            name="Reviewable Expense",
            description="Reviewable Expense",
            category="Travel",
            rate=50,
            quantity=2,
            total=100,
            created_by=self.creator,
            status=StatusType.PENDING.value,
        )

    def _build_serializer(self, user, has_review_permission):
        request = Mock(user=user, query_params={})
        permission_patch = patch(
            "project_management.serializers.time_expense.require_any_permission_multiple_modules",
            return_value=has_review_permission,
        )
        choices_patch = patch(
            "project_management.serializers.time_expense.get_valid_choices",
            return_value=[],
        )
        return request, permission_patch, choices_patch

    def test_reviewer_permission_can_approve_another_users_expense(self):
        request, permission_patch, choices_patch = self._build_serializer(
            self.reviewer,
            True,
        )
        with permission_patch as permission_mock, choices_patch:
            serializer = ExpenseCardSerializer(
                self.expense_card,
                data={"status": StatusType.APPROVED.value},
                partial=True,
                context={"request": request},
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            updated_expense = serializer.save()

        self.assertEqual(updated_expense.status, StatusType.APPROVED.value)
        self.assertEqual(updated_expense.approved_by, self.reviewer)
        checked_modules = [call.args[1] for call in permission_mock.call_args_list]
        self.assertTrue(checked_modules)
        self.assertTrue(
            all("expense_booking" in module_actions for module_actions in checked_modules)
        )

    def test_user_cannot_approve_their_own_expense(self):
        request, permission_patch, choices_patch = self._build_serializer(
            self.creator,
            True,
        )
        with permission_patch, choices_patch:
            serializer = ExpenseCardSerializer(
                self.expense_card,
                data={"status": StatusType.APPROVED.value},
                partial=True,
                context={"request": request},
            )
            self.assertFalse(serializer.is_valid())

        self.assertIn(
            "You cannot approve/reject your own expense card.",
            str(serializer.errors),
        )

    def test_user_without_review_permission_cannot_reject_expense(self):
        request, permission_patch, choices_patch = self._build_serializer(
            self.reviewer,
            False,
        )
        with permission_patch, choices_patch:
            serializer = ExpenseCardSerializer(
                self.expense_card,
                data={"status": StatusType.REJECTED.value},
                partial=True,
                context={"request": request},
            )
            self.assertFalse(serializer.is_valid())

        self.assertIn(
            "You do not have permission to approve/reject this ExpenseCard.",
            str(serializer.errors),
        )

    def test_review_permission_only_applies_to_status_decisions(self):
        permission = ExpenseBookingPermission(action="update", scope="company")
        with patch.object(
            permission,
            "_check_permission",
            side_effect=lambda _user, _module, action, _scope, _context: (
                action == "review"
            ),
        ):
            self.assertTrue(
                permission._check_update_permission(
                    self.reviewer,
                    self.expense_card,
                    is_review=True,
                )
            )
            self.assertFalse(
                permission._check_update_permission(
                    self.reviewer,
                    self.expense_card,
                    is_review=False,
                )
            )
            self.assertFalse(
                permission._check_update_permission(
                    self.creator,
                    self.expense_card,
                    is_review=True,
                )
            )


class ExpenseAndLeaveReviewNotificationTests(TestCase):
    def setUp(self):
        self.submitter = User.objects.create_user(
            email="notify-owner@example.com",
            password="password",
            first_name="Owner",
            last_name="User",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="notify-reviewer@example.com",
            password="password",
            first_name="Review",
            last_name="User",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Notify Review Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="notify-review@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.submitter.company = self.company
        self.submitter.save(update_fields=["company"])
        self.reviewer.company = self.company
        self.reviewer.save(update_fields=["company"])

        self.employee_staff = Staff.objects.create(
            user=self.submitter,
            salary=1000,
            weekly_hours=40,
            hourly_rate=70,
        )
        self.manager_staff = Staff.objects.create(
            user=self.reviewer,
            salary=1500,
            weekly_hours=40,
            hourly_rate=90,
        )
        self.employee_staff.supervisor = self.manager_staff
        self.employee_staff.save(update_fields=["supervisor"])

    def test_expense_approve_creates_notification_for_submitter(self):
        with patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        ):
            expense_card = ExpenseCard.objects.create(
                company_profile=self.company,
                name="Flight Cost",
                description="Flight Cost",
                category="Travel",
                rate=50,
                quantity=2,
                total=100,
                created_by=self.submitter,
                status="pending",
            )

            with patch(
                "project_management.signals.get_current_user",
                return_value=self.reviewer,
            ):
                expense_card.status = "approved"
                expense_card.approved_by = self.reviewer
                expense_card.save()

        notification_seen = (
            NotificationSeen.objects.filter(
                user=self.submitter,
                notification__object_id=expense_card.id,
                notification__content_type__model="expensecard",
            )
            .order_by("-notification__created_at")
            .select_related("notification")
            .first()
        )
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f'Expense Card "Flight Cost" was approved by {get_actor_name(self.reviewer)}.',
        )

    def test_expense_reject_creates_notification_for_submitter(self):
        with patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        ):
            expense_card = ExpenseCard.objects.create(
                company_profile=self.company,
                name="Hotel Cost",
                description="Hotel Cost",
                category="Travel",
                rate=80,
                quantity=1,
                total=80,
                created_by=self.submitter,
                status="pending",
            )

            with patch(
                "project_management.signals.get_current_user",
                return_value=self.reviewer,
            ):
                expense_card.status = "rejected"
                expense_card.rejected_by = self.reviewer
                expense_card.save()

        notification_seen = (
            NotificationSeen.objects.filter(
                user=self.submitter,
                notification__object_id=expense_card.id,
                notification__content_type__model="expensecard",
            )
            .order_by("-notification__created_at")
            .select_related("notification")
            .first()
        )
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f'Expense Card "Hotel Cost" was rejected by {get_actor_name(self.reviewer)}.',
        )

    def test_leave_approve_creates_notification_for_employee(self):
        with patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        ):
            leave_request = LeaveRequest.objects.create(
                employee=self.employee_staff,
                manager=self.manager_staff,
                type="Annual",
                start_date=timezone.now().date(),
                end_date=timezone.now().date(),
                status="pending",
            )

            with patch(
                "human_resource_management.signals.get_current_user",
                return_value=self.reviewer,
            ):
                leave_request.status = "approved"
                leave_request.save()

        notification_seen = (
            NotificationSeen.objects.filter(
                user=self.submitter,
                notification__object_id=leave_request.id,
                notification__content_type__model="leaverequest",
            )
            .order_by("-notification__created_at")
            .select_related("notification")
            .first()
        )
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f"Your leave request was approved by {get_actor_name(self.reviewer)}.",
        )

    def test_leave_reject_creates_notification_for_employee(self):
        with patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        ):
            leave_request = LeaveRequest.objects.create(
                employee=self.employee_staff,
                manager=self.manager_staff,
                type="Annual",
                start_date=timezone.now().date(),
                end_date=timezone.now().date(),
                status="pending",
            )

            with patch(
                "human_resource_management.signals.get_current_user",
                return_value=self.reviewer,
            ):
                leave_request.status = "rejected"
                leave_request.save()

        notification_seen = (
            NotificationSeen.objects.filter(
                user=self.submitter,
                notification__object_id=leave_request.id,
                notification__content_type__model="leaverequest",
            )
            .order_by("-notification__created_at")
            .select_related("notification")
            .first()
        )
        self.assertIsNotNone(notification_seen)
        self.assertEqual(
            notification_seen.notification.message,
            f"Your leave request was rejected by {get_actor_name(self.reviewer)}.",
        )


class MonthlyTimesheetInvoiceablePreviewTests(TestCase):
    def setUp(self):
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.submitter = User.objects.create_user(
            email="invoiceable-preview@example.com",
            password="password",
            first_name="Invoiceable",
            last_name="Preview",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="invoiceable-reviewer@example.com",
            password="password",
            first_name="Invoiceable",
            last_name="Reviewer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Acme Invoiceable Preview",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="invoiceable-preview@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.submitter.company = self.company
        self.submitter.save(update_fields=["company"])
        self.reviewer.company = self.company
        self.reviewer.save(update_fields=["company"])

        self.company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.submitter,
            created_by=self.reviewer,
            name="Invoiceable Monthly Task",
            target_date=timezone.now().date(),
            status="open",
        )
        week_start = timezone.datetime(2026, 5, 4).date()
        activity = TimesheetActivity.objects.create(
            user=self.submitter,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.submitter,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name=self.company_action_item.name,
            is_invoiceable=False,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=4,
        )

    def test_monthly_preview_builder_includes_invoiceable_state(self):
        preview = _build_monthly_timesheet_preview(self.submitter, 5, 2026)

        self.assertEqual(len(preview["weeks"]), 1)
        self.assertEqual(len(preview["weeks"][0]["activities"]), 1)
        self.assertFalse(preview["weeks"][0]["activities"][0]["is_invoiceable"])

    def test_submission_preview_response_includes_invoiceable_state(self):
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.submitter,
            month=5,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )

        payload = MonthlyTimesheetViewSet()._build_submission_preview_response(submission)

        self.assertEqual(payload["submission"]["id"], submission.id)
        self.assertFalse(payload["weeks"][0]["activities"][0]["is_invoiceable"])


class MonthlyTimesheetPreviewMetadataTests(TestCase):
    def setUp(self):
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        post_save.disconnect(create_default_folders, sender=Project)
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="preview-metadata@example.com",
            password="password",
            first_name="Preview",
            last_name="Metadata",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="preview-metadata-reviewer@example.com",
            password="password",
            first_name="Preview",
            last_name="Reviewer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Preview Metadata Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="preview-metadata@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.reviewer.company = self.company
        self.reviewer.save(update_fields=["company"])

        self.project = Project.objects.create(
            project_name="Metadata Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.contact_company = ContactCompany.objects.create(
            user=self.user,
            company_profile=self.company,
            company_name="Client Company GmbH",
            contact_email="client@example.com",
        )
        ProjectClient.objects.create(
            project=self.project,
            contact_company=self.contact_company,
        )
        self.project_folder = Folder.objects.create(
            project=self.project,
            name="Monthly Evidence",
        )
        self.purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-META-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=100,
            vat_rate=19,
            vat_amount=19,
            amount_incl_vat=119,
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        self.document = Document.objects.create(
            folder=self.project_folder,
            author=self.user,
            created_by=self.user,
            file="https://example.com/monthly-evidence.pdf",
            file_name="monthly-evidence.pdf",
            document_type="Evidence",
            status="Approved",
        )

    def _build_request(self, data=None, method="put"):
        request_factory = getattr(self.factory, method)
        request = request_factory(
            "/api/project_management/monthly-timesheets/activity-documents/",
            data or {},
            format="json",
        )
        force_authenticate(request, user=self.user)
        return request

    def _create_project_purchase_order_activity(self, week_start_date, *, is_invoiceable, entry_date, hours):
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start_date,
            week_end_date=week_start_date + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=self.purchase_order,
            activity_code=str(self.purchase_order.id),
            activity_name="Metadata Project / PO-META-001",
            is_invoiceable=is_invoiceable,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=entry_date,
            hours=hours,
        )
        return activity

    def test_monthly_preview_builder_includes_project_purchase_order_filter_metadata(self):
        self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 4).date(),
            is_invoiceable=True,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=4,
        )
        self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 11).date(),
            is_invoiceable=True,
            entry_date=timezone.datetime(2026, 5, 12).date(),
            hours=6,
        )
        self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 18).date(),
            is_invoiceable=False,
            entry_date=timezone.datetime(2026, 5, 19).date(),
            hours=2,
        )

        preview = _build_monthly_timesheet_preview(self.user, 5, 2026)
        activity_rows = [
            week["activities"][0]
            for week in preview["weeks"]
        ]

        self.assertEqual(
            activity_rows[0]["client"],
            {
                "id": self.contact_company.id,
                "key": f"company:{self.contact_company.id}",
                "type": "company",
                "name": "Client Company GmbH",
            },
        )
        self.assertEqual(
            activity_rows[0]["project"],
            {
                "id": self.project.id,
                "name": "Metadata Project",
            },
        )
        self.assertEqual(
            activity_rows[0]["purchase_order"],
            {
                "id": self.purchase_order.id,
                "reference": "PO-META-001",
                "po_type": PurchaseOrder.POType.OUTGOING,
                "document_type": PurchaseOrder.DocumentType.PURCHASE_ORDER,
                "status": "Received",
            },
        )
        self.assertEqual(
            activity_rows[0]["row_signature"],
            activity_rows[1]["row_signature"],
        )
        self.assertNotEqual(
            activity_rows[0]["row_signature"],
            activity_rows[2]["row_signature"],
        )
        self.assertIsNone(activity_rows[0]["activity_subtitle"])

    def test_monthly_preview_builder_includes_project_purchase_order_subtitle_context(self):
        week_start = timezone.datetime(2026, 5, 4).date()
        milestone = Milestone.objects.create(
            project=self.project,
            name="Execution Phase",
            due_date=timezone.now().date(),
        )
        action_item = ActionItem.objects.create(
            milestone=milestone,
            assignee=self.user,
            created_by=self.user,
            name="Finalize packing",
            target_date=timezone.now().date(),
            status="open",
        )
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=self.purchase_order,
            project_milestone=milestone,
            project_action_item=action_item,
            activity_code=str(self.purchase_order.id),
            activity_name="Metadata Project / PO-META-001",
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=4,
        )

        preview = _build_monthly_timesheet_preview(self.user, 5, 2026)
        activity_row = preview["weeks"][0]["activities"][0]

        self.assertEqual(
            activity_row["activity_subtitle"],
            "Phase: Execution Phase · Action Item: Finalize packing",
        )

    def test_submission_preview_response_includes_monthly_activity_metadata(self):
        self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 4).date(),
            is_invoiceable=True,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=4,
        )
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=5,
            year=2026,
            status="pending",
            submitted_at=timezone.now(),
        )

        payload = MonthlyTimesheetViewSet()._build_submission_preview_response(submission)
        activity = payload["weeks"][0]["activities"][0]

        self.assertEqual(activity["project"]["id"], self.project.id)
        self.assertEqual(activity["purchase_order"]["id"], self.purchase_order.id)
        self.assertEqual(activity["client"]["key"], f"company:{self.contact_company.id}")
        self.assertIn("is_invoiceable:1", activity["row_signature"])
        self.assertIn("activity_subtitle", activity)

    def test_monthly_preview_builder_includes_linked_documents_for_matching_row_signature(self):
        self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 4).date(),
            is_invoiceable=True,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=4,
        )
        invoiceable_activity = self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 11).date(),
            is_invoiceable=True,
            entry_date=timezone.datetime(2026, 5, 12).date(),
            hours=6,
        )
        self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 18).date(),
            is_invoiceable=False,
            entry_date=timezone.datetime(2026, 5, 19).date(),
            hours=2,
        )

        invoiceable_signature = (
            "source_type:project_purchase_order"
            f"|company_action_item:0|company_calendar_event:0|project:{self.project.id}"
            f"|purchase_order:{self.purchase_order.id}|project_milestone:0"
            "|project_action_item:0|is_invoiceable:1"
        )

        with patch.object(
            MonthlyTimesheetViewSet,
            "_user_can_manage_own_timesheets",
            return_value=True,
        ):
            response = MonthlyTimesheetViewSet.as_view(
                {"put": "activity_documents"}
            )(
                self._build_request(
                    data={
                        "month": 5,
                        "year": 2026,
                        "row_signature": invoiceable_signature,
                        "document_ids": [self.document.id],
                    },
                    method="put",
                )
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        preview = _build_monthly_timesheet_preview(self.user, 5, 2026)
        activity_rows = [
            week["activities"][0]
            for week in preview["weeks"]
        ]

        self.assertEqual(
            [len(row["linked_documents"]) for row in activity_rows],
            [1, 1, 0],
        )
        self.assertEqual(
            activity_rows[0]["linked_documents"][0]["id"],
            self.document.id,
        )

    def test_activity_documents_action_rejects_document_from_other_scope(self):
        self._create_project_purchase_order_activity(
            timezone.datetime(2026, 5, 4).date(),
            is_invoiceable=True,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=4,
        )
        company_folder = Folder.objects.create(
            company_profile=self.company,
            name="Company Evidence",
        )
        company_document = Document.objects.create(
            folder=company_folder,
            author=self.user,
            created_by=self.user,
            file="https://example.com/company-evidence.pdf",
            file_name="company-evidence.pdf",
            document_type="Evidence",
            status="Approved",
        )
        row_signature = (
            "source_type:project_purchase_order"
            f"|company_action_item:0|company_calendar_event:0|project:{self.project.id}"
            f"|purchase_order:{self.purchase_order.id}|project_milestone:0"
            "|project_action_item:0|is_invoiceable:1"
        )
        view = MonthlyTimesheetViewSet.as_view({"put": "activity_documents"})
        request = self._build_request(
            data={
                "month": 5,
                "year": 2026,
                "row_signature": row_signature,
                "document_ids": [company_document.id],
            },
            method="put",
        )

        with patch.object(
            MonthlyTimesheetViewSet,
            "_user_can_manage_own_timesheets",
            return_value=True,
        ):
            response = view(request)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_ids", response.data)


class MonthlyTimesheetStrictMonthPreviewTests(TestCase):
    def setUp(self):
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )

        self.user = User.objects.create_user(
            email="strict-month-preview@example.com",
            password="password",
            first_name="Strict",
            last_name="Preview",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="strict-month-preview-reviewer@example.com",
            password="password",
            first_name="Strict",
            last_name="Reviewer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Strict Month Preview Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="strict-month-preview@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.reviewer.company = self.company
        self.reviewer.save(update_fields=["company"])

        self.action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.reviewer,
            name="Strict Month Task",
            target_date=timezone.now().date(),
            status="open",
        )

    def test_preview_includes_first_overlapping_week_and_counts_only_month_dates(self):
        first_week_start = timezone.datetime(2026, 2, 23).date()
        last_week_start = timezone.datetime(2026, 3, 30).date()

        first_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=first_week_start,
            week_end_date=first_week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.action_item,
            activity_code=str(self.action_item.id),
            activity_name=self.action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=first_activity,
            entry_date=timezone.datetime(2026, 2, 27).date(),
            hours=4,
        )
        TimesheetEntry.objects.create(
            activity=first_activity,
            entry_date=timezone.datetime(2026, 3, 1).date(),
            hours=5,
        )

        last_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=last_week_start,
            week_end_date=last_week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.action_item,
            activity_code=str(self.action_item.id),
            activity_name=self.action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=last_activity,
            entry_date=timezone.datetime(2026, 3, 31).date(),
            hours=7,
        )
        TimesheetEntry.objects.create(
            activity=last_activity,
            entry_date=timezone.datetime(2026, 4, 1).date(),
            hours=8,
        )

        preview = _build_monthly_timesheet_preview(self.user, 3, 2026)

        self.assertEqual(len(preview["weeks"]), 2)
        self.assertEqual(preview["weeks"][0]["week_start_date"], first_week_start)
        self.assertEqual(preview["weeks"][1]["week_start_date"], last_week_start)
        self.assertEqual(preview["month_total_hours"], 12.0)
        self.assertEqual(preview["weeks"][0]["week_total_hours"], 5.0)
        self.assertEqual(preview["weeks"][1]["week_total_hours"], 7.0)

    def test_preview_marks_out_of_month_days_and_excludes_out_of_month_entries(self):
        week_start = timezone.datetime(2026, 3, 30).date()
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.action_item,
            activity_code=str(self.action_item.id),
            activity_name=self.action_item.name,
        )
        march_entry = TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 3, 30).date(),
            hours=3,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 4, 1).date(),
            hours=9,
        )

        preview = _build_monthly_timesheet_preview(self.user, 3, 2026)
        week = preview["weeks"][0]
        activity_row = week["activities"][0]

        self.assertEqual(
            [day["is_in_month"] for day in week["days"]],
            [True, True, False, False, False, False, False],
        )
        self.assertEqual(
            [item["date"] for item in week["daily_totals"]],
            [
                timezone.datetime(2026, 3, 30).date(),
                timezone.datetime(2026, 3, 31).date(),
            ],
        )
        self.assertEqual(activity_row["row_total_hours"], 3.0)
        self.assertEqual(
            activity_row["entries"],
            [
                {
                    "id": march_entry.id,
                    "entry_date": timezone.datetime(2026, 3, 30).date(),
                    "hours": 3.0,
                }
            ],
        )

    def test_preview_includes_weekday_warning_metadata_for_in_month_days_only(self):
        Staff.objects.create(
            user=self.user,
            salary=1000,
            weekly_hours=40,
        )
        week_start = timezone.datetime(2026, 3, 30).date()
        activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.action_item,
            activity_code=str(self.action_item.id),
            activity_name=self.action_item.name,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 3, 30).date(),
            hours=6,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 3, 31).date(),
            hours=9,
        )
        TimesheetEntry.objects.create(
            activity=activity,
            entry_date=timezone.datetime(2026, 4, 1).date(),
            hours=20,
        )

        preview = _build_monthly_timesheet_preview(self.user, 3, 2026)
        week = preview["weeks"][0]

        self.assertEqual(preview["expected_daily_hours"], 8.0)
        self.assertEqual(preview["under_allocated_days_count"], 1)
        self.assertEqual(preview["over_allocated_days_count"], 1)
        self.assertEqual(
            week["daily_warning_map"],
            {
                "2026-03-30": "under",
                "2026-03-31": "over",
            },
        )


class MonthlyTimesheetBillingIntegrationTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(milestone_post_save_operations, sender=Milestone)
        post_save.disconnect(notify_on_milestone_save, sender=Milestone)
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            milestone_post_save_operations,
            sender=Milestone,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_milestone_save,
            sender=Milestone,
        )
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="billing-timesheet@example.com",
            password="password",
            first_name="Billing",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Acme Billing Timesheet",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="billing-timesheet@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        purchase_order_role = Role.objects.create(
            name="Billing Purchase Order Viewer",
            company=self.company,
            type="solution",
            scope="company",
            is_default=False,
            is_active=True,
            created_by=self.user,
        )
        purchase_order_permission, _ = Permission.objects.get_or_create(
            key="purchase_order.view",
            defaults={
                "name": "View Purchase Orders",
                "module": "purchase_order",
            },
        )
        RolePermission.objects.create(
            role=purchase_order_role,
            permission=purchase_order_permission,
        )
        UserRole.objects.create(
            user=self.user,
            role=purchase_order_role,
            company=self.company,
            assigned_by=self.user,
        )
        self.staff = Staff.objects.create(
            user=self.user,
            salary=1000,
            weekly_hours=40,
            hourly_rate=70,
        )
        CompanyConfig.objects.create(
            solution="Energy Consultant Solution",
            company_profile=self.company,
            invoice_status=["open", "due", "overdue", "paid"],
        )
        self.recipient = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Client Co",
        )
        self.project = Project.objects.create(
            project_name="Billable Project",
            contract_value=5000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.project_milestone = Milestone.objects.create(
            project=self.project,
            name="Billing Phase",
            due_date=timezone.now().date(),
        )
        self.submission = MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=5,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.user,
        )
        self.company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Internal Admin",
            target_date=timezone.now().date(),
            status="open",
        )
        self.company_time_card = TimeCard.objects.create(
            company_profile=self.company,
            action_item=self.company_action_item,
            name="Internal Admin",
            date=timezone.datetime(2026, 5, 5).date(),
            hours=5,
            status="approved",
            approved_by=self.user,
            created_by=self.user,
            timesheet_submission=self.submission,
            is_generated_from_timesheet=True,
        )
        self.company_timesheet_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=self.company_action_item,
            activity_code=str(self.company_action_item.id),
            activity_name="Internal Admin",
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=self.company_timesheet_activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=5,
        )
        self.company_row_signature = (
            "source_type:company_action_item"
            f"|company_action_item:{self.company_action_item.id}"
            "|company_calendar_event:0|project:0|purchase_order:0"
            "|project_milestone:0|project_action_item:0|is_invoiceable:1"
        )
        self.project_time_card = TimeCard.objects.create(
            project=self.project,
            milestone=self.project_milestone,
            name="Project Delivery",
            date=timezone.datetime(2026, 5, 6).date(),
            hours=3,
            status="approved",
            approved_by=self.user,
            created_by=self.user,
            timesheet_submission=self.submission,
            is_generated_from_timesheet=True,
        )
        self.colleague = User.objects.create_user(
            email="billing-colleague@example.com",
            password="password",
            first_name="Billing",
            last_name="Collaborator",
            user_type="company_user",
            company=self.company,
        )
        self.outsider = User.objects.create_user(
            email="billing-outsider@example.com",
            password="password",
            first_name="Outside",
            last_name="User",
            user_type="company_user",
        )
        self.outsider_company = CompanyProfile.objects.create(
            admin_user=self.outsider,
            company_name="Outside Co",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="987",
            company_mob_no="654",
            company_web_address="https://outside.example.com",
            company_email_address="outside@example.com",
            company_tax_reference="tax-2",
            company_iban="iban-2",
        )
        self.outsider.company = self.outsider_company
        self.outsider.save(update_fields=["company"])
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="member",
            team_member=self.user,
        )
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="member",
            team_member=self.colleague,
        )
        self.project_folder = Folder.objects.create(
            project=self.project,
            name="Invoice Support",
        )
        self.supporting_document = Document.objects.create(
            folder=self.project_folder,
            author=self.user,
            created_by=self.user,
            file="https://example.com/invoice-support.pdf",
            file_name="invoice-support.pdf",
            document_type="Evidence",
            status="Approved",
        )
        self.project_timesheet_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="project_phase",
            project=self.project,
            project_milestone=self.project_milestone,
            activity_code=str(self.project_milestone.id),
            activity_name="Billable Project / Billing Phase",
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=self.project_timesheet_activity,
            entry_date=timezone.datetime(2026, 5, 6).date(),
            hours=3,
        )
        self.project_row_signature = (
            "source_type:project_phase"
            "|company_action_item:0|company_calendar_event:0"
            f"|project:{self.project.id}|purchase_order:0"
            f"|project_milestone:{self.project_milestone.id}"
            "|project_action_item:0|is_invoiceable:1"
        )
        MonthlyTimesheetActivityDocumentLink.objects.create(
            user=self.user,
            month=5,
            year=2026,
            row_signature=self.project_row_signature,
            source_type="project_phase",
            project=self.project,
            project_milestone=self.project_milestone,
            document=self.supporting_document,
            is_invoiceable=True,
        )

    def _build_request(self, path, method="get", data=None):
        request_factory = getattr(self.factory, method.lower())
        request = request_factory(path, data=data or {}, format="json")
        force_authenticate(request, user=self.user)
        request.user = self.user
        request.query_params = request.GET
        return request

    def _create_invoice_pdf_document(self, invoice, file_name="invoice.pdf"):
        root_folder, _ = Folder.objects.get_or_create(
            company_profile=self.company,
            parent=None,
            name=f"{self.company.id}_company_root_folder",
        )
        invoices_folder, _ = Folder.objects.get_or_create(
            company_profile=self.company,
            parent=root_folder,
            name="invoices",
        )
        document = Document(
            folder=invoices_folder,
            document_type="Customer Invoice",
            file_name=file_name,
            author=self.user,
            status="Draft",
        )
        document.file.save(
            file_name,
            SimpleUploadedFile(file_name, b"%PDF-1.4 test invoice pdf"),
        )
        document.save()
        return document

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_approved_timesheet_summaries_exclude_only_invoiced_scope(
        self,
        mock_create_pdf,
    ):
        company_view = InvoiceViewSet()
        company_request = self._build_request(
            f"/api/project_management/billing-invoice/approved-timesheet-summaries/?company_profile={self.company.id}",
        )
        company_view.request = company_request
        company_response = company_view.approved_timesheet_summaries(company_request)

        self.assertEqual(company_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(company_response.data), 1)
        self.assertEqual(company_response.data[0]["submission_id"], self.submission.id)
        self.assertEqual(company_response.data[0]["total_hours"], 8.0)

        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-05-31",
                "due_date": "2026-05-31",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={"request": self._build_request("/api/project_management/billing-invoice/", method="post")},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        company_view.request = company_request
        company_response = company_view.approved_timesheet_summaries(company_request)
        self.assertEqual(company_response.status_code, status.HTTP_200_OK)
        self.assertEqual(company_response.data, [])

        project_view = InvoiceViewSet()
        project_request = self._build_request(
            f"/api/project_management/billing-invoice/approved-timesheet-summaries/?project={self.project.id}",
        )
        project_view.request = project_request
        project_response = project_view.approved_timesheet_summaries(project_request)

        self.assertEqual(project_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(project_response.data), 1)
        self.assertEqual(project_response.data[0]["submission_id"], self.submission.id)
        self.assertEqual(project_response.data[0]["total_hours"], 3.0)
        self.assertEqual(
            [document["id"] for document in project_response.data[0]["linked_documents"]],
            [self.supporting_document.id],
        )

    def test_approved_timesheet_summaries_filter_by_purchase_order_and_collaborator(
        self,
    ):
        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-SCOPED-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        other_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-SCOPED-002",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        po_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=purchase_order,
            activity_code=str(purchase_order.id),
            activity_name=purchase_order.reference,
            is_invoiceable=True,
        )
        other_po_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=other_purchase_order,
            activity_code=str(other_purchase_order.id),
            activity_name=other_purchase_order.reference,
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=po_activity,
            entry_date=timezone.datetime(2026, 5, 7).date(),
            hours=2,
        )
        TimesheetEntry.objects.create(
            activity=other_po_activity,
            entry_date=timezone.datetime(2026, 5, 8).date(),
            hours=4,
        )
        po_document = Document.objects.create(
            folder=self.project_folder,
            author=self.user,
            created_by=self.user,
            file="https://example.com/po-support.pdf",
            file_name="po-support.pdf",
            document_type="Evidence",
            status="Approved",
        )
        MonthlyTimesheetActivityDocumentLink.objects.create(
            user=self.user,
            month=5,
            year=2026,
            row_signature=(
                "source_type:project_purchase_order"
                "|company_action_item:0|company_calendar_event:0"
                f"|project:{self.project.id}|purchase_order:{purchase_order.id}"
                "|project_milestone:0|project_action_item:0|is_invoiceable:1"
            ),
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=purchase_order,
            document=po_document,
            is_invoiceable=True,
        )

        view = InvoiceViewSet()
        request = self._build_request(
            (
                "/api/project_management/billing-invoice/"
                "approved-timesheet-summaries/"
                f"?company_profile={self.company.id}"
                f"&purchase_order={purchase_order.id}"
                f"&collaborators={self.user.id}"
            )
        )
        view.request = request
        response = view.approved_timesheet_summaries(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["submission_id"], self.submission.id)
        self.assertEqual(response.data[0]["total_hours"], 2.0)
        self.assertEqual(response.data[0]["invoiceable_hours"], 2.0)
        self.assertIn("PO-SCOPED-001", response.data[0]["description"])
        self.assertEqual(
            [document["id"] for document in response.data[0]["linked_documents"]],
            [po_document.id],
        )

    @patch("project_management.utils.create_monthly_timesheet_pdf_bytes")
    def test_export_package_generates_purchase_order_scoped_timesheet_pdf(
        self,
        mock_create_monthly_timesheet_pdf_bytes,
    ):
        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-PDF-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        other_purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-PDF-002",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        po_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=purchase_order,
            activity_code=str(purchase_order.id),
            activity_name=purchase_order.reference,
            is_invoiceable=True,
        )
        other_po_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 5, 4).date(),
            week_end_date=timezone.datetime(2026, 5, 10).date(),
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=other_purchase_order,
            activity_code=str(other_purchase_order.id),
            activity_name=other_purchase_order.reference,
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=po_activity,
            entry_date=timezone.datetime(2026, 5, 7).date(),
            hours=2,
        )
        TimesheetEntry.objects.create(
            activity=other_po_activity,
            entry_date=timezone.datetime(2026, 5, 8).date(),
            hours=4,
        )
        invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=140,
            vat_rate=0,
            purchase_order=purchase_order,
        )
        invoice.collaborators.set([self.user])
        InvoiceLineItem.objects.create(
            invoice=invoice,
            monthly_timesheet_submission=self.submission,
            price=70,
            quantity=2,
            vat_amount=0,
        )
        invoice_pdf_document = self._create_invoice_pdf_document(
            invoice,
            file_name="invoice-po-pdf.pdf",
        )
        mock_create_monthly_timesheet_pdf_bytes.return_value = b"%PDF-1.4 po monthly"

        with patch(
            "project_management.utils.create_pdf_invoice_by_bill",
            return_value=(invoice_pdf_document, "updated"),
        ):
            package_document, action = create_invoice_export_package(
                invoice,
                include_mode=INVOICE_EXPORT_INCLUDE_MODE_WITH_TIMESHEETS,
                request_user=self.user,
            )

        self.assertEqual(action, "created")
        self.assertTrue(package_document.file.name.endswith(".zip"))
        mock_create_monthly_timesheet_pdf_bytes.assert_called_once()
        filtered_preview = mock_create_monthly_timesheet_pdf_bytes.call_args.args[1]
        self.assertEqual(filtered_preview["month_total_hours"], 2.0)
        self.assertEqual(
            filtered_preview["submission"]["status"],
            self.submission.status,
        )
        exported_purchase_order_ids = {
            activity["purchase_order"]["id"]
            for week in filtered_preview["weeks"]
            for activity in week["activities"]
        }
        self.assertEqual(exported_purchase_order_ids, {purchase_order.id})

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_invoice_rejects_purchase_order_timesheet_without_po_time(
        self,
        _mock_create_pdf,
    ):
        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-VALIDATION-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )

        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-05-31",
                "due_date": "2026-05-31",
                "status": "open",
                "grand_total": 210,
                "vat_rate": 0,
                "purchase_order": purchase_order.id,
                "collaborators": [self.user.id],
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 3,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(ValidationError):
            serializer.save()

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_invoice_allows_multiple_purchase_order_timesheet_lines(
        self,
        _mock_create_pdf,
    ):
        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-MULTI-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        colleague_submission = MonthlyTimesheetSubmission.objects.create(
            user=self.colleague,
            month=5,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.user,
        )

        for activity_user, hours in [(self.user, 2), (self.colleague, 4)]:
            po_activity = TimesheetActivity.objects.create(
                user=activity_user,
                week_start_date=timezone.datetime(2026, 5, 4).date(),
                week_end_date=timezone.datetime(2026, 5, 10).date(),
                created_by=self.user,
                source_type="project_purchase_order",
                project=self.project,
                purchase_order=purchase_order,
                activity_code=str(purchase_order.id),
                activity_name=purchase_order.reference,
                is_invoiceable=True,
            )
            TimesheetEntry.objects.create(
                activity=po_activity,
                entry_date=timezone.datetime(2026, 5, 6).date(),
                hours=hours,
            )

        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-05-31",
                "due_date": "2026-05-31",
                "status": "open",
                "grand_total": 420,
                "vat_rate": 0,
                "purchase_order": purchase_order.id,
                "collaborators": [self.user.id, self.colleague.id],
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 99,
                        "vat_amount": 0,
                    },
                    {
                        "monthly_timesheet_submission": colleague_submission.id,
                        "price": 70,
                        "quantity": 99,
                        "vat_amount": 0,
                    },
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        invoice = serializer.save()
        self.assertEqual(invoice.line_items.count(), 2)
        self.assertEqual(
            set(
                invoice.line_items.values_list(
                    "monthly_timesheet_submission_id",
                    flat=True,
                )
            ),
            {self.submission.id, colleague_submission.id},
        )
        quantities_by_submission_id = {
            line_item.monthly_timesheet_submission_id: line_item.quantity
            for line_item in invoice.line_items.all()
        }
        self.assertEqual(quantities_by_submission_id[self.submission.id], 2)
        self.assertEqual(quantities_by_submission_id[colleague_submission.id], 4)

    def test_invoice_pdf_renders_purchase_order_scoped_timesheet_lines(
        self,
    ):
        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-PDF-LINES-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        colleague_submission = MonthlyTimesheetSubmission.objects.create(
            user=self.colleague,
            month=6,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.user,
        )

        for activity_user, week_start_date, entry_date, hours in [
            (
                self.user,
                timezone.datetime(2026, 5, 4).date(),
                timezone.datetime(2026, 5, 6).date(),
                2,
            ),
            (
                self.colleague,
                timezone.datetime(2026, 6, 1).date(),
                timezone.datetime(2026, 6, 2).date(),
                4,
            ),
        ]:
            po_activity = TimesheetActivity.objects.create(
                user=activity_user,
                week_start_date=week_start_date,
                week_end_date=week_start_date + timezone.timedelta(days=6),
                created_by=self.user,
                source_type="project_purchase_order",
                project=self.project,
                purchase_order=purchase_order,
                activity_code=str(purchase_order.id),
                activity_name=purchase_order.reference,
                is_invoiceable=True,
            )
            TimesheetEntry.objects.create(
                activity=po_activity,
                entry_date=entry_date,
                hours=hours,
            )

        invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.datetime(2026, 6, 30).date(),
            due_date=timezone.datetime(2026, 6, 30).date(),
            status="open",
            grand_total=999,
            vat_rate=0,
            purchase_order=purchase_order,
        )
        invoice.collaborators.set([self.user, self.colleague])
        root_folder = Folder.objects.create(
            company_profile=self.company,
            name=f"{self.company.id}_company_root_folder",
        )
        Folder.objects.create(
            company_profile=self.company,
            parent=root_folder,
            name="invoices",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            monthly_timesheet_submission=self.submission,
            price=70,
            quantity=99,
            vat_amount=0,
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            monthly_timesheet_submission=colleague_submission,
            price=70,
            quantity=99,
            vat_amount=0,
        )

        with patch(
            "project_management.utils.render_to_string",
            return_value="<html><body>invoice</body></html>",
        ) as mock_render:
            create_pdf_invoice_by_bill(invoice, self.user)

        context = mock_render.call_args.args[1]
        self.assertEqual(
            set(context["collaborator_names"]),
            {"Billing Owner", "Billing Collaborator"},
        )
        self.assertEqual(
            [item["quantity"] for item in context["line_items"]],
            [2, 4],
        )
        self.assertEqual(
            [item["total"] for item in context["line_items"]],
            ["€140.00", "€280.00"],
        )
        self.assertEqual(context["subtotal"], "€420.00")
        self.assertEqual(context["grand_total"], "€420.00")
        descriptions = [item["description"] for item in context["line_items"]]
        self.assertTrue(all("PO-PDF-LINES-001" in value for value in descriptions))
        self.assertTrue(any("05/2026" in value for value in descriptions))
        self.assertTrue(any("06/2026" in value for value in descriptions))

    def test_invoice_pdf_line_vat_matches_invoice_vat_rate_not_stale_amount(
        self,
    ):
        """Line VAT in parentheses must follow invoice VAT %, not a stale stored amount.

        Regression: default VAT is 12%, so qty*price*0.12 can remain on the line
        after the invoice VAT rate is changed to 10%. Totals used grand_total
        (correct) while the line showed the old 12% amount.
        """
        root_folder = Folder.objects.create(
            company_profile=self.company,
            name=f"{self.company.id}_company_root_folder_vat",
        )
        Folder.objects.create(
            company_profile=self.company,
            parent=root_folder,
            name="invoices",
        )
        invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.datetime(2026, 7, 27).date(),
            due_date=timezone.datetime(2026, 8, 27).date(),
            status="open",
            grand_total=1100,
            vat_rate=10,
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            description="Equipment purchase",
            price=500,
            quantity=2,
            # Stale amount from previous 12% rate (1000 * 0.12)
            vat_amount=120,
        )

        with patch(
            "project_management.utils.render_to_string",
            return_value="<html><body>invoice</body></html>",
        ) as mock_render:
            create_pdf_invoice_by_bill(invoice, self.user)

        context = mock_render.call_args.args[1]
        self.assertEqual(context["line_items"][0]["vat_rate"], 10)
        self.assertEqual(context["line_items"][0]["vat_amount"], "€100.00")
        self.assertEqual(context["vat_rate"], 10)
        self.assertEqual(context["vat_amount"], "€100.00")
        self.assertEqual(context["subtotal"], "€1,000.00")
        self.assertEqual(context["grand_total"], "€1,100.00")

    def test_activity_documents_action_updates_project_phase_rows_without_purchase_order(self):
        second_supporting_document = Document.objects.create(
            folder=self.project_folder,
            author=self.user,
            created_by=self.user,
            file="https://example.com/project-support-2.pdf",
            file_name="project-support-2.pdf",
            document_type="Evidence",
            status="Approved",
        )
        view = MonthlyTimesheetViewSet.as_view({"put": "activity_documents"})
        request = self._build_request(
            "/api/project_management/monthly-timesheets/activity-documents/",
            method="put",
            data={
                "month": 5,
                "year": 2026,
                "row_signature": self.project_row_signature,
                "document_ids": [self.supporting_document.id, second_supporting_document.id],
            },
        )

        with patch.object(
            MonthlyTimesheetViewSet,
            "_user_can_manage_own_timesheets",
            return_value=True,
        ):
            response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            sorted(item["id"] for item in response.data["linked_documents"]),
            sorted([self.supporting_document.id, second_supporting_document.id]),
        )

    def test_activity_documents_action_updates_company_action_item_rows_without_project(self):
        company_folder = Folder.objects.create(
            company_profile=self.company,
            name="Company Activity Evidence",
        )
        company_document = Document.objects.create(
            folder=company_folder,
            author=self.user,
            created_by=self.user,
            file="https://example.com/company-activity-evidence.pdf",
            file_name="company-activity-evidence.pdf",
            document_type="Evidence",
            status="Approved",
        )
        view = MonthlyTimesheetViewSet.as_view({"put": "activity_documents"})
        request = self._build_request(
            "/api/project_management/monthly-timesheets/activity-documents/",
            method="put",
            data={
                "month": 5,
                "year": 2026,
                "row_signature": self.company_row_signature,
                "document_ids": [company_document.id],
            },
        )

        with patch.object(
            MonthlyTimesheetViewSet,
            "_user_can_manage_own_timesheets",
            return_value=True,
        ):
            response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["linked_documents"]],
            [company_document.id],
        )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_invoice_create_auto_links_monthly_submission_documents_and_collaborators(
        self,
        mock_create_pdf,
    ):
        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-05-31",
                "due_date": "2026-05-31",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "purchase_order": None,
                "collaborators": [],
                "documents": [],
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        invoice = serializer.save()
        self.assertEqual(
            list(invoice.documents.values_list("id", flat=True)),
            [self.supporting_document.id],
        )

        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-INV-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        po_submission = MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=6,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.user,
        )
        po_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=timezone.datetime(2026, 6, 1).date(),
            week_end_date=timezone.datetime(2026, 6, 7).date(),
            created_by=self.user,
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=purchase_order,
            activity_code=str(purchase_order.id),
            activity_name=purchase_order.reference,
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=po_activity,
            entry_date=timezone.datetime(2026, 6, 2).date(),
            hours=5,
        )
        MonthlyTimesheetActivityDocumentLink.objects.create(
            user=self.user,
            month=6,
            year=2026,
            row_signature=(
                "source_type:project_purchase_order"
                "|company_action_item:0|company_calendar_event:0"
                f"|project:{self.project.id}|purchase_order:{purchase_order.id}"
                "|project_milestone:0|project_action_item:0|is_invoiceable:1"
            ),
            source_type="project_purchase_order",
            project=self.project,
            purchase_order=purchase_order,
            document=self.supporting_document,
            is_invoiceable=True,
        )

        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-06-30",
                "due_date": "2026-06-30",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "purchase_order": purchase_order.id,
                "collaborators": [self.colleague.id],
                "documents": [],
                "line_items": [
                    {
                        "monthly_timesheet_submission": po_submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        invoice = serializer.save()
        self.assertEqual(
            list(invoice.collaborators.values_list("id", flat=True)),
            [self.colleague.id],
        )
        self.assertEqual(
            list(invoice.documents.values_list("id", flat=True)),
            [self.supporting_document.id],
        )
        self.assertEqual(
            list(invoice.excluded_auto_documents.values_list("id", flat=True)),
            [],
        )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_invoice_update_can_exclude_auto_linked_timesheet_document(
        self,
        _mock_create_pdf,
    ):
        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-05-31",
                "due_date": "2026-05-31",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "documents": [],
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        invoice = serializer.save()
        line_item = invoice.line_items.get()

        update_serializer = InvoiceSerializer(
            invoice,
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-05-31",
                "due_date": "2026-05-31",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "documents": [],
                "line_items": [
                    {
                        "id": line_item.id,
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    f"/api/project_management/billing-invoice/{invoice.id}/",
                    method="patch",
                )
            },
            partial=True,
        )

        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        invoice = update_serializer.save()
        self.assertEqual(list(invoice.documents.values_list("id", flat=True)), [])
        self.assertEqual(
            list(invoice.excluded_auto_documents.values_list("id", flat=True)),
            [self.supporting_document.id],
        )

        invoice_data = InvoiceSerializer(invoice).data
        self.assertEqual(invoice_data.get("documents", []), [])

        second_update_serializer = InvoiceSerializer(
            invoice,
            data={"notes": "Keep excluded"},
            context={
                "request": self._build_request(
                    f"/api/project_management/billing-invoice/{invoice.id}/",
                    method="patch",
                )
            },
            partial=True,
        )
        self.assertTrue(
            second_update_serializer.is_valid(),
            second_update_serializer.errors,
        )
        invoice = second_update_serializer.save()
        self.assertEqual(list(invoice.documents.values_list("id", flat=True)), [])
        self.assertEqual(
            list(invoice.excluded_auto_documents.values_list("id", flat=True)),
            [self.supporting_document.id],
        )

    def test_invoice_serializer_rejects_invalid_collaborator_without_purchase_order(self):
        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-06-30",
                "due_date": "2026-06-30",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "collaborators": [self.outsider.id],
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("collaborators", serializer.errors)

    def test_invoice_serializer_rejects_unassociated_collaborator_even_with_purchase_order(self):
        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-INV-002",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-06-30",
                "due_date": "2026-06-30",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "purchase_order": purchase_order.id,
                "collaborators": [self.outsider.id],
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("collaborators", serializer.errors)

    def test_invoice_serializer_rejects_collaborator_not_on_purchase_order_project(self):
        purchase_order = PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference="PO-INV-TEAM-001",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Received",
            amount_excl_vat=1000,
            vat_rate=0,
            vat_amount=0,
            amount_incl_vat=1000,
            po_type=PurchaseOrder.POType.INCOMING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        non_team_colleague = User.objects.create_user(
            email="billing-non-team@example.com",
            password="password",
            first_name="Non",
            last_name="Team",
            user_type="company_user",
            company=self.company,
        )
        serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-06-30",
                "due_date": "2026-06-30",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "purchase_order": purchase_order.id,
                "collaborators": [non_team_colleague.id],
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={
                "request": self._build_request(
                    "/api/project_management/billing-invoice/",
                    method="post",
                )
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("collaborators", serializer.errors)

    @patch("project_management.views.billing.create_invoice_export_package")
    def test_export_package_action_returns_package_metadata(
        self,
        mock_create_invoice_export_package,
    ):
        invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=100,
            vat_rate=0,
        )
        package_document = self._create_invoice_pdf_document(
            invoice,
            file_name="invoice-package.zip",
        )
        mock_create_invoice_export_package.return_value = (package_document, "created")

        request = self._build_request(
            f"/api/project_management/billing-invoice/{invoice.id}/export-package/",
            method="post",
            data={"include_mode": INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY},
        )
        request.data = {"include_mode": INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY}
        request.user = self.user
        view = InvoiceViewSet()
        view.request = request
        view.kwargs = {"pk": invoice.id}
        view.action = "export_package"
        view.get_object = lambda: invoice
        response = view.export_package(request, pk=invoice.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["document_id"], package_document.id)
        self.assertEqual(response.data["include_mode"], INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY)
        self.assertTrue(response.data["file_url"])
        mock_create_invoice_export_package.assert_called_once_with(
            invoice,
            include_mode=INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
            request_user=self.user,
        )

    @patch("project_management.views.billing.create_invoice_export_package")
    @patch("project_management.permissions.InvoicePermission._check_permission", return_value=True)
    def test_export_package_action_allows_detail_post_for_viewable_invoice(
        self,
        _mock_check_permission,
        mock_create_invoice_export_package,
    ):
        invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=100,
            vat_rate=0,
        )
        package_document = self._create_invoice_pdf_document(
            invoice,
            file_name="invoice-package.zip",
        )
        mock_create_invoice_export_package.return_value = (package_document, "created")

        request = self._build_request(
            f"/api/project_management/billing-invoice/{invoice.id}/export-package/",
            method="post",
            data={"include_mode": INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY},
        )
        response = InvoiceViewSet.as_view({"post": "export_package"})(
            request,
            pk=invoice.id,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["document_id"], package_document.id)
        mock_create_invoice_export_package.assert_called_once_with(
            invoice,
            include_mode=INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
            request_user=self.user,
        )

    @patch("project_management.utils._read_document_bytes", return_value=b"document-bytes")
    @patch("project_management.utils.create_monthly_timesheet_pdf_bytes")
    def test_export_package_builds_zip_with_timesheets_and_deduped_supporting_documents(
        self,
        mock_create_monthly_timesheet_pdf_bytes,
        mock_read_document_bytes,
    ):
        invoice = Invoice.objects.create(
            project=self.project,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=210,
            vat_rate=0,
        )
        invoice_pdf_document = self._create_invoice_pdf_document(
            invoice,
            file_name="invoice-project.pdf",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            monthly_timesheet_submission=self.submission,
            price=70,
            quantity=3,
            vat_amount=0,
        )
        mock_create_monthly_timesheet_pdf_bytes.return_value = b"%PDF-1.4 monthly"

        with patch(
            "project_management.utils.create_pdf_invoice_by_bill",
            return_value=(invoice_pdf_document, "updated"),
        ):
            package_document, action = create_invoice_export_package(
                invoice,
                include_mode=INVOICE_EXPORT_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
                request_user=self.user,
            )

        self.assertEqual(action, "created")
        self.assertTrue(package_document.file.name.endswith(".zip"))
        with package_document.file.open("rb") as package_file:
            with zipfile.ZipFile(io.BytesIO(package_file.read()), "r") as archive:
                names = archive.namelist()

        self.assertEqual(len(names), 3)
        self.assertTrue(any(name.endswith(".pdf") for name in names))
        self.assertTrue(any(name.endswith(".pdf") and "monthly-timesheet" in name for name in names))
        self.assertTrue(
            any(
                "support" in name.lower() or name.startswith("document-")
                for name in names
            )
        )
        self.assertTrue(mock_read_document_bytes.called)

    @patch("project_management.utils._read_document_bytes", return_value=b"document-bytes")
    @patch("project_management.utils.create_monthly_timesheet_pdf_bytes")
    def test_export_package_includes_manual_invoice_documents_with_supporting_documents(
        self,
        mock_create_monthly_timesheet_pdf_bytes,
        mock_read_document_bytes,
    ):
        invoice = Invoice.objects.create(
            project=self.project,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=210,
            vat_rate=0,
        )
        invoice_pdf_document = self._create_invoice_pdf_document(
            invoice,
            file_name="invoice-project.pdf",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            monthly_timesheet_submission=self.submission,
            price=70,
            quantity=3,
            vat_amount=0,
        )
        company_folder = Folder.objects.create(
            company_profile=self.company,
            name="Manual Invoice Docs",
        )
        manual_invoice_document = Document.objects.create(
            folder=company_folder,
            author=self.user,
            created_by=self.user,
            file="https://example.com/manual-invoice-doc.pdf",
            file_name="manual-invoice-doc.pdf",
            document_type="Evidence",
            status="Approved",
        )
        invoice.documents.set([manual_invoice_document])
        mock_create_monthly_timesheet_pdf_bytes.return_value = b"%PDF-1.4 monthly"

        with patch(
            "project_management.utils.create_pdf_invoice_by_bill",
            return_value=(invoice_pdf_document, "updated"),
        ):
            package_document, action = create_invoice_export_package(
                invoice,
                include_mode=INVOICE_EXPORT_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
                request_user=self.user,
            )

        self.assertEqual(action, "created")
        with package_document.file.open("rb") as package_file:
            with zipfile.ZipFile(io.BytesIO(package_file.read()), "r") as archive:
                names = archive.namelist()

        self.assertEqual(len(names), 4)
        self.assertTrue(any("manual-invoice-doc" in name for name in names))
        self.assertTrue(mock_read_document_bytes.called)

    @patch("project_management.utils._read_document_bytes", return_value=b"invoice-bytes")
    def test_export_package_invoice_only_contains_single_invoice_pdf(
        self,
        mock_read_document_bytes,
    ):
        invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=100,
            vat_rate=0,
        )
        invoice_pdf_document = self._create_invoice_pdf_document(
            invoice,
            file_name="invoice-only.pdf",
        )

        with patch(
            "project_management.utils.create_pdf_invoice_by_bill",
            return_value=(invoice_pdf_document, "updated"),
        ):
            package_document, action = create_invoice_export_package(
                invoice,
                include_mode=INVOICE_EXPORT_INCLUDE_MODE_INVOICE_ONLY,
                request_user=self.user,
            )

        self.assertEqual(action, "created")
        with package_document.file.open("rb") as package_file:
            with zipfile.ZipFile(io.BytesIO(package_file.read()), "r") as archive:
                names = archive.namelist()

        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".pdf"))
        self.assertTrue(mock_read_document_bytes.called)

    @patch("project_management.utils._read_document_bytes", return_value=b"document-bytes")
    @patch("project_management.utils.create_monthly_timesheet_pdf_bytes")
    def test_export_package_with_timesheets_omits_supporting_documents(
        self,
        mock_create_monthly_timesheet_pdf_bytes,
        mock_read_document_bytes,
    ):
        invoice = Invoice.objects.create(
            project=self.project,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=210,
            vat_rate=0,
        )
        invoice_pdf_document = self._create_invoice_pdf_document(
            invoice,
            file_name="invoice-with-timesheet.pdf",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            monthly_timesheet_submission=self.submission,
            price=70,
            quantity=3,
            vat_amount=0,
        )
        mock_create_monthly_timesheet_pdf_bytes.return_value = b"%PDF-1.4 monthly"

        with patch(
            "project_management.utils.create_pdf_invoice_by_bill",
            return_value=(invoice_pdf_document, "updated"),
        ):
            package_document, action = create_invoice_export_package(
                invoice,
                include_mode="invoice_with_timesheets",
                request_user=self.user,
            )

        self.assertEqual(action, "created")
        with package_document.file.open("rb") as package_file:
            with zipfile.ZipFile(io.BytesIO(package_file.read()), "r") as archive:
                names = archive.namelist()

        self.assertEqual(len(names), 2)
        self.assertEqual(sum(1 for name in names if name.endswith(".pdf")), 2)
        self.assertFalse(
            any("support" in name.lower() or name.startswith("document-") for name in names)
        )
        self.assertTrue(mock_read_document_bytes.called)

    def test_export_package_rejects_invalid_include_mode(self):
        invoice = Invoice.objects.create(
            company_profile=self.company,
            reciepient_company=self.recipient,
            created_by=self.user,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=100,
            vat_rate=0,
        )

        with self.assertRaises(ValueError):
            create_invoice_export_package(
                invoice,
                include_mode="not_a_valid_mode",
                request_user=self.user,
            )

    @patch("project_management.serializers.billing.create_pdf_invoice_by_bill")
    def test_monthly_submission_cannot_be_billed_twice_in_same_scope_but_can_in_other_scope(
        self,
        mock_create_pdf,
    ):
        first_serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-05-31",
                "due_date": "2026-05-31",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={"request": self._build_request("/api/project_management/billing-invoice/", method="post")},
        )

        self.assertTrue(first_serializer.is_valid(), first_serializer.errors)
        first_serializer.save()

        duplicate_scope_serializer = InvoiceSerializer(
            data={
                "company_profile": self.company.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-06-01",
                "due_date": "2026-06-01",
                "status": "open",
                "grand_total": 350,
                "vat_rate": 0,
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 5,
                        "vat_amount": 0,
                    }
                ],
            },
            context={"request": self._build_request("/api/project_management/billing-invoice/", method="post")},
        )

        self.assertTrue(duplicate_scope_serializer.is_valid(), duplicate_scope_serializer.errors)
        with self.assertRaises(ValidationError):
            duplicate_scope_serializer.save()

        other_scope_serializer = InvoiceSerializer(
            data={
                "project": self.project.id,
                "reciepient_company": self.recipient.id,
                "invoice_date": "2026-06-01",
                "due_date": "2026-06-01",
                "status": "open",
                "grand_total": 210,
                "vat_rate": 0,
                "line_items": [
                    {
                        "monthly_timesheet_submission": self.submission.id,
                        "price": 70,
                        "quantity": 3,
                        "vat_amount": 0,
                    }
                ],
            },
            context={"request": self._build_request("/api/project_management/billing-invoice/", method="post")},
        )

        self.assertTrue(other_scope_serializer.is_valid(), other_scope_serializer.errors)
        other_scope_invoice = other_scope_serializer.save()
        self.assertEqual(other_scope_invoice.project_id, self.project.id)


class MonthlyTimesheetInvoiceableBillingSummaryTests(TestCase):
    def setUp(self):
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="invoiceable-billing@example.com",
            password="password",
            first_name="Invoiceable",
            last_name="Billing",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Acme Invoiceable Billing",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="invoiceable-billing@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.staff = Staff.objects.create(
            user=self.user,
            salary=1000,
            weekly_hours=40,
            hourly_rate=70,
        )
        self.submission = MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=5,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.user,
        )
        company_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Invoiceable Company Work",
            target_date=timezone.now().date(),
            status="open",
        )
        non_invoiceable_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.user,
            name="Non Invoiceable Work",
            target_date=timezone.now().date(),
            status="open",
        )
        week_start = timezone.datetime(2026, 5, 4).date()
        invoiceable_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=company_action_item,
            activity_code=str(company_action_item.id),
            activity_name=company_action_item.name,
            is_invoiceable=True,
        )
        non_invoiceable_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=non_invoiceable_action_item,
            activity_code=str(non_invoiceable_action_item.id),
            activity_name=non_invoiceable_action_item.name,
            is_invoiceable=False,
        )
        TimesheetEntry.objects.create(
            activity=invoiceable_activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=5,
        )
        TimesheetEntry.objects.create(
            activity=non_invoiceable_activity,
            entry_date=timezone.datetime(2026, 5, 6).date(),
            hours=3,
        )
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            name="National Holiday",
            date=timezone.datetime(2026, 5, 7).date(),
            source="manual",
            created_by=self.user,
        )

    def _build_request(self, path, method="get", data=None):
        request_factory = getattr(self.factory, method.lower())
        request = request_factory(path, data=data or {}, format="json")
        request.user = self.user
        return request

    def test_approved_timesheet_summaries_return_total_and_invoiceable_hours(self):
        view = InvoiceViewSet.as_view({"get": "approved_timesheet_summaries"})
        request = self._build_request(
            f"/api/project_management/billing-invoice/approved-timesheet-summaries/?company_profile={self.company.id}",
        )
        force_authenticate(request, user=self.user)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["submission_id"], self.submission.id)
        self.assertEqual(response.data[0]["total_hours"], 16.0)
        self.assertEqual(response.data[0]["invoiceable_hours"], 13.0)


class MonthlyTimesheetGeneratedTimeCardInvoiceableSnapshotTests(TestCase):
    def setUp(self):
        post_save.disconnect(action_item_post_save_operations, sender=ActionItem)
        pre_delete.disconnect(notify_timecard_delete, sender=TimeCard)
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_timecard_delete,
            sender=TimeCard,
        )
        self.user = User.objects.create_user(
            email="generated-snapshot@example.com",
            password="password",
            first_name="Generated",
            last_name="Snapshot",
            user_type="company_user",
        )
        self.reviewer = User.objects.create_user(
            email="generated-snapshot-reviewer@example.com",
            password="password",
            first_name="Generated",
            last_name="Reviewer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.reviewer,
            company_name="Acme Generated Snapshot",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="generated-snapshot@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.reviewer.company = self.company
        self.reviewer.save(update_fields=["company"])

        self.submission = MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=5,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.reviewer,
        )

        invoiceable_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.reviewer,
            name="Invoiceable Snapshot Task",
            target_date=timezone.now().date(),
            status="open",
        )
        non_invoiceable_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.reviewer,
            name="Non Invoiceable Snapshot Task",
            target_date=timezone.now().date(),
            status="open",
        )
        week_start = timezone.datetime(2026, 5, 4).date()
        self.invoiceable_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=invoiceable_action_item,
            activity_code=str(invoiceable_action_item.id),
            activity_name=invoiceable_action_item.name,
            is_invoiceable=True,
        )
        self.non_invoiceable_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=week_start,
            week_end_date=week_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=non_invoiceable_action_item,
            activity_code=str(non_invoiceable_action_item.id),
            activity_name=non_invoiceable_action_item.name,
            is_invoiceable=False,
        )
        self.invoiceable_entry = TimesheetEntry.objects.create(
            activity=self.invoiceable_activity,
            entry_date=timezone.datetime(2026, 5, 5).date(),
            hours=5,
        )
        self.non_invoiceable_entry = TimesheetEntry.objects.create(
            activity=self.non_invoiceable_activity,
            entry_date=timezone.datetime(2026, 5, 6).date(),
            hours=3,
        )

    def test_refresh_generated_timecards_snapshots_invoiceable_state(self):
        MonthlyTimesheetViewSet()._refresh_generated_timecards(self.submission, self.reviewer)

        generated_cards = {
            card.timesheet_entry_id: card
            for card in TimeCard.objects.filter(timesheet_submission=self.submission)
        }

        self.assertEqual(len(generated_cards), 2)
        self.assertTrue(generated_cards[self.invoiceable_entry.id].is_invoiceable_from_timesheet)
        self.assertFalse(generated_cards[self.non_invoiceable_entry.id].is_invoiceable_from_timesheet)

    def test_refresh_generated_timecards_updates_snapshot_on_regeneration(self):
        MonthlyTimesheetViewSet()._refresh_generated_timecards(self.submission, self.reviewer)

        self.non_invoiceable_activity.is_invoiceable = True
        self.non_invoiceable_activity.save(update_fields=["is_invoiceable"])

        MonthlyTimesheetViewSet()._refresh_generated_timecards(self.submission, self.reviewer)

        regenerated_card = TimeCard.objects.get(
            timesheet_submission=self.submission,
            timesheet_entry=self.non_invoiceable_entry,
        )
        self.assertTrue(regenerated_card.is_invoiceable_from_timesheet)

    def test_refresh_generated_timecards_generates_public_holiday_card_instead_of_overridden_entry(self):
        CompanyPublicHoliday.objects.create(
            company_profile=self.company,
            date=timezone.datetime(2026, 5, 5).date(),
            name="Founders Day",
            created_by=self.user,
        )

        MonthlyTimesheetViewSet()._refresh_generated_timecards(
            self.submission,
            self.reviewer,
        )

        generated_cards = TimeCard.objects.filter(
            timesheet_submission=self.submission,
        ).order_by("date", "id")

        self.assertEqual(generated_cards.count(), 2)
        self.assertEqual(generated_cards[0].name, "Founders Day")
        self.assertIsNone(generated_cards[0].timesheet_entry_id)
        self.assertEqual(generated_cards[0].hours, 8.0)
        self.assertTrue(generated_cards[0].is_invoiceable_from_timesheet)
        self.assertEqual(generated_cards[1].timesheet_entry_id, self.non_invoiceable_entry.id)

    def test_refresh_generated_timecards_uses_only_strict_month_overlap_dates(self):
        submission = MonthlyTimesheetSubmission.objects.create(
            user=self.user,
            month=3,
            year=2026,
            status="approved",
            submitted_at=timezone.now(),
            reviewed_at=timezone.now(),
            reviewed_by=self.reviewer,
        )

        overlap_action_item = ActionItem.objects.create(
            company_profile=self.company,
            assignee=self.user,
            created_by=self.reviewer,
            name="Overlap Snapshot Task",
            target_date=timezone.now().date(),
            status="open",
        )
        first_overlap_start = timezone.datetime(2026, 2, 23).date()
        last_overlap_start = timezone.datetime(2026, 3, 30).date()
        first_overlap_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=first_overlap_start,
            week_end_date=first_overlap_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=overlap_action_item,
            activity_code=str(overlap_action_item.id),
            activity_name=overlap_action_item.name,
            is_invoiceable=True,
        )
        last_overlap_activity = TimesheetActivity.objects.create(
            user=self.user,
            week_start_date=last_overlap_start,
            week_end_date=last_overlap_start + timezone.timedelta(days=6),
            created_by=self.user,
            source_type="company_action_item",
            company_action_item=overlap_action_item,
            activity_code=str(overlap_action_item.id),
            activity_name=overlap_action_item.name,
            is_invoiceable=True,
        )
        TimesheetEntry.objects.create(
            activity=first_overlap_activity,
            entry_date=timezone.datetime(2026, 2, 27).date(),
            hours=4,
        )
        march_first_entry = TimesheetEntry.objects.create(
            activity=first_overlap_activity,
            entry_date=timezone.datetime(2026, 3, 1).date(),
            hours=5,
        )
        march_last_entry = TimesheetEntry.objects.create(
            activity=last_overlap_activity,
            entry_date=timezone.datetime(2026, 3, 31).date(),
            hours=7,
        )
        TimesheetEntry.objects.create(
            activity=last_overlap_activity,
            entry_date=timezone.datetime(2026, 4, 1).date(),
            hours=8,
        )

        MonthlyTimesheetViewSet()._refresh_generated_timecards(submission, self.reviewer)

        generated_cards = TimeCard.objects.filter(
            timesheet_submission=submission,
        ).order_by("date", "id")

        self.assertEqual(generated_cards.count(), 2)
        self.assertEqual(
            [card.timesheet_entry_id for card in generated_cards],
            [march_first_entry.id, march_last_entry.id],
        )
        self.assertEqual(
            [card.date for card in generated_cards],
            [
                timezone.datetime(2026, 3, 1).date(),
                timezone.datetime(2026, 3, 31).date(),
            ],
        )


class PurchaseOrderPermissionTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)

        self.creator = User.objects.create_user(
            email="po-permission-creator@example.com",
            password="password",
            first_name="PO",
            last_name="Creator",
            user_type="company_user",
        )
        self.other_user = User.objects.create_user(
            email="po-permission-other@example.com",
            password="password",
            first_name="PO",
            last_name="Other",
            user_type="company_user",
        )
        self.company = self._create_company(
            self.creator,
            "PO Permission Company",
        )
        for user in (self.creator, self.other_user):
            user.company = self.company
            user.save(update_fields=["company"])

        self.other_company_user = User.objects.create_user(
            email="po-permission-external@example.com",
            password="password",
            first_name="PO",
            last_name="External",
            user_type="company_user",
        )
        self.other_company = self._create_company(
            self.other_company_user,
            "Other PO Permission Company",
        )
        self.other_company_user.company = self.other_company
        self.other_company_user.save(update_fields=["company"])

        self.project = self._create_project(self.company, self.creator, "Own Project")
        self.other_project = self._create_project(
            self.other_company,
            self.other_company_user,
            "Other Project",
        )
        ProjectTeamMembers.objects.bulk_create(
            [
                ProjectTeamMembers(
                    project=self.project,
                    role="Project Member",
                    team_member=self.creator,
                )
            ]
        )
        self.own_root = self._create_purchase_order(
            self.project,
            self.creator,
            "PO-OWN",
        )
        self.other_root = self._create_purchase_order(
            self.project,
            self.other_user,
            "PO-OTHER",
        )
        self.foreign_notice_on_own_root = self._create_purchase_order(
            self.project,
            self.other_user,
            "CN-OWN-ROOT",
            document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
            source_po=self.own_root,
        )
        self.own_notice_on_other_root = self._create_purchase_order(
            self.project,
            self.creator,
            "CN-OTHER-ROOT",
            document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
            source_po=self.other_root,
        )
        self.cross_company_root = self._create_purchase_order(
            self.other_project,
            self.creator,
            "PO-CROSS-COMPANY",
        )

    def _create_company(self, admin_user, name):
        return CompanyProfile.objects.create(
            admin_user=admin_user,
            company_name=name,
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=admin_user.email,
            company_tax_reference=f"tax-{admin_user.id}",
            company_iban=f"iban-{admin_user.id}",
        )

    def _create_project(self, company, owner, name):
        return Project.objects.create(
            project_name=name,
            contract_value=1000,
            company_profile=company,
            user=owner,
            created_by=owner,
        )

    def _create_purchase_order(
        self,
        project,
        creator,
        reference,
        *,
        document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        source_po=None,
    ):
        return PurchaseOrder.objects.create(
            project=project,
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=document_type,
            source_po=source_po,
            reference=reference,
            po_date=timezone.now().date(),
            currency="EUR",
            status="Open",
            amount_excl_vat=Decimal("100.00"),
            change_amount_excl_vat=(
                Decimal("10.00")
                if document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE
                else None
            ),
            vat_rate=Decimal("19.00"),
            vat_amount=Decimal("19.00"),
            amount_incl_vat=Decimal("119.00"),
            created_by=creator,
        )

    def _permission(self, action, granted_actions):
        permission = PurchaseOrderPermission(action=action, scope="company")
        permission._check_permission = Mock(
            side_effect=(
                lambda _user, module, checked_action, scope, context: (
                    module == "purchase_order"
                    and scope == "company"
                    and context == {}
                    and checked_action in granted_actions
                )
            )
        )
        return permission

    def _request(self, user, method="GET"):
        return Mock(user=user, method=method)

    def _assign_purchase_order_permission(self, action):
        role = Role.objects.create(
            name=f"Purchase Order {action} Role",
            company=self.company,
            type="solution",
            scope="company",
            is_default=False,
            is_active=True,
            created_by=self.creator,
        )
        permission, _ = Permission.objects.get_or_create(
            key=f"purchase_order.{action}",
            defaults={
                "name": f"Purchase Order {action}",
                "module": "purchase_order",
            },
        )
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(
            user=self.creator,
            role=role,
            company=self.company,
            assigned_by=self.creator,
        )

    def test_owner_and_created_visibility_filters(self):
        owner_scope_ids = set(
            filter_purchase_orders_to_owner_scope(
                PurchaseOrder.objects.all(),
                self.creator,
            ).values_list("id", flat=True)
        )
        self.assertIn(self.own_root.id, owner_scope_ids)
        self.assertIn(self.other_root.id, owner_scope_ids)
        self.assertNotIn(self.cross_company_root.id, owner_scope_ids)

        created_visible_ids = set(
            filter_purchase_orders_to_created_visibility(
                PurchaseOrder.objects.all(),
                self.creator,
            ).values_list("id", flat=True)
        )
        self.assertIn(self.own_root.id, created_visible_ids)
        self.assertIn(self.foreign_notice_on_own_root.id, created_visible_ids)
        self.assertIn(self.own_notice_on_other_root.id, created_visible_ids)
        self.assertNotIn(self.other_root.id, created_visible_ids)
        self.assertNotIn(self.cross_company_root.id, created_visible_ids)

    def test_project_visibility_requires_explicit_team_membership(self):
        creator_visible_ids = set(
            filter_purchase_orders_to_project_visibility(
                PurchaseOrder.objects.all(),
                self.creator,
            ).values_list("id", flat=True)
        )
        other_user_visible_ids = set(
            filter_purchase_orders_to_project_visibility(
                PurchaseOrder.objects.all(),
                self.other_user,
            ).values_list("id", flat=True)
        )

        self.assertEqual(
            creator_visible_ids,
            {
                self.own_root.id,
                self.other_root.id,
                self.foreign_notice_on_own_root.id,
                self.own_notice_on_other_root.id,
            },
        )
        self.assertEqual(other_user_visible_ids, set())

    def test_created_and_project_visibility_combines_both_scopes(self):
        second_project = self._create_project(
            self.company,
            self.other_user,
            "Second Company Project",
        )
        created_purchase_order = self._create_purchase_order(
            second_project,
            self.creator,
            "PO-CREATED-NOT-MEMBER",
        )

        visible_ids = set(
            filter_purchase_orders_to_created_or_project_visibility(
                PurchaseOrder.objects.all(),
                self.creator,
            ).values_list("id", flat=True)
        )

        self.assertIn(created_purchase_order.id, visible_ids)
        self.assertIn(self.other_root.id, visible_ids)
        self.assertNotIn(self.cross_company_root.id, visible_ids)

    def test_view_created_includes_foreign_notice_on_own_root(self):
        permission = self._permission("view", {"view_created"})
        request = self._request(self.creator)

        self.assertTrue(
            permission.has_object_permission(
                request,
                Mock(),
                self.foreign_notice_on_own_root,
            )
        )
        self.assertTrue(
            permission.has_object_permission(
                request,
                Mock(),
                self.own_notice_on_other_root,
            )
        )
        self.assertFalse(
            permission.has_object_permission(request, Mock(), self.other_root)
        )

    def test_serializer_marks_inherited_change_notice_visibility(self):
        request = self._request(self.creator)

        inherited_data = PurchaseOrderSerializer(
            self.foreign_notice_on_own_root,
            context={"request": request},
        ).data
        own_notice_data = PurchaseOrderSerializer(
            self.own_notice_on_other_root,
            context={"request": request},
        ).data
        unrelated_root_data = PurchaseOrderSerializer(
            self.other_root,
            context={"request": request},
        ).data

        self.assertTrue(inherited_data["can_view_via_source_po"])
        self.assertFalse(own_notice_data["can_view_via_source_po"])
        self.assertFalse(unrelated_root_data["can_view_via_source_po"])

    def test_created_mutation_does_not_inherit_from_source_po(self):
        request = self._request(self.creator, method="PATCH")
        update_created = self._permission("update", {"update_created"})

        self.assertTrue(
            update_created.has_object_permission(request, Mock(), self.own_root)
        )
        self.assertFalse(
            update_created.has_object_permission(
                request,
                Mock(),
                self.foreign_notice_on_own_root,
            )
        )

        update_all = self._permission("update", {"update"})
        self.assertTrue(
            update_all.has_object_permission(
                request,
                Mock(),
                self.foreign_notice_on_own_root,
            )
        )

        delete_created = self._permission("delete", {"delete_created"})
        delete_request = self._request(self.creator, method="DELETE")
        self.assertTrue(
            delete_created.has_object_permission(
                delete_request,
                Mock(),
                self.own_root,
            )
        )
        self.assertFalse(
            delete_created.has_object_permission(
                delete_request,
                Mock(),
                self.foreign_notice_on_own_root,
            )
        )

    def test_full_access_cannot_cross_owner_scope(self):
        permission = self._permission("view", {"full_access"})

        self.assertTrue(
            permission.has_object_permission(
                self._request(self.creator),
                Mock(),
                self.own_root,
            )
        )
        self.assertFalse(
            permission.has_object_permission(
                self._request(self.creator),
                Mock(),
                self.cross_company_root,
            )
        )

    def test_global_permission_accepts_created_variants(self):
        self.assertTrue(
            self._permission("view", {"view_created"}).has_permission(
                self._request(self.creator),
                Mock(),
            )
        )
        self.assertTrue(
            self._permission("update", {"update_created"}).has_permission(
                self._request(self.creator, method="PATCH"),
                Mock(),
            )
        )
        self.assertFalse(
            self._permission("delete", set()).has_permission(
                self._request(self.creator, method="DELETE"),
                Mock(),
            )
        )
        self.assertTrue(
            self._permission("view", {"view_project"}).has_permission(
                self._request(self.creator),
                Mock(),
            )
        )

    def test_view_project_object_permission_requires_team_membership(self):
        permission = self._permission("view", {"view_project"})
        project_only_permission = self._permission(
            "view_project",
            {"view_project"},
        )

        self.assertTrue(
            permission.has_object_permission(
                self._request(self.creator),
                Mock(),
                self.other_root,
            )
        )
        self.assertFalse(
            permission.has_object_permission(
                self._request(self.creator),
                Mock(),
                self.cross_company_root,
            )
        )
        self.assertTrue(
            project_only_permission.has_object_permission(
                self._request(self.creator),
                Mock(),
                self.other_root,
            )
        )
        self.assertFalse(
            self._permission(
                "view_project",
                {"view_created"},
            ).has_object_permission(
                self._request(self.creator),
                Mock(),
                self.own_root,
            )
        )

    def test_invoice_permission_does_not_grant_purchase_order_access(self):
        role = Role.objects.create(
            name="Purchase Order Permission Role",
            company=self.company,
            type="solution",
            scope="company",
            is_default=False,
            is_active=True,
            created_by=self.creator,
        )
        invoice_permission = Permission.objects.create(
            key="invoice.full_access",
            name="Invoice Full Access",
            module="invoice",
        )
        purchase_order_permission = Permission.objects.create(
            key="purchase_order.view_created",
            name="View Created Purchase Orders",
            module="purchase_order",
        )
        RolePermission.objects.create(
            role=role,
            permission=invoice_permission,
        )
        UserRole.objects.create(
            user=self.creator,
            role=role,
            company=self.company,
            assigned_by=self.creator,
        )
        permission = PurchaseOrderPermission(action="view", scope="company")
        request = self._request(self.creator)

        with patch("builtins.print"):
            self.assertFalse(permission.has_permission(request, Mock()))

        RolePermission.objects.create(
            role=role,
            permission=purchase_order_permission,
        )
        with patch("builtins.print"):
            self.assertTrue(permission.has_permission(request, Mock()))

    def test_viewset_maps_crud_and_sum_actions(self):
        expected_actions = {
            "list": "view",
            "retrieve": "view",
            "sum": "view",
            "create": "create",
            "update": "update",
            "partial_update": "update",
            "destroy": "delete",
        }
        viewset = PurchaseOrderViewSet()

        for view_action, permission_action in expected_actions.items():
            viewset.action = view_action
            permissions = viewset.get_permissions()
            self.assertEqual(len(permissions), 1)
            self.assertIsInstance(permissions[0], PurchaseOrderPermission)
            self.assertEqual(permissions[0].action, permission_action)

    def test_view_querysets_apply_created_visibility_to_list_retrieve_and_sum(self):
        expected_ids = {
            self.own_root.id,
            self.foreign_notice_on_own_root.id,
            self.own_notice_on_other_root.id,
        }

        for action in ("list", "retrieve", "sum"):
            viewset = PurchaseOrderViewSet()
            viewset.action = action
            viewset.request = self._request(self.creator)
            with patch.object(
                PurchaseOrderPermission,
                "get_view_scope",
                return_value="created",
            ):
                visible_ids = set(
                    viewset.get_queryset().values_list("id", flat=True)
                )
            self.assertEqual(visible_ids, expected_ids)

    def test_mutation_queryset_keeps_owner_scope_for_object_permissions(self):
        viewset = PurchaseOrderViewSet()
        viewset.action = "partial_update"
        viewset.request = self._request(self.creator, method="PATCH")

        mutation_ids = set(
            viewset.get_queryset().values_list("id", flat=True)
        )

        self.assertIn(self.own_root.id, mutation_ids)
        self.assertIn(self.other_root.id, mutation_ids)
        self.assertNotIn(self.cross_company_root.id, mutation_ids)

    def test_source_po_field_only_accepts_visible_root_purchase_orders(self):
        with patch.object(
            PurchaseOrderPermission,
            "get_view_scope",
            return_value="created",
        ):
            serializer = PurchaseOrderSerializer(
                context={"request": self._request(self.creator)}
            )

        source_ids = set(
            serializer.fields["source_po"].queryset.values_list(
                "id",
                flat=True,
            )
        )
        self.assertEqual(source_ids, {self.own_root.id})

        with self.assertRaises(ValidationError):
            serializer.fields["source_po"].run_validation(self.other_root.id)

    def test_linked_po_history_excludes_rows_outside_view_created_scope(self):
        with patch.object(
            PurchaseOrderPermission,
            "get_view_scope",
            return_value="created",
        ):
            serializer = PurchaseOrderSerializer(
                context={"request": self._request(self.creator)}
            )
            own_notice_history = serializer.get_linked_po_documents(
                self.own_notice_on_other_root
            )

        self.assertEqual(own_notice_history, [])

        with patch.object(
            PurchaseOrderPermission,
            "get_view_scope",
            return_value="created",
        ):
            serializer = PurchaseOrderSerializer(
                context={"request": self._request(self.creator)}
            )
            own_root_history = serializer.get_linked_po_documents(self.own_root)

        self.assertEqual(
            {row["id"] for row in own_root_history},
            {self.foreign_notice_on_own_root.id},
        )

    def test_list_endpoint_denies_invoice_only_permission(self):
        role = Role.objects.create(
            name="Invoice Only List Role",
            company=self.company,
            type="solution",
            scope="company",
            is_default=False,
            is_active=True,
            created_by=self.creator,
        )
        invoice_permission = Permission.objects.create(
            key="invoice.view",
            name="View Invoices",
            module="invoice",
        )
        RolePermission.objects.create(
            role=role,
            permission=invoice_permission,
        )
        UserRole.objects.create(
            user=self.creator,
            role=role,
            company=self.company,
            assigned_by=self.creator,
        )
        request = APIRequestFactory().get(
            "/api/project_management/purchase-orders/"
        )
        force_authenticate(request, user=self.creator)

        with patch("builtins.print"):
            response = PurchaseOrderViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_view_created_endpoints_hide_unrelated_rows_and_scope_sum(self):
        self._assign_purchase_order_permission("view_created")

        visible_request = APIRequestFactory().get(
            f"/api/project_management/purchase-orders/"
            f"{self.foreign_notice_on_own_root.id}/"
        )
        force_authenticate(visible_request, user=self.creator)
        hidden_request = APIRequestFactory().get(
            f"/api/project_management/purchase-orders/{self.other_root.id}/"
        )
        force_authenticate(hidden_request, user=self.creator)
        sum_request = APIRequestFactory().get(
            "/api/project_management/purchase-orders/sum/"
        )
        force_authenticate(sum_request, user=self.creator)

        retrieve_view = PurchaseOrderViewSet.as_view({"get": "retrieve"})
        sum_view = PurchaseOrderViewSet.as_view({"get": "sum"})
        with patch("builtins.print"):
            visible_response = retrieve_view(
                visible_request,
                pk=self.foreign_notice_on_own_root.id,
            )
            hidden_response = retrieve_view(
                hidden_request,
                pk=self.other_root.id,
            )
            with patch(
                "project_management.views.purchase_order.get_user_currency",
                return_value="EUR",
            ):
                sum_response = sum_view(sum_request)

        self.assertEqual(visible_response.status_code, status.HTTP_200_OK)
        self.assertEqual(hidden_response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(sum_response.status_code, status.HTTP_200_OK)
        self.assertEqual(sum_response.data["purchase_order_count"], 3)

    def test_view_project_endpoint_returns_only_team_project_purchase_orders(self):
        company_purchase_order = PurchaseOrder.objects.create(
            company_profile=self.company,
            po_type=PurchaseOrder.POType.OUTGOING,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
            reference="PO-COMPANY",
            po_date=timezone.now().date(),
            currency="EUR",
            status="Open",
            amount_excl_vat=Decimal("100.00"),
            vat_rate=Decimal("19.00"),
            vat_amount=Decimal("19.00"),
            amount_incl_vat=Decimal("119.00"),
            created_by=self.creator,
        )
        self._assign_purchase_order_permission("view_project")
        viewset = PurchaseOrderViewSet()
        viewset.action = "list"
        viewset.request = self._request(self.creator)

        with patch("builtins.print"):
            visible_ids = set(
                viewset.get_queryset().values_list("id", flat=True)
            )

        self.assertEqual(
            visible_ids,
            {
                self.own_root.id,
                self.other_root.id,
                self.foreign_notice_on_own_root.id,
                self.own_notice_on_other_root.id,
            },
        )
        self.assertNotIn(company_purchase_order.id, visible_ids)
        self.assertNotIn(self.cross_company_root.id, visible_ids)

    def test_created_mutation_endpoints_reject_inherited_change_notice(self):
        self._assign_purchase_order_permission("update_created")
        self._assign_purchase_order_permission("delete_created")

        update_request = APIRequestFactory().patch(
            f"/api/project_management/purchase-orders/"
            f"{self.foreign_notice_on_own_root.id}/",
            {"notes": "Not allowed"},
            format="json",
        )
        force_authenticate(update_request, user=self.creator)
        delete_request = APIRequestFactory().delete(
            f"/api/project_management/purchase-orders/"
            f"{self.foreign_notice_on_own_root.id}/"
        )
        force_authenticate(delete_request, user=self.creator)

        update_view = PurchaseOrderViewSet.as_view({"patch": "partial_update"})
        delete_view = PurchaseOrderViewSet.as_view({"delete": "destroy"})
        with patch("builtins.print"):
            update_response = update_view(
                update_request,
                pk=self.foreign_notice_on_own_root.id,
            )
            delete_response = delete_view(
                delete_request,
                pk=self.foreign_notice_on_own_root.id,
            )

        self.assertEqual(update_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(delete_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            PurchaseOrder.objects.filter(
                pk=self.foreign_notice_on_own_root.id
            ).exists()
        )

    def test_invoice_and_milestone_hide_and_reject_inaccessible_purchase_orders(self):
        invoice = Invoice.objects.create(
            project=self.project,
            company_profile=self.company,
            purchase_order=self.other_root,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="not paid",
            document_type=Invoice.DocumentType.INVOICE,
            reference_number="INV-HIDDEN-PO",
            grand_total=Decimal("50.00"),
            created_by=self.creator,
        )
        invoice.refresh_from_db()
        phase = ProjectPhase.objects.create(
            project=self.project,
            name="Hidden PO Phase",
            amount=Decimal("10.00"),
            purchase_order=self.other_root,
        )

        with patch.object(
            PurchaseOrderPermission,
            "get_view_scope",
            return_value="created",
        ):
            invoice_serializer = InvoiceSerializer(
                invoice,
                context={"request": self._request(self.creator)},
            )
            phase_serializer = ProjectPhaseSerializer(
                phase,
                context={"request": self._request(self.creator)},
            )
            invoice_data = invoice_serializer.data
            phase_data = phase_serializer.data

        self.assertIsNone(invoice_data["purchase_order"])
        self.assertIsNone(phase_data["purchase_order"])
        self.assertEqual(phase_data["purchase_orders"], [])
        self.assertEqual(phase_data["linked_purchase_orders"], [])
        with self.assertRaises(ValidationError):
            invoice_serializer.fields["purchase_order"].run_validation(
                self.other_root.id
            )
        with self.assertRaises(ValidationError):
            phase_serializer.fields["purchase_order"].run_validation(
                self.other_root.id
            )

    def test_project_embedding_contains_only_visible_purchase_orders(self):
        with patch.object(
            PurchaseOrderPermission,
            "get_view_scope",
            return_value="created",
        ):
            serializer = ProjectSerializer(
                self.project,
                context={"request": self._request(self.creator)},
            )
            data = serializer.data

        self.assertEqual(
            {row["id"] for row in data["purchase_orders"]},
            {
                self.own_root.id,
                self.foreign_notice_on_own_root.id,
                self.own_notice_on_other_root.id,
            },
        )
        self.assertEqual(
            Decimal(str(data["total_po_earning"])),
            Decimal("300.00"),
        )

    def test_planned_activity_selector_requires_purchase_order_view(self):
        request = APIRequestFactory().get(
            "/api/project_management/timesheet-weeks/"
            "planned-activity-purchase-orders/",
            {"project_id": self.project.id},
        )
        force_authenticate(request, user=self.creator)

        with patch("builtins.print"):
            response = TimesheetWeekViewSet.as_view(
                {"get": "planned_activity_purchase_orders"}
            )(request)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_planned_activity_selector_accepts_view_project_for_team_member(self):
        self._assign_purchase_order_permission("view_project")
        request = APIRequestFactory().get(
            "/api/project_management/timesheet-weeks/"
            "planned-activity-purchase-orders/",
            {"project_id": self.project.id},
        )
        force_authenticate(request, user=self.creator)

        with patch("builtins.print"):
            response = TimesheetWeekViewSet.as_view(
                {"get": "planned_activity_purchase_orders"}
            )(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["id"] for row in response.data["purchase_orders"]],
            [self.own_root.id, self.other_root.id],
        )

    def test_planned_activity_selector_accepts_view_created_for_team_member(self):
        self._assign_purchase_order_permission("view_created")
        request = APIRequestFactory().get(
            "/api/project_management/timesheet-weeks/"
            "planned-activity-purchase-orders/",
            {"project_id": self.project.id},
        )
        force_authenticate(request, user=self.creator)

        with patch("builtins.print"):
            response = TimesheetWeekViewSet.as_view(
                {"get": "planned_activity_purchase_orders"}
            )(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["id"] for row in response.data["purchase_orders"]],
            [self.own_root.id],
        )

    def test_planned_activity_selector_rejects_non_team_member(self):
        self._assign_purchase_order_permission("view_project")
        ProjectTeamMembers.objects.filter(
            project=self.project,
            team_member=self.creator,
        ).delete()
        request = APIRequestFactory().get(
            "/api/project_management/timesheet-weeks/"
            "planned-activity-purchase-orders/",
            {"project_id": self.project.id},
        )
        force_authenticate(request, user=self.creator)

        with patch("builtins.print"):
            response = TimesheetWeekViewSet.as_view(
                {"get": "planned_activity_purchase_orders"}
            )(request)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_ai_purchase_order_lookup_uses_purchase_order_visibility(self):
        from ai_tools.services import visible_purchase_orders

        with patch("builtins.print"), self.assertRaises(PermissionDenied):
            visible_purchase_orders(
                self.creator,
                {"scope": "company"},
            )

        self._assign_purchase_order_permission("view_created")
        with patch("builtins.print"):
            visible_queryset = visible_purchase_orders(
                self.creator,
                {"scope": "company"},
            )
            visible_ids = set(visible_queryset.values_list("id", flat=True))

        self.assertEqual(
            visible_ids,
            {
                self.own_root.id,
                self.foreign_notice_on_own_root.id,
                self.own_notice_on_other_root.id,
            },
        )

    def test_view_permission_returns_all_owner_scoped_rows(self):
        null_creator_purchase_order = self._create_purchase_order(
            self.project,
            None,
            "PO-NULL-CREATOR",
        )
        self._assign_purchase_order_permission("view")
        viewset = PurchaseOrderViewSet()
        viewset.action = "list"
        viewset.request = self._request(self.creator)

        with patch("builtins.print"):
            visible_ids = set(
                viewset.get_queryset().values_list("id", flat=True)
            )

        self.assertEqual(
            visible_ids,
            {
                self.own_root.id,
                self.other_root.id,
                self.foreign_notice_on_own_root.id,
                self.own_notice_on_other_root.id,
                null_creator_purchase_order.id,
            },
        )
        self.assertNotIn(self.cross_company_root.id, visible_ids)

    def test_view_created_excludes_rows_without_creator(self):
        null_creator_purchase_order = self._create_purchase_order(
            self.project,
            None,
            "PO-NULL-CREATED-VIEW",
        )

        visible_ids = set(
            filter_purchase_orders_to_created_visibility(
                PurchaseOrder.objects.all(),
                self.creator,
            ).values_list("id", flat=True)
        )

        self.assertNotIn(null_creator_purchase_order.id, visible_ids)

    def test_freelancer_owner_scope_is_isolated(self):
        freelancer = User.objects.create_user(
            email="po-freelancer-scope@example.com",
            password="password",
            first_name="PO",
            last_name="Freelancer",
            user_type="freelancer",
        )
        other_freelancer = User.objects.create_user(
            email="po-other-freelancer@example.com",
            password="password",
            first_name="Other",
            last_name="Freelancer",
            user_type="freelancer",
        )
        freelancer_project = self._create_project(
            None,
            freelancer,
            "Freelancer PO Project",
        )
        other_freelancer_project = self._create_project(
            None,
            other_freelancer,
            "Other Freelancer PO Project",
        )
        freelancer_purchase_order = self._create_purchase_order(
            freelancer_project,
            freelancer,
            "PO-FREELANCER",
        )
        other_freelancer_purchase_order = self._create_purchase_order(
            other_freelancer_project,
            other_freelancer,
            "PO-OTHER-FREELANCER",
        )

        visible_ids = set(
            filter_purchase_orders_to_owner_scope(
                PurchaseOrder.objects.all(),
                freelancer,
            ).values_list("id", flat=True)
        )

        self.assertIn(freelancer_purchase_order.id, visible_ids)
        self.assertNotIn(other_freelancer_purchase_order.id, visible_ids)
        self.assertNotIn(self.own_root.id, visible_ids)

    def test_authorized_delete_preserves_linked_change_notice_protection(self):
        self._assign_purchase_order_permission("delete")
        request = APIRequestFactory().delete(
            f"/api/project_management/purchase-orders/{self.own_root.id}/"
        )
        force_authenticate(request, user=self.creator)

        with patch("builtins.print"):
            response = PurchaseOrderViewSet.as_view({"delete": "destroy"})(
                request,
                pk=self.own_root.id,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            PurchaseOrder.objects.filter(pk=self.own_root.id).exists()
        )

    def test_create_rejects_inaccessible_project_even_with_po_create(self):
        self._assign_purchase_order_permission("create")
        request = APIRequestFactory().post(
            "/api/project_management/purchase-orders/",
            {
                "project": self.other_project.id,
                "po_type": PurchaseOrder.POType.OUTGOING,
            },
            format="json",
        )
        force_authenticate(request, user=self.creator)

        with patch("builtins.print"):
            response = PurchaseOrderViewSet.as_view({"post": "create"})(
                request
            )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_rollout_audit_reports_and_can_fail_on_null_creators(self):
        self._create_purchase_order(
            self.project,
            None,
            "PO-NULL-AUDIT",
        )
        output = io.StringIO()

        call_command("audit_purchase_order_rollout", stdout=output)

        self.assertIn("Rows with created_by=NULL: 1", output.getvalue())
        with self.assertRaises(CommandError):
            call_command(
                "audit_purchase_order_rollout",
                fail_on_null_created_by=True,
                stdout=io.StringIO(),
            )


class PurchaseOrderContactFieldsTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="po-contact@example.com",
            password="password",
            first_name="PO",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="PO Contact Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="po-contact@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.contact_company = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Supplier GmbH",
        )
        self.project = Project.objects.create(
            project_name="PO Contact Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )

    def _serializer(self, data, *, instance=None):
        request = self.factory.post("/api/project_management/purchase-orders/")
        force_authenticate(request, user=self.user)
        serializer = PurchaseOrderSerializer(
            instance=instance,
            data=data,
            context={"request": request, "project": self.project},
            partial=instance is not None,
        )
        return serializer

    def test_incoming_po_requires_emitting_company(self):
        serializer = self._serializer(
            {
                "project": self.project.id,
                "po_type": PurchaseOrder.POType.INCOMING,
                "document_type": PurchaseOrder.DocumentType.PURCHASE_ORDER,
                "po_date": timezone.now().date().isoformat(),
                "currency": "EUR",
                "status": "Received",
                "amount_excl_vat": "100.00",
                "vat_rate": "19.00",
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("emitting_user", serializer.errors)

    def test_incoming_po_accepts_emitting_contact_company(self):
        serializer = self._serializer(
            {
                "project": self.project.id,
                "po_type": PurchaseOrder.POType.INCOMING,
                "document_type": PurchaseOrder.DocumentType.PURCHASE_ORDER,
                "po_date": timezone.now().date().isoformat(),
                "currency": "EUR",
                "status": "Received",
                "amount_excl_vat": "100.00",
                "vat_rate": "19.00",
                "emitting_company": self.contact_company.id,
                "emitting_user": None,
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        po = serializer.save(created_by=self.user)
        self.assertEqual(po.emitting_company_id, self.contact_company.id)
        self.assertIsNone(po.emitting_user_id)

    def test_outgoing_po_accepts_receiving_contact_company(self):
        serializer = self._serializer(
            {
                "project": self.project.id,
                "po_type": PurchaseOrder.POType.OUTGOING,
                "document_type": PurchaseOrder.DocumentType.PURCHASE_ORDER,
                "po_date": timezone.now().date().isoformat(),
                "currency": "EUR",
                "status": "Open",
                "amount_excl_vat": "100.00",
                "vat_rate": "19.00",
                "receiving_company": self.contact_company.id,
                "receiving_user": None,
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        po = serializer.save(created_by=self.user)
        self.assertEqual(po.receiving_company_id, self.contact_company.id)
        self.assertIsNone(po.receiving_user_id)


@override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
class PartPurchaseIntentApiTests(TestCase):
    @patch("project_management.views.matrix.require_any_permission_multiple_modules", return_value=True)
    def test_cart_eligibility_returns_only_requested_project_parts(self, _permission):
        from django.urls import reverse
        from project_management.models import BillOfMaterialPart
        self.project.solution = "Energy Consultant Solution"
        self.project.save(update_fields=["solution"])
        other_project = Project.objects.create(
            project_name="Other eligible project", contract_value=1000,
            company_profile=self.company, user=self.user, created_by=self.user,
        )
        other_part = BillOfMaterialPart.objects.create(project=other_project, name="Other project part")
        legacy = BillOfMaterialPart.objects.create(project=self.project, name="Legacy part")
        response = self.client.get(reverse("bom-parts-purchase-eligibility"), {
            "project": self.project.id, "ids": f"{self.bom_part.id},{legacy.id},{other_part.id}",
        })
        self.assertEqual(response.status_code, 200)
        results = {item["id"]: item["purchase_block_reason"] for item in response.data["results"]}
        self.assertEqual(results[self.bom_part.id], "")
        self.assertTrue(results[legacy.id])
        self.assertNotIn(other_part.id, results)
        invalid = self.client.get(reverse("bom-parts-purchase-eligibility"), {
            "project": self.project.id, "ids": "invalid",
        })
        self.assertEqual(invalid.status_code, 400)

    def _approved_part(self, project):
        import uuid
        from project_management.models import CompanySolutionSpaceItem, BillOfMaterialPart, ProjectWorkstation
        from project_management.services.workstation_composition import create_project_workstation
        from project_management.services.workstation_revision import commit_workstation_revision
        from project_management.services.workstation_bom import create_bom_plan, apply_bom_plan
        key = str(uuid.uuid4())
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company, assembly_name="Reviewed purchase part",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company, parent=source, assembly_name="Purchase component",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        station = create_project_workstation(project=project, source=source,
            actor=self.user, idempotency_key=key).workstation
        commit_workstation_revision(workstation=station, actor=self.user,
            expected_revision=1, reason="Reviewed", idempotency_key=key,
            operations=[{"op": "set_approval", "node": station.nodes.get(parent=None).id,
                         "approval_status": "agreed", "include_subtree": True}])
        plan = create_bom_plan(project=project, workstation_ids=list(ProjectWorkstation.objects.filter(project=project).values_list("id", flat=True)),
            actor=self.user, idempotency_key=key)
        apply_bom_plan(plan=plan, actor=self.user, expected_target_hash=plan.target_hash)
        return BillOfMaterialPart.objects.get(source_workstation=station, is_purchasable=True)

    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="part-buyer@example.com",
            password="password",
            first_name="Part",
            last_name="Buyer",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Buyer Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="buyer-company@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Purchasing Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.payment_method = SupplierPaymentMethod.objects.create(
            company_profile=self.company,
            name="Buyer company purchasing account",
            method_type=SupplierPaymentMethod.METHOD_INVOICE,
            display_details="Invoice account ending 1234",
            reference="PM-1234",
            created_by=self.user,
        )
        self.client.force_authenticate(self.user)

        self.bom_part = self._approved_part(self.project)

    def _payload(self):
        return {
            "source": PartPurchaseIntent.SOURCE_BOM_PART,
            "source_product_id": "bom-part-1",
            "project": self.project.id,
            "supplier": "FESTO",
            "currency": "EUR",
            "price_status": PartPurchaseIntent.PRICE_ON_REQUEST,
            "items": [
                {
                    "product_id": "part-1",
                    "bom_part_id": self.bom_part.id,
                    "name": "Demo part",
                    "quantity": 2,
                }
            ],
            "product_snapshot": {
                "name": "Demo part",
                "supplier": "FESTO",
            },
            "general_information": {
                "delivery_information": "Dock gate 2",
                "purchase_information": "ERP-123",
            },
            "delivery_method": "DHL",
            "delivery_address": {
                "company": "Buyer Company",
            },
            "invoice_address": {
                "company": "Buyer Company",
            },
            "payment_snapshot": {
                "payment_method_id": self.payment_method.id,
                "prototype_only": True,
            },
        }

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=False)
    def test_legacy_purchase_intent_does_not_require_engineering_history(self):
        from project_management.models import BillOfMaterialPart
        part = BillOfMaterialPart.objects.create(project=self.project, name="Legacy production part")
        payload = self._payload()
        payload["items"][0]["bom_part_id"] = part.pk
        response = self.client.post("/api/project_management/part-purchase-intents/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_purchase_intent_email_code_and_confirm_flow(self):
        create_response = self.client.post(
            "/api/project_management/part-purchase-intents/",
            self._payload(),
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        intent_id = create_response.data["id"]

        with patch("project_management.views.part_purchase.send_mail") as send_mail, patch(
            "project_management.views.part_purchase.random.SystemRandom"
        ) as system_random:
            system_random.return_value.randint.return_value = 123456
            code_response = self.client.post(
                f"/api/project_management/part-purchase-intents/{intent_id}/send-code/",
                format="json",
            )

        self.assertEqual(code_response.status_code, status.HTTP_200_OK)
        send_mail.assert_called_once()
        mail_args = send_mail.call_args.args
        mail_kwargs = send_mail.call_args.kwargs
        self.assertEqual(
            mail_args[0],
            "Your sharemundo links purchase confirmation code",
        )
        self.assertIn("Dear Part Buyer", mail_args[1])
        self.assertIn("123456", mail_args[1])
        self.assertEqual(mail_args[3], [self.user.email])
        self.assertFalse(mail_kwargs["fail_silently"])
        self.assertIn("Confirm your purchase request", mail_kwargs["html_message"])
        self.assertIn("123456", mail_kwargs["html_message"])
        self.assertIn("Buyer Company", mail_kwargs["html_message"])
        self.assertIn(
            create_response.data["reference"],
            mail_kwargs["html_message"],
        )
        self.assertIn("Valid for 10 minutes", mail_kwargs["html_message"])
        self.assertIn("Terms of Use", mail_kwargs["html_message"])
        self.assertIn("Privacy Policy", mail_kwargs["html_message"])
        self.assertEqual(code_response.data["status"], PartPurchaseIntent.STATUS_CODE_SENT)

        wrong_response = self.client.post(
            f"/api/project_management/part-purchase-intents/{intent_id}/verify-code/",
            {"code": "000000"},
            format="json",
        )
        self.assertEqual(wrong_response.status_code, status.HTTP_400_BAD_REQUEST)

        verify_response = self.client.post(
            f"/api/project_management/part-purchase-intents/{intent_id}/verify-code/",
            {"code": "123456"},
            format="json",
        )
        self.assertEqual(verify_response.status_code, status.HTTP_200_OK)
        self.assertEqual(verify_response.data["status"], PartPurchaseIntent.STATUS_VERIFIED)

        confirm_response = self.client.post(
            f"/api/project_management/part-purchase-intents/{intent_id}/confirm/",
            format="json",
        )
        self.assertEqual(confirm_response.status_code, status.HTTP_200_OK)
        self.assertEqual(confirm_response.data["status"], PartPurchaseIntent.STATUS_CONFIRMED)

        intent = PartPurchaseIntent.objects.get(id=intent_id)
        self.assertIsNotNone(intent.confirmed_at)
        self.assertEqual(intent.buyer_company, self.company)
        self.assertEqual(intent.project, self.project)
        self.assertEqual(intent.delivery_method, "DHL")
        self.assertEqual(
            intent.general_information["purchase_information"],
            "ERP-123",
        )

    def test_existing_cart_cannot_confirm_after_engineering_approval_is_revoked(self):
        from project_management.services.workstation_revision import commit_workstation_revision
        response = self.client.post("/api/project_management/part-purchase-intents/", self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        intent = PartPurchaseIntent.objects.get(pk=response.data["id"])
        intent.code_verified_at = timezone.now()
        intent.save(update_fields=["code_verified_at"])
        station = self.bom_part.source_workstation
        commit_workstation_revision(workstation=station, actor=self.user,
            expected_revision=station.active_revision, reason="Review again", idempotency_key="revoke-cart-approval",
            operations=[{"op": "set_approval", "node": station.nodes.get(revision__number=station.active_revision, parent=None).id,
                         "approval_status": "open"}])
        confirmation = self.client.post(f"/api/project_management/part-purchase-intents/{intent.id}/confirm/", format="json")
        self.assertEqual(confirmation.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Agreed", confirmation.data["detail"])
        intent.refresh_from_db()
        self.assertIsNone(intent.confirmed_at)

    def test_cart_rejects_missing_bom_reference_and_other_project_part(self):
        payload = self._payload()
        del payload["items"][0]["bom_part_id"]
        response = self.client.post("/api/project_management/part-purchase-intents/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("items", response.data)
        other_project = Project.objects.create(project_name="Other BoM", contract_value=0,
            company_profile=self.company, user=self.user, created_by=self.user)
        payload = self._payload()
        payload["project"] = other_project.id
        response = self.client.post("/api/project_management/part-purchase-intents/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("items", response.data)

    def test_purchase_confirmation_requires_available_payment_method(self):
        payload = self._payload()
        payload["payment_snapshot"] = {}
        create_response = self.client.post(
            "/api/project_management/part-purchase-intents/",
            payload,
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        intent = PartPurchaseIntent.objects.get(id=create_response.data["id"])
        intent.code_verified_at = timezone.now()
        intent.status = PartPurchaseIntent.STATUS_VERIFIED
        intent.save(update_fields=["code_verified_at", "status", "updated_at"])

        confirm_response = self.client.post(
            f"/api/project_management/part-purchase-intents/{intent.id}/confirm/",
            format="json",
        )

        self.assertEqual(confirm_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("payment method", confirm_response.data["detail"].lower())

    def test_lists_active_supplier_payment_methods_for_buyer_company(self):
        SupplierPaymentMethod.objects.create(
            company_profile=self.company,
            name="Inactive method",
            method_type=SupplierPaymentMethod.METHOD_CARD,
            is_active=False,
        )

        response = self.client.get(
            "/api/project_management/part-purchase-payment-methods/",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        method_names = [item["name"] for item in response.data["results"]]
        self.assertIn("Buyer company purchasing account", method_names)
        self.assertNotIn("Inactive method", method_names)

    def test_purchase_intent_accepts_mixed_supplier_cart_items(self):
        payload = self._payload()
        payload["supplier"] = "FESTO, WAGO"
        payload["unit_price"] = None
        payload["items"] = [
            {
                "product_id": "festo-1",
                "bom_part_id": self.bom_part.id,
                "name": "FESTO valve terminal",
                "supplier": "FESTO",
                "quantity": 2,
                "unit_price": "120.00",
                "currency": "EUR",
            },
            {
                "product_id": "wago-1",
                "bom_part_id": self._approved_part(self.project).id,
                "name": "WAGO connector",
                "supplier": "WAGO",
                "quantity": 1,
                "unit_price": "45.00",
                "currency": "EUR",
            },
        ]
        payload["product_snapshot"] = {
            "selected_supplier_filter": "all",
            "selected_items": payload["items"],
        }

        create_response = self.client.post(
            "/api/project_management/part-purchase-intents/",
            payload,
            format="json",
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_response.data["total_amount"], Decimal("285.00"))
        self.assertEqual(len(create_response.data["items"]), 2)
        self.assertEqual(create_response.data["items"][0]["supplier"], "FESTO")
        self.assertEqual(create_response.data["items"][1]["supplier"], "WAGO")

    def test_can_filter_active_purchase_intents_by_project(self):
        first_response = self.client.post(
            "/api/project_management/part-purchase-intents/",
            self._payload(),
            format="json",
        )
        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)

        other_project = Project.objects.create(
            project_name="Other Purchasing Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        other_payload = self._payload()
        other_payload["project"] = other_project.id
        other_payload["items"][0]["bom_part_id"] = self._approved_part(other_project).id
        other_response = self.client.post(
            "/api/project_management/part-purchase-intents/",
            other_payload,
            format="json",
        )
        self.assertEqual(other_response.status_code, status.HTTP_201_CREATED)

        PartPurchaseIntent.objects.filter(id=other_response.data["id"]).update(
            status=PartPurchaseIntent.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )

        list_response = self.client.get(
            f"/api/project_management/part-purchase-intents/?project={self.project.id}&active=true",
            format="json",
        )

        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 1)
        self.assertEqual(list_response.data["results"][0]["id"], first_response.data["id"])

class MatrixCommentSerializerAndMentionTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(create_related_objects, sender=ProjectTeamMembers)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            create_related_objects,
            sender=ProjectTeamMembers,
        )

        self.factory = APIRequestFactory()
        self.author = User.objects.create_user(
            email="matrix-comment-author@example.com",
            password="password",
            first_name="Author",
            last_name="User",
            user_type="company_user",
        )
        self.teammate = User.objects.create_user(
            email="matrix-comment-teammate@example.com",
            password="password",
            first_name="Team",
            last_name="Mate",
            user_type="company_user",
        )
        self.outsider = User.objects.create_user(
            email="matrix-comment-outsider@example.com",
            password="password",
            first_name="Out",
            last_name="Sider",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.author,
            company_name="Matrix Comment Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="matrix-comments@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        User.objects.filter(id__in=[self.author.id, self.teammate.id]).update(
            company=self.company
        )
        self.author.refresh_from_db()
        self.teammate.refresh_from_db()

        self.project = Project.objects.create(
            project_name="Matrix Comment Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.author,
            created_by=self.author,
        )
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="member",
            team_member=self.author,
        )
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="member",
            team_member=self.teammate,
        )
        self.demand = SolutionDemand.objects.create(
            project=self.project,
            subject="Demand for comments",
            demand_number="D-1",
            created_by=self.author,
        )
        self.available_part = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Available part",
            part_number="AP-1",
            created_by=self.author,
        )

    def _request(self, user):
        request = self.factory.post("/")
        request.user = user
        return request

    @patch(
        "project_management.serializers.matrix.filter_accessible_linked_objects",
        side_effect=lambda objects, user: objects,
    )
    @patch("project_management.serializers.matrix.get_matrix_mention_user_snapshots")
    def test_create_persists_normalized_mentions_and_linked_objects(
        self,
        mock_mention_snapshots,
        _mock_filter_linked_objects,
    ):
        mock_mention_snapshots.return_value = {
            self.teammate.id: {
                "user_id": self.teammate.id,
                "name": "Team Mate",
                "user_image": "",
            }
        }
        serializer = MatrixCommentSerializer(
            data={
                "target_type": MatrixComment.TARGET_SOLUTION_DEMAND,
                "solution_demand": self.demand.id,
                "content": "Please review /ai-1",
                "linked_objects": [
                    {"object_type": "action_item", "object_id": 1},
                ],
                "mentions": [{"user_id": self.teammate.id}],
            },
            context={"request": self._request(self.author)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        comment = serializer.save()

        self.assertEqual(comment.sender_id, self.author.id)
        self.assertEqual(comment.mentions[0]["user_id"], self.teammate.id)
        self.assertEqual(
            comment.linked_objects,
            [{"object_type": "action_item", "object_id": 1}],
        )

    def test_update_rejects_attachment_changes_and_allows_content_mentions(self):
        comment = MatrixComment.objects.create(
            target_type=MatrixComment.TARGET_SOLUTION_DEMAND,
            solution_demand=self.demand,
            sender=self.author,
            content="Original",
            attachments=[
                {
                    "url": "https://example.com/a.txt",
                    "file_name": "a.txt",
                    "mime_type": "text/plain",
                    "size": 10,
                    "extension": ".txt",
                }
            ],
            mentions=[],
        )

        reject_serializer = MatrixCommentSerializer(
            comment,
            data={
                "attachments": [
                    {
                        "url": "https://example.com/b.txt",
                        "file_name": "b.txt",
                        "mime_type": "text/plain",
                        "size": 11,
                        "extension": ".txt",
                    }
                ]
            },
            partial=True,
            context={"request": self._request(self.author)},
        )
        self.assertFalse(reject_serializer.is_valid())
        self.assertIn("attachments", reject_serializer.errors)

        with patch(
            "project_management.serializers.matrix.get_matrix_mention_user_snapshots",
            return_value={
                self.teammate.id: {
                    "user_id": self.teammate.id,
                    "name": "Team Mate",
                    "user_image": "",
                }
            },
        ), patch(
            "project_management.serializers.matrix.filter_accessible_linked_objects",
            side_effect=lambda objects, user: objects,
        ):
            update_serializer = MatrixCommentSerializer(
                comment,
                data={
                    "content": "Updated note",
                    "mentions": [{"id": self.teammate.id}],
                },
                partial=True,
                context={"request": self._request(self.author)},
            )
            self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
            updated = update_serializer.save()

        self.assertEqual(updated.content, "Updated note")
        self.assertEqual(updated.mentions[0]["user_id"], self.teammate.id)
        self.assertTrue(updated.is_edited)
        self.assertEqual(len(updated.attachments), 1)
        self.assertEqual(updated.attachments[0]["file_name"], "a.txt")

    def test_reply_must_target_top_level_comment_on_same_item(self):
        parent = MatrixComment.objects.create(
            target_type=MatrixComment.TARGET_SOLUTION_DEMAND,
            solution_demand=self.demand,
            sender=self.author,
            content="Parent",
        )
        nested = MatrixComment.objects.create(
            target_type=MatrixComment.TARGET_SOLUTION_DEMAND,
            solution_demand=self.demand,
            sender=self.author,
            content="Nested",
            replied_to=parent,
        )

        serializer = MatrixCommentSerializer(
            data={
                "target_type": MatrixComment.TARGET_SOLUTION_DEMAND,
                "solution_demand": self.demand.id,
                "content": "Invalid reply",
                "replied_to": nested.id,
            },
            context={"request": self._request(self.author)},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("replied_to", serializer.errors)

    @patch(
        "project_management.matrix_comment_mentions.has_matrix_target_view_access",
        return_value=True,
    )
    def test_project_mention_candidates_are_scoped_to_team_members(
        self,
        _mock_view_access,
    ):
        candidates = get_matrix_mention_candidates(
            target=self.demand,
            target_type=MatrixComment.TARGET_SOLUTION_DEMAND,
            search="Team",
            limit=20,
        )
        candidate_ids = {item["id"] for item in candidates}

        self.assertIn(self.teammate.id, candidate_ids)
        self.assertNotIn(self.outsider.id, candidate_ids)

        queryset_ids = set(
            get_matrix_mention_candidate_queryset(
                self.demand,
                MatrixComment.TARGET_SOLUTION_DEMAND,
            ).values_list("id", flat=True)
        )
        self.assertEqual(queryset_ids, {self.author.id, self.teammate.id})

    @patch(
        "project_management.matrix_comment_mentions.has_matrix_target_view_access",
        return_value=True,
    )
    def test_company_mention_candidates_are_scoped_to_company_users(
        self,
        _mock_view_access,
    ):
        queryset_ids = set(
            get_matrix_mention_candidate_queryset(
                self.available_part,
                MatrixComment.TARGET_AVAILABLE_PRODUCT_PART,
            ).values_list("id", flat=True)
        )
        self.assertEqual(queryset_ids, {self.author.id, self.teammate.id})
        self.assertNotIn(self.outsider.id, queryset_ids)

    def test_mention_candidates_endpoint_returns_results(self):
        from rest_framework.permissions import IsAuthenticated

        original_permissions = MatrixCommentViewSet.permission_classes
        MatrixCommentViewSet.permission_classes = [IsAuthenticated]
        self.addCleanup(
            setattr,
            MatrixCommentViewSet,
            "permission_classes",
            original_permissions,
        )

        view = MatrixCommentViewSet.as_view({"get": "mention_candidates"})
        request = self.factory.get(
            f"/api/project_management/solution-demands/{self.demand.id}/comments/mention-candidates/",
            {"search": "Team", "limit": 10},
        )
        force_authenticate(request, user=self.author)

        with patch.object(MatrixCommentViewSet, "_check_target_permission"), patch(
            "project_management.views.matrix.get_matrix_mention_candidates",
            return_value=[
                {
                    "id": self.teammate.id,
                    "name": "Team Mate",
                    "user_image": "",
                }
            ],
        ):
            response = view(
                request,
                target_type=MatrixComment.TARGET_SOLUTION_DEMAND,
                item_id=self.demand.id,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"][0]["id"], self.teammate.id)


class MilestonePermissionProjectContextTests(TestCase):
    def test_project_id_resolver_accepts_canonical_project_query_param(self):
        request = Mock(data={}, query_params={"project": "2010"})

        self.assertEqual(get_project_id_from_request(request), "2010")

    def test_project_id_resolver_accepts_legacy_project_id_query_param(self):
        request = Mock(data={}, query_params={"projectId": "2010"})

        self.assertEqual(get_project_id_from_request(request), "2010")

    def test_milestone_list_permission_uses_canonical_project_context(self):
        request = Mock(data={}, query_params={"project": "2010"})
        view = Mock(action="list")

        context = MilestonePermission()._get_context(
            request,
            view,
            "project",
        )

        self.assertEqual(context, {"project": "2010"})
