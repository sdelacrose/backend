from project_management.test_correspondence_template import TemplateBase, BASE
from project_management.services.correspondence_template import is_document_distribution_template


class TransmittalTemplateAliasTests(TemplateBase):
    def test_existing_english_template_is_available_only_for_transmittals(self):
        template=self.make(name='Transmittal Template',language='EN')
        normal=self.make(name='Ordinary correspondence')
        foreign=self.make(company=self.foreign.company,name='Transmittal Template',language='EN')
        client=self.api(self.manager)
        distribution=client.get(BASE,{'usage':'transmittal','project':self.project.pk})
        self.assertEqual(distribution.status_code,200)
        self.assertIn(template.pk,[row['id'] for row in distribution.data])
        self.assertNotIn(normal.pk,[row['id'] for row in distribution.data])
        self.assertNotIn(foreign.pk,[row['id'] for row in distribution.data])
        ordinary=client.get(BASE,{'usage':'correspondence','project':self.project.pk})
        self.assertEqual(ordinary.status_code,200)
        self.assertNotIn(template.pk,[row['id'] for row in ordinary.data])
        self.assertIn(normal.pk,[row['id'] for row in ordinary.data])

    def test_alias_is_case_and_outer_whitespace_tolerant_without_matching_arbitrary_names(self):
        self.assertTrue(is_document_distribution_template('  TRANSMITTAL TEMPLATE  '))
        self.assertFalse(is_document_distribution_template('Template'))
        self.assertFalse(is_document_distribution_template('Demande d’information'))
