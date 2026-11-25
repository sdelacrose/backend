from unittest.mock import patch
from django.db import transaction
from django.test import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from models.models import Notification, NotificationSeen
from notifications.serializers import NotificationSerializer
from supplier_portal.tests import SupplierRegisterTests
from supplier_portal.notifications import recipients, deliver_supplier_notification, schedule_supplier_notification
from supplier_portal.views import InternalTasks
from project_management.services.supplier_portal import submit_revision


@override_settings(SUPPLIER_REGISTER_ENABLED=True)
class SupplierNotificationTests(SupplierRegisterTests):
    def setUp(self):
        super().setUp()
        # Exercise event semantics separately from the real permission integration tests.
        self.receive_patch = patch('supplier_portal.notifications.can_receive', return_value=True)
        self.receive_patch.start()
        self.addCleanup(self.receive_patch.stop)

    def tasks(self, user):
        # License tables belong to the external licensing service, not this isolated schema.
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True):
            return self.call(InternalTasks, user, project_id=self.project.pk)

    def deliver(self, batch):
        with patch('supplier_portal.notifications.recipients', return_value=[self.creator]):
            deliver_supplier_notification('proposal', batch.pk)

    def test_batch_creates_one_actionable_inbox_entry(self):
        batch = self.propose([self.metadata('N-1'), self.metadata('N-2')])
        self.deliver(batch)
        notice = Notification.objects.get(content_type__app_label='supplier_portal')
        self.assertEqual(list(notice.user.values_list('pk', flat=True)), [self.creator.pk])
        self.assertTrue(NotificationSeen.objects.filter(user=self.creator, notification=notice).exists())
        self.assertEqual(NotificationSerializer(notice).data['target_url'], f'/supplier-register?project={self.project.pk}&batch={batch.pk}')

    def test_all_invalid_batch_creates_no_company_task_alert(self):
        batch = self.propose([{'file_name': ''}])
        self.deliver(batch)
        self.assertFalse(Notification.objects.filter(content_type__app_label='supplier_portal').exists())

    def test_transaction_rollback_never_delivers(self):
        with patch('supplier_portal.notifications.deliver_supplier_notification') as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        schedule_supplier_notification('proposal', 123)
                        raise ValueError('rollback')
                except ValueError:
                    pass
            deliver.assert_not_called()

    def test_read_state_does_not_resolve_pending_proposals(self):
        batch = self.propose()
        self.deliver(batch)
        NotificationSeen.objects.filter(user=self.creator).update(status='read')
        with patch('supplier_portal.views.assert_project_manager'):
            response = self.tasks(self.creator)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['pending_proposals'], 1)
        self.decide(batch)
        with patch('supplier_portal.views.assert_project_manager'):
            response = self.tasks(self.creator)
        self.assertEqual(response.data['pending_proposals'], 0)

    def test_supplier_cannot_read_internal_tasks(self):
        self.member()
        self.assertIn(self.tasks(self.supplier).status_code, [403,404])

    def test_new_file_creates_document_target_and_task(self):
        batch = self.propose()
        self.decide(batch)
        doc = batch.rows.get().document
        with patch('supplier_portal.notifications.recipients', return_value=[self.creator]), self.captureOnCommitCallbacks(execute=True):
            submission = submit_revision(user=self.supplier, document_id=doc.pk,
                uploaded_file=SimpleUploadedFile('supplier.pdf', b'new controlled content'), external_revision='A')
        notice = Notification.objects.get(content_type__model='supplierportalsubmission', object_id=submission.pk)
        self.assertEqual(NotificationSerializer(notice).data['target_url'], f'/company-documents/{doc.pk}')
        with patch('supplier_portal.views.assert_project_manager'):
            response = self.tasks(self.creator)
        self.assertIn(doc.pk, [row['id'] for row in response.data['documents']])

    def test_recipient_selection_excludes_external_and_inactive_accounts(self):
        self.other.company = self.creator.company
        self.other.is_active = False
        self.other.save()
        with patch('supplier_portal.notifications._explicit_role_right', return_value=True):
            selected = [u.pk for u in recipients(self.project)]
        self.assertIn(self.creator.pk, selected)
        self.assertNotIn(self.supplier.pk, selected)
        self.assertNotIn(self.other.pk, selected)

    def test_starting_review_resolves_file_task_but_keeps_notification(self):
        batch = self.propose()
        self.decide(batch)
        doc = batch.rows.get().document
        with patch('supplier_portal.notifications.recipients', return_value=[self.creator]), self.captureOnCommitCallbacks(execute=True):
            submission = submit_revision(user=self.supplier, document_id=doc.pk,
                uploaded_file=SimpleUploadedFile('review.pdf', b'ready for review'))
        from project_management.services.supplier_surveillance import create_supplier_surveillance
        create_supplier_surveillance(document_revision=submission.revision, actor=self.creator, template=self.make_template())
        with patch('supplier_portal.views.assert_project_manager'):
            tasks = self.tasks(self.creator)
        self.assertNotIn(doc.pk, [row['id'] for row in tasks.data['documents']])
        self.assertTrue(Notification.objects.filter(content_type__model='supplierportalsubmission', object_id=submission.pk).exists())

    def test_generic_internal_member_is_not_a_recipient(self):
        with patch('supplier_portal.notifications._explicit_role_right', return_value=False):
            selected = recipients(self.project)
        self.assertTrue(all(u.pk == self.project.company_profile.admin_user_id for u in selected))

    def test_missing_document_access_prevents_alert_even_with_cycle_right(self):
        batch = self.propose()
        with patch('supplier_portal.notifications.can_receive', return_value=False):
            self.deliver(batch)
        self.assertFalse(Notification.objects.filter(content_type__app_label='supplier_portal').exists())

