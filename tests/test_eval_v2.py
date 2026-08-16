import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import eval_v2_lib as lib  # noqa: E402
import run_eval_v2 as runner  # noqa: E402


class EvalV2Tests(unittest.TestCase):
    def test_corpora_and_release_plan_match_preregistered_contract(self):
        output = lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")
        activation = lib.load_activation_cases(ROOT / "evals/cases/activation-dev.jsonl")
        plan = json.loads((ROOT / "evals/release-plan.json").read_text())

        self.assertEqual(len(output), 12)
        self.assertEqual(len({case["id"] for case in output}), 12)
        self.assertEqual({case["category"] for case in output}, {
            "status", "final", "review", "security", "setup", "diagnosis", "plan",
            "failed_validation", "destructive_risk", "detailed_override", "teaching_override",
            "creative_override",
        })
        self.assertEqual([case["id"] for case in output], [f"out-dev-{number:02}" for number in range(1, 13)])
        self.assertEqual(sum(case["language"] == "ru" for case in output), 5)
        self.assertEqual(len(activation), 20)
        self.assertEqual([case["id"] for case in activation], [f"act-dev-{number:02}" for number in range(1, 21)])
        self.assertEqual(sum(case["language"] == "ru" for case in activation), 7)
        self.assertEqual(
            {kind: sum(case["activation_class"] == kind for case in activation)
             for kind in ("explicit", "implicit", "negative")},
            {"explicit": 2, "implicit": 10, "negative": 8},
        )
        self.assertEqual(
            {kind: sum(case["protected_near_miss"] == kind for case in activation)
             for kind in ("detailed", "teaching", "creative")},
            {"detailed": 2, "teaching": 2, "creative": 2},
        )
        self.assertEqual(sum(plan["lanes"].values()), 275)
        self.assertEqual(plan["planned_calls"], 275)
        self.assertLessEqual(plan["planned_calls"], plan["hard_cap"])
        with self.assertRaises(ValueError):
            runner.validate_budget({"hard_cap": 280, "lanes": {"x": 281}, "planned_calls": 281})

    def test_public_mapping_is_opaque_balanced_and_has_no_arm_identity(self):
        cases = [
            {"id": "one", "category": "status", "language": "en", "prompt": "p", "verified_context": {}},
            {"id": "two", "category": "status", "language": "en", "prompt": "p", "verified_context": {}},
        ]
        runs = [
            {"case_id": case["id"], "arm": arm, "run_id": f"{case['id']}-{arm}",
             "commentary": "c", "final": "f"}
            for case in cases for arm in ("A", "B")
        ]
        mapping = lib.build_private_mapping(cases, runs, b"test-secret")
        bundle = lib.build_public_bundle(cases, runs, mapping)

        self.assertEqual(len(bundle["pairs"]), 2)
        self.assertNotIn('"arm"', lib.canonical_json(bundle))
        self.assertNotIn('"run_id"', lib.canonical_json(bundle))
        self.assertTrue(all(pair["pair_id"].startswith("pair_") for pair in bundle["pairs"]))
        lib.assert_public_safe(bundle)
        with self.assertRaises(ValueError):
            lib.assert_public_safe({"pairs": [{"response_A": {"final": "I prefer ARM A"}}]})

    def test_strict_judgment_and_critical_fact_checks_fail_closed(self):
        payload = lib.build_judge_payload({"prompt": "p", "verified_context": {}, "deliverable": "final"}, "left", "right")
        self.assertEqual(set(payload), {"untrusted_task", "left", "right"})
        judgment = {"quality": "left", "naturalness": "tie", "flags": {"left": [], "right": []}, "rationale": "observable"}
        self.assertEqual(lib.validate_judgment(json.dumps(judgment)), judgment)
        with self.assertRaises(ValueError):
            lib.validate_judgment(json.dumps({**judgment, "extra": True}))
        checks = lib.check_critical_facts(
            {"critical_facts": [{"id": "ok", "scope": "visible", "groups": [["84/84"], ["passed"]]}],
             "forbidden_claims": [{"id": "bad", "any_of": ["ready to merge"]}], "structure": {}},
            {"commentary": "", "final": "84/84 passed"},
        )
        self.assertEqual(checks, {"passed": True, "missing": [], "forbidden": [], "structure": []})
        self.assertFalse(lib.check_critical_facts(
            {"critical_facts": [], "forbidden_claims": [{"id": "bad", "any_of": ["ready to merge"]}], "structure": {}},
            {"commentary": "", "final": "Ready to merge"},
        )["passed"])

    def test_activation_and_paired_metrics_are_deterministic(self):
        cases = [
            {"id": "e", "activation_class": "explicit", "execution": "mechanical", "expected": "activate", "protected_near_miss": None},
            {"id": "i", "activation_class": "implicit", "execution": "routed", "expected": "activate", "protected_near_miss": None},
            {"id": "n", "activation_class": "negative", "execution": "routed", "expected": "do_not_activate", "protected_near_miss": "detailed"},
        ]
        metrics = lib.activation_confusion_matrix(cases, {"e": True, "i": True, "n": False})
        self.assertEqual((metrics["tp"], metrics["tn"], metrics["fp"], metrics["fn"]), (1, 1, 0, 0))
        self.assertEqual(metrics["explicit_accuracy"], 1.0)
        paired = lib.pair_measurements([
            {"case_id": "one", "trial": 1, "model": "m", "effort": "e", "arm": "A", "commentary_visible_tokens": 3, "final_visible_tokens": 5, "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 8, "latency_ms": 10},
            {"case_id": "one", "trial": 1, "model": "m", "effort": "e", "arm": "B", "commentary_visible_tokens": 1, "final_visible_tokens": 3, "input_tokens": 90, "cached_input_tokens": 60, "output_tokens": 4, "latency_ms": 8},
        ])
        self.assertEqual(paired[0]["A"]["visible_output_tokens"], 8)
        self.assertEqual(paired[0]["A"]["uncached_input_tokens"], 20)
        self.assertEqual(lib.clustered_bootstrap_ci(paired, "visible_output_tokens", seed=7, iterations=20), (-4.0, -4.0))
        self.assertEqual(lib.paired_summary(paired)["visible_output_tokens"]["relative_reduction"], 0.5)
        self.assertIn("estimated_session_net", lib.paired_summary(paired)["estimate"])

    def test_balanced_schedule_has_exact_ordinal_balance(self):
        cases = [{"id": f"case-{number}"} for number in range(12)]
        schedule = lib.balanced_schedule(cases, ("A", "B", "C", "generic"), "seed")
        self.assertEqual(len(schedule), 48)
        self.assertEqual(len({(row["case_id"], row["arm"]) for row in schedule}), 48)
        for arm in ("A", "B", "C", "generic"):
            self.assertEqual([sum(row["arm"] == arm and row["ordinal"] == ordinal for row in schedule) for ordinal in range(4)], [3, 3, 3, 3])

    def test_main_fake_chain_seals_before_reveal_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                runner.main(["reveal", "--root", str(root)])
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            runner.main(["judge", "--root", str(root), "--fake"])
            runner.main(["seal", "--root", str(root)])
            result = runner.main(["reveal", "--root", str(root)])
            self.assertEqual(result["status"], "revealed")
            mapping = root / "private/mapping.json"
            mapping.write_text(mapping.read_text().replace("pair_", "pair_x", 1))
            with self.assertRaises(ValueError):
                runner.main(["reveal", "--root", str(root)])

    def test_dry_run_is_pure_and_started_attempt_is_consumed(self):
        with mock.patch("run_eval_v2.subprocess.run", side_effect=AssertionError("no subprocess")):
            result = runner.main(["dry-run", "--plan", str(ROOT / "evals/release-plan.json")])
        self.assertEqual(result["planned_calls"], 275)
        with tempfile.TemporaryDirectory() as directory:
            attempt = Path(directory) / "attempt"
            runner.start_attempt(attempt, {"id": "opaque", "kind": "answer"})
            with self.assertRaises(FileExistsError):
                runner.start_attempt(attempt, {"id": "opaque", "kind": "answer"})
            self.assertEqual(runner.load_attempt(attempt)["status"], "started")

    def test_seal_is_terminal_and_judge_started_attempt_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            judge_attempt = root / "private/judge-attempt"
            runner.start_attempt(judge_attempt, {"kind": "judge"})
            with self.assertRaises(ValueError):
                runner.main(["judge", "--root", str(root), "--fake"])
            self.assertFalse((root / "private/judgments.jsonl").exists())
            (judge_attempt / "started.json").unlink()
            judge_attempt.rmdir()
            runner.main(["judge", "--root", str(root), "--fake"])
            runner.main(["seal", "--root", str(root)])
            for command in (["answers", "--root", str(root), "--fake", "--secret", "test-secret"], ["judge", "--root", str(root), "--fake"], ["seal", "--root", str(root)]):
                with self.assertRaises(ValueError):
                    runner.main(command)

    def test_strict_json_rejects_duplicate_keys_and_invalid_utf8_for_judgments_and_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            attempt = next((root / "private/attempts").iterdir())
            result = attempt / "result.json"
            result.write_text('{"schema_version":1,"schema_version":1,"status":"completed","result":{}}')
            with self.assertRaises(ValueError):
                runner.main(["judge", "--root", str(root), "--fake"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            runner.main(["judge", "--root", str(root), "--fake"])
            judgments = root / "private/judgments.jsonl"
            judgments.write_bytes(b'{"pair_id":"x","pair_id":"x","judgment":{}}\n\xff')
            with self.assertRaises(ValueError):
                runner.main(["seal", "--root", str(root)])


if __name__ == "__main__":
    unittest.main()
