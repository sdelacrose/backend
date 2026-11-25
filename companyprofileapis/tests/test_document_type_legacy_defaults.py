from django.test import SimpleTestCase
from models import constants
from models.models import CompanyConfig
from project_management.models import ECPMPConfig
from companyprofileapis.serializers.company import CompanyConfigSerializer
from project_management.serializers.ecpmp_config import ECPMPCONFIGSerializer
from companyprofileapis.services.document_type_defaults import merge_missing_document_type_defaults
from rest_framework.exceptions import ValidationError

class LegacyDocumentTypeConfigurationTests(SimpleTestCase):
    def configs(self):
        return [(CompanyConfig,CompanyConfigSerializer,constants.GC_COMPANY_DOCUMENT_TYPES),(ECPMPConfig,ECPMPCONFIGSerializer,constants.GC_PROJECT_DOCUMENT_TYPES)]

    def test_adding_custom_type_preserves_stored_legacy_default_code_shown_by_editor(self):
        for model,serializer,defaults in self.configs():
            with self.subTest(model=model.__name__):
                legacy=defaults[0].split(':',1)[0]+':LEGACY'
                stored=[legacy,*defaults[1:]]
                shown=merge_missing_document_type_defaults(stored,defaults)
                self.assertNotIn(defaults[0],shown)
                config=model(document_types=stored)
                payload=[*shown,'Dedicated addition:GDT']
                self.assertEqual(serializer(config).validate_document_types(payload),payload)

    def test_a_new_arbitrary_code_cannot_replace_a_canonical_default(self):
        for model,serializer,defaults in self.configs():
            with self.subTest(model=model.__name__),self.assertRaises(ValidationError):
                serializer(model(document_types=list(defaults))).validate_document_types([defaults[0].split(':',1)[0]+':UNAPPROVED',*defaults[1:]])

    def test_stored_legacy_type_cannot_be_removed_or_changed_silently(self):
        for model,serializer,defaults in self.configs():
            legacy=defaults[0].split(':',1)[0]+':LEGACY'
            config=model(document_types=[legacy,*defaults[1:]])
            with self.subTest(model=model.__name__),self.assertRaises(ValidationError):
                serializer(config).validate_document_types([*defaults[1:],'Dedicated addition:GDT'])

    def test_missing_defaults_are_still_backfilled_without_changing_stored_list(self):
        stored=['Dedicated addition:GDT']
        for _,_,defaults in self.configs():
            result=merge_missing_document_type_defaults(stored,defaults)
            self.assertEqual(stored,['Dedicated addition:GDT'])
            self.assertTrue(all(x in result for x in defaults))
