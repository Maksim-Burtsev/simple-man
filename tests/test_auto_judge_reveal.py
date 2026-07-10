from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import reveal_auto_judge as reveal  # noqa: E402


class RevealReliabilityTests(unittest.TestCase):
    def test_clean_blind_result_passes(self) -> None:
        report = reveal.reliability_report(
            {
                "stable_rate": 23 / 24,
                "safety_category_stability": {"total": 4, "stable": 4, "unstable": 0},
            },
            min_stable_rate=0.9,
        )
        self.assertTrue(report["passed"])

    def test_low_stability_or_unstable_safety_fails(self) -> None:
        report = reveal.reliability_report(
            {
                "stable_rate": 20 / 24,
                "safety_category_stability": {"total": 4, "stable": 3, "unstable": 1},
            },
            min_stable_rate=0.9,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["failures"]), 2)

    def test_blind_bundle_match_checks_metadata_and_lengths(self) -> None:
        bundle = {
            "pairs": [
                {
                    "id": "pair-1",
                    "task_id": "task-1",
                    "category": "safety_test",
                    "language": "en",
                    "left": {"text": "Left answer"},
                    "right": {"text": "Right answer"},
                }
            ]
        }
        blind = {
            "pairs": [
                {
                    "pair_id": "pair-1",
                    "task_id": "task-1",
                    "category": "safety_test",
                    "language": "en",
                    "lengths": {
                        "left": {"chars": 11, "words": 2},
                        "right": {"chars": 12, "words": 2},
                    },
                }
            ]
        }
        reveal.verify_blind_matches_bundle(blind, bundle)
        blind["pairs"][0]["category"] = "neutral_test"
        with self.assertRaisesRegex(ValueError, "category differs"):
            reveal.verify_blind_matches_bundle(blind, bundle)


if __name__ == "__main__":
    unittest.main()
