import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import coding_gate as gate  # noqa: E402


class CodingGateTests(unittest.TestCase):
    def validate_source(self, fixture, relative, transform):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "model"
        baseline = gate.prepare_model_workspace(fixture, workspace)
        path = workspace / relative
        path.write_text(transform(path.read_text()))
        patch = gate.collect_patch(fixture, workspace, baseline)
        return gate.validate_patch(
            fixture,
            patch.production,
            root / "validation",
            trusted_offline=True,
        )

    def validate_edit(self, fixture, relative, old, new):
        def transform(source):
            self.assertIn(old, source)
            return source.replace(old, new, 1)

        return self.validate_source(fixture, relative, transform)

    def test_auth_strict_less_than_patch_passes_visible_test_but_fails_hidden_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "model"
            baseline = gate.prepare_model_workspace(gate.FIXTURES["node-auth-api"], workspace)
            middleware = workspace / "src/middleware.js"
            middleware.write_text(
                middleware.read_text().replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt < store.now()) return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                )
            )

            patch = gate.collect_patch(gate.FIXTURES["node-auth-api"], workspace, baseline)
            result = gate.validate_patch(
                gate.FIXTURES["node-auth-api"],
                patch.production,
                Path(tmp) / "validation",
                trusted_offline=True,
            )

        self.assertTrue(result.canonical.passed)
        self.assertFalse(result.passed)
        self.assertEqual(
            [case.case_id for case in result.hidden if not case.passed],
            ["boundary"],
        )

    def test_global_ledger_cache_passes_visible_test_but_fails_independent_key(self):
        def global_cache(source):
            source = source.replace(
                "        self.remote_charges = []",
                "        self.remote_charges = []\n        self._cached_result = None",
                1,
            )
            source = source.replace(
                "    def charge(self, amount_cents, idempotency_key):\n"
                "        self.calls += 1",
                "    def charge(self, amount_cents, idempotency_key):\n"
                "        if self._cached_result is not None:\n"
                "            return self._cached_result\n"
                "        self.calls += 1",
                1,
            )
            source = source.replace(
                "        if self.calls == 1:\n"
                "            raise GatewayTimeout(\"provider accepted charge but response timed out\")\n"
                "        return {\"id\": charge_id, \"amount_cents\": amount_cents}",
                "        self._cached_result = {\"id\": charge_id, \"amount_cents\": amount_cents}\n"
                "        if self.calls == 1:\n"
                "            raise GatewayTimeout(\"provider accepted charge but response timed out\")\n"
                "        return self._cached_result",
                1,
            )
            source = source.replace(
                "        self.local_charges = []",
                "        self.local_charges = []\n        self._cached_charge = None",
                1,
            )
            source = source.replace(
                "    def charge(self, customer_id, amount_cents, idempotency_key):\n"
                "        result = self.gateway.charge(amount_cents, idempotency_key)",
                "    def charge(self, customer_id, amount_cents, idempotency_key):\n"
                "        if self._cached_charge is not None:\n"
                "            return self._cached_charge\n"
                "        result = self.gateway.charge(amount_cents, idempotency_key)",
                1,
            )
            return source.replace(
                "        self.local_charges.append(charge)\n        return charge",
                "        self.local_charges.append(charge)\n"
                "        self._cached_charge = charge\n"
                "        return charge",
                1,
            )

        result = self.validate_source(
            gate.FIXTURES["python-payment-ledger"], "ledger.py", global_cache
        )

        self.assertTrue(result.canonical.passed)
        self.assertTrue(next(case for case in result.hidden if case.case_id == "timeout_retry_repeat").passed)
        self.assertFalse(next(case for case in result.hidden if case.case_id == "independent_second_key").passed)
        self.assertFalse(result.passed)

    def test_last_key_ledger_cache_passes_first_cases_but_fails_old_key_replay(self):
        def last_key_cache(source):
            source = source.replace(
                "        self.remote_charges = []",
                "        self.remote_charges = []\n"
                "        self._last_key = None\n"
                "        self._last_result = None",
                1,
            )
            source = source.replace(
                "    def charge(self, amount_cents, idempotency_key):\n"
                "        self.calls += 1",
                "    def charge(self, amount_cents, idempotency_key):\n"
                "        if self._last_key == idempotency_key:\n"
                "            return self._last_result\n"
                "        self.calls += 1",
                1,
            )
            source = source.replace(
                "        if self.calls == 1:\n"
                "            raise GatewayTimeout(\"provider accepted charge but response timed out\")\n"
                "        return {\"id\": charge_id, \"amount_cents\": amount_cents}",
                "        self._last_key = idempotency_key\n"
                "        self._last_result = {\"id\": charge_id, \"amount_cents\": amount_cents}\n"
                "        if self.calls == 1:\n"
                "            raise GatewayTimeout(\"provider accepted charge but response timed out\")\n"
                "        return self._last_result",
                1,
            )
            source = source.replace(
                "        self.local_charges = []",
                "        self.local_charges = []\n"
                "        self._last_key = None\n"
                "        self._last_charge = None",
                1,
            )
            source = source.replace(
                "    def charge(self, customer_id, amount_cents, idempotency_key):\n"
                "        result = self.gateway.charge(amount_cents, idempotency_key)",
                "    def charge(self, customer_id, amount_cents, idempotency_key):\n"
                "        if self._last_key == idempotency_key:\n"
                "            return self._last_charge\n"
                "        result = self.gateway.charge(amount_cents, idempotency_key)",
                1,
            )
            return source.replace(
                "        self.local_charges.append(charge)\n        return charge",
                "        self.local_charges.append(charge)\n"
                "        self._last_key = idempotency_key\n"
                "        self._last_charge = charge\n"
                "        return charge",
                1,
            )

        result = self.validate_source(
            gate.FIXTURES["python-payment-ledger"], "ledger.py", last_key_cache
        )

        hidden = {case.case_id: case.passed for case in result.hidden}
        self.assertTrue(result.canonical.passed)
        self.assertTrue(hidden["timeout_retry_repeat"])
        self.assertTrue(hidden["independent_second_key"])
        self.assertFalse(hidden["replay_old_after_new"])
        self.assertFalse(result.passed)

    def test_sqlite_rebuild_that_loses_extra_columns_and_data_is_caught(self):
        result = self.validate_edit(
            gate.FIXTURES["sqlite-rollout-runner"],
            "rollout.py",
            "def rollout(conn):\n    apply_drop_migration(conn)\n    backup = backup_legacy_sessions(conn)\n    return {\"backup\": backup}\n",
            "def rollout(conn):\n"
            "    backup = backup_legacy_sessions(conn)\n"
            "    conn.executescript(\"\"\"\n"
            "        CREATE TABLE replacement (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL);\n"
            "        INSERT INTO replacement SELECT id, user_id FROM legacy_sessions;\n"
            "        DROP TABLE legacy_sessions;\n"
            "        ALTER TABLE replacement RENAME TO legacy_sessions;\n"
            "    \"\"\")\n"
            "    return {\"backup\": backup}\n",
        )

        self.assertTrue(result.canonical.passed)
        self.assertFalse(result.passed)
        self.assertEqual(
            [case.case_id for case in result.hidden if not case.passed],
            ["preserve_rows_and_note"],
        )

    def test_reference_fixes_pass_canonical_and_hidden_validation(self):
        def ledger_reference(source):
            source = source.replace(
                "        self.remote_charges = []",
                "        self.remote_charges = []\n        self._results = {}",
                1,
            )
            source = source.replace(
                "    def charge(self, amount_cents, idempotency_key):\n"
                "        self.calls += 1",
                "    def charge(self, amount_cents, idempotency_key):\n"
                "        if idempotency_key in self._results:\n"
                "            return self._results[idempotency_key]\n"
                "        self.calls += 1",
                1,
            )
            source = source.replace(
                "        if self.calls == 1:\n"
                "            raise GatewayTimeout(\"provider accepted charge but response timed out\")\n"
                "        return {\"id\": charge_id, \"amount_cents\": amount_cents}",
                "        self._results[idempotency_key] = {\"id\": charge_id, \"amount_cents\": amount_cents}\n"
                "        if self.calls == 1:\n"
                "            raise GatewayTimeout(\"provider accepted charge but response timed out\")\n"
                "        return self._results[idempotency_key]",
                1,
            )
            source = source.replace(
                "        self.local_charges = []",
                "        self.local_charges = []\n        self._charges = {}",
                1,
            )
            source = source.replace(
                "    def charge(self, customer_id, amount_cents, idempotency_key):\n"
                "        result = self.gateway.charge(amount_cents, idempotency_key)",
                "    def charge(self, customer_id, amount_cents, idempotency_key):\n"
                "        if idempotency_key in self._charges:\n"
                "            return self._charges[idempotency_key]\n"
                "        result = self.gateway.charge(amount_cents, idempotency_key)",
                1,
            )
            return source.replace(
                "        self.local_charges.append(charge)\n        return charge",
                "        self.local_charges.append(charge)\n"
                "        self._charges[idempotency_key] = charge\n"
                "        return charge",
                1,
            )

        edits = (
            (
                gate.FIXTURES["node-auth-api"],
                "src/middleware.js",
                lambda source: source.replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt <= store.now()) return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                    1,
                ),
            ),
            (gate.FIXTURES["python-payment-ledger"], "ledger.py", ledger_reference),
            (
                gate.FIXTURES["sqlite-rollout-runner"],
                "rollout.py",
                lambda source: source.replace(
                    "    apply_drop_migration(conn)\n    backup = backup_legacy_sessions(conn)",
                    "    backup = backup_legacy_sessions(conn)\n    apply_drop_migration(conn)",
                    1,
                ),
            ),
        )
        for fixture, relative, transform in edits:
            with self.subTest(fixture=fixture.key):
                self.assertTrue(
                    self.validate_source(fixture, relative, transform).passed
                )

    def test_model_workspace_contains_only_fixture_and_git_metadata(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "model"
            gate.prepare_model_workspace(fixture, workspace)
            copied = {
                path.relative_to(workspace).as_posix()
                for path in workspace.rglob("*")
                if path.is_file()
                and ".git" not in path.relative_to(workspace).parts
            }
            source = {
                path.relative_to(fixture.root).as_posix()
                for path in fixture.root.rglob("*")
                if path.is_file()
            }

        self.assertEqual(copied, source)
        self.assertTrue(
            all(not worker.is_relative_to(gate.FIXTURE_ROOT) for worker in gate.WORKERS)
        )
        self.assertEqual(len(gate.WORKERS), 3)
        self.assertEqual(
            set(gate.WORKERS),
            {path for path in gate.WORKER_ROOT.glob("*") if path.is_file()},
        )

    def test_collect_patch_rejects_baseline_tests_fixture_metadata_and_empty_diff(self):
        fixture = gate.FIXTURES["node-auth-api"]
        for relative in ("test/auth.test.js", "package.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "model"
                baseline = gate.prepare_model_workspace(fixture, workspace)
                path = workspace / relative
                path.write_text(path.read_text() + "\n")
                with self.assertRaises(gate.IntegrityError):
                    gate.collect_patch(fixture, workspace, baseline)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            with self.assertRaises(gate.IntegrityError):
                gate.collect_patch(fixture, workspace, baseline)

    def test_worker_protocol_fails_closed(self):
        case = gate.HiddenCase("case", {}, {"ok": True})
        bad = (
            ('{"schema_version":1,"case_id":"wrong","observation":{"ok":true}}\n', ""),
            ('{"schema_version":1,"case_id":"case","observation":{"ok":true},"extra":1}\n', ""),
            ("not-json\n", ""),
            ('{"schema_version":1,"case_id":"case","observation":{"ok":true}}\n', "noise"),
        )
        for stdout, stderr in bad:
            with self.subTest(stdout=stdout, stderr=stderr):
                with self.assertRaises(gate.IntegrityError):
                    gate.parse_worker_output(case, stdout, stderr)

    def test_live_source_isolation_fails_closed_off_macos(self):
        with mock.patch.object(gate.platform, "system", return_value="Linux"):
            with self.assertRaises(gate.UnsupportedPlatformError):
                gate.SourceIsolation.live(
                    sandbox_executable="codex",
                    protected_roots=(ROOT,),
                )
            with self.assertRaises(gate.UnsupportedPlatformError):
                gate.SourceIsolation("/bin/false", (ROOT,)).wrap(
                    ("python3", "-V"), Path("/tmp/coding-gate")
                )

    def test_model_generated_validation_requires_explicit_isolation_or_trust(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            middleware = workspace / "src/middleware.js"
            middleware.write_text(
                middleware.read_text().replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt <= store.now()) return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                )
            )
            patch = gate.collect_patch(fixture, workspace, baseline)

            with self.assertRaises(gate.IntegrityError):
                gate.validate_patch(fixture, patch.production, root / "untrusted")

            validation = root / "trusted"
            gate.validate_patch(
                fixture,
                patch.production,
                validation,
                trusted_offline=True,
            )
            with tempfile.TemporaryDirectory() as environment:
                with self.assertRaises(gate.IntegrityError):
                    gate.run_hidden_cases(
                        fixture,
                        validation,
                        env=gate.validation_environment(Path(environment)),
                    )

    def test_hidden_worker_invalid_utf8_fails_closed(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            middleware = workspace / "src/middleware.js"
            middleware.write_text(
                middleware.read_text().replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt <= store.now()) return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                )
            )
            patch = gate.collect_patch(fixture, workspace, baseline)
            validation = root / "validation"
            gate.validate_patch(
                fixture,
                patch.production,
                validation,
                trusted_offline=True,
            )
            worker = root / "invalid_utf8.py"
            worker.write_text(
                "import sys\nsys.stdout.buffer.write(b'\\xff\\n')\n"
            )
            invalid = replace(
                fixture,
                worker=worker,
                worker_runtime=sys.executable,
                hidden_cases=(gate.HiddenCase("invalid_utf8", {}, {}),),
            )
            with tempfile.TemporaryDirectory() as environment:
                with self.assertRaises(gate.IntegrityError):
                    gate.run_hidden_cases(
                        invalid,
                        validation,
                        env=gate.validation_environment(Path(environment)),
                        trusted_offline=True,
                    )

    def test_credential_free_self_check(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = gate.self_check()
        self.assertEqual(result["fixtures"], 3)
        self.assertEqual(result["workers"], 3)
        self.assertTrue(result["passed"])

    def test_safe_path_keeps_resolved_ci_runtime_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "bin"
            binary.mkdir()
            for name in ("git", "node", "npm", "python3"):
                executable = binary / name
                executable.write_text("#!/bin/sh\n")
                executable.chmod(0o700)

            safe_path = gate.build_safe_path(str(binary))

        self.assertIn(str(binary.resolve()), safe_path.split(os.pathsep))

    def test_bounded_runner_reaps_children_after_leader_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = (
                "import time; from pathlib import Path; "
                "time.sleep(0.2); Path('late-child').write_text('escaped')"
            )
            parent = (
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}])"
            )
            with tempfile.TemporaryDirectory() as environment:
                gate.run_bounded(
                    (sys.executable, "-c", parent),
                    cwd=root,
                    env=gate.validation_environment(Path(environment)),
                    monitor_workspace=root,
                    trusted_offline=True,
                )
            time.sleep(0.3)

            self.assertFalse((root / "late-child").exists())

    def test_bounded_runner_reports_timeout_output_and_tree_caps(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as environment:
            root = Path(tmp)
            env = gate.validation_environment(Path(environment))
            timeout = gate.run_bounded(
                (sys.executable, "-c", "import time; time.sleep(1)"),
                cwd=root,
                env=env,
                trusted_offline=True,
                timeout_seconds=0.02,
            )
            output = gate.run_bounded(
                (sys.executable, "-c", "print('x' * 10000)"),
                cwd=root,
                env=env,
                trusted_offline=True,
                max_output_bytes=64,
            )
            (root / "entry").write_text("x")
            with mock.patch.object(gate, "MAX_FILES", 0):
                tree = gate.run_bounded(
                    (sys.executable, "-c", "import time; time.sleep(1)"),
                    cwd=root,
                    env=env,
                    monitor_workspace=root,
                    trusted_offline=True,
                )

        self.assertTrue(timeout.timed_out)
        self.assertTrue(output.output_limited)
        self.assertTrue(tree.tree_limited)


if __name__ == "__main__":
    unittest.main()
