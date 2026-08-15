import json
import contextlib
import io
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
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Authorization: Bearer live_token {\"access_token\":\"quoted_live_token\"} /Users/name/repo'}}))",
            "print(json.dumps({'type':'item.completed','item':{'type':'tool_result','nested':{'api_key':'nested_token','path':'/tmp/private'}}}))",
            "print('password=stderr_token /tmp/stderr', file=sys.stderr)",
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

            raw = comparison.ledger_path(output, project, "candidate")
            raw.parent.mkdir(parents=True, exist_ok=True)
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
            private = comparison.private_root(output)
            self.assertIn("Authorization: Bearer live_token", (private / "raw" / "fixture-candidate-1.stdout.jsonl").read_text())
            self.assertIn("{not-json", (private / "raw" / "fixture-candidate-1.stdout.jsonl").read_text())
            self.assertIn("stderr_token", (private / "raw" / "fixture-candidate-1.stderr.txt").read_text())

    def test_real_main_started_only_resume_is_consumed_failure(self):
        """Removing started-attempt handling must crash or invoke fake Codex again."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = root / "output"
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                parser = comparison.build_parser()
                args = parser.parse_args(self.live_args(policy, output))
                variants = comparison.parse_variants(args.variant)
                comparison.preflight(args, parser, variants)
                identity = comparison.run_identity(project, "candidate", policy, 1, args, variants)
                raw = comparison.ledger_path(output, project, "candidate")
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_text(json.dumps({"record_type": "identity", "identity": identity}) + "\n" + json.dumps({"record_type": "call", "status": "started"}) + "\n")
                self.assertEqual(comparison.main([*self.live_args(policy, output), "--resume"]), 1)
            self.assertFalse((root / "fake-codex.calls").exists())

    def test_seeded_plan_is_deterministic_and_public_export_redacts_adversarial_values(self):
        """Changing identity/redaction would expose unstable plans or private data."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            output = root / "output"
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX="fake"):
                with mock.patch.object(comparison, "source_commit", return_value="base"):
                    with mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                        streams = [io.StringIO(), io.StringIO()]
                        with contextlib.redirect_stdout(streams[0]):
                            self.assertEqual(comparison.main(["--dry-run", "--seed", "7", "--variant", f"candidate={policy}", "--output-dir", str(output)]), 0)
                        with contextlib.redirect_stdout(streams[1]):
                            self.assertEqual(comparison.main(["--dry-run", "--seed", "7", "--variant", f"candidate={policy}", "--output-dir", str(output)]), 0)
                        plan_a, plan_b = (stream.getvalue() for stream in streams)
            self.assertEqual(plan_a, plan_b)

            exported = comparison.public_export(
                output,
                [{"identity": {"id": "run", "project": "fixture", "variant": "candidate", "seed": 7, "trial": 1}, "status": "failed", "messages": ["Authorization: Bearer secret /Users/name/repo"], "stderr": "password=secret", "event": {"api_key": "secret", "path": str(output)}}],
            )
            text = json.dumps(exported)
            for forbidden in ("secret", "Bearer", "/Users/name/repo", str(output)):
                self.assertNotIn(forbidden, text)

    def test_real_main_completed_resume_and_public_artifact_are_safe(self):
        """Removing completion/resume or artifact redaction must repeat or leak fake output."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = root / "output"
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                self.assertEqual(comparison.main(self.live_args(policy, output)), 0)
                self.assertEqual(comparison.main([*self.live_args(policy, output), "--resume"]), 0)
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")
            public = (output / "summary.json").read_text()
            public_run = json.loads(public)["runs"][0]
            self.assertEqual(set(public_run), {"id", "project", "variant", "seed", "trial", "status", "usage"})
            self.assertEqual(set(public_run["usage"]), {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"})
            self.assertNotIn("tool_result", public)
            for forbidden in ("live_token", "quoted_live_token", "nested_token", "stderr_token", "/Users/name/repo", "/tmp/private", "/tmp/stderr", "Bearer"):
                self.assertNotIn(forbidden, public)

            raw = comparison.ledger_path(output, project, "candidate")
            original = [json.loads(line) for line in raw.read_text().splitlines()]
            corruptions = []
            event = json.loads(json.dumps(original)); event[2]["event"] = "corrupt"; corruptions.append(event)
            usage = json.loads(json.dumps(original)); usage[-2]["usage"]["output_tokens"] = 99; corruptions.append(usage)
            boolean_usage = json.loads(json.dumps(original)); boolean_usage[-2]["usage"]["output_tokens"] = True; corruptions.append(boolean_usage)
            message = json.loads(json.dumps(original)); next(record for record in message if record.get("record_type") == "message").pop("text"); corruptions.append(message)
            divergent = json.loads(json.dumps(original)); next(record for record in divergent if record.get("record_type") == "message")["text"] = "different"; corruptions.append(divergent)
            forged = json.loads(json.dumps(original)); forged[-1]["codex_exit"] = 1; corruptions.append(forged)
            for records in corruptions:
                raw.write_text("".join(json.dumps(record) + "\n" for record in records))
                with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                    with self.assertRaises(SystemExit):
                        comparison.main([*self.live_args(policy, output), "--resume"])
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")

    def test_private_symlinks_and_same_identity_non_resume_fail_before_fake_call(self):
        """Removing no-follow or ledger guards must write outside private storage or repeat a call."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = root / "output"
            private = comparison.private_root(output)
            private.symlink_to(root / "escape")
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                with self.assertRaises(SystemExit):
                    comparison.main(self.live_args(policy, output))
            self.assertFalse((root / "fake-codex.calls").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            escape = root / "escape"
            escape.mkdir()
            (root / "output-link").symlink_to(escape, target_is_directory=True)
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                with self.assertRaises(SystemExit):
                    comparison.main(self.live_args(policy, root / "output-link" / "output"))
            self.assertFalse((root / "fake-codex.calls").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = root / "output"
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                self.assertEqual(comparison.main(self.live_args(policy, output)), 0)
                with self.assertRaises(SystemExit):
                    comparison.main(self.live_args(policy, output))
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")

    def test_private_temp_symlink_collision_never_follows_escape_target(self):
        """Replacing no-follow atomic writes must truncate the symlink target."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = comparison.safe_output_dir(root / "output")
            private = comparison.prepare_private_root(output)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            ledger = comparison.ledger_path(output, project, "candidate")
            escape = root / "escape"
            escape.write_text("must survive")
            ledger.with_name(f".{ledger.name}.occupied.tmp").symlink_to(escape)
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"), mock.patch.object(comparison.secrets, "token_hex", side_effect=["occupied", "ledger", "summary"]):
                self.assertEqual(comparison.main(self.live_args(policy, output)), 0)
            self.assertEqual(escape.read_text(), "must survive")
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")

    def test_resume_rejects_raw_only_and_tampered_raw_evidence_before_fake_call(self):
        """Dropping raw evidence binding must either start a fresh call or trust altered evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = comparison.safe_output_dir(root / "output")
            private = comparison.prepare_private_root(output)
            (private / "raw" / "fixture-candidate-1.jsonl").write_text("legacy raw evidence\n")
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                with self.assertRaises(SystemExit):
                    comparison.main([*self.live_args(policy, output), "--resume"])
            self.assertFalse((root / "fake-codex.calls").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = root / "output"
            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"):
                self.assertEqual(comparison.main(self.live_args(policy, output)), 0)
                comparison.raw_path(comparison.safe_output_dir(output), project, "candidate", "stdout.jsonl").write_text("altered evidence\n")
                with self.assertRaises(SystemExit):
                    comparison.main([*self.live_args(policy, output), "--resume"])
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")

    def test_check_launch_error_is_a_consumed_trace_with_saved_usage(self):
        """Dropping the check-error state must reject an otherwise complete failed attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeds, policy, auth, project = self.fixture(root)
            fake = self.fake_codex(root)
            output = root / "output"
            original_run = comparison.run
            check_calls = 0

            def fail_only_the_post_model_check(cmd, *, cwd, env=None):
                nonlocal check_calls
                if cmd == project.check:
                    check_calls += 1
                    if check_calls == 2:
                        raise OSError("check executable disappeared")
                return original_run(cmd, cwd=cwd, env=env)

            with mock.patch.multiple(comparison, SEEDS=seeds, PROJECTS=[project], AUTH=auth, CODEX=str(fake)), mock.patch.object(comparison.platform, "system", return_value="Darwin"), mock.patch.object(comparison, "run", side_effect=fail_only_the_post_model_check):
                self.assertEqual(comparison.main(self.live_args(policy, output)), 1)
                self.assertEqual(comparison.main([*self.live_args(policy, output), "--resume"]), 1)
            records = [json.loads(line) for line in comparison.ledger_path(comparison.safe_output_dir(output), project, "candidate").read_text().splitlines()]
            self.assertTrue(any(record.get("record_type") == "usage" for record in records))
            self.assertEqual(records[-1]["check_error"], "check executable disappeared")
            self.assertEqual((root / "fake-codex.calls").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
