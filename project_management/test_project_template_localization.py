from django.test import SimpleTestCase

from project_management.models.constants import (
    PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS,
    PROJECT_MILESTONE_ACTION_CONFIG,
    PROJECT_PHASE_TEMPLATE_TRANSLATIONS,
    SUPPORTED_PROJECT_TEMPLATE_LOCALES,
    get_default_project_phase_name,
    get_localized_project_template,
    normalize_project_template_locale,
    resolve_project_template_locale,
)


class ProjectTemplateLocalizationTests(SimpleTestCase):
    def test_every_generated_name_has_a_complete_translation(self):
        generated_phase_names = {
            phase["name"]
            for config in PROJECT_MILESTONE_ACTION_CONFIG.values()
            for phase in config["milestones"]
        }
        translated_phase_names = {
            translations["de"]
            for key, translations in PROJECT_PHASE_TEMPLATE_TRANSLATIONS.items()
            if key != "default.project_phase"
        }
        generated_action_names = {
            action_item["name"]
            for config in PROJECT_MILESTONE_ACTION_CONFIG.values()
            for phase in config["milestones"]
            for action_item in phase["action_items"]
        }
        translated_action_names = {
            translations["de"]
            for translations in PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS.values()
        }

        self.assertEqual(generated_phase_names, translated_phase_names)
        self.assertEqual(generated_action_names, translated_action_names)

        all_translations = (
            list(PROJECT_PHASE_TEMPLATE_TRANSLATIONS.values())
            + list(PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS.values())
        )
        for translations in all_translations:
            self.assertEqual(
                set(translations),
                set(SUPPORTED_PROJECT_TEMPLATE_LOCALES),
            )
            self.assertTrue(all(translations.values()))

    def test_localized_templates_preserve_structure_and_due_dates(self):
        for project_type, source_config in PROJECT_MILESTONE_ACTION_CONFIG.items():
            for locale in SUPPORTED_PROJECT_TEMPLATE_LOCALES:
                localized_config = get_localized_project_template(
                    project_type,
                    locale,
                )

                self.assertIsNotNone(localized_config)
                self.assertIsNot(localized_config, source_config)
                self.assertEqual(
                    len(localized_config["milestones"]),
                    len(source_config["milestones"]),
                )

                for source_phase, localized_phase in zip(
                    source_config["milestones"],
                    localized_config["milestones"],
                ):
                    phase_translations = PROJECT_PHASE_TEMPLATE_TRANSLATIONS[
                        localized_phase["template_key"]
                    ]
                    self.assertEqual(localized_phase["name"], phase_translations[locale])
                    self.assertEqual(
                        localized_phase["due_date"],
                        source_phase["due_date"],
                    )

                    for source_action, localized_action in zip(
                        source_phase["action_items"],
                        localized_phase["action_items"],
                    ):
                        action_translations = (
                            PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS[
                                localized_action["template_key"]
                            ]
                        )
                        self.assertEqual(
                            localized_action["name"],
                            action_translations[locale],
                        )
                        self.assertEqual(
                            localized_action["due_date"],
                            source_action["due_date"],
                        )

    def test_locale_aliases_and_fallback(self):
        aliases = {
            "de": "de",
            "de_DE": "de",
            "German": "de",
            "en": "en",
            "en-GB": "en",
            "English": "en",
            "fr": "fr",
            "fr_FR": "fr",
            "French": "fr",
            None: "en",
            "unsupported": "en",
        }

        for locale, expected in aliases.items():
            self.assertEqual(normalize_project_template_locale(locale), expected)

    def test_invalid_or_missing_locale_falls_back_to_user_preference(self):
        self.assertEqual(
            resolve_project_template_locale(None, "German"),
            "de",
        )
        self.assertEqual(
            resolve_project_template_locale("unsupported", "French"),
            "fr",
        )
        self.assertEqual(
            resolve_project_template_locale("unsupported", "unsupported"),
            "en",
        )
        self.assertEqual(
            resolve_project_template_locale("de", "French"),
            "de",
        )

    def test_default_phase_is_localized(self):
        self.assertEqual(get_default_project_phase_name("en"), "Default Project Phase")
        self.assertEqual(get_default_project_phase_name("de"), "Standardprojektphase")
        self.assertEqual(
            get_default_project_phase_name("fr"),
            "Phase de projet par défaut",
        )

    def test_missing_offer_translations_are_defined(self):
        self.assertEqual(
            PROJECT_PHASE_TEMPLATE_TRANSLATIONS["offer.inquiry"]["en"],
            "Inquiry",
        )
        self.assertEqual(
            PROJECT_PHASE_TEMPLATE_TRANSLATIONS["offer.inquiry"]["fr"],
            "Demande initiale",
        )
        self.assertEqual(
            PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS[
                "offer.create_and_send_offer"
            ]["fr"],
            "Offre créée et envoyée",
        )

    def test_unknown_project_type_has_no_template(self):
        self.assertIsNone(get_localized_project_template("Unknown", "en"))
