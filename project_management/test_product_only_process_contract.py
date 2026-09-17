from types import SimpleNamespace

from django.test import SimpleTestCase

from project_management.services.process_requirements_extraction import (
    ProcessRequirementsExtractionError,
    validate_process_assembly_result,
)


class ProductOnlyProcessContractTests(SimpleTestCase):
    def test_initial_catalog_gap_remains_a_reviewable_result(self):
        response = {
            "schema_version": "1", "rows": [],
            "unresolved_items": ["Torque verification requires an approved catalog capability."],
            "warnings": [],
        }
        result = validate_process_assembly_result(response, SimpleNamespace(hint_only=False))
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["unresolved_items"], response["unresolved_items"])

    def test_unexplained_initial_empty_result_is_not_accepted(self):
        with self.assertRaises(ProcessRequirementsExtractionError):
            validate_process_assembly_result(
                {"schema_version": "1", "rows": [], "unresolved_items": [], "warnings": []},
                SimpleNamespace(hint_only=False),
            )
