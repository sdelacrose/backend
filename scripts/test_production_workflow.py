"""Static checks only: never execute workflow steps or contact deployment services.

Run: python -m unittest discover -s scripts -p test_production_workflow.py -v
Requires PyYAML, already declared in requirements.txt.
"""
import itertools
from pathlib import Path
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/push_workflow_prod.yml"


class ProductionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves GitHub's `on` key instead of treating it as YAML 1.1 True.
        cls.workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_only_manual_dispatch_can_trigger_production(self):
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})

    def test_confirmation_is_a_required_boolean_defaulting_to_false(self):
        field = self.workflow["on"]["workflow_dispatch"]["inputs"]["confirm_deploy"]
        self.assertEqual(field["type"], "boolean")
        self.assertEqual(field["required"], "true")
        self.assertEqual(field["default"], "false")

    def test_entire_job_is_gated_for_every_event_ref_and_confirmation(self):
        self.assertEqual(set(self.workflow["jobs"]), {"build_test"})
        gate = self.workflow["jobs"]["build_test"]["if"].strip()
        self.assertTrue(gate.startswith("${{") and gate.endswith("}}"))
        expression = gate[3:-2].strip().replace("&&", " and ")
        replacements = {
            "github.event_name": "event",
            "github.repository": "repository",
            "github.ref": "ref",
            "inputs.confirm_deploy": "confirmed",
            "true": "True",
        }
        for before, after in replacements.items():
            expression = expression.replace(before, after)
        expression = " ".join(expression.split())
        for event, repository, ref, confirmed in itertools.product(
            ("push", "pull_request", "workflow_dispatch"),
            ("sdelacrose/backend", "other/backend"),
            ("refs/heads/main", "refs/heads/stage", "refs/heads/fix/example", "refs/tags/main"),
            (False, True),
        ):
            with self.subTest(event=event, repository=repository, ref=ref, confirmed=confirmed):
                allowed = eval(expression, {"__builtins__": {}}, {
                    "event": event, "repository": repository, "ref": ref, "confirmed": confirmed,
                })
                self.assertEqual(allowed, event == "workflow_dispatch" and
                                 repository == "sdelacrose/backend" and
                                 ref == "refs/heads/main" and confirmed)


if __name__ == "__main__":
    unittest.main()
