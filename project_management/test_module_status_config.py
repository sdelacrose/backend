from copy import deepcopy

from django.test import SimpleTestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from project_management.models import ECPMPConfig
from project_management.models import constants
from project_management.serializers.ecpmp_config import ECPMPCONFIGSerializer
from models.hierarchical_configurations import (
    DOCUMENT_DISCIPLINES_KEY,
    get_default_nested_configurations,
)


MODULE_STATUS_FIELDS = (
    "demand_status_options",
    "requirement_status_options",
    "bom_status_options",
)


class ModuleStatusConfigTests(SimpleTestCase):
    def test_project_config_defaults_are_independent(self):
        first_config = ECPMPConfig()
        second_config = ECPMPConfig()

        first_config.demand_status_options.append("custom:#123456")

        self.assertNotIn(
            "custom:#123456",
            first_config.requirement_status_options,
        )
        self.assertNotIn("custom:#123456", first_config.bom_status_options)
        self.assertNotIn("custom:#123456", second_config.demand_status_options)

    def test_project_serializer_allows_independent_reordering(self):
        reordered = list(reversed(constants.ECPMP_DEMAND_STATUS_OPTIONS))
        serializer = ECPMPCONFIGSerializer(
            ECPMPConfig(),
            data={"demand_status_options": reordered},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["demand_status_options"],
            reordered,
        )

    def test_project_serializer_rejects_default_removal(self):
        serializer = ECPMPCONFIGSerializer(
            ECPMPConfig(),
            data={
                "bom_status_options": constants.ECPMP_BOM_STATUS_OPTIONS[:-1],
            },
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("bom_status_options", serializer.errors)

    def test_project_config_include_defaults_marks_module_statuses(self):
        request = Request(
            APIRequestFactory().get("/", {"include_defaults": "true"}),
        )
        config = ECPMPConfig()
        data = ECPMPCONFIGSerializer(
            config,
            context={"request": request},
        ).data

        for field_name in MODULE_STATUS_FIELDS:
            self.assertEqual(
                [item["value"] for item in data[field_name]],
                getattr(config, field_name),
            )
            self.assertTrue(
                all(item["is_default"] for item in data[field_name]),
            )

    def test_project_serializer_updates_nested_configuration(self):
        current = get_default_nested_configurations()
        updated = deepcopy(current[DOCUMENT_DISCIPLINES_KEY])
        other_group = next(
            group for group in updated["groups"] if group["id"] == "other"
        )
        other_group["children"].append(
            {
                "id": "custom-project-discipline",
                "label": "Project Custom Discipline",
                "code": "PCD",
            }
        )
        serializer = ECPMPCONFIGSerializer(
            ECPMPConfig(
                nested_configurations=current,
            ),
            data={
                "nested_configurations": {
                    DOCUMENT_DISCIPLINES_KEY: updated,
                }
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIn(
            "Project Custom Discipline:PCD",
            serializer.validated_data["document_disciplines"],
        )
        self.assertEqual(
            serializer.validated_data["nested_configurations"][
                DOCUMENT_DISCIPLINES_KEY
            ],
            updated,
        )
