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
            out, ROOT / "evals/cases/output-dev.jsonl", ROOT / "evals/cases/activation-dev.jsonl"
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
