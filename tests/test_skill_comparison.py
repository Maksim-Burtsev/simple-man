from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
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

codex_home = Path(os.environ.get("CODEX_HOME", "/__missing_codex_home__"))
policy_path = codex_home / "AGENTS.md"
policy = policy_path.read_text() if policy_path.exists() else None

if "sandbox" in args:
    command = args[args.index("--") + 1:]
    if command[:1] == ["/usr/bin/touch"]:
        Path(command[1]).touch()
        raise SystemExit(0)
    if command[:1] == ["/bin/cat"]:
        print("cat: Operation not permitted", file=sys.stderr)
        raise SystemExit(1)
    completed = subprocess.run(command, cwd=Path.cwd(), capture_output=True, text=True)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    raise SystemExit(completed.returncode)

if "debug" in args and "prompt-input" in args:
    messages = [
        {"role": "developer", "content": [{"type": "input_text", "text":
            "workspace-write Network access is restricted Denied filesystem reads "
            + str(codex_home)}]},
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
worker_visible = any(cwd.glob("._case_*"))

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
        "worker_visible": worker_visible,
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
                self.assertFalse(project.worker.is_relative_to(project.root))
                self.assertEqual(
                    set(details[project.key]["allowed_paths"]),
                    set(project.allowed_paths),
                )
                destination = self.root / f"copy-{project.key}"
                quality.copy_fixture(project, destination)
                copied = {
                    path.relative_to(destination).as_posix()
                    for path in destination.rglob("*")
                    if path.is_file()
                }
                tracked = {
                    path.relative_to(project.root).as_posix()
                    for path in quality.tracked_fixture_files(project)
                }
                self.assertEqual(copied, tracked)
                self.assertFalse(any("__pycache__" in path for path in copied))

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
        isolated = quality.IsolatedRun(
            root=Path("/tmp/neutral-run"),
            home=Path("/tmp/neutral-run/home"),
            codex_home=Path("/tmp/neutral-run/codex-home"),
            workspace=Path("/tmp/neutral-run/workspace"),
            env={},
        )
        command = quality.build_codex_command(
            executable="codex",
            model="model",
            effort="high",
            isolated=isolated,
            source_isolation=quality.SourceIsolation((Path("/Users/neutral"),)),
        )

        self.assertIn("never", command)
        self.assertIn('model_verbosity="low"', command)
        self.assertNotIn("--sandbox", command)
        self.assertTrue(any("default_permissions" in value for value in command))
        self.assertTrue(
            any("codex-home" in value and "deny" in value for value in command)
        )
        self.assertTrue(any("network={enabled=false}" in value for value in command))
        self.assertTrue(
            any("shell_environment_policy.inherit" in value for value in command)
        )
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--strict-config", command)
        for feature in quality.DISABLED_FEATURES:
            self.assertIn(feature, command)

    def test_source_isolation_protects_home_worktree_and_common_repository(
        self,
    ) -> None:
        if sys.platform != "darwin":
            with self.assertRaisesRegex(RuntimeError, "require macOS"):
                quality.source_isolation_contract()
            return

        isolation = quality.source_isolation_contract()
        self.assertIn(Path.home().resolve(), isolation.protected_roots)
        self.assertIn(ROOT.resolve(), isolation.protected_roots)
        self.assertTrue(all(path.is_absolute() for path in isolation.protected_roots))

    def test_real_codex_permission_profile_denies_isolated_codex_home(self) -> None:
        executable = shutil.which("codex")
        if sys.platform != "darwin" or executable is None:
            self.skipTest("requires macOS Codex CLI")
        with quality.isolated_run_environment(
            auth_source=self.auth,
            arm=quality.Arm("native_low", None),
        ) as isolated:
            quality.verify_model_tool_isolation(
                executable=quality.resolve_executable(executable),
                isolated=isolated,
                source_isolation=quality.source_isolation_contract(),
            )

    def test_real_codex_prompt_preflight_keeps_native_arm_clean(self) -> None:
        executable = shutil.which("codex")
        if sys.platform != "darwin" or executable is None:
            self.skipTest("requires macOS Codex CLI")
        executable = quality.resolve_executable(executable)
        isolation = quality.source_isolation_contract()
        project = quality.PROJECTS[0]
        arms = quality.build_arms(quality.DEFAULT_POLICY.read_text(encoding="utf-8"))
        for arm in arms.values():
            with self.subTest(arm=arm.name):
                with quality.isolated_run_environment(
                    auth_source=self.auth,
                    arm=arm,
                ) as isolated:
                    quality.initialize_workspace(project, isolated)
                    quality.preflight_model_input(
                        executable=executable,
                        model="gpt-5.5",
                        effort="high",
                        project=project,
                        arm=arm,
                        isolated=isolated,
                        source_isolation=isolation,
                        timeout_seconds=30,
                    )

    def test_validation_profile_denies_source_and_network_and_has_no_auth_env(
        self,
    ) -> None:
        executable = shutil.which("codex")
        if sys.platform != "darwin" or executable is None:
            self.skipTest("requires macOS Codex CLI")
        root = self.root / "validation-profile"
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        environment = quality.validation_environment(root)
        isolation = quality.source_isolation_contract()
        executable = quality.resolve_executable(executable)

        denied = quality.run_bounded(
            quality.validation_sandbox_command(
                executable=executable,
                workspace=workspace,
                protected_roots=isolation.protected_roots,
                command=("/bin/cat", str(ROOT / "README.md")),
            ),
            cwd=workspace,
            env=environment,
            timeout_seconds=10,
            max_output_bytes=10_000,
            max_file_bytes=100_000,
        )
        self.assertNotEqual(denied.process.returncode, 0)
        self.assertIn("Operation not permitted", denied.process.stderr)

        visible_env = quality.run_bounded(
            quality.validation_sandbox_command(
                executable=executable,
                workspace=workspace,
                protected_roots=isolation.protected_roots,
                command=("/usr/bin/env",),
            ),
            cwd=workspace,
            env=environment,
            timeout_seconds=10,
            max_output_bytes=10_000,
            max_file_bytes=100_000,
        )
        self.assertEqual(visible_env.process.returncode, 0)
        self.assertNotIn("CODEX_HOME=", visible_env.process.stdout)
        self.assertNotIn("OPENAI_API_KEY=", visible_env.process.stdout)

        sibling = root / "sibling"
        sibling.mkdir()
        sibling_write = quality.run_bounded(
            quality.validation_sandbox_command(
                executable=executable,
                workspace=workspace,
                protected_roots=isolation.protected_roots,
                command=("/bin/sh", "-c", "echo changed > ../sibling/proof"),
            ),
            cwd=workspace,
            env=environment,
            timeout_seconds=10,
            max_output_bytes=10_000,
            max_file_bytes=100_000,
        )
        self.assertNotEqual(sibling_write.process.returncode, 0)
        self.assertFalse((sibling / "proof").exists())

        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            network = quality.run_bounded(
                quality.validation_sandbox_command(
                    executable=executable,
                    workspace=workspace,
                    protected_roots=isolation.protected_roots,
                    command=(
                        "python3",
                        "-c",
                        "import socket; socket.create_connection(('127.0.0.1', "
                        f"{port}), 1)",
                    ),
                ),
                cwd=workspace,
                env=environment,
                timeout_seconds=10,
                max_output_bytes=10_000,
                max_file_bytes=100_000,
            )
        finally:
            listener.close()
        self.assertNotEqual(network.process.returncode, 0)

    def test_bounded_process_stops_groups_and_caps_output(self) -> None:
        environment = quality.safe_environment()
        timeout = quality.run_bounded(
            ("/bin/sh", "-c", "sleep 30 & wait"),
            cwd=self.root,
            env=environment,
            timeout_seconds=1,
            max_output_bytes=10_000,
            max_file_bytes=100_000,
        )
        self.assertTrue(timeout.timed_out)

        output = quality.run_bounded(
            ("python3", "-c", "print('x' * 100000)"),
            cwd=self.root,
            env=environment,
            timeout_seconds=10,
            max_output_bytes=1_000,
            max_file_bytes=200_000,
        )
        self.assertTrue(output.output_limited)

        workspace = self.root / "workspace-cap"
        workspace.mkdir()
        files = quality.run_bounded(
            (
                "python3",
                "-c",
                "from pathlib import Path; "
                "[Path(f'f{i}').write_text('x') for i in range(201)]",
            ),
            cwd=workspace,
            env=environment,
            timeout_seconds=10,
            max_output_bytes=10_000,
            max_file_bytes=100_000,
            monitor_workspace=workspace,
        )
        self.assertTrue(files.workspace_limited)

        churn = self.root / "workspace-churn"
        churn.mkdir()
        churned = quality.run_bounded(
            (
                "python3",
                "-c",
                "from pathlib import Path\n"
                "import time\n"
                "end = time.monotonic() + 0.7\n"
                "i = 0\n"
                "while time.monotonic() < end:\n"
                "    path = Path(f'tmp-{i}')\n"
                "    path.write_text('x')\n"
                "    path.unlink()\n"
                "    i += 1\n",
            ),
            cwd=churn,
            env=environment,
            timeout_seconds=10,
            max_output_bytes=10_000,
            max_file_bytes=100_000,
            monitor_workspace=churn,
        )
        self.assertEqual(churned.process.returncode, 0)
        self.assertFalse(churned.workspace_limited)

        detached = quality.run_bounded(
            ("/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"),
            cwd=self.root,
            env=environment,
            timeout_seconds=10,
            max_output_bytes=10_000,
            max_file_bytes=100_000,
        )
        background_pid = int(detached.process.stdout.strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(background_pid, 0)

    def test_trace_parser_requires_json_final_usage_and_observes_successful_tests(
        self,
    ) -> None:
        raw = self.root / "trace.jsonl"
        raw.write_text(
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread"}),
                    json.dumps({"type": "turn.started"}),
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
        valid_trace = raw.read_text(encoding="utf-8")
        parsed = quality.parse_codex_trace(
            raw, expected_test_command=("npm", "test"), max_raw_bytes=100_000
        )
        self.assertTrue(parsed["tests_invoked"])

        duplicate_terminal = valid_trace.splitlines()
        duplicate_terminal.insert(
            -1, json.dumps({"type": "turn.completed", "usage": {}})
        )
        raw.write_text("\n".join(duplicate_terminal) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "multiple turn.completed"):
            quality.parse_codex_trace(
                raw,
                expected_test_command=("npm", "test"),
                max_raw_bytes=100_000,
            )

        disallowed_item = valid_trace.splitlines()
        disallowed_item.insert(
            2,
            json.dumps({"type": "item.started", "item": {"type": "mcp_tool_call"}}),
        )
        raw.write_text("\n".join(disallowed_item) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "disallowed item type"):
            quality.parse_codex_trace(
                raw,
                expected_test_command=("npm", "test"),
                max_raw_bytes=100_000,
            )

        misleading = valid_trace.replace(
            "/bin/sh -lc 'npm test'", "/bin/sh -lc 'echo npm test'"
        )
        raw.write_text(misleading, encoding="utf-8")
        parsed = quality.parse_codex_trace(
            raw, expected_test_command=("npm", "test"), max_raw_bytes=100_000
        )
        self.assertFalse(parsed["tests_invoked"])

        for bypass in ("npm test || true", "npm test -- --help"):
            raw.write_text(
                misleading.replace("echo npm test", bypass), encoding="utf-8"
            )
            parsed = quality.parse_codex_trace(
                raw,
                expected_test_command=("npm", "test"),
                max_raw_bytes=100_000,
            )
            self.assertFalse(parsed["tests_invoked"])

        raw.write_text("not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            quality.parse_codex_trace(
                raw, expected_test_command=("npm", "test"), max_raw_bytes=100_000
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
        quality.copy_fixture(project, workspace)
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
            executable=quality.resolve_executable("codex"),
            source_isolation=quality.source_isolation_contract(),
        )

        self.assertFalse(evidence["paths_allowed"])
        self.assertTrue(validation["checks"]["canonical_tests_restored"])
        self.assertTrue(validation["checks"]["canonical_tests_passed"])
        self.assertTrue(validation["checks"]["hidden_cases_passed"])
        self.assertFalse(validation["passed"])

    def test_validation_rejects_early_success_exit_without_test_completion(
        self,
    ) -> None:
        project = next(
            item for item in quality.PROJECTS if item.key == "sqlite-rollout-runner"
        )
        workspace = self.root / "early-exit-workspace"
        quality.copy_fixture(project, workspace)
        environment = quality.safe_environment()
        for command in (
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
        ):
            self.assertEqual(
                quality.run(command, cwd=workspace, env=environment).returncode, 0
            )
        baseline = quality.run(
            ("git", "rev-parse", "HEAD"), cwd=workspace, env=environment
        ).stdout.strip()
        rollout = workspace / "rollout.py"
        rollout.write_text(
            'print("Ran 1 test in 0.001s\\n\\nOK")\nraise SystemExit(0)\n'
            + rollout.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
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
            parent=self.root / "early-exit-validation",
            executable=quality.resolve_executable("codex"),
            source_isolation=quality.source_isolation_contract(),
        )
        self.assertFalse(validation["checks"]["canonical_tests_passed"])
        self.assertFalse(validation["checks"]["hidden_cases_passed"])
        self.assertFalse(validation["passed"])

    def test_parent_owned_hidden_case_catches_boundary_bug(self) -> None:
        project = next(item for item in quality.PROJECTS if item.key == "node-auth-api")
        workspace = self.root / "boundary-workspace"
        quality.copy_fixture(project, workspace)
        environment = quality.safe_environment()
        for command in (
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
        ):
            self.assertEqual(
                quality.run(command, cwd=workspace, env=environment).returncode, 0
            )
        baseline = quality.run(
            ("git", "rev-parse", "HEAD"), cwd=workspace, env=environment
        ).stdout.strip()
        middleware = workspace / "src" / "middleware.js"
        middleware.write_text(
            middleware.read_text(encoding="utf-8").replace(
                '  if (!session) return { status: 401, body: "invalid session" };\n',
                '  if (!session) return { status: 401, body: "invalid session" };\n'
                "  if (session.expiresAt < store.now()) {\n"
                '    return { status: 401, body: "expired session" };\n'
                "  }\n",
            ),
            encoding="utf-8",
        )
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
            parent=self.root / "boundary-validation",
            executable=quality.resolve_executable("codex"),
            source_isolation=quality.source_isolation_contract(),
        )
        cases = {case["case_id"]: case for case in validation["hidden_cases"]}
        self.assertTrue(validation["checks"]["canonical_tests_passed"])
        self.assertTrue(cases["future"]["passed"])
        self.assertFalse(cases["boundary"]["passed"])
        self.assertTrue(cases["expired"]["passed"])
        self.assertFalse(validation["checks"]["hidden_cases_passed"])

    def test_payment_oracle_accepts_request_counting_idempotency(self) -> None:
        project = next(
            item for item in quality.PROJECTS if item.key == "python-payment-ledger"
        )
        workspace = self.root / "request-counting-workspace"
        quality.copy_fixture(project, workspace)
        environment = quality.safe_environment()
        for command in (
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
        ):
            self.assertEqual(
                quality.run(command, cwd=workspace, env=environment).returncode, 0
            )
        baseline = quality.run(
            ("git", "rev-parse", "HEAD"), cwd=workspace, env=environment
        ).stdout.strip()
        (workspace / "ledger.py").write_text(
            """class GatewayTimeout(Exception):
    pass


class FakeGateway:
    def __init__(self):
        self.calls = 0
        self.remote_charges = []
        self._by_key = {}
        self._next_id = 0

    def charge(self, amount_cents, idempotency_key):
        self.calls += 1
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        self._next_id += 1
        result = {"id": f"ch_{self._next_id}", "amount_cents": amount_cents}
        self.remote_charges.append({**result, "idempotency_key": idempotency_key})
        self._by_key[idempotency_key] = result
        if self._next_id == 1:
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
""",
            encoding="utf-8",
        )
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
            parent=self.root / "request-counting-validation",
            executable=quality.resolve_executable("codex"),
            source_isolation=quality.source_isolation_contract(),
        )
        self.assertTrue(validation["checks"]["canonical_tests_passed"])
        self.assertTrue(validation["checks"]["hidden_cases_passed"])
        self.assertTrue(validation["passed"])

    def test_manifest_rejects_embedded_config_tampering(self) -> None:
        path = self.root / "manifest.json"
        config = {"sealed": True}
        manifest = quality.load_or_create_manifest(path, config=config)
        manifest["config"]["sealed"] = False
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(quality.IntegrityError, "embedded config"):
            quality.load_or_create_manifest(path, config=config)

    def test_started_attempt_is_consumed_without_retry(self) -> None:
        paths = quality.attempt_paths(self.root, "run")
        identity = {"project": "fixture", "arm": "native_low", "trial": 1}
        quality.start_attempt(paths, identity=identity)
        self.assertEqual(
            quality.validate_attempt_envelope(paths, identity=identity),
            "interrupted",
        )
        (paths.root / "unexpected.txt").write_text("tamper", encoding="utf-8")
        with self.assertRaisesRegex(quality.IntegrityError, "unexpected artifact"):
            quality.validate_attempt_envelope(paths, identity=identity)
        (paths.root / "unexpected.txt").unlink()
        self.assertEqual(
            quality.validate_attempt_envelope(paths, identity=identity),
            "interrupted",
        )


class EndToEndTests(RunnerFixture):
    @mock.patch.object(quality, "source_git_provenance", return_value=("a" * 40, False))
    def test_infrastructure_failure_is_inconclusive_and_never_retried(
        self, _provenance: mock.Mock
    ) -> None:
        with mock.patch.object(
            quality,
            "execute_run",
            side_effect=quality.InfrastructureError("provider unavailable"),
        ) as execute:
            self.assertEqual(quality.main(self.args()), 2)
            self.assertEqual(quality.main(self.args()), 2)
        execute.assert_called_once()
        summary = json.loads(
            (self.output / "gate-summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary["gate"]["status"], "INCONCLUSIVE")
        self.assertEqual(summary["gate"]["exit_code"], 2)

    @mock.patch.object(quality, "source_git_provenance", return_value=("a" * 40, False))
    def test_fake_run_is_hermetic_sealed_resumable_and_passes_gate(
        self, _provenance: mock.Mock
    ) -> None:
        fixture_hashes_before = {
            project.key: quality.fixture_sha256(project) for project in quality.PROJECTS
        }

        self.assertEqual(quality.main(self.args()), 0)

        calls = self.state()["calls"]
        self.assertEqual(len(calls), 12)
        self.assertEqual(len({call["home"] for call in calls}), 12)
        self.assertEqual(len({call["codex_home"] for call in calls}), 12)
        self.assertEqual(len({call["cwd"] for call in calls}), 12)
        self.assertTrue(all(call["is_git"] for call in calls))
        self.assertTrue(all(not call["worker_visible"] for call in calls))
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
            for path in (self.output / "private" / "attempts").glob("*/01/result.json")
        ]
        self.assertEqual(len(run_payloads), 12)
        self.assertTrue(
            all(payload["validation"]["passed"] for payload in run_payloads)
        )
        self.assertTrue(
            all(
                payload["validation"]["checks"]["canonical_tests_restored"]
                and payload["validation"]["checks"]["worker_injected_after_codex"]
                and payload["validation"]["checks"]["tests_invoked_in_trace"]
                for payload in run_payloads
            )
        )
        self.assertEqual(
            fixture_hashes_before,
            {
                project.key: quality.fixture_sha256(project)
                for project in quality.PROJECTS
            },
        )

        self.assertEqual(quality.main(self.args()), 0)
        self.assertEqual(len(self.state()["calls"]), 12)

        result_path = next(
            (self.output / "private" / "attempts").glob("*/01/result.json")
        )
        original_result = result_path.read_text(encoding="utf-8")
        tampered_result = json.loads(result_path.read_text(encoding="utf-8"))
        tampered_result["repository"]["paths_allowed"] = False
        tampered_result["repository"]["has_production_diff"] = False
        result_path.write_text(json.dumps(tampered_result), encoding="utf-8")

        with self.assertRaisesRegex(quality.IntegrityError, "attempt outcome evidence"):
            quality.main(self.args())
        result_path.write_text(original_result, encoding="utf-8")

        raw = next((self.output / "private" / "attempts").glob("*/01/raw.jsonl"))
        raw.write_text(raw.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(quality.IntegrityError, "attempt outcome evidence"):
            quality.main(self.args())


if __name__ == "__main__":
    unittest.main()
