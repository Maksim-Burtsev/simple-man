from __future__ import annotations

import contextlib
import io
import json
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import run_skill_comparison as quality  # noqa: E402


FAKE_CODEX = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
state_path = root / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {"calls": []}
args = sys.argv[1:]

if "--version" in args:
    print("codex-cli fake-quality-1.0")
    raise SystemExit(0)

codex_home = Path(os.environ["CODEX_HOME"])
policy_path = codex_home / "AGENTS.md"
policy = policy_path.read_text() if policy_path.exists() else None

if "debug" in args and "prompt-input" in args:
    messages = [
        {"role": "developer", "content": [{"type": "input_text", "text": "neutral base"}]},
    ]
    if policy is not None:
        messages.append(
            {"role": "developer", "content": [{"type": "input_text", "text": policy}]}
        )
    messages.append(
        {"role": "user", "content": [{"type": "input_text", "text": args[-1]}]}
    )
    print(json.dumps(messages))
    raise SystemExit(0)

if "exec" not in args:
    raise SystemExit(3)

cwd = Path.cwd()
prompt = sys.stdin.read()
is_git = subprocess.run(
    ["git", "rev-parse", "--is-inside-work-tree"],
    cwd=cwd,
    capture_output=True,
    text=True,
).stdout.strip() == "true"
validator_visible = any(cwd.glob("*quality_validator*"))

if (cwd / "src" / "middleware.js").exists():
    project = "node-auth-api"
    path = cwd / "src" / "middleware.js"
    source = path.read_text()
    source = source.replace(
        '  if (!session) return { status: 401, body: "invalid session" };\n',
        '  if (!session) return { status: 401, body: "invalid session" };\n'
        '  if (session.expiresAt <= store.now()) {\n'
        '    return { status: 401, body: "expired session" };\n'
        '  }\n',
    )
    path.write_text(source)
    test_command = "npm test"
elif (cwd / "ledger.py").exists():
    project = "python-payment-ledger"
    (cwd / "ledger.py").write_text('''class GatewayTimeout(Exception):
    pass


class FakeGateway:
    def __init__(self):
        self.calls = 0
        self.remote_charges = []
        self._by_key = {}

    def charge(self, amount_cents, idempotency_key):
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        self.calls += 1
        result = {"id": f"ch_{self.calls}", "amount_cents": amount_cents}
        self.remote_charges.append({**result, "idempotency_key": idempotency_key})
        self._by_key[idempotency_key] = result
        if self.calls == 1:
            raise GatewayTimeout("provider accepted charge but response timed out")
        return result


class PaymentLedger:
    def __init__(self, gateway):
        self.gateway = gateway
        self.local_charges = []
        self._by_key = {}

    def charge(self, customer_id, amount_cents, idempotency_key):
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        result = self.gateway.charge(amount_cents, idempotency_key)
        charge = {
            "provider_id": result["id"],
            "customer_id": customer_id,
            "amount_cents": amount_cents,
            "idempotency_key": idempotency_key,
        }
        self.local_charges.append(charge)
        self._by_key[idempotency_key] = charge
        return charge
''')
    test_command = "python3 -m unittest -v"
elif (cwd / "rollout.py").exists():
    project = "sqlite-rollout-runner"
    path = cwd / "rollout.py"
    source = path.read_text()
    source = source.replace(
        "    apply_drop_migration(conn)\n    backup = backup_legacy_sessions(conn)\n",
        "    backup = backup_legacy_sessions(conn)\n    apply_drop_migration(conn)\n",
    )
    path.write_text(source)
    test_command = "python3 -m unittest -v"
else:
    raise SystemExit(4)

auth = codex_home / "auth.json"
auth_before = auth.read_text()
call_number = len(state["calls"]) + 1
auth.write_text(f"refreshed-{call_number}")
state["calls"].append(
    {
        "argv": args,
        "prompt": prompt,
        "project": project,
        "candidate": policy is not None,
        "policy": policy,
        "home": os.environ["HOME"],
        "codex_home": os.environ["CODEX_HOME"],
        "cwd": str(cwd),
        "is_git": is_git,
        "validator_visible": validator_visible,
        "auth_before": auth_before,
    }
)
state_path.write_text(json.dumps(state))

print(json.dumps({"type": "thread.started", "thread_id": f"fake-{call_number}"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({
    "type": "item.completed",
    "item": {
        "id": "command",
        "type": "command_execution",
        "command": f"/bin/sh -lc '{test_command}'",
        "aggregated_output": "tests passed",
        "exit_code": 0,
        "status": "completed",
    },
}))
print(json.dumps({
    "type": "item.completed",
    "item": {
        "id": "answer",
        "type": "agent_message",
        "text": f"Fixed {project}; {test_command} passed.",
    },
}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": len(prompt), "cached_input_tokens": 0, "output_tokens": 8},
}))
"""


class RunnerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake = self.root / "fake-codex.py"
        self.fake.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
        self.fake.chmod(0o700)
        self.auth = self.root / "auth.json"
        self.auth.write_text("original-auth", encoding="utf-8")
        self.output = self.root / "output"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self, *extra: str) -> list[str]:
        return [
            "--candidate-policy",
            str(ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md"),
            "--output-dir",
            str(self.output),
            "--auth-file",
            str(self.auth),
            "--codex",
            str(self.fake),
            "--model",
            "fake-model",
            "--effort",
            "high",
            *extra,
        ]

    def state(self) -> dict:
        return json.loads((self.root / "state.json").read_text(encoding="utf-8"))


class ContractTests(RunnerFixture):
    def test_fixture_contract_is_three_expected_failing_projects(self) -> None:
        details = quality.ensure_fixture_contract()

        self.assertEqual(len(quality.PROJECTS), 3)
        self.assertEqual(set(details), {project.key for project in quality.PROJECTS})
        for project in quality.PROJECTS:
            with self.subTest(project=project.key):
                self.assertFalse(project.validator.is_relative_to(project.root))
                self.assertEqual(
                    set(details[project.key]["allowed_paths"]),
                    set(project.allowed_paths),
                )

    def test_plan_is_exact_and_caps_fail_before_codex(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(quality.main(self.args("--dry-run")), 0)
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["projects"], 3)
        self.assertEqual(plan["arms"], ["native_low", "candidate_runtime"])
        self.assertEqual(plan["trials"], 2)
        self.assertEqual(plan["calls"], 12)
        self.assertEqual(plan["model_verbosity"], "low")
        self.assertFalse((self.root / "state.json").exists())

    def test_live_run_requires_clean_source_checkout(self) -> None:
        with mock.patch.object(
            quality, "source_git_provenance", return_value=("a" * 40, True)
        ):
            with self.assertRaisesRegex(RuntimeError, "clean source Git checkout"):
                quality.main(self.args())
        self.assertFalse((self.root / "state.json").exists())

        with self.assertRaisesRegex(ValueError, "planned calls exceed"):
            quality.main(self.args("--dry-run", "--max-calls", "11"))
        self.assertFalse((self.root / "state.json").exists())

    def test_schedule_is_secret_deterministic_and_first_arm_balanced(self) -> None:
        keys = quality.planned_run_keys()
        first = quality.secret_balanced_schedule("secret", keys)
        second = quality.secret_balanced_schedule("secret", keys)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 12)
        self.assertEqual(len(set(first)), 12)
        first_arms = [first[index].arm for index in range(0, len(first), 2)]
        self.assertEqual(first_arms.count("native_low"), 3)
        self.assertEqual(first_arms.count("candidate_runtime"), 3)
        for index in range(0, len(first), 2):
            pair = first[index : index + 2]
            self.assertEqual(pair[0].project, pair[1].project)
            self.assertEqual(pair[0].trial, pair[1].trial)
            self.assertEqual({run.arm for run in pair}, set(quality.ARMS))

    def test_codex_command_is_low_verbosity_isolated_and_noninteractive(self) -> None:
        command = quality.build_codex_command(
            executable="codex",
            model="model",
            effort="high",
            workspace=Path("/tmp/neutral-workspace"),
        )

        self.assertIn("workspace-write", command)
        self.assertIn("never", command)
        self.assertIn('model_verbosity="low"', command)
        self.assertIn("sandbox_workspace_write.network_access=false", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--strict-config", command)
        for feature in quality.DISABLED_FEATURES:
            self.assertIn(feature, command)

    def test_source_seatbelt_denies_worktree_and_common_git_reads(self) -> None:
        if sys.platform != "darwin":
            with self.assertRaisesRegex(RuntimeError, "require macOS"):
                quality.source_isolation_contract()
            return

        isolation = quality.source_isolation_contract()
        targets = [
            quality.VALIDATORS / "node-auth-api.test.js",
            isolation.protected_roots[-1] / ".git" / "HEAD",
        ]
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(target.is_file())
                process = quality.run(
                    quality.source_isolated_command(
                        ("/bin/cat", str(target)), isolation
                    ),
                    cwd=self.root,
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("Operation not permitted", process.stderr)

    def test_trace_parser_requires_json_final_usage_and_observes_successful_tests(
        self,
    ) -> None:
        raw = self.root / "trace.jsonl"
        raw.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": "/bin/sh -lc 'npm test'",
                                "exit_code": 0,
                                "status": "completed",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "done"},
                        }
                    ),
                    json.dumps(
                        {"type": "turn.completed", "usage": {"input_tokens": 1}}
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        parsed = quality.parse_codex_trace(
            raw, test_pattern=quality.NODE_TEST_PATTERN, max_raw_bytes=100_000
        )
        self.assertTrue(parsed["tests_invoked"])

        misleading = raw.read_text(encoding="utf-8").replace(
            "/bin/sh -lc 'npm test'", "/bin/sh -lc 'echo npm test'"
        )
        raw.write_text(misleading, encoding="utf-8")
        parsed = quality.parse_codex_trace(
            raw, test_pattern=quality.NODE_TEST_PATTERN, max_raw_bytes=100_000
        )
        self.assertFalse(parsed["tests_invoked"])

        raw.write_text("not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            quality.parse_codex_trace(
                raw, test_pattern=quality.NODE_TEST_PATTERN, max_raw_bytes=100_000
            )

    def test_gate_distinguishes_candidate_failure_from_bad_control(self) -> None:
        results = [
            {
                "project": key.project,
                "arm": key.arm,
                "trial": key.trial,
                "validation": {"passed": True},
            }
            for key in quality.planned_run_keys()
        ]
        self.assertEqual(quality.gate_results(results)["status"], "PASS")

        candidate = next(
            result for result in results if result["arm"] == "candidate_runtime"
        )
        candidate["validation"]["passed"] = False
        candidate_failure = quality.gate_results(results)
        self.assertEqual(candidate_failure["status"], "FAIL")
        self.assertEqual(candidate_failure["exit_code"], 1)

        candidate["validation"]["passed"] = True
        native = next(result for result in results if result["arm"] == "native_low")
        native["validation"]["passed"] = False
        control_failure = quality.gate_results(results)
        self.assertEqual(control_failure["status"], "INCONCLUSIVE")
        self.assertEqual(control_failure["exit_code"], 2)

    def test_validation_restores_tests_but_rejects_their_modification(self) -> None:
        project = next(
            item for item in quality.PROJECTS if item.key == "sqlite-rollout-runner"
        )
        workspace = self.root / "workspace"
        shutil.copytree(project.root, workspace)
        environment = quality.safe_environment()
        commands = (
            ("git", "init", "--quiet"),
            ("git", "add", "."),
            (
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "seed",
            ),
        )
        for command in commands:
            self.assertEqual(
                quality.run(command, cwd=workspace, env=environment).returncode, 0
            )
        baseline = quality.run(
            ("git", "rev-parse", "HEAD"), cwd=workspace, env=environment
        ).stdout.strip()
        rollout = workspace / "rollout.py"
        rollout.write_text(
            rollout.read_text(encoding="utf-8").replace(
                "    apply_drop_migration(conn)\n    backup = backup_legacy_sessions(conn)\n",
                "    backup = backup_legacy_sessions(conn)\n    apply_drop_migration(conn)\n",
            ),
            encoding="utf-8",
        )
        (workspace / "test_rollout.py").write_text("# agent replaced canonical tests\n")

        evidence = quality.collect_repository_evidence(
            project=project,
            workspace=workspace,
            baseline=baseline,
            env=environment,
        )
        production_patch = str(evidence.pop("production_patch"))
        evidence.pop("full_patch")
        validation = quality.validate_production_patch(
            project=project,
            production_patch=production_patch,
            trace_tests_invoked=True,
            repository_evidence=evidence,
            parent=self.root,
            env=environment,
        )

        self.assertFalse(evidence["paths_allowed"])
        self.assertTrue(validation["checks"]["canonical_tests_restored"])
        self.assertTrue(validation["checks"]["canonical_tests_passed"])
        self.assertTrue(validation["checks"]["hidden_validator_passed"])
        self.assertFalse(validation["passed"])


class EndToEndTests(RunnerFixture):
    @mock.patch.object(quality, "source_git_provenance", return_value=("a" * 40, False))
    def test_fake_run_is_hermetic_sealed_resumable_and_passes_gate(
        self, _provenance: mock.Mock
    ) -> None:
        fixture_hashes_before = {
            project.key: quality.tree_sha256(project.root)
            for project in quality.PROJECTS
        }

        self.assertEqual(quality.main(self.args()), 0)

        calls = self.state()["calls"]
        self.assertEqual(len(calls), 12)
        self.assertEqual(len({call["home"] for call in calls}), 12)
        self.assertEqual(len({call["codex_home"] for call in calls}), 12)
        self.assertEqual(len({call["cwd"] for call in calls}), 12)
        self.assertTrue(all(call["is_git"] for call in calls))
        self.assertTrue(all(not call["validator_visible"] for call in calls))
        self.assertEqual(sum(call["candidate"] for call in calls), 6)
        self.assertEqual(sum(not call["candidate"] for call in calls), 6)
        self.assertTrue(
            all(call["policy"] is None for call in calls if not call["candidate"])
        )
        candidate_policy = (
            ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(
            all(
                call["policy"] == candidate_policy
                for call in calls
                if call["candidate"]
            )
        )
        self.assertTrue(all('model_verbosity="low"' in call["argv"] for call in calls))
        self.assertEqual(calls[0]["auth_before"], "original-auth")
        self.assertEqual(calls[1]["auth_before"], "refreshed-1")
        self.assertEqual(self.auth.read_text(encoding="utf-8"), "original-auth")

        first_arms = [calls[index]["candidate"] for index in range(0, 12, 2)]
        self.assertEqual(first_arms.count(True), 3)
        self.assertEqual(first_arms.count(False), 3)

        summary_path = self.output / "gate-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["gate"]["status"], "PASS")
        self.assertEqual(summary["gate"]["passed"], {arm: 6 for arm in quality.ARMS})
        self.assertEqual(stat.S_IMODE(summary_path.stat().st_mode), 0o600)
        manifest = json.loads(
            (self.output / "private" / "manifest.json").read_text(encoding="utf-8")
        )
        config = manifest["config"]
        self.assertEqual(config["source_git_commit"], "a" * 40)
        self.assertFalse(config["source_git_dirty"])
        self.assertEqual(config["source_isolation"]["platform"], "macOS Seatbelt")
        self.assertIn("sqlite", config["runtime_versions"])
        run_payloads = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.output / "private" / "runs").glob("*.json")
        ]
        self.assertEqual(len(run_payloads), 12)
        self.assertTrue(
            all(payload["validation"]["passed"] for payload in run_payloads)
        )
        self.assertTrue(
            all(
                payload["validation"]["checks"]["canonical_tests_restored"]
                and payload["validation"]["checks"]["validator_injected_after_codex"]
                and payload["validation"]["checks"]["tests_invoked_in_trace"]
                for payload in run_payloads
            )
        )
        self.assertEqual(
            fixture_hashes_before,
            {
                project.key: quality.tree_sha256(project.root)
                for project in quality.PROJECTS
            },
        )

        result_path = next((self.output / "private" / "runs").glob("*.json"))
        tampered_result = json.loads(result_path.read_text(encoding="utf-8"))
        tampered_result["repository"]["paths_allowed"] = False
        tampered_result["repository"]["has_production_diff"] = False
        result_path.write_text(json.dumps(tampered_result), encoding="utf-8")

        self.assertEqual(quality.main(self.args()), 0)
        self.assertEqual(len(self.state()["calls"]), 12)
        repaired_result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertTrue(repaired_result["repository"]["paths_allowed"])
        self.assertTrue(repaired_result["repository"]["has_production_diff"])

        raw = next((self.output / "private" / "raw").glob("*.jsonl"))
        raw.write_text(raw.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "artifact hash mismatch"):
            quality.main(self.args())


if __name__ == "__main__":
    unittest.main()
