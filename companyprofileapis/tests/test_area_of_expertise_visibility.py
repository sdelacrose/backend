from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework import serializers

from companyprofileapis.serializers.company import CompanyConfigSerializer
from models import constants
from models.hierarchical_configurations import (
    AREA_OF_EXPERTISE_KEY,
    get_default_nested_configuration,
)
from models.models import CompanyConfig
from companyprofileapis.services.hierarchical_configurations import (
    validate_area_of_expertise_visibility,
)


class AreaOfExpertiseVisibilityTests(SimpleTestCase):
    def setUp(self):
        self.configuration = SimpleNamespace(
            user_id=None,
            company_profile_id=104,
        )

    @patch(
        "companyprofileapis.services.hierarchical_configurations."
        "_contact_expertise_values_in_use"
    )
    def test_newly_hidden_contact_values_are_blocked(self, values_in_use):
        values_in_use.side_effect = lambda _config, field, _values: {
            "main_area_of_expertise": {"project-program-management"},
            "sub_area_of_expertise": {"program-management"},
        }[field]

        with self.assertRaises(serializers.ValidationError) as raised:
            validate_area_of_expertise_visibility(
                self.configuration,
                {},
                {},
                {
                    AREA_OF_EXPERTISE_KEY: {
                        "disabled_group_ids": ["project-program-management"],
                        "disabled_child_ids": ["program-management"],
                    }
                },
            )

        detail = raised.exception.detail["configuration_visibility"][
            AREA_OF_EXPERTISE_KEY
        ]
        self.assertEqual(str(detail["code"]), "configuration_value_in_use")
        self.assertEqual(
            [str(value) for value in detail["values"]],
            ["Program Management", "Project & Program Management"],
        )

    @patch(
        "companyprofileapis.services.hierarchical_configurations."
        "_contact_expertise_values_in_use"
    )
    def test_already_hidden_value_does_not_block_an_unrelated_save(
        self,
        values_in_use,
    ):
        visibility = {
            AREA_OF_EXPERTISE_KEY: {
                "disabled_child_ids": ["program-management"],
            }
        }

        validate_area_of_expertise_visibility(
            self.configuration,
            {},
            visibility,
            visibility,
        )

        values_in_use.assert_not_called()

    @patch(
        "companyprofileapis.serializers.company."
        "validate_area_of_expertise_visibility"
    )
    def test_company_config_update_runs_usage_validation(self, validate_usage):
        hierarchy = get_default_nested_configuration(AREA_OF_EXPERTISE_KEY)
        instance = CompanyConfig(
            contract_type=list(constants.GC_CONTRACT_TYPES),
            nested_configurations={AREA_OF_EXPERTISE_KEY: hierarchy},
            configuration_visibility={},
        )
        serializer = CompanyConfigSerializer(
            instance=instance,
            data={
                "configuration_visibility": {
                    AREA_OF_EXPERTISE_KEY: {
                        "disabled_group_ids": ["project-program-management"],
                    }
                }
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        validate_usage.assert_called_once()
