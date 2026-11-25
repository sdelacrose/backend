from datetime import date, datetime, timezone
from unittest.mock import patch
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from project_management.services.document_timeline_targets import forecast_targets
from project_management.views.client_surveillance import DocumentTimelineTargetsView
from project_management.test_transmittal import TransmittalTestBase, make_user
from project_management.models import DocumentTypeReviewDuration

DURATIONS = {"author_preparation_effective_days": 2, "internal_review_duration_effective_days": 3,
 "internal_approval_duration_effective_days": 4, "client_issue_duration_effective_days": 1,
 "client_review_duration_effective_days": 10, "supplier_review_duration_effective_days": 7}

class ForecastTests(SimpleTestCase):
    def calculate(self, **kwargs):
        values = dict(origin="INTERNAL", base=date(2026,11,1), durations=DURATIONS,
                      three_step=True, category="For review")
        values.update(kwargs)
        return forecast_targets(**values)

    def test_three_step_calendar_chain(self):
        self.assertEqual(self.calculate(), {"author":date(2026,11,3), "reviewer":date(2026,11,6),
            "approver":date(2026,11,10), "client_sent":date(2026,11,11), "client_review":date(2026,11,21)})

    def test_real_decisions_anchor_next_step_and_two_step_skips_review(self):
        result=self.calculate(three_step=False, decisions={"author":datetime(2026,11,5,12,tzinfo=timezone.utc)})
        self.assertNotIn("reviewer",result)
        self.assertEqual(result["approver"],date(2026,11,9))
        self.assertEqual(result["client_sent"],date(2026,11,10))
        self.assertEqual(self.calculate(decisions={"approver":date(2026,11,20)})["client_sent"],date(2026,11,21))

    def test_sent_information_and_received_client_do_not_fabricate_reviews(self):
        self.assertNotIn("client_sent",self.calculate(already_sent=True))
        self.assertNotIn("client_review",self.calculate(category="For information"))
        self.assertNotIn("client_sent",self.calculate(category="Not sent"))
        self.assertEqual(self.calculate(origin="CLIENT"),{})
        self.assertTrue(all(value is None for value in self.calculate(base=None).values()))

    def test_supplier_forecast_preserves_actual_frozen_deadline_and_real_closure(self):
        self.assertEqual(self.calculate(origin="SUPPLIER")["supplier_review"],date(2026,11,8))
        result=self.calculate(origin="SUPPLIER",supplier_exists=True,supplier_due=date(2026,11,12),supplier_closed=date(2026,11,15))
        self.assertEqual(result["supplier_review"],date(2026,11,12))
        self.assertEqual(result["client_sent"],date(2026,11,16))
        self.assertIsNone(self.calculate(origin="SUPPLIER",supplier_exists=True)["supplier_review"])
        self.assertIsNone(self.calculate(origin="SUPPLIER",supplier_information=True)["supplier_review"])

class TimelineAccessTests(TransmittalTestBase):
    def call(self, document, user):
        request=APIRequestFactory().get('/x/')
        if user: force_authenticate(request,user=user)
        with patch('ecpmpapp.permissions.LicensePermission.has_permission',return_value=True):
            return DocumentTimelineTargetsView.as_view()(request,document_id=document.pk)

    def test_document_scope_and_read_only_defaults(self):
        from models.models import Role, Permission, RolePermission, UserRole
        role=Role.objects.create(name='Timeline document access', company=self.company, type='solution', scope='company', is_active=True, created_by=self.owner)
        permission,_=Permission.objects.get_or_create(key='documents.full_access', defaults={'name':'Document access','module':'documents','description':'test'})
        RolePermission.objects.create(role=role,permission=permission)
        UserRole.objects.create(user=self.owner,role=role,company=self.company,assigned_by=self.owner)
        self.project.solution='Energy Consultant Solution'
        self.project.save(update_fields=['solution'])
        document=self.make_document()
        before=DocumentTypeReviewDuration.objects.count()
        response=self.call(document,self.owner)
        self.assertEqual(response.status_code,200)
        self.assertIn('author',response.data['targets'])
        self.assertEqual(DocumentTypeReviewDuration.objects.count(),before)
        other=make_user('unrelated-timeline@local.invalid')
        self.assertIn(self.call(document,other).status_code,(403,404))
        self.assertIn(self.call(document,None).status_code,(401,403))
