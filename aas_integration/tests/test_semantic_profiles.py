from django.test import SimpleTestCase, override_settings

from aas_integration.adapters import _with_semantic_profile

from aas_integration.semantic_profiles import (
    SemanticProfileDefinition,
    classify_semantic_profile,
)


class SemanticProfileContractTests(SimpleTestCase):
    def setUp(self):
        self.definition = SemanticProfileDefinition(
            key="fixture-profile",
            version="1.0",
            semantic_ids=("https://semantic.example.test/fixture/1.0",),
            family_prefixes=("https://semantic.example.test/fixture/",),
            required_element_semantic_ids=(
                "https://semantic.example.test/fixture/required-field",
            ),
        )

    def test_recognition_uses_exact_semantic_id_and_reports_missing_required_semantics(self):
        submodel = {
            "id": "urn:submodel:1",
            "id_short": "A translated title that must not drive matching",
            "semantic_ids": ["https://semantic.example.test/fixture/1.0"],
            "elements": [],
        }
        classified = classify_semantic_profile(submodel, (self.definition,))
        self.assertEqual(classified["semantic_profile"]["key"], "fixture-profile")
        self.assertEqual(classified["semantic_profile"]["status"], "invalid_required_semantics")
        self.assertEqual(
            classified["semantic_profile"]["missing_required_semantic_ids"],
            ["https://semantic.example.test/fixture/required-field"],
        )

    def test_unknown_and_title_only_submodels_keep_all_generic_data(self):
        submodel = {
            "id": "urn:submodel:unknown",
            "id_short": "Product Carbon Footprint",
            "display_name": [{"language": "de", "text": "CO2-Fußabdruck"}],
            "semantic_ids": ["https://semantic.example.test/unknown/1.0"],
            "elements": [{"id_short": "Nested", "model_type": "Collection", "children": []}],
        }
        classified = classify_semantic_profile(submodel, (self.definition,))
        self.assertEqual(classified["semantic_profile"]["status"], "generic")
        self.assertEqual(classified["elements"], submodel["elements"])
        self.assertEqual(classified["display_name"], submodel["display_name"])

    def test_known_family_with_unsupported_version_is_explicit(self):
        classified = classify_semantic_profile(
            {
                "id": "urn:submodel:2",
                "id_short": "Fixture",
                "semantic_ids": ["https://semantic.example.test/fixture/2.0"],
                "elements": [],
            },
            (self.definition,),
        )
        self.assertEqual(classified["semantic_profile"]["status"], "version_mismatch")
        self.assertEqual(classified["semantic_profile"]["supported_versions"], ["1.0"])

    def test_missing_semantic_id_is_explicit_and_generic(self):
        classified = classify_semantic_profile(
            {"id": "urn:submodel:3", "id_short": "No semantics", "semantic_ids": [], "elements": []},
            (self.definition,),
        )
        self.assertEqual(classified["semantic_profile"]["status"], "missing_semantic_id")

    @override_settings(AAS_SEMANTIC_PROFILE_UI_ENABLED=False)
    def test_adapter_metadata_is_default_off(self):
        submodel = {"id": "urn:submodel:flag", "semantic_ids": [], "elements": []}
        self.assertNotIn("semantic_profile", _with_semantic_profile(submodel))

    @override_settings(AAS_SEMANTIC_PROFILE_UI_ENABLED=True)
    def test_adapter_metadata_is_exposed_only_when_enabled(self):
        submodel = {"id": "urn:submodel:flag", "semantic_ids": [], "elements": []}
        self.assertEqual(
            _with_semantic_profile(submodel)["semantic_profile"]["status"],
            "missing_semantic_id",
        )
