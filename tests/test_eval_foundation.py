import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import run_skill_comparison as comparison


class EvalFoundationTest(unittest.TestCase):
    def write_fake_codex(self, root: Path) -> Path:
        fake = root / "fake-codex.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "cwd = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])\n"
            "counter = pathlib.Path(__file__).with_suffix('.calls')\n"
            "counter.write_text(str(int(counter.read_text() if counter.exists() else '0') + 1))\n"
            "cwd.joinpath('env.json').write_text(json.dumps(dict(os.environ)))\n"
            "cwd.joinpath('fixed').write_text('ok')\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'working'}}))\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}))\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 2, 'output_tokens': 4, 'reasoning_output_tokens': 1}}))\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake

    def test_main_records_sanitized_trace_and_resume_never_repeats_completed_call(self):
        """Removing trace persistence or resume status must repeat a completed model call."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds = root / "seeds"
            seed = seeds / "fixture"
            seed.mkdir(parents=True)
            (seed / "README.md").write_text("seed")
            policy = root / "policy.md"
            policy.write_text("Policy")
            auth = root / "auth.json"
            auth.write_text('{"token":"secret"}')
            output = root / "output"
            fake = self.write_fake_codex(root)
            project = comparison.Project(
                key="fixture",
                title="Fixture",
                task="Fix it",
                check=[sys.executable, "-c", "from pathlib import Path; assert Path('fixed').exists()"],
            )

            with mock.patch.dict(os.environ, {"EVAL_SECRET": "must-not-reach-codex"}), mock.patch.multiple(
                comparison,
                SEEDS=seeds,
                CODEX=str(fake),
                AUTH=auth,
                PROJECTS=[project],
            ):
                args = [
                    "--variant",
                    f"candidate={policy}",
                    "--model",
                    "fake-model",
                    "--effort",
                    "low",
                    "--max-calls",
                    "1",
                    "--seed",
                    "123",
                    "--output-dir",
                    str(output),
                ]
                self.assertEqual(comparison.main(args), 0)
                self.assertEqual(comparison.main([*args, "--resume"]), 0)

            trace = [json.loads(line) for line in (output / "raw" / "fixture-candidate-1.jsonl").read_text().splitlines()]
            messages = [entry for entry in trace if entry.get("record_type") == "message"]
            self.assertEqual([entry["role"] for entry in messages], ["commentary", "final"])
            self.assertEqual(messages[-1]["text"], "done")
            usage = next(entry for entry in trace if entry.get("record_type") == "usage")
            self.assertEqual(usage["usage"]["output_tokens"], 4)
            artifact = (output / "summary.json").read_text()
            self.assertNotIn(str(root), artifact)
            self.assertNotIn("secret", artifact)
            self.assertEqual(len(list((output / "raw").glob("*.jsonl"))), 1)
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")
            env = json.loads((output / "runs" / "fixture" / "candidate" / "env.json").read_text())
            self.assertNotIn("EVAL_SECRET", env)
            self.assertEqual(env["HOME"], str((output / "homes" / "fixture" / "candidate").resolve()))

    def test_preflight_rejects_bad_trace_missing_inputs_and_budget_without_subprocess(self):
        """Removing a preflight guard must allow an invalid call to reach Codex."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds = root / "seeds"
            (seeds / "fixture").mkdir(parents=True)
            policy = root / "policy.md"
            policy.write_text("Policy")
            project = comparison.Project("fixture", "Fixture", "Fix it", [sys.executable, "-c", "pass"])
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=root / "missing-auth.json"):
                with mock.patch.object(comparison.subprocess, "run") as run:
                    with self.assertRaises(SystemExit):
                        comparison.main(["--variant", f"candidate={policy}", "--model", "m", "--effort", "low", "--max-calls", "1"])
                    with self.assertRaises(SystemExit):
                        comparison.main(["--variant", "candidate=missing.md", "--model", "m", "--effort", "low", "--max-calls", "1"])
                    with self.assertRaises(SystemExit):
                        comparison.main(["--variant", f"candidate={policy}", "--model", "m", "--effort", "low", "--max-calls", "0"])
                    self.assertFalse(run.called)

            output = root / "output"
            raw = output / "raw"
            raw.mkdir(parents=True)
            (raw / "fixture-candidate-1.jsonl").write_text("not json\n")
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=root / "missing-auth.json"):
                with mock.patch.object(comparison.subprocess, "run") as run:
                    with self.assertRaises(SystemExit):
                        comparison.main(["--dry-run", "--variant", f"candidate={policy}", "--output-dir", str(output), "--resume"])
                    self.assertFalse(run.called)

    def test_passing_seed_and_invalid_trace_fail_before_fake_codex(self):
        """Changing a failing seed or accepting an incomplete trace must reach no model call."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds = root / "seeds"
            (seeds / "fixture").mkdir(parents=True)
            policy = root / "policy.md"
            policy.write_text("Policy")
            auth = root / "auth.json"
            auth.write_text("auth")
            project = comparison.Project("fixture", "Fixture", "Fix it", [sys.executable, "-c", "pass"])
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth), mock.patch.object(comparison, "source_commit", return_value="base"), mock.patch.object(comparison, "run", return_value=subprocess.CompletedProcess([], 0)), mock.patch.object(comparison.subprocess, "run") as codex:
                with self.assertRaisesRegex(RuntimeError, "seed must fail"):
                    comparison.main(["--variant", f"candidate={policy}", "--model", "m", "--effort", "low", "--max-calls", "1", "--output-dir", str(root / "output")])
                self.assertFalse(codex.called)

        with self.assertRaisesRegex(ValueError, "expected one final"):
            comparison.parse_codex_events(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "only message"}}))
        with self.assertRaisesRegex(ValueError, "invalid usage"):
            comparison.parse_codex_events(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": -1, "reasoning_output_tokens": 0}}))

    def test_resume_rejects_changed_identity_and_never_retries_failed_attempt(self):
        """Removing identity or failed-attempt guards would repeat a consumed call."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = root / "policy.md"
            policy.write_text("Policy")
            output = root / "output"
            raw = output / "raw" / "fixture-candidate-1.jsonl"
            raw.parent.mkdir(parents=True)
            identity = {"id": "first"}
            raw.write_text(json.dumps({"record_type": "identity", "identity": identity}) + "\n" + json.dumps({"record_type": "call", "status": "started"}) + "\n" + json.dumps({"record_type": "result", "status": "failed"}) + "\n")

            self.assertEqual(comparison.recorded_status(raw, identity), "failed")
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                comparison.recorded_status(raw, {"id": "changed"})

            project = comparison.Project("fixture", "Fixture", "Fix it", [sys.executable, "-c", "pass"])
            with mock.patch.object(comparison, "PROJECTS", [project]):
                with self.assertRaises(SystemExit):
                    comparison.main(["--dry-run", "--variant", f"candidate={policy}", "--output-dir", str(output), "--resume"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = root / "policy.md"
            policy.write_text("Policy")
            with self.assertRaises(SystemExit):
                comparison.main(["--dry-run", "--variant", f"candidate={policy}", "--output-dir", str(ROOT)])



if __name__ == "__main__":
    unittest.main()
