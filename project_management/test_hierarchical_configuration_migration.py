from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from models.hierarchical_configurations import (
    AREA_OF_EXPERTISE_KEY,
    DOCUMENT_DISCIPLINES_KEY,
    get_default_nested_configuration,
)
from models.models import GlobalConfig
from project_management.services.hierarchical_configuration_migration import (
    build_discipline_lookup,
    build_document_discipline_hierarchy,
)


class HierarchicalConfigurationMigrationTests(TestCase):
    def test_legacy_defaults_map_and_custom_values_are_retained(self):
        hierarchy = build_document_discipline_hierarchy(
            get_default_nested_configuration(DOCUMENT_DISCIPLINES_KEY),
            ["Electrical:EL", "Hydraulic:HY", "Custom Discipline:CUS"],
        )
        lookup = build_discipline_lookup(hierarchy)

        self.assertEqual(
            lookup["electrical"][1]["label"],
            "Electrical Engineering",
        )
        self.assertEqual(lookup["hydraulic"][0]["label"], "Other")
        self.assertEqual(lookup["custom discipline"][1]["code"], "CUS")

    def test_previous_defaults_are_replaced_and_custom_hierarchy_is_retained(self):
        previous_hierarchy = {
            "groups": [
                {
                    "id": "engineering",
                    "label": "Engineering",
                    "children": [
                        {
                            "id": "civil-structural-engineering",
                            "label": "Civil & Structural Engineering",
                            "code": "CSE",
                        },
                        {
                            "id": "custom-engineering",
                            "label": "Custom Engineering",
                            "code": "CUS",
                        },
                        {
                            "id": "legacy-architectural",
                            "label": "Architectural",
                            "code": "AR",
                        },
                    ],
                },
                {
                    "id": "custom-group",
                    "label": "Custom Group",
                    "children": [
                        {
                            "id": "custom-child",
                            "label": "Custom Child",
                            "code": "CCH",
                        }
                    ],
                },
            ]
        }

        hierarchy = build_document_discipline_hierarchy(
            previous_hierarchy,
            ["Civil & Structural Engineering:CSE"],
            ["Civil & Structural Engineering"],
        )
        lookup = build_discipline_lookup(hierarchy)

        self.assertEqual(lookup["custom engineering"][0]["id"], "engineering")
        self.assertEqual(lookup["custom child"][0]["id"], "custom-group")
        self.assertEqual(
            lookup["civil & structural engineering"][0]["id"],
            "other",
        )
        self.assertNotIn(
            "Architectural",
            [
                child["label"]
                for group in hierarchy["groups"]
                for child in group["children"]
            ],
        )

    def test_unlinked_previous_default_is_not_reintroduced(self):
        hierarchy = build_document_discipline_hierarchy(
            {},
            ["Civil & Structural Engineering:CSE"],
        )

        self.assertNotIn(
            "civil & structural engineering",
            build_discipline_lookup(hierarchy),
        )

    def test_dry_run_does_not_write(self):
        config = GlobalConfig.objects.create(
            document_disciplines=["Electrical:EL", "Custom Discipline:CUS"]
        )

        call_command("migrate_hierarchical_configurations", "--dry-run", stdout=StringIO())

        config.refresh_from_db()
        self.assertEqual(config.nested_configurations, {})

    def test_apply_is_idempotent(self):
        config = GlobalConfig.objects.create(
            document_disciplines=["Electrical:EL", "Custom Discipline:CUS"]
        )

        call_command("migrate_hierarchical_configurations", "--apply", stdout=StringIO())
        config.refresh_from_db()
        first_nested = config.nested_configurations

        call_command("migrate_hierarchical_configurations", "--apply", stdout=StringIO())
        config.refresh_from_db()

        self.assertEqual(config.nested_configurations, first_nested)
        self.assertIn(AREA_OF_EXPERTISE_KEY, config.nested_configurations)
        custom_children = [
            child
            for group in config.nested_configurations[DOCUMENT_DISCIPLINES_KEY]["groups"]
            for child in group["children"]
            if child["label"] == "Custom Discipline"
        ]
        self.assertEqual(len(custom_children), 1)

    def test_migration_replaces_renamed_area_of_expertise_defaults(self):
        legacy_area = get_default_nested_configuration(AREA_OF_EXPERTISE_KEY)
        legacy_labels = {
            "expertise-change-management": "Change Management",
            "people-change-management": "Change Management",
        }
        for group in legacy_area["groups"]:
            for child in group["children"]:
                if child["id"] in legacy_labels:
                    child["label"] = legacy_labels[child["id"]]

        config = GlobalConfig.objects.create(
            nested_configurations={AREA_OF_EXPERTISE_KEY: legacy_area}
        )

        call_command("migrate_hierarchical_configurations", "--dry-run", stdout=StringIO())
        config.refresh_from_db()
        stored_area = config.nested_configurations[AREA_OF_EXPERTISE_KEY]
        self.assertEqual(
            next(
                child["label"]
                for group in stored_area["groups"]
                for child in group["children"]
                if child["id"] == "expertise-change-management"
            ),
            "Change Management",
        )

        call_command("migrate_hierarchical_configurations", "--apply", stdout=StringIO())
        config.refresh_from_db()
        migrated_area = config.nested_configurations[AREA_OF_EXPERTISE_KEY]
        migrated_labels = {
            child["id"]: child["label"]
            for group in migrated_area["groups"]
            for child in group["children"]
        }
        self.assertEqual(
            migrated_labels["expertise-change-management"],
            "Change Control Management",
        )
        self.assertEqual(
            migrated_labels["people-change-management"],
            "Organizational Change Management",
        )
