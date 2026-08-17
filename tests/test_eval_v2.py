import copy
import json
import hashlib
import os
import shutil
import stat
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
             "trial": 1, "model": "m", "effort": "e", "cli": "cli",
             "commentary": "c", "final": "f"}
            for case in cases for arm in ("A", "B")
        ]
        mapping = lib.build_private_mapping(
            cases,
            runs,
            b"test-secret",
            comparisons=[{
                "comparison_id": "baseline-candidate",
                "baseline_arm": "A",
                "candidate_arm": "B",
                "run_selectors": [
                    {"case_id": case_id, "trial": 1, "model": "m", "effort": "e", "cli": "cli"}
                    for case_id in ("one", "two")
                ],
            }],
        )
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
            {"case_id": "one", "trial": 1, "model": "m", "effort": "e", "cli": "cli", "arm": "A", "commentary_visible_tokens": 3, "final_visible_tokens": 5, "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 8, "latency_ms": 10},
            {"case_id": "one", "trial": 1, "model": "m", "effort": "e", "cli": "cli", "arm": "B", "commentary_visible_tokens": 1, "final_visible_tokens": 3, "input_tokens": 90, "cached_input_tokens": 60, "output_tokens": 4, "latency_ms": 8},
        ])
        self.assertEqual(paired[0]["A"]["visible_output_tokens"], 8)
        self.assertEqual(paired[0]["A"]["uncached_input_tokens"], 20)
        self.assertEqual(lib.clustered_bootstrap_ci(paired, "visible_output_tokens", seed=7, iterations=20), (0.5, 0.5))
        self.assertEqual(lib.paired_summary(paired)["visible_output_tokens"]["relative_reduction"], 0.5)
        self.assertIn("estimated_session_net", lib.paired_summary(paired)["estimate"])

    def test_arbitrary_comparisons_cover_b_vs_c_and_winner_vs_baselines(self):
        cases = [
            {"id": case_id, "category": "status", "language": "en", "prompt": "p", "verified_context": {}}
            for case_id in ("one", "two")
        ]
        runs = [
            {
                "case_id": case["id"], "cluster_id": case["id"], "trial": 1,
                "model": "gpt-5.6-sol", "effort": "high", "cli": "codex-cli 0.145.0", "arm": arm,
                "run_id": f"{case['id']}-{arm}", "commentary": "", "final": arm,
                "commentary_visible_tokens": 0, "final_visible_tokens": tokens,
                "input_tokens": 100, "cached_input_tokens": 80,
                "output_tokens": tokens, "latency_ms": 1,
            }
            for case in cases
            for arm, tokens in (("A", 10), ("B", 8), ("C", 6), ("generic", 7))
        ]
        comparisons = [{
            "comparison_id": "naturalness-b-c",
            "baseline_arm": "B",
            "candidate_arm": "C",
            "run_selectors": [
                {"case_id": case_id, "trial": 1, "model": "gpt-5.6-sol", "effort": "high", "cli": "codex-cli 0.145.0"}
                for case_id in ("one", "two")
            ],
        }]
        mapping = lib.build_private_mapping(cases, runs, "secret", comparisons=comparisons)
        mapped_arms = {
            side["arm"]
            for pair in mapping["pairs"].values()
            for side in (pair["left"], pair["right"])
        }
        self.assertEqual(mapped_arms, {"B", "C"})

        b_vs_c = lib.pair_measurements(runs, baseline_arm="B", candidate_arm="C")
        c_vs_a = lib.pair_measurements(runs, baseline_arm="A", candidate_arm="C")
        c_vs_generic = lib.pair_measurements(runs, baseline_arm="generic", candidate_arm="C")
        self.assertEqual(lib.paired_summary(b_vs_c)["visible_output_tokens"]["relative_reduction"], 0.25)
        self.assertEqual(lib.paired_summary(c_vs_a)["visible_output_tokens"]["relative_reduction"], 0.4)
        self.assertEqual(
            lib.clustered_bootstrap_ci(c_vs_generic, "visible_output_tokens", seed=1, iterations=10),
            (1 / 7, 1 / 7),
        )

    def test_judge_identities_use_registered_comparison_slots_not_bundle_order(self):
        plan = runner.build_plan(ROOT / "evals/release-plan.json")
        bundle = {"pairs": [
            {"pair_id": "pair_primary"},
            {"pair_id": "pair_dev"},
            {"pair_id": "pair_metrics_only"},
        ]}
        mapping = {"pairs": {
            "pair_primary": {"comparison_id": "primary-winner-a"},
            "pair_dev": {"comparison_id": "dev-naturalness-b-c"},
            "pair_metrics_only": {"comparison_id": "primary-winner-generic"},
        }}
        records = runner._judge_records(plan)
        bundle_sha256 = runner._bundle_sha256(bundle)
        self.assertEqual(
            runner._judge_identity(bundle["pairs"][0], bundle_sha256, plan, bundle["pairs"], mapping)["call_id"],
            records[12]["call_id"],
        )
        self.assertEqual(
            runner._judge_identity(bundle["pairs"][1], bundle_sha256, plan, bundle["pairs"], mapping)["call_id"],
            records[0]["call_id"],
        )
        with self.assertRaises(ValueError):
            runner._judge_identity(bundle["pairs"][2], bundle_sha256, plan, bundle["pairs"], mapping)

    def test_comparison_identity_separates_same_case_by_trial_model_effort_and_cli(self):
        case = {"id": "same", "category": "status", "language": "en", "prompt": "p", "verified_context": {}}
        selectors = [
            {"case_id": "same", "trial": 1, "model": "gpt-5.6-sol", "effort": "high", "cli": cli}
            for cli in ("codex-cli 0.145.0", "codex-cli 0.146.0")
        ]
        runs = [
            {**selector, "cluster_id": "same", "arm": arm, "run_id": f"{selector['cli']}-{arm}",
             "commentary": "", "final": arm, "commentary_visible_tokens": 0,
             "final_visible_tokens": 1, "input_tokens": 10, "cached_input_tokens": 5,
             "output_tokens": 1, "latency_ms": 1}
            for selector in selectors for arm in ("B", "C")
        ]
        mapping = lib.build_private_mapping(
            [case], runs, "secret",
            comparisons=[{"comparison_id": "b-c", "baseline_arm": "B", "candidate_arm": "C", "run_selectors": selectors}],
        )
        self.assertEqual(len(mapping["pairs"]), 2)
        pairs = lib.pair_measurements(runs, baseline_arm="B", candidate_arm="C")
        self.assertEqual({pair["cli"] for pair in pairs}, {"codex-cli 0.145.0", "codex-cli 0.146.0"})

    def test_side_balance_is_independent_inside_every_comparison(self):
        cases = [
            {"id": f"case-{number:02}", "category": "status", "language": "en", "prompt": "p", "verified_context": {}}
            for number in range(15)
        ]
        selector = lambda case: {"case_id": case["id"], "trial": 1, "model": "m", "effort": "e", "cli": "cli"}
        runs = [
            {**selector(case), "arm": arm, "run_id": f"{case['id']}-{arm}", "commentary": "", "final": arm}
            for case in cases for arm in ("A", "B", "C", "generic")
        ]
        comparisons = [
            {"comparison_id": "twelve", "baseline_arm": "B", "candidate_arm": "C", "run_selectors": [selector(case) for case in cases[:12]]},
            {"comparison_id": "fifteen", "baseline_arm": "A", "candidate_arm": "B", "run_selectors": [selector(case) for case in cases]},
            {"comparison_id": "one", "baseline_arm": "generic", "candidate_arm": "C", "run_selectors": [selector(cases[0])]},
        ]
        singleton_positions = set()
        for secret in tuple(f"balance-{number}" for number in range(1, 17)):
            mapping = lib.build_private_mapping(cases, runs, secret, comparisons=comparisons)
            for comparison in comparisons:
                pairs = [pair for pair in mapping["pairs"].values() if pair["comparison_id"] == comparison["comparison_id"]]
                baseline_left = sum(pair["left"]["arm"] == comparison["baseline_arm"] for pair in pairs)
                self.assertLessEqual(abs(baseline_left - (len(pairs) - baseline_left)), 1)
                if comparison["comparison_id"] == "one":
                    singleton_positions.add(bool(baseline_left))
        self.assertEqual(singleton_positions, {False, True})

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
            runner.start_attempt(attempt, {"call_id": "call_opaque", "id": "opaque", "kind": "answer"})
            with self.assertRaises(FileExistsError):
                runner.start_attempt(attempt, {"call_id": "call_opaque", "id": "opaque", "kind": "answer"})
            self.assertEqual(runner.load_attempt(attempt)["status"], "started")

    def test_seal_is_terminal_and_judge_started_attempt_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            judge_attempt = root / "private/judge-attempt"
            runner.start_attempt(judge_attempt, {"call_id": "call_unexpected", "kind": "judge"})
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

    def test_bundle_commits_mapping_before_judge_and_rejects_arm_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            bundle = json.loads((root / "public/bundle.json").read_text())
            self.assertIn("mapping_commitment", bundle)
            mapping_path = root / "private/mapping.json"
            mapping = json.loads(mapping_path.read_text())
            pair = next(iter(mapping["pairs"].values()))
            pair["left"]["arm"], pair["right"]["arm"] = pair["right"]["arm"], pair["left"]["arm"]
            mapping_path.write_text(json.dumps(mapping))
            with self.assertRaises(ValueError):
                runner.main(["judge", "--root", str(root), "--fake"])

    def test_raw_answer_and_judgment_results_are_reconstructed_before_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            result = next((root / "private/attempts").iterdir()) / "result.json"
            value = json.loads(result.read_text())
            value["result"]["final"] = "forged"
            result.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                runner.main(["judge", "--root", str(root), "--fake"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            runner.main(["judge", "--root", str(root), "--fake"])
            result = next((root / "private/judge-attempts").iterdir()) / "result.json"
            value = json.loads(result.read_text())
            value["result"]["judgment"]["quality"] = "left"
            result.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                runner.main(["seal", "--root", str(root)])

    def test_attempt_inventories_reject_extra_files_and_started_judge_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            attempt = next((root / "private/attempts").iterdir())
            (attempt / "extra.json").write_text("{}")
            with self.assertRaises(ValueError):
                runner.main(["judge", "--root", str(root), "--fake"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            runner.start_attempt(root / "private/judge-attempts" / "unexpected", {"call_id": "call_unexpected", "kind": "judge"})
            with self.assertRaises(ValueError):
                runner.main(["judge", "--root", str(root), "--fake"])

    def test_committed_schedule_executes_balanced_sides_and_rejects_extra_attempt(self):
        cases = lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")
        sides = lib.balanced_sides(cases, "test-secret")
        self.assertLessEqual(abs(sum(sides.values()) - (len(sides) - sum(sides.values()))), 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            schedule = json.loads((root / "private/schedule.json").read_text())
            self.assertEqual(len(schedule["calls"]), 84)
            self.assertEqual(len(list((root / "private/attempts").glob("*"))), 84)
            self.assertEqual(sum(call["kind"] == "output" for call in schedule["calls"]), 48)
            self.assertEqual(sum(call["kind"] == "activation" for call in schedule["calls"]), 36)
            (root / "private/attempts/extra").mkdir()
            with self.assertRaises(ValueError):
                runner.main(["judge", "--root", str(root), "--fake"])

    def test_private_artifacts_are_no_follow_private_and_root_is_not_source(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            os.symlink(outside, root / "private")
            with self.assertRaises(ValueError):
                runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            self.assertEqual(stat.S_IMODE((root / "private").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "private/mapping.json").stat().st_mode), 0o600)
        with self.assertRaises(ValueError):
            runner.main(["answers", "--root", str(ROOT / "eval-v2-output"), "--fake", "--secret", "test-secret"])

    def test_artifact_writes_handle_short_writes_and_zero_progress(self):
        real_write = os.write

        def short_write(descriptor, data):
            return real_write(descriptor, data[: max(1, len(data) // 3)])

        for exclusive in (False, True):
            with self.subTest(exclusive=exclusive), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "artifact.json"
                with mock.patch.object(runner.os, "write", side_effect=short_write):
                    runner._write_json(
                        path,
                        {"schema_version": 1, "payload": "x" * 128},
                        private=True,
                        exclusive=exclusive,
                    )
                self.assertEqual(
                    runner._read_json(path, private=True),
                    {"schema_version": 1, "payload": "x" * 128},
                )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempt"
            with mock.patch.object(runner.os, "write", return_value=0):
                with self.assertRaises(OSError):
                    runner.start_attempt(path, {"call_id": "call-zero-write"})

    def test_public_leak_scan_uses_dynamic_identifiers_secrets_and_paths(self):
        with self.assertRaises(ValueError):
            lib.assert_public_safe(
                {"pairs": [{"response_A": {"final": "ARM-A Bearer live_token api_key=live_token /Users/name/repo call_private_123"}}]},
                arm_aliases={"A", "B"}, private_ids={"call_private_123"}, protected_roots={Path("/Users/name")},
            )

    def test_public_leak_scan_rejects_contextual_aliases_in_values_and_bundles(self):
        aliases = {"A", "B", "C"}
        for value in ("candidate B", "policy C", "winner B", "baseline A", "control C", "runner-up B"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lib.assert_public_safe({"note": value}, arm_aliases=aliases)
        unsafe_keys = (
            "candidate B",
            "policy C",
            "/Users/name/private/repo",
            r"C:\Users\name\auth.json",
            "api_key=secret",
            "password",
            "api_key",
            "access_token",
            "token",
            "credentials",
            "OPENAI_API_KEY",
            "SIMPLE_MAN_PASSWORD",
            "github_token",
        )
        for key in unsafe_keys:
            with self.subTest(key=key), self.assertRaises(ValueError):
                lib.assert_public_safe({key: "value"}, arm_aliases=aliases)
        lib.assert_public_safe(
            {"note": "The candidate before policy review remains under baseline controls."},
            arm_aliases=aliases,
        )

        cases = [{"id": "one", "category": "status", "language": "en", "prompt": "p", "verified_context": {}}]
        selector = {"case_id": "one", "trial": 1, "model": "m", "effort": "e", "cli": "cli"}
        runs = [
            {**selector, "arm": arm, "run_id": f"one-{arm}", "commentary": "", "final": arm}
            for arm in ("A", "B")
        ]
        mapping = lib.build_private_mapping(
            cases,
            runs,
            "context-test",
            comparisons=[{
                "comparison_id": "context",
                "baseline_arm": "A",
                "candidate_arm": "B",
                "run_selectors": [selector],
            }],
        )
        bundle = lib.build_public_bundle(cases, runs, mapping)
        bundle["pairs"][0]["prompt"] = "candidate B"
        with self.assertRaises(ValueError):
            lib.assert_public_safe(bundle, arm_aliases=aliases)

    def test_public_leak_scan_rejects_absolute_paths_but_allows_repo_relative_locations(self):
        absolute_paths = (
            "/etc/passwd",
            "config: /opt/simple-man/config.json",
            "/Volumes/evals/run.json",
            r"C:\Users\name\.codex\auth.json",
            "D:/evals/private.json",
            r"\\server\share\evidence.json",
            "//server/share/evidence.json",
            "file:///etc/passwd",
            "file:///C:/Users/name/.codex/auth.json",
        )
        for value in absolute_paths:
            with self.subTest(value=value), self.assertRaises(ValueError):
                lib.assert_public_safe({"location": value})
        lib.assert_public_safe({
            "locations": ["api/users.js:42", "GET /api/accounts/:id", "GET /v1/balance", "https://example.com/docs"],
        })

    def test_holdout_validation_matches_nested_contract(self):
        with self.assertRaises(ValueError):
            lib.validate_holdout_case({"kind": "output", "category": "status"})
        case = lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")[0]
        lib.validate_holdout_case(case)
        case = dict(case)
        case["verified_context"] = {"nested": {"candidate": "leak"}}
        with self.assertRaises(ValueError):
            lib.validate_holdout_case(case)
        case = dict(lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")[0])
        case["verified_context"] = {"nested": {"value": object()}}
        with self.assertRaises(ValueError):
            lib.validate_holdout_case(case)
        case = dict(lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")[0])
        case["structure"] = {"max_words": 9}
        with self.assertRaises(ValueError):
            lib.validate_holdout_case(case)
        activation = dict(lib.load_activation_cases(ROOT / "evals/cases/activation-dev.jsonl")[2])
        activation["protected_near_miss"] = "detailed"
        with self.assertRaises(ValueError):
            lib.validate_holdout_case(activation)

    def test_holdout_schema_closes_nested_case_contracts(self):
        schema = json.loads((ROOT / "evals/schemas/holdout.schema.json").read_text())
        definitions = schema["$defs"]
        self.assertFalse(definitions["critical_fact"]["additionalProperties"])
        self.assertEqual(definitions["critical_fact"]["required"], ["id", "scope", "groups"])
        self.assertFalse(definitions["forbidden_claim"]["additionalProperties"])
        self.assertFalse(definitions["structure"]["additionalProperties"])

    def test_budget_records_are_exact_and_no_lane_transfer_is_allowed(self):
        plan = json.loads((ROOT / "evals/release-plan.json").read_text())
        derived = runner.build_plan(ROOT / "evals/release-plan.json")
        self.assertEqual(len(derived["records"]), 275)
        moved = dict(plan)
        moved["lanes"] = dict(plan["lanes"])
        moved["lanes"]["dev_output"] = 0
        moved["lanes"]["compatibility"] += 48
        with self.assertRaises(ValueError):
            runner.validate_budget(moved)

    def test_paired_summary_and_bootstrap_use_median_percentage_reduction(self):
        pairs = []
        for number, reduction in enumerate((0.5, 0.1, 0.1)):
            pairs.append({"case_id": str(number), "cluster_id": str(number), "trial": 1, "model": "m", "effort": "e", "A": {"visible_output_tokens": 100}, "B": {"visible_output_tokens": 100 * (1 - reduction)}})
        self.assertEqual(lib.paired_summary(pairs)["visible_output_tokens"]["relative_reduction"], 0.1)
        self.assertEqual(lib.clustered_bootstrap_ci(pairs, "visible_output_tokens", seed=3, iterations=50), (0.1, 0.5))

    def test_negative_forbidden_claim_does_not_reject_required_not_ready_fact(self):
        case = lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")[0]
        result = lib.check_critical_facts(case, {"commentary": "", "final": "Implementation finished; 84/84 passed; integration not run because auth returns 503; PR #27 is not ready to merge."})
        self.assertTrue(result["passed"], result)

    def test_seal_and_reveal_reconstruct_fixed_identity_artifacts(self):
        mutations = (
            ("private/schedule.json", lambda value: value["calls"][0].__setitem__("ordinal", 99)),
            ("private/manifest.json", lambda value: value.__setitem__("runner", "forged-runner")),
            ("private/judge-manifest.json", lambda value: value.__setitem__("judge", "forged-judge")),
        )
        for path, mutate in mutations:
            with self.subTest(phase="seal", path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
                runner.main(["judge", "--root", str(root), "--fake"])
                artifact = root / path
                value = json.loads(artifact.read_text())
                mutate(value)
                artifact.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    runner.main(["seal", "--root", str(root)])
            with self.subTest(phase="reveal", path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
                runner.main(["judge", "--root", str(root), "--fake"])
                runner.main(["seal", "--root", str(root)])
                artifact = root / path
                value = json.loads(artifact.read_text())
                mutate(value)
                artifact.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    runner.main(["reveal", "--root", str(root)])

    def test_manifest_reconstructs_source_policy_and_git_identity_at_every_phase(self):
        with tempfile.TemporaryDirectory() as source_directory:
            source_root = Path(source_directory) / "source"
            plan = runner.build_plan(ROOT / "evals/release-plan.json")
            for relative in runner._manifest_relative_paths(plan):
                target = source_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)

            source = source_root / "evals/eval_v2_lib.py"
            policy = source_root / "skills/simple-man/SKILL.md"

            def mutate_then_reject(path, command, prepare):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    prepare(root)
                    original = path.read_bytes()
                    try:
                        path.write_bytes(original + b"\n# post-answer mutation\n")
                        with self.assertRaises(ValueError):
                            runner.main([command, "--root", str(root), *(["--fake"] if command == "judge" else [])])
                    finally:
                        path.write_bytes(original)

            with mock.patch.object(runner, "MANIFEST_SOURCE_ROOT", source_root):
                for path in (source, policy):
                    with self.subTest(path=path.name, phase="judge"):
                        mutate_then_reject(
                            path,
                            "judge",
                            lambda root: runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"]),
                        )
                    with self.subTest(path=path.name, phase="seal"):
                        def prepare_seal(root):
                            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
                            runner.main(["judge", "--root", str(root), "--fake"])
                        mutate_then_reject(path, "seal", prepare_seal)
                    with self.subTest(path=path.name, phase="reveal"):
                        def prepare_reveal(root):
                            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
                            runner.main(["judge", "--root", str(root), "--fake"])
                            runner.main(["seal", "--root", str(root)])
                        mutate_then_reject(path, "reveal", prepare_reveal)

                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
                    manifest = json.loads((root / "private/manifest.json").read_text())
        self.assertEqual(set(manifest["evaluated_git"]), {"head_sha", "tree_sha"})
        self.assertIn("evals/eval_v2_lib.py", manifest["source_sha256"])
        self.assertIn("evals/coding_gate.py", manifest["source_sha256"])
        self.assertIn("skills/simple-man/SKILL.md", manifest["source_sha256"])
        self.assertEqual(set(manifest["arm_policy_sha256"]), {"A", "B", "C", "generic"})

    def test_dry_plan_has_concrete_preregistered_call_identities(self):
        derived = runner.build_plan(ROOT / "evals/release-plan.json")
        records = derived["records"]
        required = {"call_id", "lane", "case_slot", "treatment", "description", "trial", "model", "effort", "cli", "policy_role", "conditional"}
        self.assertEqual(len(records), 275)
        self.assertEqual(len({record["call_id"] for record in records}), 275)
        self.assertTrue(all(set(record) == required for record in records))
        self.assertTrue(all(record["call_id"].startswith("call_") for record in records))
        self.assertTrue(all(isinstance(record["case_slot"], str) and record["case_slot"] for record in records))
        self.assertTrue(all(isinstance(record["model"], str) and record["model"] for record in records))
        self.assertTrue(all(isinstance(record["effort"], str) and record["effort"] for record in records))
        self.assertEqual(
            {lane: sum(record["lane"] == lane for record in records) for lane in runner.LANES},
            runner.LANES,
        )
        self.assertEqual(
            {record["treatment"] for record in records if record["lane"] == "dev_output"},
            {"A", "B", "C", "generic"},
        )
        self.assertNotIn("prompt", lib.canonical_json(records))

        lane_identity = {
            lane: {(record["model"], record["effort"], record["cli"])
                   for record in records if record["lane"] == lane}
            for lane in runner.LANES
        }
        self.assertEqual(lane_identity["primary_coding"], {("gpt-5.6-sol", "xhigh", "codex-cli 0.145.0")})
        self.assertEqual(lane_identity["blind_judge_and_tiebreak"], {("gpt-5.6-terra", "medium", "codex-cli 0.145.0")})
        self.assertEqual(lane_identity["compatibility"], {("gpt-5.5", "high", "codex-cli 0.145.0")})
        for lane in set(runner.LANES) - {"primary_coding", "blind_judge_and_tiebreak", "compatibility"}:
            self.assertEqual(lane_identity[lane], {("gpt-5.6-sol", "high", "codex-cli 0.145.0")})

        record = next(record for record in records if record["lane"] == "dev_output")
        self.assertEqual(
            runner.execution_identity_status(
                record,
                actual_model=record["model"],
                actual_effort=record["effort"],
                actual_cli=record["cli"],
            )["status"],
            "READY",
        )
        self.assertEqual(
            runner.execution_identity_status(
                record,
                actual_model="substituted-model",
                actual_effort=record["effort"],
                actual_cli=record["cli"],
            )["status"],
            "INCONCLUSIVE",
        )
        self.assertEqual(
            runner.execution_identity_status(record, actual_model=None, actual_effort=None, actual_cli=None)["status"],
            "INCONCLUSIVE",
        )

        comparisons = derived["comparison_contract"]
        self.assertEqual(comparisons["judge_cap"], 28)
        self.assertEqual(sum(item["max_judge_calls"] for item in comparisons["comparisons"]), 28)
        self.assertEqual(
            {(item["baseline_arm"], item["candidate_arm"]) for item in comparisons["comparisons"]},
            {("B", "C"), ("A", "winner"), ("generic", "winner"), ("runner_up", "winner")},
        )

    def test_fake_executor_is_recorded_separately_from_planned_live_identity(self):
        plan = runner.build_plan(ROOT / "evals/release-plan.json")
        self.assertNotIn("offline-fake", lib.canonical_json(plan["records"]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            schedule = json.loads((root / "private/schedule.json").read_text())
            self.assertEqual({call["executor"] for call in schedule["calls"]}, {runner.FAKE_EXECUTOR_ID})
            manifest = json.loads((root / "private/manifest.json").read_text())
            self.assertFalse(manifest["release_eligible"])
            self.assertEqual(manifest["execution_mode"], "offline_fake")
            runner.main(["judge", "--root", str(root), "--fake"])
            judge_manifest = json.loads((root / "private/judge-manifest.json").read_text())
            self.assertEqual(judge_manifest["executor"], runner.FAKE_JUDGE_ID)
            self.assertEqual(judge_manifest["model"], "gpt-5.6-terra")

    def test_live_readiness_requires_frozen_policy_hashes_and_clean_exact_head(self):
        plan = runner.build_plan(ROOT / "evals/release-plan.json")
        record = next(record for record in plan["records"] if record["lane"] == "dev_output")
        identity = {"actual_model": record["model"], "actual_effort": record["effort"]}
        rogues = ({**record, "call_id": "call_rogue"}, {**record, "policy_role": "forged"})
        with mock.patch("run_eval_v2._probe_cli_identity", side_effect=AssertionError("rogue record reached CLI probe")):
            for rogue in rogues:
                self.assertEqual(
                    runner.live_execution_status(
                        plan, rogue, **identity, arm_policy_sha256={}, description_policy_sha256={},
                        evaluated_head_sha="a" * 40, evaluated_tree_sha="c" * 40,
                    )["status"],
                    "INCONCLUSIVE",
                )
        self.assertEqual(
            runner.live_execution_status(
                plan, record, **identity, arm_policy_sha256={}, description_policy_sha256={},
                evaluated_head_sha="a" * 40, evaluated_tree_sha="c" * 40,
            )["status"],
            "INCONCLUSIVE",
        )

        frozen = copy.deepcopy(plan)
        for configs in (frozen["arm_policies"], frozen["description_policies"]):
            for name, policy in configs.items():
                policy.update({"state": "frozen", "source": f"evals/policies/{name}.md", "offline_only": False})
        policy_hashes = {name: "1" * 64 for name in frozen["arm_policies"]}
        description_hashes = {name: "2" * 64 for name in frozen["description_policies"]}
        source_hashes = {
            policy["source"]: policy_hashes[name]
            for name, policy in frozen["arm_policies"].items()
        } | {
            policy["source"]: description_hashes[name]
            for name, policy in frozen["description_policies"].items()
        }
        with mock.patch("run_eval_v2._probe_cli_identity", return_value="codex-cli 0.145.0"), mock.patch(
            "run_eval_v2._source_hashes", return_value=source_hashes,
        ), mock.patch(
            "run_eval_v2._live_git_state", return_value={"head_sha": "a" * 40, "tree_sha": "c" * 40, "clean": True},
        ):
            self.assertEqual(
                runner.live_execution_status(
                    frozen, record, **identity, arm_policy_sha256=policy_hashes,
                    description_policy_sha256=description_hashes, evaluated_head_sha="a" * 40, evaluated_tree_sha="c" * 40,
                )["status"],
                "READY",
            )
        with mock.patch("run_eval_v2._probe_cli_identity", return_value="codex-cli 0.145.0"), mock.patch(
            "run_eval_v2._source_hashes", return_value=source_hashes,
        ), mock.patch(
            "run_eval_v2._live_git_state", return_value={"head_sha": "b" * 40, "tree_sha": "c" * 40, "clean": True},
        ):
            self.assertEqual(
                runner.live_execution_status(
                    frozen, record, **identity, arm_policy_sha256=policy_hashes,
                    description_policy_sha256=description_hashes, evaluated_head_sha="a" * 40, evaluated_tree_sha="c" * 40,
                )["status"],
                "INCONCLUSIVE",
            )

        future = copy.deepcopy(plan)
        future["arm_policies"]["B"] = {"state": "frozen", "source": "evals/policies/simple_man_b.md", "offline_only": False}
        self.assertIn("evals/policies/simple_man_b.md", runner._manifest_relative_paths(future))

    def test_coherent_answer_and_judgment_rewrites_break_runner_commitments(self):
        def rewrite_answer(root):
            schedule = json.loads((root / "private/schedule.json").read_text())
            activation = next(call for call in schedule["calls"] if call["kind"] == "activation")
            attempt = root / "private/attempts" / activation["run_id"]
            raw_path = attempt / "raw.jsonl"
            payload = json.loads(raw_path.read_text())
            payload["final"] = '{"activate":false}' if payload["final"] != '{"activate":false}' else '{"activate":true}'
            raw_bytes = (lib.canonical_json(payload) + "\n").encode()
            raw_path.write_bytes(raw_bytes)
            result_path = attempt / "result.json"
            result = json.loads(result_path.read_text())
            result["result"] = {**payload, "raw_sha256": hashlib.sha256(raw_bytes).hexdigest()}
            result_path.write_text(lib.canonical_json(result) + "\n")

        def rewrite_judgment(root):
            attempt = next((root / "private/judge-attempts").iterdir())
            raw_path = attempt / "raw.jsonl"
            payload = json.loads(raw_path.read_text())
            payload["judgment"]["quality"] = "left"
            payload["judgment"]["rationale"] = "coherently rewritten"
            raw_bytes = (lib.canonical_json(payload) + "\n").encode()
            raw_path.write_bytes(raw_bytes)
            result_path = attempt / "result.json"
            result = json.loads(result_path.read_text())
            result["result"] = {**payload, "raw_sha256": hashlib.sha256(raw_bytes).hexdigest()}
            result_path.write_text(lib.canonical_json(result) + "\n")
            aggregate = root / "private/judgments.jsonl"
            rows = [json.loads(line) for line in aggregate.read_text().splitlines()]
            row = next(row for row in rows if row["pair_id"] == payload["pair_id"])
            row["judgment"] = payload["judgment"]
            aggregate.write_text("".join(lib.canonical_json(row) + "\n" for row in rows))

        answer_phases = (
            ("judge", lambda root: None),
            ("seal", lambda root: runner.main(["judge", "--root", str(root), "--fake"])),
            ("reveal", lambda root: (runner.main(["judge", "--root", str(root), "--fake"]), runner.main(["seal", "--root", str(root)]))),
        )
        for phase, prepare in answer_phases:
            with self.subTest(kind="answer", phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
                prepare(root)
                rewrite_answer(root)
                with self.assertRaises(ValueError):
                    runner.main([phase, "--root", str(root), *(["--fake"] if phase == "judge" else [])])

        for phase in ("seal", "reveal"):
            with self.subTest(kind="judgment", phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
                runner.main(["judge", "--root", str(root), "--fake"])
                if phase == "reveal":
                    runner.main(["seal", "--root", str(root)])
                rewrite_judgment(root)
                with self.assertRaises(ValueError):
                    runner.main([phase, "--root", str(root)])

    def test_resume_marks_started_failed_and_interrupted_answer_calls_spent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            attempts = sorted((root / "private/attempts").iterdir())
            states = ("started", "failed", "interrupted")
            consumed = []
            for attempt, state in zip(attempts, states):
                identity = runner.load_attempt(attempt)["identity"]
                consumed.append(identity["call_id"])
                if state == "started":
                    (attempt / "result.json").unlink()
                elif state == "failed":
                    result = json.loads((attempt / "result.json").read_text())
                    result["status"] = "failed"
                    (attempt / "result.json").write_text(lib.canonical_json(result) + "\n")
                else:
                    (attempt / "raw.jsonl").unlink()
                    (attempt / "result.json").unlink()
                    runner.finish_attempt(attempt, {}, state)
            partial_raw = (attempts[0] / "raw.jsonl").read_bytes()
            untouched = attempts[3]
            shutil.rmtree(untouched)
            (root / "private/mapping.json").unlink()
            (root / "private/answer-commitment.json").unlink()
            (root / "public/bundle.json").unlink()

            first = runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            self.assertEqual(first["status"], "incomplete")
            self.assertTrue(first["unsealable"])
            self.assertEqual(set(first["spent_call_ids"]), set(consumed))
            self.assertTrue((untouched / "result.json").exists())
            self.assertFalse((attempts[0] / "result.json").exists())
            self.assertEqual((attempts[0] / "raw.jsonl").read_bytes(), partial_raw)
            with mock.patch("run_eval_v2._fake_response", side_effect=AssertionError("spent call retried")):
                second = runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            self.assertEqual(second["status"], "incomplete")
            with self.assertRaises(ValueError):
                runner.main(["seal", "--root", str(root)])

    def test_resume_marks_started_judge_call_spent_and_runs_only_untouched_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            bundle = json.loads((root / "public/bundle.json").read_text())
            mapping = json.loads((root / "private/mapping.json").read_text())
            plan = runner.build_plan(ROOT / "evals/release-plan.json")
            pair = bundle["pairs"][0]
            identity = runner._judge_identity(pair, runner._bundle_sha256(bundle), plan, bundle["pairs"], mapping)
            attempt = root / "private/judge-attempts" / pair["pair_id"]
            runner.start_attempt(attempt, identity)
            partial = {"pair_id": pair["pair_id"], "judgment": runner._fake_judgment()}
            partial_raw = (lib.canonical_json(partial) + "\n").encode()
            (attempt / "raw.jsonl").write_bytes(partial_raw)

            result = runner.main(["judge", "--root", str(root), "--fake"])
            self.assertEqual(result["status"], "incomplete")
            self.assertTrue(result["unsealable"])
            self.assertEqual(result["spent_call_ids"], [identity["call_id"]])
            self.assertFalse((attempt / "result.json").exists())
            self.assertEqual((attempt / "raw.jsonl").read_bytes(), partial_raw)
            self.assertFalse((root / "private/judgments.jsonl").exists())
            self.assertTrue((root / "private/judge-manifest.json").exists())
            with mock.patch("run_eval_v2._fake_judgment", side_effect=AssertionError("spent judge retried")):
                self.assertEqual(runner.main(["judge", "--root", str(root), "--fake"])["status"], "incomplete")

    def test_partial_attempts_reject_extra_tampered_and_bad_hash_artifacts(self):
        answer_identity = {"call_id": "call-answer", "kind": "output"}
        answer_payload = runner._answer_payload(answer_identity, "", "done")
        judgment_identity = {"call_id": "call-judge", "pair_id": "pair-one"}
        judgment_payload = {"pair_id": "pair-one", "judgment": runner._fake_judgment()}
        specs = (
            ("answer", answer_identity, answer_payload, runner._validate_partial_answer_raw),
            ("judge", judgment_identity, judgment_payload, runner._validate_partial_judgment_raw),
        )
        for kind, identity, payload, validator in specs:
            for corruption in ("extra", "content", "hash"):
                with self.subTest(kind=kind, corruption=corruption), tempfile.TemporaryDirectory() as directory:
                    attempt = Path(directory) / "attempt"
                    runner.start_attempt(attempt, identity)
                    raw = attempt / "raw.jsonl"
                    raw.write_text(lib.canonical_json(payload) + "\n")
                    if corruption == "extra":
                        (attempt / "extra.json").write_text("{}")
                    elif corruption == "content":
                        tampered = dict(payload)
                        tampered["call_id" if kind == "answer" else "pair_id"] = "tampered"
                        raw.write_text(lib.canonical_json(tampered) + "\n")
                    else:
                        runner.finish_attempt(attempt, {"raw_sha256": "0" * 64}, "failed")
                    with self.assertRaises(ValueError):
                        runner._attempt_state(attempt, identity, partial_raw_validator=validator)

    def test_resume_quarantines_truncated_answer_artifacts_and_continues_untouched_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            attempts = sorted((root / "private/attempts").iterdir())
            raw_only, bad_result, untouched = attempts[:3]
            spent = {
                runner.load_attempt(raw_only)["identity"]["call_id"],
                runner.load_attempt(bad_result)["identity"]["call_id"],
            }
            (raw_only / "result.json").unlink()
            (raw_only / "raw.jsonl").write_bytes(b'{"truncated"')
            (bad_result / "result.json").write_bytes(b'{"schema_version":')
            shutil.rmtree(untouched)
            for artifact in (root / "private/mapping.json", root / "private/answer-commitment.json", root / "public/bundle.json"):
                artifact.unlink()

            result = runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(set(result["spent_call_ids"]), spent)
            self.assertTrue((untouched / "result.json").exists())
            self.assertEqual((raw_only / "raw.jsonl").read_bytes(), b'{"truncated"')
            self.assertEqual((bad_result / "result.json").read_bytes(), b'{"schema_version":')
            with mock.patch("run_eval_v2._fake_response", side_effect=AssertionError("spent answer retried")), mock.patch(
                "run_eval_v2._fake_activation_response", side_effect=AssertionError("spent activation retried"),
            ):
                self.assertEqual(runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])["status"], "incomplete")
            with self.assertRaises(ValueError):
                runner.main(["seal", "--root", str(root)])

    def test_resume_quarantines_truncated_judge_artifacts_and_continues_untouched_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            bundle = json.loads((root / "public/bundle.json").read_text())
            mapping = json.loads((root / "private/mapping.json").read_text())
            plan = runner.build_plan(ROOT / "evals/release-plan.json")
            pairs = bundle["pairs"][:2]
            spent = set()
            attempts = []
            for pair in pairs:
                identity = runner._judge_identity(pair, runner._bundle_sha256(bundle), plan, bundle["pairs"], mapping)
                spent.add(identity["call_id"])
                attempt = root / "private/judge-attempts" / pair["pair_id"]
                attempts.append(attempt)
                runner.start_attempt(attempt, identity)
                payload = {"pair_id": pair["pair_id"], "judgment": runner._fake_judgment()}
                (attempt / "raw.jsonl").write_text(lib.canonical_json(payload) + "\n")
            (attempts[0] / "raw.jsonl").write_bytes(b'{"truncated"')
            (attempts[1] / "result.json").write_bytes(b'{"schema_version":')

            result = runner.main(["judge", "--root", str(root), "--fake"])
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(set(result["spent_call_ids"]), spent)
            untouched = root / "private/judge-attempts" / bundle["pairs"][2]["pair_id"]
            self.assertTrue((untouched / "result.json").exists())
            self.assertFalse((root / "private/judgments.jsonl").exists())
            with mock.patch("run_eval_v2._fake_judgment", side_effect=AssertionError("spent judge retried")):
                self.assertEqual(runner.main(["judge", "--root", str(root), "--fake"])["status"], "incomplete")
            with self.assertRaises(ValueError):
                runner.main(["seal", "--root", str(root)])

    def test_public_inventory_is_exact_and_neutral_positions_are_left_right(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            bundle = json.loads((root / "public/bundle.json").read_text())
            self.assertEqual(set(bundle["pairs"][0]) - {"pair_id", "case_id", "language", "prompt", "verified_context"}, {"left", "right"})
            self.assertNotIn("response_A", lib.canonical_json(bundle))
            self.assertNotIn("response_B", lib.canonical_json(bundle))
            runner.main(["judge", "--root", str(root), "--fake"])
            (root / "public/arm-map.json").write_text('{"arm":"A"}')
            with self.assertRaises(ValueError):
                runner.main(["seal", "--root", str(root)])
        with self.assertRaises(ValueError):
            lib.assert_public_safe({"note": "generic terse"}, arm_aliases={"generic"})

    def test_root_ownership_and_holdout_runtime_schema_are_fail_closed(self):
        for unsafe in ("/", str(Path.home()), tempfile.gettempdir(), str(ROOT / "eval-v2-output")):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                runner._root(unsafe)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "foreign.txt").write_text("not an eval root")
            with self.assertRaises(ValueError):
                runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])

        case = copy.deepcopy(lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")[0])
        case["critical_facts"][0]["id"] = " "
        with self.assertRaises(ValueError):
            lib.validate_holdout_case(case)
        case = copy.deepcopy(lib.load_output_cases(ROOT / "evals/cases/output-dev.jsonl")[0])
        case["critical_facts"][0]["groups"][0][0] = " "
        with self.assertRaises(ValueError):
            lib.validate_holdout_case(case)
        judgment = {"quality": "tie", "naturalness": "tie", "flags": {"left": [], "right": []}, "rationale": " "}
        with self.assertRaises(ValueError):
            lib.validate_judgment(judgment)
        schema = json.loads((ROOT / "evals/schemas/holdout.schema.json").read_text())
        self.assertEqual(schema["$defs"]["critical_fact"]["properties"]["id"]["pattern"], ".*\\S.*")

    def test_pair_measurements_preserve_registered_cluster_id(self):
        runs = []
        for case_id in ("one", "two"):
            for arm in ("A", "B"):
                runs.append({"case_id": case_id, "cluster_id": "shared", "trial": 1, "model": "m", "effort": "e", "arm": arm,
                             "cli": "cli",
                             "commentary_visible_tokens": 1, "final_visible_tokens": 1, "input_tokens": 10,
                             "cached_input_tokens": 5, "output_tokens": 2, "latency_ms": 1})
        pairs = lib.pair_measurements(runs)
        self.assertEqual({pair["cluster_id"] for pair in pairs}, {"shared"})


if __name__ == "__main__":
    unittest.main()
