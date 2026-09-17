import importlib

from django.test import SimpleTestCase


class ProductionReleaseGateTests(SimpleTestCase):
    def test_production_settings_disable_engineering_preview_features(self):
        production = importlib.import_module("ecpmpapp.settings.production")
        for name in (
            "PROJECT_WORKSTATION_COMPOSITION_ENABLED",
            "PROCESS_ASSEMBLY_BOM_ENABLED",
            "PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED",
            "AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED",
            "AI_ACCEPTANCE_ACTION_ITEMS_ENABLED",
            "AAS_ADVANCED_EDITOR_ENABLED",
            "AAS_EXTERNAL_WRITE_ENABLED",
            "AAS_SEMANTIC_PROFILE_UI_ENABLED",
        ):
            with self.subTest(feature=name):
                self.assertFalse(getattr(production, name))
