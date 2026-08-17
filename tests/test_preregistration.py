"""The preregistration is only meaningful if it is enforced.

It is committed before any live call. These tests fail if an input it pinned is
edited afterwards, which is what would let results be selected after the fact.
"""

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "evals" / "releases" / "v0.3.0" / "preregistration.json"


class PreregistrationTests(unittest.TestCase):
    def setUp(self):
        self.prereg = json.loads(PREREG.read_text())

    def test_every_pinned_input_still_hashes_to_its_registered_value(self):
        for relative, expected in self.prereg["input_sha256"].items():
            with self.subTest(path=relative):
                path = ROOT / relative
                self.assertTrue(path.is_file(), f"{relative} is gone")
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(
                    actual,
                    expected,
                    f"{relative} changed after preregistration; the run is no longer "
                    "described by its own contract and must be rerun",
                )

    def test_every_arm_policy_is_pinned(self):
        pinned = set(self.prereg["input_sha256"])
        for arm, spec in self.prereg["arms"].items():
            with self.subTest(arm=arm):
                if spec["policy"] is not None:
                    self.assertIn(spec["policy"], pinned)

    def test_both_descriptions_are_pinned(self):
        pinned = set(self.prereg["input_sha256"])
        for name, path in self.prereg["descriptions"].items():
            with self.subTest(description=name):
                self.assertIn(path, pinned)

    def test_gates_are_declared_before_the_run(self):
        names = [gate["name"] for gate in self.prereg["gates"]]
        self.assertEqual(len(names), len(set(names)))
        for required in (
            "implicit_recall",
            "precision",
            "protected_near_miss_false_positives",
            "median_output_reduction_vs_N",
            "bootstrap_95_lower_bound_vs_N",
            "median_output_reduction_vs_G",
        ):
            self.assertIn(required, names)
        for gate in self.prereg["gates"]:
            with self.subTest(gate=gate["name"]):
                self.assertIn(gate["op"], ("eq", "gte", "gt"))

    def test_the_control_arm_carries_no_policy(self):
        self.assertIsNone(self.prereg["arms"]["N"]["policy"])

    def test_a_generic_terse_control_is_registered(self):
        """Without this arm the benchmark cannot be falsified."""
        self.assertIsNotNone(self.prereg["arms"]["G"]["policy"])
        self.assertIn("B:G", self.prereg["comparisons_judged"])


if __name__ == "__main__":
    unittest.main()
