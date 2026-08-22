"""Session benchmark harness: billing guard, preregistration, statistics, rebuild."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals" / "session"))

import collect  # noqa: E402
import session_gates as gates  # noqa: E402
import session_report as report  # noqa: E402
import run_ab  # noqa: E402

PREREG = ROOT / "evals" / "releases" / "session-v1" / "preregistration.json"


def trial(task, arm, *, reward=1.0, cost=1.0, out=100, turns=10, error=None, delivered=None, started="2026-08-22T10:00:00"):
    return {
        "task": task, "arm": arm, "job": f"{arm}-b00", "trial": f"{task}__x", "reward": reward,
        "cost_usd": cost, "input_tokens": 10, "cache_read_tokens": 50, "cache_write_tokens": 5,
        "output_tokens": out, "turns": turns, "wall_ms": 1000, "started_at": started, "error": error,
        "delivered": (arm != "N") if delivered is None else delivered,
    }


class BillingGuardTests(unittest.TestCase):
    def test_api_credentials_refused(self):
        for name in run_ab.BANNED_ENV:
            with self.assertRaises(run_ab.BillingGuard):
                run_ab.assert_subscription_billing({name: "x", run_ab.OAUTH_ENV: "tok"})

    def test_oauth_token_required(self):
        with self.assertRaises(run_ab.BillingGuard):
            run_ab.assert_subscription_billing({})
        run_ab.assert_subscription_billing({run_ab.OAUTH_ENV: "tok"})

    def test_dry_run_makes_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            sb = Path(tmp) / "sb"
            for task in run_ab.task_order(json.loads(PREREG.read_text()))[:2]:
                (sb / "tasks" / task / "environment" / "skills" / "s").mkdir(parents=True)
                (sb / "tasks" / task / "environment" / "skills" / "s" / "SKILL.md").write_text("x")
            env = {k: v for k, v in os.environ.items() if k not in run_ab.BANNED_ENV and k != run_ab.OAUTH_ENV}
            env["PATH"] = tmp  # no harbor, no claude reachable
            proc = subprocess.run(
                [sys.executable, str(ROOT / "evals" / "session" / "run_ab.py"), "--prereg", str(PREREG),
                 "--arm", "B2", "--pilot", "2", "--jobs-dir", str(Path(tmp) / "jobs"), "--skillsbench", str(sb), "--dry-run"],
                capture_output=True, text=True, env=env,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("harbor run -p "), 2)
        self.assertIn("append_system_prompt=", proc.stdout)
        self.assertIn("--skill", proc.stdout)
        self.assertFalse((Path(tmp) / "jobs").exists())

    def test_trial_cap_refused(self):
        prereg = json.loads(PREREG.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs"
            for i in range(prereg["max_trials"]):
                d = jobs / "N-b00" / f"t{i}" / f"t{i}__x"
                d.mkdir(parents=True)
                (d / "result.json").write_text("{}")
            self.assertEqual(run_ab.count_trials(jobs), prereg["max_trials"])


class PreregistrationTests(unittest.TestCase):
    def test_payloads_hash_to_registered_values(self):
        run_ab.load_prereg(PREREG)  # raises on mismatch

    def test_shipped_policy_is_the_treated_arm(self):
        prereg = json.loads(PREREG.read_text())
        self.assertEqual(prereg["arms"]["B2"], "evals/policies/v0.3/B2-runtime.md")
        self.assertIsNone(prereg["arms"]["N"])
        self.assertEqual(prereg["primary_comparison"], ["N", "B2"])

    def test_gates_declared(self):
        prereg = json.loads(PREREG.read_text())
        names = {g["name"] for g in prereg["gates"]}
        self.assertEqual(names, {"delivery_treated", "delivery_control", "enough_pairs", "reward_not_worse"})

    def test_task_order_is_deterministic_and_complete(self):
        prereg = json.loads(PREREG.read_text())
        order = run_ab.task_order(prereg)
        self.assertEqual(order, run_ab.task_order(prereg))
        self.assertEqual(sorted(order), sorted(prereg["tasks"]))
        self.assertNotEqual(order, sorted(order))
        self.assertEqual(len(prereg["tasks"]), 87)


class StatisticsTests(unittest.TestCase):
    def test_sign_test_exact(self):
        self.assertAlmostEqual(report.sign_test_p(0, 0) or -1, -1)
        self.assertAlmostEqual(report.sign_test_p(5, 5), 1.0)
        self.assertAlmostEqual(report.sign_test_p(10, 0), 2 / 1024)
        self.assertAlmostEqual(report.sign_test_p(7, 5), 0.7744140625)

    def test_wilcoxon_direction_and_ties(self):
        self.assertIsNone(report.wilcoxon_p([1, 2, 3]))
        strong = report.wilcoxon_p([-5, -4, -6, -3, -7, -2, -8, -1, -9, -10])
        weak = report.wilcoxon_p([-5, 4, -6, 3, -7, 2, 8, -1, 9, -10])
        self.assertLess(strong, 0.01)
        self.assertGreater(weak, 0.3)
        self.assertIsNotNone(report.wilcoxon_p([-1, -1, -1, -1, -1, -1, -1, 2]))

    def test_bootstrap_is_seeded(self):
        values = [-0.1, -0.2, -0.3, 0.1, -0.4, -0.25]
        self.assertEqual(report.bootstrap_median_ci(values), report.bootstrap_median_ci(values))


class PairingTests(unittest.TestCase):
    def build(self, rows):
        return report.build(rows, json.loads(PREREG.read_text()))

    def test_pairs_deltas_and_quality(self):
        rows = []
        for i, (base, cand) in enumerate([(100, 50), (100, 80), (100, 120), (100, 70), (100, 60), (100, 90)]):
            rows.append(trial(f"t{i}", "N", out=base, cost=base / 100, reward=1.0 if i < 3 else 0.0))
            rows.append(trial(f"t{i}", "B2", out=cand, cost=cand / 100, reward=1.0 if i < 4 else 0.0))
        comp = self.build(rows)["comparisons"][0]
        self.assertEqual((comp["baseline"], comp["candidate"], comp["n_pairs"]), ("N", "B2", 6))
        out = next(m for m in comp["metrics"] if m["metric"] == "output_tokens")
        self.assertAlmostEqual(out["median_rel_delta"], -0.25)
        self.assertAlmostEqual(out["totals_rel_delta"], (470 - 600) / 600)
        self.assertEqual((comp["quality"]["better"], comp["quality"]["worse"], comp["quality"]["tie"]), (1, 0, 5))

    def test_one_sided_failure_pending_and_retry_replaces(self):
        rows = [trial("t0", "N"), trial("t0", "B2", error="AgentTimeoutError", reward=None, cost=None)]
        comp = self.build(rows)["comparisons"][0]
        self.assertEqual(comp["n_pairs"], 0)
        self.assertEqual(comp["one_sided"][0]["failed_arm"], "B2")
        rows.append({**trial("t0", "B2", started="2026-08-22T12:00:00"), "job": "B2-retry-t0"})
        comp = self.build(rows)["comparisons"][0]
        self.assertEqual(comp["n_pairs"], 1)
        self.assertEqual(comp["one_sided"], [])

    def test_symmetric_drop(self):
        rows = [trial("t0", "N", error="RuntimeError", reward=None), trial("t0", "B2", error="RuntimeError", reward=None)]
        comp = self.build(rows)["comparisons"][0]
        self.assertEqual([d["task"] for d in comp["dropped"]], ["t0"])

    def test_gates_read_from_preregistration(self):
        prereg = json.loads(PREREG.read_text())
        rows = []
        for i in range(60):
            rows.append(trial(f"t{i}", "N"))
            rows.append(trial(f"t{i}", "B2"))
        result = gates.evaluate(report.build(rows, prereg), prereg)
        self.assertEqual(result["failed"], [])
        rows[1]["delivered"] = False
        result = gates.evaluate(report.build(rows, prereg), prereg)
        self.assertEqual(result["failed"], ["delivery_treated"])
        rows[1]["delivered"] = True
        for i in range(20):
            rows[2 * i + 1]["reward"] = 0.0
        result = gates.evaluate(report.build(rows, prereg), prereg)
        self.assertEqual(result["failed"], ["reward_not_worse"])
        self.assertEqual(result["decision"], "REVIEW_POLICY")


class CollectTests(unittest.TestCase):
    def test_arm_from_job_name(self):
        self.assertEqual(collect.arm_of("pilot-B2"), "B2")
        self.assertEqual(collect.arm_of("N-b03"), "N")
        self.assertEqual(collect.arm_of("G-retry-some-task"), "G")

    def test_stream_result_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "claude-code.txt"
            p.write_text('{"type":"system"}\nnoise\n{"type":"result","num_turns":7,"total_cost_usd":0.5,"usage":{"output_tokens":9}}\n')
            ev = collect.stream_result(p)
        self.assertEqual((ev["num_turns"], ev["usage"]["output_tokens"]), (7, 9))


class ChecksumTests(unittest.TestCase):
    def test_sha256sums_cover_the_installed_payloads(self):
        text = (ROOT / "SHA256SUMS.txt").read_text()
        for rel in ("skills/simple-man/SKILL.md", "AGENTS.md.snippet", "evals/policies/v0.3/B2-runtime.md"):
            self.assertIn(f"  {rel}\n", text)
        prereg = json.loads(PREREG.read_text())
        self.assertIn(prereg["payload_sha256"]["B2"] + "  ", text)


class PublishedRecordsTests(unittest.TestCase):
    def test_every_published_session_report_rebuilds_from_raw_records(self):
        for release in sorted((ROOT / "evals" / "releases").glob("session-*")):
            for run in ("run", "pilot"):
                trials = release / run / "trials.jsonl"
                if not trials.exists():
                    continue
                with self.subTest(release=release.name, run=run):
                    for script, target in (("session_report.py", "report.md"), ("session_gates.py", "gates.md")):
                        if not (release / run / target).exists():
                            continue
                        proc = subprocess.run(
                            [sys.executable, str(ROOT / "evals" / "session" / script), "--trials", str(trials),
                             "--prereg", str(release / "preregistration.json"), "--check", str(release / run / target)],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
