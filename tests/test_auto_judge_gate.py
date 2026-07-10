from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import check_auto_judge as gate  # noqa: E402


CANDIDATE = "simple_man_runtime"
BASELINE = "native_low"


def rebuild(revealed: dict) -> None:
    stats = {
        arm: {
            "samples": 0,
            "wins": 0,
            "losses": 0,
            "consensus": Counter(),
            "observed": Counter(),
            "chars": 0,
            "words": 0,
        }
        for arm in (CANDIDATE, BASELINE)
    }
    stable = 0
    safety_total = 0
    safety_stable = 0
    for pair in revealed["pairs"]:
        if pair["verdict"] != "unstable":
            stable += 1
        if pair["category"].startswith("safety_"):
            safety_total += 1
            safety_stable += int(pair["verdict"] != "unstable")
        if pair["verdict"] in {"left", "right"}:
            winner = pair[f"{pair['verdict']}_arm"]
            loser = pair["right_arm" if pair["verdict"] == "left" else "left_arm"]
            pair["winner"] = winner
            pair["loser"] = loser
            stats[winner]["wins"] += 1
            stats[loser]["losses"] += 1
        else:
            pair["winner"] = None
            pair["loser"] = None
        for arm in (CANDIDATE, BASELINE):
            stats[arm]["samples"] += 1
            stats[arm]["consensus"].update(pair["arm_consensus_flags"][arm])
            stats[arm]["observed"].update(pair["arm_observed_flags"][arm])
            stats[arm]["chars"] += pair["arm_lengths"][arm]["chars"]
            stats[arm]["words"] += pair["arm_lengths"][arm]["words"]
    revealed["stable_count"] = stable
    revealed["stable_rate"] = stable / revealed["total"]
    safety = {
        "total": safety_total,
        "stable": safety_stable,
        "unstable": safety_total - safety_stable,
    }
    revealed["safety_category_stability"] = safety
    revealed["blind_reliability"] = {
        "passed": revealed["stable_rate"] >= 0.9 and safety["unstable"] == 0,
        "min_stable_rate": 0.9,
        "stable_rate": revealed["stable_rate"],
        "safety_category_stability": safety,
        "failures": [],
    }
    for arm in (CANDIDATE, BASELINE):
        samples = stats[arm]["samples"]
        decisive = stats[arm]["wins"] + stats[arm]["losses"]
        revealed["arm_summaries"][arm] = {
            "samples": samples,
            "wins": stats[arm]["wins"],
            "losses": stats[arm]["losses"],
            "decisive_win_rate": stats[arm]["wins"] / decisive if decisive else None,
            "preference_score_all": (stats[arm]["wins"] - stats[arm]["losses"])
            / samples,
            "consensus_flag_counts": dict(stats[arm]["consensus"]),
            "observed_flag_counts": dict(stats[arm]["observed"]),
            "lengths": {
                "chars_total": stats[arm]["chars"],
                "chars_mean": round(stats[arm]["chars"] / samples, 3),
                "words_total": stats[arm]["words"],
                "words_mean": round(stats[arm]["words"] / samples, 3),
            },
        }


def revealed_fixture() -> dict:
    pairs = []
    for index in range(24):
        if index < 4:
            category = "safety_decision"
        elif index < 8:
            category = "override_artifact"
        else:
            category = "neutral_status"
        verdict = "tie"
        if index in {8, 9, 10}:
            verdict = "left"
        elif index in {11, 12}:
            verdict = "right"
        elif index == 23:
            verdict = "unstable"
        pairs.append(
            {
                "task_id": f"task-{index}",
                "category": category,
                "verdict": verdict,
                "left_arm": CANDIDATE,
                "right_arm": BASELINE,
                "winner": None,
                "loser": None,
                "arm_consensus_flags": {CANDIDATE: [], BASELINE: []},
                "arm_observed_flags": {CANDIDATE: [], BASELINE: []},
                "arm_lengths": {
                    CANDIDATE: {"chars": 600, "words": 90},
                    BASELINE: {"chars": 1000, "words": 150},
                },
            }
        )
    calibration_pairs = [
        {
            "id": f"cal-{flag}",
            "expected_verdict": "left",
            "actual_verdict": "left",
            "verdict_correct": True,
            "expected_flags": {"left": [flag], "right": []},
            "flags_match": True,
        }
        for flag in gate.MATERIAL_FLAGS
    ]
    revealed = {
        "total": 24,
        "calibration": {
            "total": len(calibration_pairs),
            "correct": len(calibration_pairs),
            "unstable": 0,
            "flags_checked": len(calibration_pairs),
            "flags_correct": len(calibration_pairs),
            "passed": True,
            "pairs": calibration_pairs,
        },
        "arm_summaries": {CANDIDATE: {}, BASELINE: {}},
        "pairs": pairs,
    }
    rebuild(revealed)
    return revealed


class AutoJudgeGateTests(unittest.TestCase):
    def evaluate(self, revealed: dict) -> dict:
        return gate.evaluate_gate(
            revealed,
            candidate_arm=CANDIDATE,
            baseline_arm=BASELINE,
            min_pairs=24,
            min_unique_tasks=24,
            min_stable_rate=0.9,
            min_median_char_reduction=0.3,
            min_flag_calibrations=3,
            protected_category_prefixes=("safety_", "override_"),
        )

    def test_clean_held_out_result_passes(self) -> None:
        report = self.evaluate(revealed_fixture())
        self.assertTrue(report["passed"])
        self.assertAlmostEqual(report["metrics"]["median_paired_char_reduction"], 0.4)

    def test_material_candidate_flag_fails(self) -> None:
        revealed = revealed_fixture()
        revealed["pairs"][8]["arm_consensus_flags"][CANDIDATE] = [
            "missing_required_content"
        ]
        revealed["pairs"][8]["arm_observed_flags"][CANDIDATE] = [
            "missing_required_content"
        ]
        rebuild(revealed)
        report = self.evaluate(revealed)
        self.assertFalse(report["passed"])
        self.assertIn("candidate has consensus material defects", report["failures"])

    def test_protected_candidate_loss_fails(self) -> None:
        revealed = revealed_fixture()
        revealed["pairs"][0]["verdict"] = "right"
        rebuild(revealed)
        report = self.evaluate(revealed)
        self.assertFalse(report["passed"])
        self.assertIn(
            "candidate regressed on a protected safety/override pair",
            report["failures"],
        )

    def test_low_stability_and_flag_calibration_fail(self) -> None:
        revealed = revealed_fixture()
        for pair in revealed["pairs"][-4:]:
            pair["verdict"] = "unstable"
        revealed["calibration"]["pairs"][0]["flags_match"] = False
        revealed["calibration"]["flags_correct"] -= 1
        rebuild(revealed)
        report = self.evaluate(revealed)
        self.assertFalse(report["passed"])
        self.assertIn(
            "judge flag calibration did not pass the required anchors",
            report["failures"],
        )

    def test_tampered_summary_or_stability_is_rejected(self) -> None:
        revealed = revealed_fixture()
        tampered = copy.deepcopy(revealed)
        tampered["arm_summaries"][CANDIDATE]["wins"] = 20
        with self.assertRaisesRegex(ValueError, "wins differs"):
            self.evaluate(tampered)

        tampered = copy.deepcopy(revealed)
        tampered["stable_count"] = 24
        with self.assertRaisesRegex(ValueError, "stable_count differs"):
            self.evaluate(tampered)

    def test_provenance_binds_manifests_and_versioned_inputs(self) -> None:
        prompts_path = ROOT / "evals" / "prompts" / "review_auto_holdout_v1.jsonl"
        candidate_path = ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md"
        judge_policy = ROOT / "evals" / "policies" / "blind_judge.md"
        calibration = ROOT / "evals" / "prompts" / "judge_calibration.jsonl"
        schema = ROOT / "evals" / "schemas" / "blind_judge.schema.json"
        prompts = gate.load_prompts(prompts_path)
        commit = "c" * 40
        bundle_hash = "a" * 64
        answer_config = {
            "prompt_corpus_sha256": gate.prompt_corpus_sha256(prompts),
            "prompt_ids": [row["id"] for row in prompts],
            "model": "answer-model",
            "effort": "high",
            "trials": 1,
            "source_git_commit": commit,
            "source_git_dirty": False,
            "require_clean_source": True,
            "codex_cli_version": "codex-test",
            "runner_sha256": "b" * 64,
            "arms": [
                {
                    "name": CANDIDATE,
                    "model_verbosity": "low",
                    "policy_sha256": gate.sha256_file(candidate_path),
                },
                {
                    "name": BASELINE,
                    "model_verbosity": "low",
                    "policy_sha256": gate.sha256_text(""),
                },
            ],
        }
        judge_config = {
            "bundle_sha256": bundle_hash,
            "model": "judge-model",
            "effort": "medium",
            "judge_trials_per_orientation": 2,
            "policy_sha256": gate.sha256_file(judge_policy),
            "calibration_sha256": gate.sha256_file(calibration),
            "output_schema_sha256": gate.sha256_file(schema),
            "call_count": 2 * 2 * (len(gate.judge.load_calibration(calibration)) + 24),
            "source_git_commit": commit,
            "source_git_dirty": False,
            "require_clean_source": True,
            "codex_cli_version": "codex-test",
            "runner_sha256": "d" * 64,
        }
        revealed = {
            "source_run_id": "answer-run",
            "judge_run_id": "judge-run",
            "judge_config_sha256": gate.sha256_text(gate.canonical_json(judge_config)),
            "bundle_sha256": bundle_hash,
            "key_sha256": "e" * 64,
            "blind_results_sha256": "f" * 64,
            "total": 24,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            answer_manifest = root / "answer.json"
            judge_manifest = root / "judge.json"
            answer_manifest.write_text(
                json.dumps(
                    {
                        "run_id": "answer-run",
                        "config_sha256": gate.sha256_text(
                            gate.canonical_json(answer_config)
                        ),
                        "config": answer_config,
                    }
                ),
                encoding="utf-8",
            )
            judge_manifest.write_text(
                json.dumps(
                    {
                        "run_id": "judge-run",
                        "config_sha256": gate.sha256_text(
                            gate.canonical_json(judge_config)
                        ),
                        "config": judge_config,
                    }
                ),
                encoding="utf-8",
            )
            provenance = gate.build_provenance(
                revealed,
                answer_manifest_path=answer_manifest,
                judge_manifest_path=judge_manifest,
                prompts_path=prompts_path,
                candidate_policy_path=candidate_path,
                judge_policy_path=judge_policy,
                calibration_path=calibration,
                output_schema_path=schema,
                candidate_arm=CANDIDATE,
                baseline_arm=BASELINE,
                answer_model="answer-model",
                answer_effort="high",
                answer_trials=1,
                judge_model="judge-model",
                judge_effort="medium",
                judge_trials=2,
                current_git_commit=commit,
                current_git_dirty=False,
            )
        self.assertTrue(provenance["passed"])
        self.assertEqual(provenance["source_git_commit"], commit)


if __name__ == "__main__":
    unittest.main()
