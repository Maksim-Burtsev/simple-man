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
        self.assertEqual(lib.clustered_bootstrap_ci(paired, "visible_output_tokens", seed=7, iterations=20), (0.5, 0.5))
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

    def test_public_leak_scan_uses_dynamic_identifiers_secrets_and_paths(self):
        with self.assertRaises(ValueError):
            lib.assert_public_safe(
                {"pairs": [{"response_A": {"final": "ARM-A Bearer live_token api_key=live_token /Users/name/repo call_private_123"}}]},
                arm_aliases={"A", "B"}, private_ids={"call_private_123"}, protected_roots={Path("/Users/name")},
            )

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
                (attempt / "raw.jsonl").unlink()
                (attempt / "result.json").unlink()
                if state != "started":
                    runner.finish_attempt(attempt, {}, state)
            untouched = attempts[3]
            shutil.rmtree(untouched)
            (root / "private/mapping.json").unlink()
            (root / "public/bundle.json").unlink()

            first = runner.main(["answers", "--root", str(root), "--fake", "--secret", "test-secret"])
            self.assertEqual(first["status"], "incomplete")
            self.assertTrue(first["unsealable"])
            self.assertEqual(set(first["spent_call_ids"]), set(consumed))
            self.assertTrue((untouched / "result.json").exists())
            self.assertFalse((attempts[0] / "result.json").exists())
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
            plan = runner.build_plan(ROOT / "evals/release-plan.json")
            pair = bundle["pairs"][0]
            identity = runner._judge_identity(pair, runner._bundle_sha256(bundle), plan, bundle["pairs"])
            attempt = root / "private/judge-attempts" / pair["pair_id"]
            runner.start_attempt(attempt, identity)

            result = runner.main(["judge", "--root", str(root), "--fake"])
            self.assertEqual(result["status"], "incomplete")
            self.assertTrue(result["unsealable"])
            self.assertEqual(result["spent_call_ids"], [identity["call_id"]])
            self.assertFalse((attempt / "result.json").exists())
            self.assertFalse((root / "private/judgments.jsonl").exists())
            self.assertTrue((root / "private/judge-manifest.json").exists())
            with mock.patch("run_eval_v2._fake_judgment", side_effect=AssertionError("spent judge retried")):
                self.assertEqual(runner.main(["judge", "--root", str(root), "--fake"])["status"], "incomplete")

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
                             "commentary_visible_tokens": 1, "final_visible_tokens": 1, "input_tokens": 10,
                             "cached_input_tokens": 5, "output_tokens": 2, "latency_ms": 1})
        pairs = lib.pair_measurements(runs)
        self.assertEqual({pair["cluster_id"] for pair in pairs}, {"shared"})


if __name__ == "__main__":
    unittest.main()
