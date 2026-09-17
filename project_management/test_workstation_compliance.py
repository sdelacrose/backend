from django.test import TestCase, override_settings
from rest_framework.test import force_authenticate
from models.models import Role, Permission, RolePermission, UserRole

from project_management.models import Project, Requirement
from project_management.test_workstation_revisions import (
    WorkstationRevisionTests as Fixtures,
)
from project_management.views.workstation import ProjectWorkstationViewSet


@override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
class ComplianceAPITests(TestCase):
    def setUp(self):
        Fixtures.setUp(self)
        role = Role.objects.create(
            name="Compliance editor",
            company=self.company,
            type="solution",
            scope="company",
        )
        self.role = role
        permission, _ = Permission.objects.get_or_create(
            key="projects.full_access",
            defaults={"name": "Projects", "module": "projects"},
        )
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=self.user, role=role, company=self.company)
        self.requirement = Requirement.objects.create(
            project=self.project, name="Capacity at least 10"
        )
        self.workstation.source_requirement = self.requirement
        self.workstation.save(update_fields=["source_requirement"])
        self.node = self.workstation.nodes.get(parent=None)
        self.node.custom_fields = {"capacity": {"value": "12", "unit": "kg"}}
        self.node.save(update_fields=["custom_fields"])

    def call(self, method="get", data=None):
        request = getattr(self.factory, method)(
            "/compliance/", data or {}, format="json"
        )
        force_authenticate(request, user=self.user)
        return ProjectWorkstationViewSet.as_view({method: "compliance"})(
            request, pk=self.workstation.pk
        )

    def rule(self):
        return {
            "action": "create",
            "title": "Capacity",
            "requirement": self.requirement.pk,
            "occurrence": str(self.node.occurrence_id),
            "source": "snapshot",
            "property": "capacity",
            "value_type": "number",
            "operator": "gte",
            "expected": "10",
            "unit": "kg",
        }

    def test_engineer_can_create_evaluate_and_retrieve_numeric_rule(self):
        options = self.call(data={"occurrence": str(self.node.occurrence_id)})
        self.assertEqual(options.data["properties"]["snapshot"][0]["id"], "capacity")
        self.assertEqual(options.data["requirements"][0]["id"], self.requirement.pk)
        response = self.call("post", self.rule())
        self.assertEqual(response.status_code, 201, response.data)
        rule_id = response.data["id"]
        response = self.call(
            "post", {"action": "evaluate", "id": rule_id, "version": 1}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["result"]["outcome"], "meets")
        response = self.call()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["rules"][0]["result"]["actual"], "12")
        self.assertFalse(response.data["rules"][0]["result"]["stale"])

    def test_edit_and_archive_preserve_evaluation_history_and_check_versions(self):
        rule_id = self.call("post", self.rule()).data["id"]
        self.call("post", {"action": "evaluate", "id": rule_id, "version": 1})
        edit = {
            **self.rule(),
            "action": "update",
            "id": rule_id,
            "version": 1,
            "expected": "20",
        }
        updated = self.call("post", edit)
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["version"], 2)
        self.assertEqual(updated.data["editor"], self.user.pk)
        self.assertTrue(updated.data["result"]["stale"])
        self.assertEqual(self.call("post", edit).status_code, 409)
        result = self.call("post", {"action": "evaluate", "id": rule_id, "version": 2})
        self.assertEqual(result.data["result"]["outcome"], "does_not_meet")
        history = self.call().data["rules"][0]["history"]
        self.assertEqual(
            [item["outcome"] for item in history], ["does_not_meet", "meets"]
        )
        self.assertEqual(history[-1]["definition"]["expected"], "10")
        self.assertEqual(history[-1]["actual"], "12")
        self.assertEqual(history[-1]["unit"], "kg")
        archived = self.call("post", {"action": "archive", "id": rule_id, "version": 2})
        self.assertEqual(archived.status_code, 200, archived.data)
        self.assertTrue(archived.data["archived"])
        listing = self.call().data
        self.assertEqual(listing["rules"], [])
        self.assertEqual(len(listing["archived_rules"][0]["history"]), 2)

    def test_untyped_scalar_property_is_unknown_not_a_server_error(self):
        self.node.custom_fields = {"capacity": "12"}
        self.node.save(update_fields=["custom_fields"])
        rule_id = self.call("post", self.rule()).data["id"]
        response = self.call(
            "post", {"action": "evaluate", "id": rule_id, "version": 1}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["result"]["outcome"], "unknown")
        self.assertIn("explicit unit", response.data["result"]["reason"])

    def test_replaced_source_does_not_inherit_snapshot_check(self):
        created = self.call("post", self.rule()).data
        self.node.source_company_item = None
        self.node.source_project_item = None
        self.node.save(update_fields=["source_company_item", "source_project_item"])
        result = self.call(
            "post", {"action": "evaluate", "id": created["id"], "version": 1}
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["result"]["outcome"], "unknown")

    def test_declared_text_property_is_not_reinterpreted_as_number(self):
        self.node.custom_fields = {
            "capacity": {"value": "12", "unit": "kg", "value_type": "xs:string"}
        }
        self.node.save(update_fields=["custom_fields"])
        created = self.call("post", self.rule()).data
        result = self.call(
            "post", {"action": "evaluate", "id": created["id"], "version": 1}
        )
        self.assertEqual(result.data["result"]["outcome"], "unknown")

    def test_malformed_recorded_type_returns_unknown(self):
        self.node.custom_fields = {"capacity": {"value": "12", "unit": "kg", "value_type": []}}
        self.node.save(update_fields=["custom_fields"])
        created = self.call("post", self.rule()).data
        result = self.call("post", {"action": "evaluate", "id": created["id"], "version": 1})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["result"]["outcome"], "unknown")

    def test_numeric_operators_compare_exactly_without_tolerance(self):
        for operator, expected, upper, outcome in [
            ("eq", "12", None, "meets"),
            ("lte", "11", None, "does_not_meet"),
            ("range", "12", "13", "meets"),
            ("range", "1", "11.999", "does_not_meet"),
        ]:
            with self.subTest(operator=operator, expected=expected):
                payload = {**self.rule(), "operator": operator, "expected": expected}
                if upper is not None:
                    payload["upper"] = upper
                response = self.call("post", payload)
                self.assertEqual(response.status_code, 201, response.data)
                response = self.call(
                    "post",
                    {"action": "evaluate", "id": response.data["id"], "version": 1},
                )
                self.assertEqual(response.data["result"]["outcome"], outcome)

    def test_text_and_boolean_checks_are_exact_and_do_not_coerce_types(self):
        for kind, actual, expected, outcome in [
            ("text", "Steel", "Steel", "meets"),
            ("text", "Steel", "steel", "does_not_meet"),
            ("boolean", False, False, "meets"),
            ("boolean", "false", False, "unknown"),
        ]:
            with self.subTest(kind=kind, actual=actual):
                self.node.custom_fields = {"capacity": {"value": actual, "unit": ""}}
                self.node.save(update_fields=["custom_fields"])
                created = self.call(
                    "post",
                    {
                        **self.rule(),
                        "value_type": kind,
                        "operator": "eq",
                        "expected": expected,
                        "unit": "",
                    },
                )
                self.assertEqual(created.status_code, 201, created.data)
                result = self.call(
                    "post",
                    {"action": "evaluate", "id": created.data["id"], "version": 1},
                )
                self.assertEqual(result.data["result"]["outcome"], outcome)

    def test_invalid_rule_inputs_return_validation_errors(self):
        for change in [
            {"occurrence": "not-a-uuid"},
            {"title": []},
            {"expected": "NaN"},
            {"expected": True},
            {"operator": "range", "expected": "10", "upper": "9"},
            {"unit": None},
            {"property": ""},
        ]:
            with self.subTest(change=change):
                response = self.call("post", {**self.rule(), **change})
                self.assertEqual(response.status_code, 400, response.data)

    def test_malformed_identifiers_and_types_are_client_errors(self):
        for change in [
            {"requirement": "invalid"},
            {"requirement": []},
            {"value_type": []},
            {"operator": {}},
            {"action": []},
        ]:
            with self.subTest(change=change):
                self.assertEqual(
                    self.call("post", {**self.rule(), **change}).status_code, 400
                )

    def test_requirement_changes_make_previous_result_stale(self):
        rule_id = self.call("post", self.rule()).data["id"]
        self.call("post", {"action": "evaluate", "id": rule_id, "version": 1})
        self.requirement.name = "Capacity at least 20"
        self.requirement.save(update_fields=["name"])
        response = self.call()
        self.assertTrue(response.data["rules"][0]["result"]["stale"])

    def test_descendant_requirement_binding_becomes_unknown_when_moved_out(self):
        child = Requirement.objects.create(
            project=self.project, name="Payload", parent=self.requirement
        )
        created = self.call("post", {**self.rule(), "requirement": child.pk})
        self.assertEqual(created.status_code, 201, created.data)
        rule_id = created.data["id"]
        evaluated = self.call(
            "post", {"action": "evaluate", "id": rule_id, "version": 1}
        )
        self.assertEqual(evaluated.data["result"]["outcome"], "meets")
        child.parent = None
        child.save(update_fields=["parent"])
        self.assertTrue(self.call().data["rules"][0]["result"]["stale"])
        evaluated = self.call(
            "post", {"action": "evaluate", "id": rule_id, "version": 1}
        )
        self.assertEqual(evaluated.data["result"]["outcome"], "unknown")

    def test_company_viewer_can_read_but_cannot_create_rules(self):
        RolePermission.objects.filter(role=self.role).delete()
        permission, _ = Permission.objects.get_or_create(
            key="projects.view_all_access",
            defaults={"name": "View projects", "module": "projects"},
        )
        RolePermission.objects.create(role=self.role, permission=permission)
        self.assertEqual(self.call().status_code, 200)
        self.assertEqual(self.call("post", self.rule()).status_code, 403)

    def test_unknown_data_and_source_changes_never_remain_current_passes(self):
        rule_id = self.call("post", self.rule()).data["id"]
        self.call("post", {"action": "evaluate", "id": rule_id, "version": 1})
        for actual in [
            {"value": "12", "unit": "g"},
            {"value": "NaN", "unit": "kg"},
            {"value": "12"},
            None,
        ]:
            with self.subTest(actual=actual):
                self.node.custom_fields = {"capacity": actual}
                self.node.save(update_fields=["custom_fields"])
                response = self.call()
                self.assertTrue(response.data["rules"][0]["result"]["stale"])
                response = self.call(
                    "post", {"action": "evaluate", "id": rule_id, "version": 1}
                )
                self.assertEqual(response.data["result"]["outcome"], "unknown")

    def test_outdated_rule_version_is_rejected(self):
        rule_id = self.call("post", self.rule()).data["id"]
        response = self.call(
            "post", {"action": "evaluate", "id": rule_id, "version": 0}
        )
        self.assertEqual(response.status_code, 409)

    def test_rule_cannot_bind_a_requirement_from_another_project(self):
        other = Project.objects.create(
            project_name="Other project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        requirement = Requirement.objects.create(
            project=other, name="Private requirement"
        )
        response = self.call("post", {**self.rule(), "requirement": requirement.pk})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.call().data["rules"], [])

    def test_user_without_project_access_cannot_read_evidence(self):
        self.call("post", self.rule())
        RolePermission.objects.filter(role=self.role).delete()
        self.assertEqual(self.call().status_code, 404)

    @override_settings(AAS_ENABLED=True)
    def test_catalog_property_selection_obeys_aas_permissions_and_redacts_history(self):
        for key in ["aas_models.view", "company_assembly_catalog.full_access"]:
            permission, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": key.split(".")[0]}
            )
            RolePermission.objects.create(role=self.role, permission=permission)
        options = self.call(data={"occurrence": str(self.node.occurrence_id)})
        self.assertEqual(options.status_code, 200, options.data)
        properties = options.data["properties"]["aas"]
        name_property = next(
            item for item in properties if item["label"].endswith("/AssemblyName")
        )
        created = self.call(
            "post",
            {
                **self.rule(),
                "source": "aas",
                "property": name_property["id"],
                "value_type": "text",
                "operator": "eq",
                "expected": self.template.assembly_name,
                "unit": "",
            },
        )
        self.assertEqual(created.status_code, 201, created.data)
        evaluated = self.call(
            "post", {"action": "evaluate", "id": created.data["id"], "version": 1}
        )
        # The catalog does not record a unit. Never infer that absence means unitless.
        self.assertEqual(evaluated.data["result"]["outcome"], "unknown")
        self.assertEqual(
            evaluated.data["result"]["actual"], self.template.assembly_name
        )
        RolePermission.objects.filter(
            role=self.role, permission__key="aas_models.view"
        ).delete()
        hidden = self.call().data["rules"][0]
        self.assertIsNone(hidden["result"]["actual"])
        self.assertTrue(hidden["result"]["stale"])
        self.assertEqual(hidden["history"], [])
