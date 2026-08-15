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
    def fixture(self, root: Path):
        seeds = root / "seeds"
        seed = seeds / "fixture"
        seed.mkdir(parents=True)
        (seed / "README.md").write_text("failing seed")
        policy = root / "policy.md"
        policy.write_text("policy")
        auth = root / "auth.json"
        auth.write_text('{"token":"private"}')
        project = comparison.Project(
            "fixture", "Fixture", "Fix it", [sys.executable, "-c", "from pathlib import Path; assert Path('fixed').exists()"]
        )
        return seeds, policy, auth, project

    def fake_codex(self, root: Path, *, failing=False, malformed=False) -> Path:
        fake = root / "fake-codex.py"
        body = [
            "#!/usr/bin/env python3",
            "import json, os, pathlib, sys",
            "if '--version' in sys.argv: print('fake-codex 1.0'); raise SystemExit(0)",
            "cwd = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])",
            "count = pathlib.Path(__file__).with_suffix('.calls')",
            "count.write_text(str(int(count.read_text() if count.exists() else '0') + 1))",
            "cwd.joinpath('fixed').write_text('ok')",
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'token=leak /tmp/output'}}))",
        ]
        if malformed:
            body.append("print('{not-json')")
        else:
            body.extend([
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))",
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'cached_input_tokens':0,'output_tokens':1,'reasoning_output_tokens':0}}))",
            ])
        body.append(f"raise SystemExit({1 if failing else 0})")
        fake.write_text("\n".join(body) + "\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake

    def live_args(self, policy: Path, output: Path):
        return ["--variant", f"candidate={policy}", "--model", "fake", "--effort", "low", "--max-calls", "1", "--output-dir", str(output)]

    def test_real_main_fails_closed_before_invocation_for_platform_budget_and_bad_saved_trace(self):
        """Removing a preflight guard must let the fake Codex run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = root / "output"
            patches = mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake))
            with patches, mock.patch.object(comparison.platform, "system", return_value="Linux"):
                with self.assertRaises(SystemExit):
                    comparison.main(self.live_args(policy, output))
            self.assertFalse((root / "fake-codex.calls").exists())

            with patches, mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                with self.assertRaises(SystemExit):
                    comparison.main([*self.live_args(policy, output), "--max-calls", "0"])
            self.assertFalse((root / "fake-codex.calls").exists())

            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project, project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                with self.assertRaises(SystemExit):
                    comparison.main(self.live_args(policy, output))
            with patches, mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                with self.assertRaises(SystemExit):
                    comparison.main([*self.live_args(policy, output), "--max-usd", "1"])
            self.assertFalse((root / "fake-codex.calls").exists())

            raw = comparison.private_root(output) / "raw" / "fixture-candidate-1.jsonl"
            raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps({"record_type": "identity", "identity": {"id": "x"}}) + "\n")
            with patches, mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                with self.assertRaises(SystemExit):
                    comparison.main([*self.live_args(policy, output), "--resume"])
            self.assertFalse((root / "fake-codex.calls").exists())

    def test_real_main_consumes_first_failed_attempt_and_resume_never_retries(self):
        """Changing stop-on-failure would invoke the fake a second time."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root, malformed=True)
            output = root / "output"
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                self.assertEqual(comparison.main(self.live_args(policy, output)), 1)
                self.assertEqual(comparison.main([*self.live_args(policy, output), "--resume"]), 1)
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")
            self.assertFalse(any("auth" in path.name for path in output.rglob("*")))

    def test_seeded_plan_is_deterministic_and_public_export_redacts_adversarial_values(self):
        """Changing identity/redaction would expose unstable plans or private data."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            output = root / "output"
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX="fake"):
                with mock.patch.object(comparison, "source_commit", return_value="base"):
                    with mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                        plan_a = comparison.render_dry_run(["--dry-run", "--seed", "7", "--variant", f"candidate={policy}", "--output-dir", str(output)])
                        plan_b = comparison.render_dry_run(["--dry-run", "--seed", "7", "--variant", f"candidate={policy}", "--output-dir", str(output)])
            self.assertEqual(plan_a, plan_b)

            exported = comparison.public_export(
                output,
                [{"identity": {"id": "run", "project": "fixture", "variant": "candidate", "seed": 7, "trial": 1}, "status": "failed", "messages": ["Authorization: Bearer secret /Users/name/repo"], "stderr": "password=secret", "event": {"api_key": "secret", "path": str(output)}}],
            )
            text = json.dumps(exported)
            for forbidden in ("secret", "Bearer", "/Users/name/repo", str(output)):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
