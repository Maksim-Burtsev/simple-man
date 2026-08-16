import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import check_eval_v2 as checker  # noqa: E402


class EvalV2GateTests(unittest.TestCase):
    def test_release_dry_run_is_credential_free_and_preregistered(self):
        with mock.patch(
            "run_eval_v2.subprocess.run",
            side_effect=AssertionError("dry-run must not start a subprocess"),
        ):
            result = checker.release_dry_run()

        self.assertEqual(result["planned_calls"], 275)
        self.assertEqual(result["hard_cap"], 280)
        self.assertEqual(result["records"], 275)
        self.assertFalse(result["holdout_content_present"])

    def test_gate_check_runs_real_main_chain_and_rejects_each_tamper_class(self):
        result = checker.run_gates()

        self.assertTrue(result["passed"])
        self.assertEqual(result["fake_answer_calls"], 84)
        self.assertEqual(result["fake_judgments"], 12)
        self.assertEqual(
            set(result["tamper_rejections"]),
            {"mapping", "run_raw", "run_result", "bundle", "judgment", "seal"},
        )
        self.assertTrue(result["reveal_before_seal_rejected"])
        self.assertTrue(result["public_bundle_blind"])
        self.assertEqual(result["coding_fixtures"], 3)


if __name__ == "__main__":
    unittest.main()
