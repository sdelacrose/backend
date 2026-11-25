from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError
from companyprofileapis.serializers.company import CompanyConfigSerializer

class DocumentaryConfigurationPermissionTests(SimpleTestCase):
    def setUp(self):
        self.company=SimpleNamespace(pk=7)
        self.user=SimpleNamespace(is_freelancer=False,company=self.company,groups=MagicMock())
        self.user.groups.filter.return_value.exists.return_value=False
        self.instance=SimpleNamespace(company_profile=self.company)
        self.serializer=CompanyConfigSerializer(context={'request':SimpleNamespace(user=self.user)})

    def update(self,data,allowed=True):
        with patch('ecpmpapp.utils.require_any_permission_multiple_modules',return_value=allowed), patch('rest_framework.serializers.ModelSerializer.update',return_value=self.instance) as save:
            result=self.serializer.update(self.instance,data)
            self.assertIs(result,self.instance)
            save.assert_called_once_with(self.instance,data)

    def test_document_types_update_with_explicit_role_does_not_require_legacy_admin_group(self):
        self.update({'document_types':['Custom:CT']})

    def test_documentary_settings_only_are_supported(self):
        for field in ['document_status','document_categories','document_internal_review_status','document_external_review_status','transmittal_category','document_control_configuration']:
            with self.subTest(field=field): self.update({field:[]})

    def test_discipline_nested_update_is_documentary(self):
        self.update({'nested_configurations':{'document_disciplines':{}}})

    def test_without_explicit_configuration_permission_is_refused(self):
        with self.assertRaises(ValidationError): self.update({'document_types':[]},allowed=False)

    def test_other_company_is_refused_even_with_permission(self):
        self.instance.company_profile=SimpleNamespace(pk=8)
        with self.assertRaises(ValidationError): self.update({'document_types':[]})

    def test_non_documentary_and_mixed_payloads_keep_existing_admin_rule(self):
        for data in [{'rooms':[]},{'leave_types':[]},{'document_types':[],'rooms':[]},{'nested_configurations':{'skills':{}}},{'nested_configurations':{'document_disciplines':{},'skills':{}}},{}, {'company_profile':7}]:
            with self.subTest(data=data),self.assertRaises(ValidationError): self.update(data)

    def test_legacy_admin_behaviour_is_preserved(self):
        self.user.groups.filter.return_value.exists.return_value=True
        self.update({'rooms':[]},allowed=False)
