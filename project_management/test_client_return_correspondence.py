from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from project_management.models import Correspondence, ContactCompany, ClientSurveillanceReturn
from project_management.services.client_surveillance_return import eligible_surveillance_correspondences
from project_management.test_client_surveillance_return import ReturnBase


class SurveillanceCorrespondenceTests(ReturnBase):
    def correspondence(self, **overrides):
        values = dict(project=self.project, direction='IN', status='ISSUED', correspondence_type='Surveillance', sender_company=self.client_company, subject='Client review sheet', reference='SURV-001', created_by=self.owner)
        values.update(overrides)
        return Correspondence.objects.create(**values)

    def test_one_correspondence_can_be_associated_to_ten_document_revisions(self):
        correspondence = self.correspondence()
        revisions = [self.review() for _ in range(9)]
        self.issue_and_send([(revision, 'FOR_REVIEW') for revision in revisions])
        issues = [self.client_issue] + [revision.client_issues.get(client_company=self.client_company) for revision in revisions]
        for issue in issues:
            self.record(issue=issue, source_correspondence_id=correspondence.pk)
        self.assertEqual(correspondence.client_surveillance_returns.count(), 10)
        self.assertEqual(Correspondence.objects.filter(pk=correspondence.pk).count(), 1)
        self.assertEqual(sum(record.files.count() for record in correspondence.client_surveillance_returns.all()), 0)

    def test_duplicate_is_rejected_only_for_the_same_client_revision(self):
        correspondence = self.correspondence()
        self.record(source_correspondence_id=correspondence.pk)
        with self.assertRaises(ValidationError):
            self.record(source_correspondence_id=correspondence.pk)
        self.assertEqual(correspondence.client_surveillance_returns.count(), 1)

    def test_only_issued_in_correspondence_of_the_same_project_is_eligible_other_company_or_type_needs_confirmation(self):
        # SURVEILLANCE IN LOT A: project, IN direction and issued status stay hard limits; the sender company and the type
        # are no longer silent exclusions (they are ranked and confirmed explicitly -- see test_client_proof_selector).
        valid = self.correspondence()
        other_client = ContactCompany.objects.create(company_profile=self.company, company_name="Other client")
        for overrides in [dict(direction="OUT"), dict(status="DRAFT"), dict(project=self.other_project)]:
            invalid = self.correspondence(reference="", **overrides)
            self.assertFalse(eligible_surveillance_correspondences(self.client_issue).filter(pk=invalid.pk).exists())
            with self.assertRaises(ValidationError):
                self.record(source_correspondence_id=invalid.pk)
        for overrides in [dict(correspondence_type="Email"), dict(sender_company=other_client)]:
            candidate = self.correspondence(reference="", **overrides)
            self.assertTrue(eligible_surveillance_correspondences(self.client_issue).filter(pk=candidate.pk).exists())
            with self.assertRaises(ValidationError):  # but never without the explicit confirmation
                self.record(source_correspondence_id=candidate.pk)
        self.assertTrue(eligible_surveillance_correspondences(self.client_issue).filter(pk=valid.pk).exists())

    def test_permission_is_enforced_without_creating_a_return(self):
        correspondence = self.correspondence()
        with self.assertRaises(PermissionDenied):
            self.record(source_correspondence_id=correspondence.pk, can_view_correspondence=lambda _: False)
        response = self.post(self.manager, {'source_correspondence': correspondence.pk})
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(correspondence.client_surveillance_returns.exists())

    def test_available_sources_exclude_used_correspondence_and_link_is_retained(self):
        correspondence = self.correspondence()
        user = self.grant('surveillance.correspondence@links-local.invalid', self.project, ['documents.view', 'documents.update', 'documents.correspondence_view'])
        data = self.listing(user)
        self.assertEqual(data.status_code, 200, data.data)
        self.assertIn(correspondence.pk, [source['id'] for source in data.data['results'][0]['surveillance_correspondences']])
        recorded = self.post(user, {'source_correspondence': correspondence.pk})
        self.assertEqual(recorded.status_code, 201, recorded.data)
        self.assertEqual(recorded.data['source_correspondence']['id'], correspondence.pk)
        after = self.listing(user).data['results'][0]
        self.assertNotIn(correspondence.pk, [source['id'] for source in after['surveillance_correspondences']])
        self.assertEqual(after['returns'][0]['source_correspondence']['id'], correspondence.pk)

    def test_correction_preserves_source_without_reimporting(self):
        correspondence = self.correspondence()
        original = self.record(source_correspondence_id=correspondence.pk)
        corrected = self.record(supersedes_id=original.pk, correction_reason='Correct result')
        self.assertEqual(corrected.source_correspondence_id, correspondence.pk)
        with self.assertRaises(ProtectedError):
            correspondence.delete()
