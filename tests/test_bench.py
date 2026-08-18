"""Offline tests for the lean Claude Code benchmark harness.

Every test runs against a fake ``claude`` binary placed on PATH. No test in this
file may make a live model call.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "evals" / "bench"))

import report as bench_report  # noqa: E402
import runner as bench  # noqa: E402

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, sys, os

argv = sys.argv[1:]
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": os.environ.get("FAKE_AUTH", "claude.ai"),
                      "subscriptionType": "max"}))
    raise SystemExit(0)
if argv[:1] == ["--version"]:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)

prompt = argv[argv.index("-p") + 1]
system = open(argv[argv.index("--system-prompt-file") + 1]).read()

# Refuse anything that would bill outside the subscription.
for banned in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "CLAUDE_CODE_OAUTH_TOKEN"):
    if os.environ.get(banned):
        print(f"credential {banned} leaked into subprocess", file=sys.stderr)
        raise SystemExit(3)

if "YES or NO" in prompt:
    result = "YES" if "compact" in prompt or "$simple-man" in prompt else "NO"
elif "untrusted_task" in prompt:
    result = json.dumps({"quality": "left", "naturalness": "tie",
                         "flags": {"left": [], "right": []}, "rationale": "fake judgment"})
else:
    # Longer answer when no policy is present, so arms differ measurably.
    result = "84/84 " + ("word " * (40 if len(system) < 300 else 10))

print(json.dumps({
    "result": result,
    "usage": {"input_tokens": len(system) // 4, "output_tokens": len(result) // 4,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    "total_cost_usd": 0.001,
}))
"""


class BenchHarnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bench-test-")
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.claude = bin_dir / "claude"
        self.claude.write_text(FAKE_CLAUDE)
        self.claude.chmod(0o755)
        self._env_backup = dict(os.environ)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        for banned in bench.BANNED_ENV:
            os.environ.pop(banned, None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    def run_bench(self, *args):
        return bench.main([*args, "--claude", str(self.claude)])

    # -- billing guard ----------------------------------------------------

    def test_refuses_to_start_when_an_api_key_is_present(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-never-be-used"
        with self.assertRaises(bench.BillingGuard) as ctx:
            bench.preflight(str(self.claude))
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_refuses_a_non_subscription_login(self):
        os.environ["FAKE_AUTH"] = "apiKey"
        with self.assertRaises(bench.BillingGuard):
            bench.preflight(str(self.claude))

    def test_credentials_never_reach_the_subprocess(self):
        """The fake CLI exits nonzero if a banned variable survives into it."""
        os.environ["ANTHROPIC_BASE_URL"] = "https://proxy.invalid"
        env = bench.clean_env()
        for banned in bench.BANNED_ENV:
            self.assertNotIn(banned, env)

    def test_preflight_accepts_a_subscription_login(self):
        identity = bench.preflight(str(self.claude))
        self.assertEqual(identity["auth_method"], "claude.ai")
        self.assertEqual(identity["cli"], "9.9.9 (Fake Claude)")

    # -- arm composition --------------------------------------------------

    def test_control_arm_has_no_policy_and_others_do(self):
        control = bench.system_prompt("N")
        self.assertNotIn("Simple Man", control)
        self.assertEqual(bench.policy_tokens("N"), 0)
        for arm in ("A", "B", "G", "C"):
            with self.subTest(arm=arm):
                prompt = bench.system_prompt(arm)
                self.assertTrue(prompt.startswith(control))
                self.assertGreater(len(prompt), len(control))
                self.assertGreater(bench.policy_tokens(arm), 0)

    def test_every_arm_is_told_that_tools_are_unavailable(self):
        for arm in bench.ARMS:
            with self.subTest(arm=arm):
                self.assertIn(bench.NO_TOOLS_NOTE, bench.system_prompt(arm))

    # -- planning ---------------------------------------------------------

    def test_dry_run_counts_calls_and_makes_none(self):
        out = self.tmp / "run"
        self.run_bench(
            "all", "--output-dir", str(out), "--max-calls", "500", "--dry-run", "--limit", "3"
        )
        self.assertFalse(out.exists())

    def test_plan_over_the_call_cap_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_bench(
                "output", "--output-dir", str(self.tmp / "x"), "--max-calls", "2", "--dry-run"
            )

    def test_unknown_arm_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_bench(
                "output", "--output-dir", str(self.tmp / "x"), "--max-calls", "9",
                "--arm", "Z", "--dry-run",
            )

    # -- end to end -------------------------------------------------------

    def test_full_chain_produces_a_rebuildable_report(self):
        out = self.tmp / "run"
        self.run_bench(
            "all", "--output-dir", str(out), "--max-calls", "500", "--limit", "4",
            "--arm", "N", "--arm", "A", "--arm", "B", "--arm", "G",
        )
        for name in ("output.jsonl", "activation.jsonl", "judge.jsonl"):
            self.assertTrue((out / name).is_file(), name)

        summary = bench_report.build(
            out, ROOT / "evals/cases/bench-output.jsonl", ROOT / "evals/cases/bench-activation.jsonl"
        )
        self.assertEqual(summary["meta"]["cli"], "9.9.9 (Fake Claude)")
        self.assertTrue(summary["pairwise"])
        self.assertIn("B", summary["retention"])

        rendered = bench_report.render(summary)
        target = self.tmp / "report.md"
        target.write_text(rendered)
        self.assertEqual(0, bench_report.main(["--run-dir", str(out), "--check", str(target)]))
        target.write_text(rendered + "tampered\n")
        self.assertEqual(1, bench_report.main(["--run-dir", str(out), "--check", str(target)]))

    def test_rerun_resumes_instead_of_repeating_paid_calls(self):
        out = self.tmp / "run"
        args = ["output", "--output-dir", str(out), "--max-calls", "500", "--limit", "2",
                "--arm", "N", "--arm", "B"]
        self.run_bench(*args)
        first = (out / "output.jsonl").read_text()
        self.run_bench(*args)
        self.assertEqual(first, (out / "output.jsonl").read_text())

    def test_judge_records_both_orderings_of_every_pair(self):
        out = self.tmp / "run"
        self.run_bench(
            "output", "--output-dir", str(out), "--max-calls", "500", "--limit", "3",
            "--arm", "A", "--arm", "B",
        )
        self.run_bench(
            "judge", "--output-dir", str(out), "--max-calls", "500", "--limit", "3",
            "--compare", "B:A",
        )
        rows = [json.loads(line) for line in (out / "judge.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertIn(row["position"], ("primary", "swapped"))
            self.assertNotEqual(row["left_arm"], row["right_arm"])
        for case_id in {row["case_id"] for row in rows}:
            sides = {
                row["position"]: (row["left_arm"], row["right_arm"])
                for row in rows
                if row["case_id"] == case_id
            }
            self.assertEqual(sides["primary"], tuple(reversed(sides["swapped"])))

    def test_judge_payload_carries_no_arm_identity(self):
        """Blinding is structural: there is no arm label in what the judge sees."""
        case = {
            "prompt": "p", "verified_context": {}, "requested_shape": "compact", "id": "x",
        }
        payload = bench.lib.build_judge_payload(case, "left text", "right text")
        blob = json.dumps(payload)
        for arm in bench.ARMS:
            self.assertNotIn(f'"{arm}"', blob)
        self.assertNotIn("arm", blob)


class CorpusTests(unittest.TestCase):
    """The corpus is the benchmark. Guard its shape, not just its syntax."""

    OUTPUT = ROOT / "evals" / "cases" / "bench-output.jsonl"
    ACTIVATION = ROOT / "evals" / "cases" / "bench-activation.jsonl"

    def setUp(self):
        self.output = bench.load_cases(self.OUTPUT)
        self.activation = bench.load_cases(self.ACTIVATION)

    def test_corpus_is_large_enough_for_a_clustered_bootstrap(self):
        self.assertGreaterEqual(len(self.output), 60)
        self.assertGreaterEqual(len(self.activation), 30)

    def test_every_output_category_has_replication(self):
        counts = {}
        for case in self.output:
            counts[case["category"]] = counts.get(case["category"], 0) + 1
        self.assertEqual(len(counts), 12, counts)
        for category, count in counts.items():
            with self.subTest(category=category):
                self.assertGreaterEqual(count, 4, f"{category} has no replication")

    def test_both_languages_are_well_represented(self):
        russian = sum(1 for case in self.output if case["language"] == "ru")
        self.assertGreaterEqual(russian / len(self.output), 0.3)

    def test_clusters_are_unique_so_bootstrap_resamples_independent_units(self):
        clusters = [case["cluster_id"] for case in self.output]
        self.assertEqual(len(clusters), len(set(clusters)))

    def test_override_categories_do_not_request_a_compact_shape(self):
        for case in self.output:
            if case["category"].endswith("_override"):
                with self.subTest(case=case["id"]):
                    self.assertNotEqual(case["requested_shape"], "compact")

    def test_no_prompt_mentions_the_benchmark_or_the_skill(self):
        """A prompt that names the thing under test would cue the model."""
        banned = ("simple man", "benchmark", "token budget", "compression policy")
        for case in self.output + self.activation:
            lowered = case["prompt"].lower()
            # explicit-invocation activation cases must name the skill by design
            explicit = "$simple-man" in lowered
            for word in banned:
                with self.subTest(case=case["id"], word=word):
                    self.assertNotIn(word, lowered)
            if not explicit:
                with self.subTest(case=case["id"]):
                    self.assertNotIn("simple-man", lowered)

    def test_activation_classes_stay_balanced_enough_to_measure_precision(self):
        negatives = [c for c in self.activation if c["activation_class"] == "negative"]
        positives = [c for c in self.activation if c["expected"] == "activate"]
        self.assertGreaterEqual(len(negatives), 10)
        self.assertGreaterEqual(len(positives), 10)
        protected = {c["protected_near_miss"] for c in negatives} - {None}
        self.assertEqual(protected, {"detailed", "teaching", "creative"})


class ReportMathTests(unittest.TestCase):
    def _record(self, case_id, arm, tokens):
        return {
            "phase": "output", "case_id": case_id, "cluster_id": case_id, "arm": arm,
            "output_tokens": tokens, "input_tokens": 100, "latency_ms": 10,
        }

    def test_reduction_is_positive_when_the_candidate_is_shorter(self):
        records = [self._record("c1", "N", 100), self._record("c1", "B", 60)]
        row = bench_report.pairwise(records, "N", "B")
        self.assertAlmostEqual(row["median_reduction"], 0.4)

    def test_reduction_is_negative_when_the_candidate_is_longer(self):
        records = [self._record("c1", "N", 100), self._record("c1", "B", 150)]
        row = bench_report.pairwise(records, "N", "B")
        self.assertAlmostEqual(row["median_reduction"], -0.5)

    def test_a_case_missing_one_arm_is_dropped_from_the_pairing(self):
        records = [
            self._record("c1", "N", 100), self._record("c1", "B", 50),
            self._record("c2", "N", 100),
        ]
        self.assertEqual(len(bench_report.pair_records(records, "N", "B")), 1)

    def test_duplicate_records_for_one_arm_are_rejected(self):
        records = [self._record("c1", "N", 100), self._record("c1", "N", 90)]
        with self.assertRaises(ValueError):
            bench_report.pair_records(records, "N", "B")

    def test_split_decision_between_orderings_counts_as_a_tie(self):
        judgments = []
        for position, quality in (("primary", "left"), ("swapped", "left")):
            judgments.append({
                "phase": "judge", "case_id": "c1", "comparison": "B-vs-A", "position": position,
                "left_arm": "B" if position == "primary" else "A",
                "right_arm": "A" if position == "primary" else "B",
                "judgment": {"quality": quality, "naturalness": "tie",
                             "flags": {"left": [], "right": []}, "rationale": "r"},
            })
        result = bench_report.blind(judgments)
        self.assertEqual(result["comparisons"]["B-vs-A"]["ties"], 1)
        self.assertEqual(result["comparisons"]["B-vs-A"]["wins"], {})

    def test_consistent_winner_across_orderings_counts_as_a_win(self):
        judgments = [
            {"phase": "judge", "case_id": "c1", "comparison": "B-vs-A", "position": "primary",
             "left_arm": "B", "right_arm": "A",
             "judgment": {"quality": "left", "naturalness": "tie",
                          "flags": {"left": [], "right": []}, "rationale": "r"}},
            {"phase": "judge", "case_id": "c1", "comparison": "B-vs-A", "position": "swapped",
             "left_arm": "A", "right_arm": "B",
             "judgment": {"quality": "right", "naturalness": "tie",
                          "flags": {"left": [], "right": []}, "rationale": "r"}},
        ]
        result = bench_report.blind(judgments)
        self.assertEqual(result["comparisons"]["B-vs-A"]["wins"], {"B": 1})


if __name__ == "__main__":
    unittest.main()


class JudgmentParsingTests(unittest.TestCase):
    """A verbose rationale must not discard an otherwise valid judgment."""

    def _payload(self, rationale):
        return json.dumps({
            "quality": "left", "naturalness": "tie",
            "flags": {"left": [], "right": []}, "rationale": rationale,
        })

    def test_over_long_rationale_is_truncated_not_rejected(self):
        judgment = bench._parse_judgment(self._payload("x" * 5000))
        self.assertEqual(judgment["quality"], "left")
        self.assertLessEqual(len(judgment["rationale"]), bench.MAX_RATIONALE)

    def test_fenced_json_is_accepted(self):
        judgment = bench._parse_judgment("```json\n" + self._payload("fine") + "\n```")
        self.assertEqual(judgment["naturalness"], "tie")

    def test_non_json_is_still_rejected(self):
        with self.assertRaises(ValueError):
            bench._parse_judgment("I prefer the left answer.")

    def test_invalid_choice_is_still_rejected(self):
        bad = json.dumps({"quality": "middle", "naturalness": "tie",
                          "flags": {"left": [], "right": []}, "rationale": "r"})
        with self.assertRaises(ValueError):
            bench._parse_judgment(bad)


class CorpusSelectionTests(unittest.TestCase):
    def _case(self, cid, category, wave="dev"):
        return {"id": cid, "category": category, "wave": wave}

    def setUp(self):
        self.cases = [
            self._case("a", "review"),
            self._case("b", "security"),
            self._case("c", "review", "holdout-v2"),
            self._case("d", "plan", "holdout-v2"),
        ]

    def test_category_filter(self):
        picked = bench.select_cases(self.cases, categories=("review",))
        self.assertEqual([c["id"] for c in picked], ["a", "c"])

    def test_wave_filter(self):
        picked = bench.select_cases(self.cases, waves=("holdout-v2",))
        self.assertEqual([c["id"] for c in picked], ["c", "d"])

    def test_filters_compose_and_limit_applies_last(self):
        picked = bench.select_cases(
            self.cases, categories=("review", "plan"), waves=("holdout-v2",), limit=1
        )
        self.assertEqual([c["id"] for c in picked], ["c"])

    def test_no_filter_keeps_everything(self):
        self.assertEqual(len(bench.select_cases(self.cases)), 4)


class HoldoutCorpusTests(unittest.TestCase):
    """The holdout wave is the answer to "was the policy tuned to the test set"."""

    def setUp(self):
        self.holdout = bench.load_cases(ROOT / "evals/cases/bench-output-holdout.jsonl")
        self.dev = bench.load_cases(ROOT / "evals/cases/bench-output.jsonl")
        self.act_holdout = bench.load_cases(
            ROOT / "evals/cases/bench-activation-holdout.jsonl"
        )

    def test_holdout_cases_are_tagged_and_dev_defaults(self):
        self.assertTrue(self.holdout)
        for case in self.holdout:
            self.assertEqual(case["wave"], "holdout-v2")
        for case in self.dev:
            self.assertEqual(case["wave"], "dev")

    def test_holdout_covers_every_category(self):
        categories = {case["category"] for case in self.holdout}
        self.assertEqual(len(categories), 12, categories)

    def test_ids_and_clusters_never_collide_across_waves(self):
        self.assertFalse({c["id"] for c in self.holdout} & {c["id"] for c in self.dev})
        self.assertFalse(
            {c["cluster_id"] for c in self.holdout} & {c["cluster_id"] for c in self.dev}
        )

    def test_activation_holdout_keeps_protected_near_misses(self):
        negatives = [c for c in self.act_holdout if c["activation_class"] == "negative"]
        self.assertGreaterEqual(len(negatives), 5)
        self.assertTrue({c["protected_near_miss"] for c in negatives} - {None})

    def test_dev_corpus_is_unchanged_so_the_first_run_stays_reproducible(self):
        """The v0.3.0 preregistration pins this file; growth goes in a new file."""
        self.assertEqual(len(self.dev), 60)


class CodingResultTests(unittest.TestCase):
    def _record(self, arm, case_id, passed):
        return {"phase": "coding", "arm": arm, "case_id": case_id, "passed": passed}

    def test_pass_rate_and_named_failures(self):
        results = bench_report.coding_results([
            self._record("B", "node-auth-api", True),
            self._record("B", "python-payment-ledger", False),
            self._record("N", "node-auth-api", True),
        ])
        self.assertEqual(results["B"]["passed"], 1)
        self.assertEqual(results["B"]["failures"], ["python-payment-ledger"])
        self.assertAlmostEqual(results["B"]["rate"], 0.5)
        self.assertEqual(results["N"]["rate"], 1.0)

    def test_a_missing_pass_flag_counts_as_a_failure(self):
        results = bench_report.coding_results([
            {"phase": "coding", "arm": "B", "case_id": "x", "passed": None},
        ])
        self.assertEqual(results["B"]["passed"], 0)


class CodingPromptTests(unittest.TestCase):
    def test_coding_prelude_does_not_claim_tools_are_absent(self):
        """The answer phases say "you have no tools"; saying that with tools
        enabled makes the model write instructions instead of editing files."""
        self.assertIn(bench.NO_TOOLS_NOTE, bench.system_prompt("B", tools=False))
        self.assertNotIn(bench.NO_TOOLS_NOTE, bench.system_prompt("B", tools=True))

    def test_coding_prelude_still_carries_the_arm_policy(self):
        prompt = bench.system_prompt("B", tools=True)
        self.assertIn("Simple Man", prompt)
        self.assertNotIn("Simple Man", bench.system_prompt("N", tools=True))


class PublishedReportsTests(unittest.TestCase):
    """Every published report must still rebuild from its own raw records.

    Changing report.py can silently invalidate an older release's numbers. That
    is exactly the failure this project claims to have designed out, so it is
    checked for all releases rather than only the newest.
    """

    RELEASES = sorted((ROOT / "evals" / "releases").glob("v*/report.md"))

    #: Releases measured before the holdout wave existed were scored against the
    #: dev corpus only, so they must be rebuilt against the corpus they used.
    CORPUS = {
        "v0.3.0": (["bench-output.jsonl"], ["bench-activation.jsonl"]),
    }

    def test_at_least_one_release_is_published(self):
        self.assertTrue(self.RELEASES)

    def test_every_published_report_rebuilds_from_raw_records(self):
        cases_dir = ROOT / "evals" / "cases"
        for report_path in self.RELEASES:
            release = report_path.parent.name
            with self.subTest(release=release):
                run_dir = report_path.parent / "run"
                self.assertTrue(run_dir.is_dir(), f"{release} has no raw records")
                outputs, activations = self.CORPUS.get(
                    release,
                    (
                        [p.name for p in bench_report.DEFAULT_OUTPUT_CASES],
                        [p.name for p in bench_report.DEFAULT_ACTIVATION_CASES],
                    ),
                )
                summary = bench_report.build(
                    run_dir,
                    [cases_dir / name for name in outputs],
                    [cases_dir / name for name in activations],
                )
                self.assertEqual(
                    bench_report.render(summary),
                    report_path.read_text(),
                    f"{release}/report.md no longer matches its raw records",
                )
