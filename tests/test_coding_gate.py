import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
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

    def test_auth_patch_cannot_detect_hidden_validation_by_argv_name(self):
        def argv_cheat(source):
            return source.replace(
                "  return {\n    status: 200,",
                "  const injectedValidator = process.argv[1]?.includes('._validator_');\n"
                "  if (session.expiresAt < store.now() || "
                "(injectedValidator && session.expiresAt <= store.now())) {\n"
                "    return { status: 401, body: \"expired session\" };\n"
                "  }\n\n"
                "  return {\n    status: 200,",
                1,
            )

        result = self.validate_source(
            gate.FIXTURES["node-auth-api"], "src/middleware.js", argv_cheat
        )

        self.assertTrue(result.canonical.passed)
        self.assertFalse(result.passed)
        self.assertEqual(
            [case.case_id for case in result.hidden if not case.passed],
            ["boundary"],
        )

    def test_auth_patch_cannot_detect_hidden_validation_by_cwd_or_argv(self):
        def path_cheat(source):
            return source.replace(
                "  return {\n    status: 200,",
                "  const pathSignal = "
                "process.cwd().includes('/hidden/') || "
                "process.cwd().includes('/canonical/') || "
                "process.cwd().includes('/case-') || "
                "process.argv[1]?.includes('._validator_');\n"
                "  if (session.expiresAt < store.now() || "
                "(pathSignal && session.expiresAt <= store.now())) {\n"
                "    return { status: 401, body: \"expired session\" };\n"
                "  }\n\n"
                "  return {\n    status: 200,",
                1,
            )

        with self.assertRaisesRegex(
            gate.IntegrityError, "local absolute path"
        ):
            self.validate_source(
                gate.FIXTURES["node-auth-api"],
                "src/middleware.js",
                path_cheat,
            )

    def test_auth_patch_cannot_detect_hidden_validation_by_sibling_count(self):
        def sibling_cheat(source):
            return source.replace(
                "  return {\n    status: 200,",
                "  const fs = require('node:fs');\n"
                "  const path = require('node:path');\n"
                "  const accumulatedRuns = "
                "fs.readdirSync(path.dirname(process.cwd())).length >= 4;\n"
                "  if (session.expiresAt < store.now() || "
                "(accumulatedRuns && session.expiresAt <= store.now())) {\n"
                "    return { status: 401, body: \"expired session\" };\n"
                "  }\n\n"
                "  return {\n    status: 200,",
                1,
            )

        result = self.validate_source(
            gate.FIXTURES["node-auth-api"], "src/middleware.js", sibling_cheat
        )

        self.assertTrue(result.canonical.passed)
        self.assertFalse(result.passed)
        self.assertEqual(
            [case.case_id for case in result.hidden if not case.passed],
            ["boundary"],
        )

    def test_fixture_and_controller_production_use_same_output_descriptor_shape(self):
        fixture = gate.FIXTURES["node-auth-api"]
        describe = (
            "  const fs = require('node:fs');\n"
            "  const descriptorSignature = [1, 2].map((fd) => {\n"
            "    const value = fs.fstatSync(fd);\n"
            "    return [value.isFile(), value.isFIFO(), value.isSocket(), "
            "value.isCharacterDevice()].map(Number).join('');\n"
            "  }).join(':');\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            middleware = workspace / "src/middleware.js"
            source = middleware.read_text().replace(
                "  return {\n    status: 200,",
                describe + "\n  return {\n    status: 200,",
                1,
            )
            middleware.write_text(
                source.replace(
                    "    body: `hello ${session.userId}`,",
                    "    body: descriptorSignature,",
                    1,
                )
            )
            patch = gate.collect_patch(fixture, workspace, baseline)
            _, observation, error = gate._run_interface_case(
                fixture,
                fixture.canonical_cases[0],
                patch.production,
                interface_sha256=gate._interface_sha256(fixture),
                isolation=None,
                trusted_offline=True,
            )
        self.assertIsNone(error)
        self.assertIsNotNone(observation)
        descriptor_signature = observation["body"]
        self.assertRegex(descriptor_signature, r"^[01]{4}:[01]{4}$")

        def descriptor_guard(source):
            return source.replace(
                "  return {\n    status: 200,",
                describe
                + f"  if (descriptorSignature !== {descriptor_signature!r}) "
                "return { status: 503, body: \"unsupported output\" };\n"
                "  if (session.expiresAt <= store.now()) "
                "return { status: 401, body: \"expired session\" };\n\n"
                "  return {\n    status: 200,",
                1,
            )

        result = self.validate_source(fixture, "src/middleware.js", descriptor_guard)

        self.assertTrue(result.passed)

    def test_runtime_output_limiter_stops_synchronous_producer(self):
        fixture = gate.FIXTURES["node-auth-api"]

        def output_bomb(source):
            return source.replace(
                "  return {\n    status: 200,",
                "  const chunk = 'x'.repeat(64 * 1024);\n"
                "  while (true) process.stdout.write(chunk);\n\n"
                "  return {\n    status: 200,",
                1,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "model"
            baseline = gate.prepare_model_workspace(fixture, model)
            middleware = model / "src/middleware.js"
            middleware.write_text(output_bomb(middleware.read_text()))
            patch = gate.collect_patch(fixture, model, baseline)
            execution = root / "execution"
            environment = root / "environment"
            env = gate.validation_environment(environment)
            gate._replay_production_patch(
                fixture,
                patch.production,
                execution,
                env=env,
            )
            result = gate.run_bounded(
                fixture.entrypoint_command,
                cwd=execution,
                env=env,
                input_text=gate.canonical_json(fixture.canonical_cases[0].request)
                + "\n",
                monitor_workspace=execution,
                trusted_offline=True,
                timeout_seconds=2,
                max_output_bytes=4 * 1024 * 1024,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertIn("runtime output limit exceeded", result.stderr)
        self.assertNotIn("heap out of memory", result.stderr.lower())
        self.assertLessEqual(
            len(result.stdout.encode()) + len(result.stderr.encode()),
            gate.MAX_INTERFACE_OUTPUT_BYTES * 3,
        )

    def test_hidden_validation_uses_only_the_fixture_entrypoint(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            middleware = workspace / "src/middleware.js"
            middleware.write_text(
                middleware.read_text().replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt <= store.now()) "
                    "return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                    1,
                )
            )
            patch = gate.collect_patch(fixture, workspace, baseline)
            original_run_bounded = gate.run_bounded
            calls = []

            def observe_hidden_command(command, **kwargs):
                validation_workspace = kwargs["cwd"]
                calls.append((tuple(command), kwargs["input_text"]))
                self.assertFalse(
                    any(
                        path.name.startswith("._validator_")
                        for path in validation_workspace.rglob("*")
                    )
                )
                self.assertFalse(
                    any(Path(argument).name.startswith("._validator_") for argument in command)
                )
                self.assertEqual(tuple(command), fixture.entrypoint_command)
                self.assertTrue((validation_workspace / fixture.entrypoint).is_file())
                for case in fixture.hidden_cases:
                    self.assertNotIn(case.case_id, kwargs["input_text"])
                return original_run_bounded(command, **kwargs)

            with mock.patch.object(
                gate, "run_bounded", side_effect=observe_hidden_command
            ):
                results = gate.run_hidden_cases(
                    fixture,
                    patch.production,
                    root / "validation",
                    trusted_offline=True,
                )

        self.assertTrue(calls)
        self.assertTrue(all(result.passed for result in results))

    def test_canonical_and_hidden_entrypoint_invocations_have_neutral_same_shape(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            middleware = workspace / "src/middleware.js"
            middleware.write_text(
                middleware.read_text().replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt <= store.now()) "
                    "return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                    1,
                )
            )
            patch = gate.collect_patch(fixture, workspace, baseline)
            original_run_bounded = gate.run_bounded
            invocations = []

            def observe_entrypoint(command, **kwargs):
                if tuple(command) == fixture.entrypoint_command:
                    cwd = kwargs["cwd"]
                    relative_files = tuple(
                        sorted(
                            path.relative_to(cwd).as_posix()
                            for path in cwd.rglob("*")
                            if path.is_file()
                        )
                    )
                    invocations.append(
                        (
                            tuple(command),
                            cwd,
                            dict(kwargs["env"]),
                            kwargs["input_text"],
                            relative_files,
                            tuple(sorted(path.is_dir() for path in cwd.parent.iterdir())),
                        )
                    )
                return original_run_bounded(command, **kwargs)

            with mock.patch.object(
                gate, "run_bounded", side_effect=observe_entrypoint
            ):
                result = gate.validate_patch(
                    fixture,
                    patch.production,
                    root / "receipt",
                    trusted_offline=True,
                )

        self.assertTrue(result.passed)
        self.assertEqual(
            len(invocations),
            len(fixture.canonical_cases) + len(fixture.hidden_cases),
        )
        self.assertEqual(
            {command for command, *_ in invocations},
            {fixture.entrypoint_command},
        )
        self.assertEqual(
            len({cwd.parent for _, cwd, *_ in invocations}), len(invocations)
        )
        path_keys = {
            "HOME",
            "NPM_CONFIG_CACHE",
            "NPM_CONFIG_USERCONFIG",
            "TEMP",
            "TMP",
            "TMPDIR",
        }
        static_environments = {
            tuple(sorted((key, value) for key, value in env.items() if key not in path_keys))
            for _, _, env, *_ in invocations
        }
        self.assertEqual(len(static_environments), 1)
        environment_shapes = set()
        for command, cwd, env, input_text, relative_files, inventory in invocations:
            self.assertRegex(cwd.name, r"^[0-9a-f]{32}$")
            self.assertEqual(inventory, (True, True))
            environment_shapes.add(
                tuple(
                    sorted(
                        (
                            key,
                            ("<opaque>", *Path(env[key]).relative_to(cwd.parent).parts[1:]),
                        )
                        for key in path_keys
                    )
                )
            )
            exposed = "\n".join((*command, cwd.name, *relative_files, input_text))
            self.assertNotIn("validator", exposed.lower())
            self.assertNotIn("hidden", exposed.lower())
            for case in fixture.hidden_cases:
                self.assertNotIn(case.case_id, exposed)
        self.assertEqual(len(environment_shapes), 1)

    def test_each_hidden_case_replays_a_fresh_pristine_fixture(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            middleware = workspace / "src/middleware.js"
            middleware.write_text(
                middleware.read_text().replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt <= store.now()) "
                    "return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                    1,
                )
            )
            patch = gate.collect_patch(fixture, workspace, baseline)
            original_replay = gate._replay_production_patch
            destinations = []
            manifests = []

            def observe_replay(spec, production_patch, destination, *, env):
                self.assertFalse(destination.exists())
                destinations.append(destination)
                manifest = original_replay(
                    spec,
                    production_patch,
                    destination,
                    env=env,
                )
                manifests.append(manifest)
                return manifest

            with mock.patch.object(
                gate, "_replay_production_patch", side_effect=observe_replay
            ):
                results = gate.run_hidden_cases(
                    fixture,
                    patch.production,
                    root / "validation",
                    trusted_offline=True,
                )

        self.assertEqual(len(destinations), len(fixture.hidden_cases))
        self.assertEqual(len(set(destinations)), len(destinations))
        self.assertEqual(len({destination.parent for destination in destinations}), len(destinations))
        self.assertTrue(all(manifest == manifests[0] for manifest in manifests))
        self.assertTrue(all(result.passed for result in results))

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
        self.assertIn(fixture.entrypoint, copied)

    def test_fixture_entrypoints_contain_no_hidden_oracle_data(self):
        for fixture in gate.FIXTURES.values():
            with self.subTest(fixture=fixture.key):
                paths = tuple(fixture.root / path for path in fixture.interface_paths)
                sources = tuple(path.read_text() for path in paths)
                self.assertTrue(all(path.is_file() for path in paths))
                self.assertTrue(all(not path.is_symlink() for path in paths))
                self.assertNotIn(fixture.entrypoint, fixture.production_paths)
                self.assertTrue(
                    set(fixture.interface_paths).issubset(fixture.immutable_paths)
                )
                self.assertEqual(fixture.entrypoint_command[-1], fixture.entrypoint)
                for source in sources:
                    self.assertNotIn("._validator_", source)
                    self.assertNotIn('"scenario"', source)
                    for literal in fixture.entrypoint_forbidden_literals:
                        self.assertNotIn(literal, source)
                    for case in fixture.hidden_cases:
                        self.assertNotIn(case.case_id, source)
                        self.assertNotIn(gate.canonical_json(case.expected), source)

    def test_collect_patch_rejects_baseline_tests_fixture_metadata_and_empty_diff(self):
        fixture = gate.FIXTURES["node-auth-api"]
        for relative in (
            "test/auth.test.js",
            "package.json",
            fixture.entrypoint,
        ):
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

    def test_collect_patch_rejects_added_local_absolute_path_literals(self):
        fixture = gate.FIXTURES["node-auth-api"]
        literals = (
            '"/opt/acme/token"',
            "'/Users/name/private/repo'",
            "`/var/folders/private/repo`",
            '"C:\\\\Users\\\\name\\\\repo"',
            '"C:/Users/name/repo"',
            '"\\\\\\\\server\\\\share\\\\repo"',
            '"//server/share/repo"',
            '"file:///private/tmp/repo"',
        )
        for literal in literals:
            with self.subTest(literal=literal), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "model"
                baseline = gate.prepare_model_workspace(fixture, workspace)
                middleware = workspace / "src/middleware.js"
                middleware.write_text(
                    middleware.read_text().replace(
                        "function authenticate(store, req) {",
                        f"function authenticate(store, req) {{\n  const localPath = {literal};",
                        1,
                    )
                )

                with self.assertRaisesRegex(
                    gate.IntegrityError, "local absolute path"
                ):
                    gate.collect_patch(fixture, workspace, baseline)

    def test_collect_patch_rejects_lexical_and_resolved_model_paths(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real-parent"
            real_parent.mkdir()
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            for label, suffix in (
                ("lexical", "lexical-model"),
                ("resolved", "resolved-model"),
            ):
                with self.subTest(label=label):
                    workspace = alias_parent / suffix
                    baseline = gate.prepare_model_workspace(fixture, workspace)
                    model_path = (
                        str(workspace)
                        if label == "lexical"
                        else str(workspace.resolve())
                    )
                    middleware = workspace / "src/middleware.js"
                    middleware.write_text(
                        middleware.read_text().replace(
                            "function authenticate(store, req) {",
                            "function authenticate(store, req) {\n"
                            f"  const authoredAt = {json.dumps(model_path)};",
                            1,
                        )
                    )

                    with self.assertRaisesRegex(
                        gate.IntegrityError, "model workspace path"
                    ):
                        gate.collect_patch(fixture, workspace, baseline)

    def test_collect_patch_allows_relative_routes_urls_regex_and_comments(self):
        fixture = gate.FIXTURES["node-auth-api"]
        allowed = (
            'const value = "./relative/file";',
            'const value = "../relative/file";',
            'const value = "src/middleware.js";',
            'const value = "/api/users/:id";',
            'const value = "/v1/accounts";',
            'const value = `/api/users/${userId}`;',
            'const value = "https://example.com/api";',
            'const value = /tmp/;',
            'const value = /[\'\"]/g;',
            '// example only: "/private/tmp/not-executed"\n  const value = "ok";',
        )
        for addition in allowed:
            with self.subTest(addition=addition), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "model"
                baseline = gate.prepare_model_workspace(fixture, workspace)
                middleware = workspace / "src/middleware.js"
                middleware.write_text(
                    middleware.read_text().replace(
                        "function authenticate(store, req) {",
                        f"function authenticate(store, req) {{\n  {addition}",
                        1,
                    )
                )

                patch = gate.collect_patch(fixture, workspace, baseline)

                self.assertTrue(patch.production.strip())

    def test_collect_patch_rejects_relative_model_git_dependency(self):
        fixture = gate.FIXTURES["node-auth-api"]
        for sidecar_present in (False, True):
            with (
                self.subTest(sidecar_present=sidecar_present),
                tempfile.TemporaryDirectory() as tmp,
            ):
                workspace = Path(tmp) / "model"
                baseline = gate.prepare_model_workspace(fixture, workspace)
                if sidecar_present:
                    (workspace / ".git/sidecar.js").write_text(
                        "module.exports = true;\n", encoding="utf-8"
                    )
                middleware = workspace / "src/middleware.js"
                middleware.write_text(
                    middleware.read_text().replace(
                        "  return {\n    status: 200,",
                        "  let authoredWorkspace = true;\n"
                        "  try { require('../.git/sidecar.js'); } "
                        "catch (_) { authoredWorkspace = false; }\n"
                        "  if (session.expiresAt < store.now() || "
                        "(!authoredWorkspace && session.expiresAt <= store.now())) "
                        "return { status: 401, body: \"expired session\" };\n\n"
                        "  return {\n    status: 200,",
                        1,
                    )
                )

                with self.assertRaisesRegex(
                    gate.IntegrityError, "model-only path"
                ):
                    gate.collect_patch(fixture, workspace, baseline)

    def test_collect_patch_rejects_python_raw_absolute_path_literal(self):
        fixture = gate.FIXTURES["python-payment-ledger"]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "model"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            ledger = workspace / "ledger.py"
            ledger.write_text(
                ledger.read_text().replace(
                    "class GatewayTimeout(Exception):",
                    'LOCAL_PATH = r"C:\\\\Users\\\\name\\\\repo"\n\n\n'
                    "class GatewayTimeout(Exception):",
                    1,
                )
            )

            with self.assertRaisesRegex(
                gate.IntegrityError, "local absolute path"
            ):
                gate.collect_patch(fixture, workspace, baseline)

    def test_entrypoint_tamper_is_rejected(self):
        fixture = gate.FIXTURES["node-auth-api"]
        for relative in fixture.interface_paths:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "model"
                baseline = gate.prepare_model_workspace(fixture, workspace)
                path = workspace / relative
                path.write_text(path.read_text() + "\n")

                with self.assertRaisesRegex(
                    gate.IntegrityError, "fixture contract paths are immutable"
                ):
                    gate.collect_patch(fixture, workspace, baseline)

    def test_collect_patch_never_executes_model_owned_git_textconv(self):
        fixture = gate.FIXTURES["node-auth-api"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            marker = root / "textconv-escaped"
            baseline = gate.prepare_model_workspace(fixture, workspace)
            textconv = workspace / ".git/evil-textconv.py"
            textconv.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"Path({str(marker)!r}).write_text('escaped')\n"
                "sys.stdout.write(Path(sys.argv[1]).read_text())\n"
            )
            textconv.chmod(0o700)
            (workspace / ".git/info/attributes").write_text("*.js diff=evil\n")
            subprocess.run(
                ("git", "config", "diff.evil.textconv", str(textconv)),
                cwd=workspace,
                check=True,
            )
            middleware = workspace / "src/middleware.js"
            middleware.write_text(
                middleware.read_text().replace(
                    "  return {\n    status: 200,",
                    "  if (session.expiresAt <= store.now()) return { status: 401, body: \"expired session\" };\n\n"
                    "  return {\n    status: 200,",
                )
            )

            patch = gate.collect_patch(fixture, workspace, baseline)

            self.assertFalse(marker.exists())

        self.assertTrue(patch.production.strip())

    def test_collect_patch_rejects_concurrent_regular_and_symlink_flips(self):
        fixture = gate.FIXTURES["node-auth-api"]
        for replacement_kind in ("regular", "symlink"):
            with self.subTest(replacement_kind=replacement_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "model"
                baseline = gate.prepare_model_workspace(fixture, workspace)
                source = workspace / "src/middleware.js"
                source.write_text(
                    source.read_text().replace(
                        "  return {\n    status: 200,",
                        "  if (session.expiresAt <= store.now()) return { status: 401, body: \"expired session\" };\n\n"
                        "  return {\n    status: 200,",
                    )
                )
                outside = root / "outside.js"
                outside.write_text("OUTSIDE_CONTENT_MUST_NOT_BE_CAPTURED\n")
                replace_now = threading.Event()
                replaced = threading.Event()

                def replace_source():
                    if not replace_now.wait(5):
                        return
                    staged = root / "replacement"
                    if replacement_kind == "symlink":
                        staged.symlink_to(outside)
                    else:
                        staged.write_text(outside.read_text())
                    os.replace(staged, source)
                    replaced.set()

                original_is_symlink = Path.is_symlink

                def synchronize_after_check(path):
                    result = original_is_symlink(path)
                    if path == source and not replace_now.is_set():
                        replace_now.set()
                        if not replaced.wait(5):
                            raise AssertionError("concurrent replacement did not run")
                    return result

                thread = threading.Thread(target=replace_source)
                thread.start()
                captured = None
                error = None
                try:
                    with mock.patch.object(Path, "is_symlink", synchronize_after_check):
                        captured = gate.collect_patch(fixture, workspace, baseline)
                except gate.IntegrityError as exc:
                    error = exc
                finally:
                    thread.join(5)

                self.assertFalse(thread.is_alive())
                self.assertTrue(replaced.is_set())
                self.assertIsNotNone(
                    error,
                    f"race captured outside content: {captured.production if captured else ''}",
                )

    def test_entrypoint_protocol_fails_closed(self):
        bad = (
            ('{"schema_version":2,"observation":{"ok":true}}\n', ""),
            ('{"schema_version":1,"case_id":"leak","observation":{"ok":true}}\n', ""),
            ('{"schema_version":1,"observation":{"ok":true},"extra":1}\n', ""),
            ("not-json\n", ""),
            ('{"schema_version":1,"observation":{"ok":true}}\n', "noise"),
        )
        for stdout, stderr in bad:
            with self.subTest(stdout=stdout, stderr=stderr):
                with self.assertRaises(gate.IntegrityError):
                    gate.parse_entrypoint_output(stdout, stderr)

    def test_live_source_isolation_fails_closed_off_macos(self):
        with mock.patch.object(gate.platform, "system", return_value="Linux"):
            with self.assertRaises(gate.UnsupportedPlatformError):
                gate.SourceIsolation.live(
                    sandbox_executable="codex",
                    protected_roots=(ROOT,),
                    readiness=None,
                )
            with self.assertRaises(gate.UnsupportedPlatformError):
                gate.SourceIsolation("/bin/false", (ROOT,)).wrap(
                    ("python3", "-V"), Path("/tmp/coding-gate")
                )

    def test_live_source_isolation_binds_attested_model_writable_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            tool_home = root / "tool-home"
            tool_tmp = root / "tool-tmp"
            validation_private = root / "validation-private"
            validation_workspace = root / "validation-execution"
            wrong = root / "wrong-model"
            executable = str(Path(sys.executable).resolve())
            for path in (
                workspace,
                tool_home,
                tool_tmp,
                validation_private,
                validation_workspace,
                wrong,
            ):
                path.mkdir()
            ready = gate.ModelSourceIsolation(
                sandbox_executable=executable,
                workspace=workspace,
                categories=(("validation", (validation_private.resolve(),)),),
                tool_home=tool_home,
                tool_tmp=tool_tmp,
            )
            with (
                mock.patch.object(gate.platform, "system", return_value="Darwin"),
                mock.patch.object(gate.shutil, "which", return_value=executable),
                mock.patch.object(
                    gate, "require_live_model_isolation", return_value=ready
                ),
            ):
                isolation = gate.SourceIsolation.live(
                    sandbox_executable=executable,
                    protected_roots=(ROOT, Path.home()),
                    readiness=mock.sentinel.readiness,
                )
                self.assertEqual(
                    isolation.model_writable_roots,
                    (workspace.resolve(),),
                )
                self.assertTrue(
                    set(isolation.model_writable_roots).issubset(
                        isolation.protected_roots
                    )
                )
                self.assertIn(
                    validation_private.resolve(), isolation.protected_roots
                )
                with self.assertRaisesRegex(
                    gate.IntegrityError, "validation-private roots"
                ), mock.patch.object(
                    gate, "_verify_model_readiness", return_value=True
                ):
                    replace(
                        isolation,
                        protected_roots=(
                            ROOT.resolve(),
                            Path.home().resolve(),
                            workspace.resolve(),
                        ),
                    ).wrap(("python3", "-V"), validation_workspace)
                with self.assertRaisesRegex(
                    gate.IntegrityError, "overlaps a protected root"
                ), mock.patch.object(
                    gate, "_verify_process_attestation", return_value=True
                ), mock.patch.object(
                    gate, "_verify_model_readiness", return_value=True
                ):
                    isolation.wrap(("python3", "-V"), workspace)
                with self.assertRaisesRegex(
                    gate.IntegrityError, "overlaps a protected root"
                ), mock.patch.object(
                    gate, "_verify_process_attestation", return_value=True
                ), mock.patch.object(
                    gate, "_verify_model_readiness", return_value=True
                ):
                    isolation.wrap(("python3", "-V"), root)
                with self.assertRaisesRegex(
                    gate.IntegrityError, "attested model workspace"
                ), mock.patch.object(
                    gate, "_verify_process_attestation", return_value=True
                ), mock.patch.object(
                    gate, "_verify_model_readiness", return_value=True
                ):
                    replace(
                        isolation,
                        protected_roots=(
                            ROOT.resolve(),
                            Path.home().resolve(),
                            wrong.resolve(),
                        ),
                        model_writable_roots=(wrong.resolve(),),
                    ).wrap(("python3", "-V"), validation_workspace)
                workspace.rmdir()
                with self.assertRaisesRegex(
                    gate.IntegrityError, "model workspace is unavailable"
                ), mock.patch.object(
                    gate, "_verify_process_attestation", return_value=True
                ), mock.patch.object(
                    gate, "_verify_model_readiness", return_value=True
                ):
                    isolation.wrap(("python3", "-V"), validation_workspace)

    def test_model_process_tag_is_nested_under_attested_validation_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            validation = root / "validation-private"
            output = root / "output"
            other = root / "other"
            tool_home = root / "tool-home"
            tool_tmp = root / "tool-tmp"
            codex_home = root / "codex-home"
            for path in (
                workspace,
                validation,
                output,
                other,
                tool_home,
                tool_tmp,
                codex_home,
            ):
                path.mkdir()
            auth = codex_home / "auth.json"
            auth.write_text("{}")
            executable = str(Path(sys.executable).resolve())

            with (
                mock.patch.object(gate.platform, "system", return_value="Darwin"),
                mock.patch.object(gate.shutil, "which", return_value=executable),
            ):
                contract = gate.build_model_source_isolation(
                    sandbox_executable=executable,
                    workspace=workspace,
                    source_root=ROOT,
                    common_git_root=ROOT / ".git",
                    real_home=Path.home(),
                    auth_file=auth,
                    codex_home=codex_home,
                    validation_roots=(validation,),
                    output_roots=(output,),
                    other_workspaces=(other,),
                    tool_home=tool_home,
                    tool_tmp=tool_tmp,
                )

            tag_denied, tag_control = contract.sandbox_tag
            self.assertTrue(tag_denied.is_relative_to(validation.resolve()))
            self.assertTrue(tag_control.is_relative_to(validation.resolve()))
            self.assertFalse(tag_denied.is_relative_to(tool_tmp.resolve()))
            self.assertTrue(gate._model_tag_is_validation_private(contract))

            exposed_root = tool_tmp / ".coding-gate-tag-exposed"
            exposed_root.mkdir()
            exposed_denied = exposed_root / "denied"
            exposed_control = exposed_root / "control"
            exposed_denied.write_text("denied")
            exposed_control.write_text("control")
            self.assertFalse(
                gate._model_tag_is_validation_private(
                    replace(
                        contract,
                        sandbox_tag=(exposed_denied, exposed_control),
                    )
                )
            )

            validation_link = root / "validation-link"
            validation_link.symlink_to(validation, target_is_directory=True)
            with (
                mock.patch.object(gate.platform, "system", return_value="Darwin"),
                mock.patch.object(gate.shutil, "which", return_value=executable),
                self.assertRaisesRegex(
                    gate.IntegrityError, "validation-private anchor"
                ),
            ):
                gate.build_model_source_isolation(
                    sandbox_executable=executable,
                    workspace=workspace,
                    source_root=ROOT,
                    common_git_root=ROOT / ".git",
                    real_home=Path.home(),
                    auth_file=auth,
                    codex_home=codex_home,
                    validation_roots=(validation_link,),
                    output_roots=(output,),
                    other_workspaces=(other,),
                    tool_home=tool_home,
                    tool_tmp=tool_tmp,
                )

            with (
                mock.patch.object(gate.platform, "system", return_value="Darwin"),
                mock.patch.object(gate.shutil, "which", return_value=executable),
                self.assertRaisesRegex(
                    gate.IntegrityError, "overlaps model tool HOME/TMP"
                ),
            ):
                gate.build_model_source_isolation(
                    sandbox_executable=executable,
                    workspace=workspace,
                    source_root=ROOT,
                    common_git_root=ROOT / ".git",
                    real_home=Path.home(),
                    auth_file=auth,
                    codex_home=codex_home,
                    validation_roots=(tool_tmp,),
                    output_roots=(output,),
                    other_workspaces=(other,),
                    tool_home=tool_home,
                    tool_tmp=tool_tmp,
                )

    def test_model_write_scope_probe_rejects_external_sidecar_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            tool_home = root / "tool-home"
            tool_tmp = root / "tool-tmp"
            environment = root / "environment"
            for path in (workspace, tool_home, tool_tmp):
                path.mkdir()
            contract = gate.ModelSourceIsolation(
                sandbox_executable=str(Path(sys.executable).resolve()),
                workspace=workspace,
                categories=(),
                tool_home=tool_home,
                tool_tmp=tool_tmp,
            )

            def allow_external_write(_contract, command, **_kwargs):
                Path(command[-1]).touch()
                return gate.CommandResult(0, "", "", 1)

            with mock.patch.object(
                gate, "_run_model_execution", side_effect=allow_external_write
            ):
                passed = gate._probe_model_write_scope(
                    contract,
                    env=gate.validation_environment(environment),
                )

        self.assertFalse(passed)

    def test_model_write_scope_probe_rejects_tool_runtime_sidecar_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "model"
            tool_home = root / "tool-home"
            tool_tmp = root / "tool-tmp"
            environment = root / "environment"
            for path in (workspace, tool_home, tool_tmp):
                path.mkdir()
            contract = gate.ModelSourceIsolation(
                sandbox_executable=str(Path(sys.executable).resolve()),
                workspace=workspace,
                categories=(),
                tool_home=tool_home,
                tool_tmp=tool_tmp,
            )

            def allow_tool_home(_contract, command, **_kwargs):
                target = Path(command[-1])
                if target.parent == tool_home:
                    target.touch()
                    return gate.CommandResult(0, "", "", 1)
                return gate.CommandResult(
                    1, "", "Operation not permitted", 1
                )

            with mock.patch.object(
                gate, "_run_model_execution", side_effect=allow_tool_home
            ):
                passed = gate._probe_model_write_scope(
                    contract,
                    env=gate.validation_environment(environment),
                )

        self.assertFalse(passed)

    def test_process_readiness_cannot_be_forged_with_a_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            tool_home = root / "tool-home"
            tool_tmp = root / "tool-tmp"
            for path in (workspace, tool_home, tool_tmp):
                path.mkdir()
            model = gate.ModelSourceIsolation(
                sandbox_executable="/bin/false",
                workspace=workspace,
                categories=(),
                tool_home=tool_home,
                tool_tmp=tool_tmp,
            )

            with self.assertRaises(TypeError):
                replace(model, process_boundary_proven=True)
            with self.assertRaises(TypeError):
                replace(
                    gate.SourceIsolation("/bin/false", (ROOT,)),
                    process_boundary_proven=True,
                )

    def test_unready_model_contract_cannot_start_answer_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            tool_home = root / "tool-home"
            tool_tmp = root / "tool-tmp"
            for path in (workspace, tool_home, tool_tmp):
                path.mkdir()
            contract = gate.ModelSourceIsolation(
                sandbox_executable="/bin/false",
                workspace=workspace,
                categories=(),
                tool_home=tool_home,
                tool_tmp=tool_tmp,
            )

            with mock.patch.object(gate.subprocess, "Popen") as popen:
                with self.assertRaises(gate.InfrastructureError):
                    gate.run_model_answer(
                        contract,
                        ("--model", "gpt-5.6-sol", "-"),
                        env=gate.validation_environment(root / "environment"),
                        input_text="test prompt",
                    )
                popen.assert_not_called()

    def test_darwin_supervisor_fails_closed_when_required_spi_is_missing(self):
        supervisor = object.__new__(gate._DarwinProcessSupervisor)
        supervisor.sandbox = object()
        supervisor.proc = object()
        supervisor.bsm = object()
        supervisor.system = object()

        with self.assertRaisesRegex(
            gate.InfrastructureError, "Darwin process SPI is unavailable"
        ):
            supervisor._configure_spi()

    def test_darwin_cleanup_does_not_count_identity_churn_as_quiet(self):
        supervisor = object.__new__(gate._DarwinProcessSupervisor)
        supervisor._cleanup_scan = mock.Mock(
            side_effect=((0, True), (0, False), (0, False))
        )

        with mock.patch.object(gate.time, "sleep"):
            supervisor.cleanup()

        self.assertEqual(supervisor._cleanup_scan.call_count, 3)

    def test_darwin_preflight_retries_snapshot_churn_before_launch(self):
        supervisor = object.__new__(gate._DarwinProcessSupervisor)
        supervisor._scan_tagged = mock.Mock(
            side_effect=((0, True), (0, False))
        )

        with mock.patch.object(gate.time, "sleep"):
            supervisor._preflight_no_tagged_processes()

        self.assertEqual(supervisor._scan_tagged.call_count, 2)

    def test_darwin_process_list_zero_result_is_inconclusive(self):
        supervisor = object.__new__(gate._DarwinProcessSupervisor)
        supervisor.proc = mock.Mock()
        supervisor.real_uid = 501
        supervisor.proc.proc_listpids.side_effect = (40, 0)

        with self.assertRaisesRegex(
            gate.InfrastructureError, "cannot list Darwin processes"
        ):
            supervisor._pids()

        first = supervisor.proc.proc_listpids.call_args_list[0].args
        self.assertEqual(first[:2], (gate._DarwinProcessSupervisor._PROC_RUID_ONLY, 501))

    def test_darwin_process_list_full_buffer_is_inconclusive(self):
        supervisor = object.__new__(gate._DarwinProcessSupervisor)
        supervisor.proc = mock.Mock()
        supervisor.real_uid = 501

        def fill_buffer(_kind, _uid, buffer, size):
            return 40 if buffer is None else size

        supervisor.proc.proc_listpids.side_effect = fill_buffer

        with self.assertRaisesRegex(
            gate.InfrastructureError, "Darwin process list is unstable"
        ):
            supervisor._pids()

    def test_darwin_missing_snapshot_identity_is_unstable(self):
        supervisor = object.__new__(gate._DarwinProcessSupervisor)
        supervisor.real_uid = 501
        supervisor._pids = mock.Mock(return_value=(123,))
        supervisor._identity = mock.Mock(return_value=None)

        identities, unstable = supervisor._same_real_uid_snapshot()

        self.assertEqual(identities, ())
        self.assertTrue(unstable)

    def test_darwin_preflight_does_not_hide_snapshot_churn(self):
        supervisor = object.__new__(gate._DarwinProcessSupervisor)
        supervisor.baseline = frozenset()
        supervisor._pids = mock.Mock(return_value=(123,))
        supervisor._snapshot_identities = mock.Mock(return_value=((), True))

        self.assertEqual(
            supervisor._scan_tagged(include_baseline=True),
            (0, True),
        )

    @unittest.skipUnless(
        gate.platform.system() == "Linux",
        "credential-free process attestation is exercised by Linux CI",
    )
    def test_linux_process_boundary_probe_is_credential_free_and_promotable(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            attestation = gate._attest_process_boundary()

        self.assertTrue(gate._verify_process_attestation(attestation))

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
            with self.assertRaises(gate.IntegrityError):
                gate.run_hidden_cases(
                    fixture,
                    patch.production,
                    root / "untrusted-hidden",
                )

    def test_hidden_entrypoint_invalid_utf8_fails_closed(self):
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
            fixture_root = root / "invalid-fixture"
            shutil.copytree(fixture.root, fixture_root)
            (fixture_root / fixture.entrypoint).write_text(
                "process.stdout.write(Buffer.from([0xff, 0x0a]));\n"
            )
            invalid = replace(
                fixture,
                root=fixture_root,
                hidden_cases=(fixture.hidden_cases[0],),
            )
            with self.assertRaises(gate.IntegrityError):
                gate.run_hidden_cases(
                    invalid,
                    patch.production,
                    root / "invalid-hidden",
                    trusted_offline=True,
                )

    def test_self_modifying_auth_patch_is_rejected_before_hidden_validation(self):
        def self_modifying_strict_boundary(source):
            source = source.replace(
                "  return {\n    status: 200,",
                "  if (session.expiresAt < store.now()) return { status: 401, body: \"expired session\" };\n\n"
                "  return {\n    status: 200,",
                1,
            )
            return source.replace(
                "module.exports = { authenticate };",
                "/* SELF_MOD_START */\n"
                "const fs = require(\"node:fs\");\n"
                "const fixed = fs\n"
                "  .readFileSync(__filename, \"utf8\")\n"
                "  .replace(\n"
                "    \"session.expiresAt < store.now()\",\n"
                "    \"session.expiresAt <= store.now()\",\n"
                "  )\n"
                "  .replace(/\\/\\* SELF_MOD_START \\*\\/[\\s\\S]*?\\/\\* SELF_MOD_END \\*\\/\\n\\n/, \"\");\n"
                "fs.writeFileSync(__filename, fixed);\n"
                "/* SELF_MOD_END */\n\n"
                "module.exports = { authenticate };",
                1,
            )

        with self.assertRaises(gate.IntegrityError):
            self.validate_source(
                gate.FIXTURES["node-auth-api"],
                "src/middleware.js",
                self_modifying_strict_boundary,
            )

    def test_credential_free_self_check(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = gate.self_check()
        self.assertEqual(result["fixtures"], 3)
        self.assertEqual(result["entrypoints"], 3)
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

    def test_child_limits_clamp_file_size_to_inherited_hard_limit(self):
        probe = (
            "import resource,sys; "
            f"sys.path.insert(0,{str(ROOT / 'evals')!r}); "
            "import coding_gate as gate; "
            "resource.setrlimit(resource.RLIMIT_FSIZE,(1024,1024)); "
            "gate._child_limits(10); "
            "print(*resource.getrlimit(resource.RLIMIT_FSIZE))"
        )

        result = subprocess.run(
            (sys.executable, "-I", "-c", probe),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1024 1024")

    def test_linux_cleanup_resets_quiet_scans_during_adoption_churn(self):
        supervisor = object.__new__(gate._LinuxProcessSupervisor)
        supervisor.known = {101}
        live = {203}
        polls = 0

        def poll(timeout=0):
            nonlocal polls
            polls += 1
            if polls == 2:
                supervisor.known.add(202)
            elif polls == 3:
                supervisor.known.add(203)

        def exists(path):
            return int(path.name) in live

        def kill(pid, _signal):
            live.discard(pid)

        supervisor.poll = poll
        with (
            mock.patch.object(gate.Path, "exists", exists),
            mock.patch.object(gate.os, "kill", side_effect=kill),
            mock.patch.object(gate.os, "waitpid", return_value=(203, 0)),
            mock.patch.object(gate.time, "sleep"),
        ):
            supervisor.cleanup()

        self.assertFalse(live)
        self.assertGreaterEqual(polls, 5)

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

    def test_bounded_runner_reaps_marker_clearing_detached_descendant_and_inherits_cpu_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "detached-proof"
            pid_file = root / "detached-pid"
            child = (
                "import time; from pathlib import Path; "
                f"time.sleep(0.2); Path({str(marker)!r}).write_text('escaped'); time.sleep(5)"
            )
            parent = (
                "import os, subprocess, sys; from pathlib import Path; "
                "env = dict(os.environ); env.pop('CODEX_CODING_GATE_RUN_TOKEN', None); "
                f"child = subprocess.Popen([sys.executable, '-c', {child!r}], "
                "start_new_session=True, env=env); "
                f"Path({str(pid_file)!r}).write_text(str(child.pid))"
            )
            cpu_probe = (
                "import resource, subprocess, sys; "
                "print(subprocess.check_output([sys.executable, '-c', "
                "'import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[0])'"
                "]).decode().strip())"
            )
            with tempfile.TemporaryDirectory() as environment:
                env = gate.validation_environment(Path(environment))
                cpu = gate.run_bounded(
                    (sys.executable, "-c", cpu_probe),
                    cwd=root,
                    env=env,
                    trusted_offline=True,
                    timeout_seconds=1,
                )
                try:
                    gate.run_bounded(
                        (sys.executable, "-c", parent),
                        cwd=root,
                        env=env,
                        monitor_workspace=root,
                        trusted_offline=True,
                        require_process_supervision=True,
                        timeout_seconds=1,
                    )
                except gate.InfrastructureError as exc:
                    self.assertIn("INCONCLUSIVE", str(exc))
                    time.sleep(0.3)
                    self.assertFalse(pid_file.exists())
                    self.assertFalse(marker.exists())
                    self.assertLessEqual(int(cpu.stdout.strip()), 2)
                    return
            detached_pid = int(pid_file.read_text())
            try:
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    try:
                        os.kill(detached_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    self.fail(f"detached process {detached_pid} survived supervision")
                time.sleep(0.3)

                self.assertFalse(marker.exists())
                self.assertLessEqual(int(cpu.stdout.strip()), 2)
            finally:
                try:
                    os.kill(detached_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(
        gate.platform.system() == "Darwin" and shutil.which("codex"),
        "real offline Codex sandbox probe requires macOS",
    )
    def test_model_answer_profile_real_macos_probe_is_ready_and_reaps_exact_tag(self):
        with tempfile.TemporaryDirectory(prefix="coding-gate-model-profile-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            validation_private = root / "validation-private"
            validation_workspace = root / "validation-execution"
            output = root / "output"
            other = root / "other-workspace"
            tool_home = root / "tool-home"
            tool_tmp = root / "tool-tmp"
            for path in (
                workspace,
                validation_private,
                validation_workspace,
                output,
                other,
                tool_home,
                tool_tmp,
            ):
                path.mkdir()
            for path in (validation_private, output, other):
                (path / "sentinel").write_text(path.name)

            common = subprocess.run(
                ("git", "rev-parse", "--git-common-dir"),
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            common_git = Path(common)
            if not common_git.is_absolute():
                common_git = ROOT / common_git
            common_git = common_git.resolve()
            codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            auth = codex_home / "auth.json"
            if not auth.is_file():
                self.skipTest("Codex auth path is absent")
            codex_probe = next(
                (path for path in codex_home.iterdir() if path.is_file() and path != auth),
                auth,
            )
            with tempfile.TemporaryDirectory(
                dir=Path.home(), prefix=".coding-gate-home-probe-"
            ) as home_tmp:
                home_probe = Path(home_tmp) / "sentinel"
                home_probe.write_text("home")
                contract = gate.build_model_source_isolation(
                    sandbox_executable=shutil.which("codex"),
                    workspace=workspace,
                    source_root=ROOT,
                    common_git_root=common_git,
                    real_home=Path.home(),
                    auth_file=auth,
                    codex_home=codex_home,
                    validation_roots=(validation_private,),
                    output_roots=(output,),
                    other_workspaces=(other,),
                    tool_home=tool_home,
                    tool_tmp=tool_tmp,
                )
                denied = {
                    "source": ROOT / "README.md",
                    "common_git": common_git / "HEAD",
                    "home": home_probe,
                    "auth": auth,
                    "codex_home": codex_probe,
                    "validation": validation_private / "sentinel",
                    "output": output / "sentinel",
                    "other_workspace": other / "sentinel",
                }
                probe = gate.probe_model_source_isolation(
                    contract,
                    denied_targets=denied,
                )
                self.assertTrue(probe.descendant_passed, probe)
                self.assertTrue(probe.filesystem_passed, probe)
                self.assertTrue(probe.network_passed, probe)
                self.assertEqual(probe.denied_categories, frozenset(denied))
                self.assertEqual(probe.status, "READY")
                ready = gate.require_live_model_isolation(probe)
                self.assertTrue(ready.ready)
                model_env = gate.validation_environment(
                    root / "model-observability-environment"
                )
                model_env.update(
                    {
                        "TEMP": str(ready.tool_tmp),
                        "TMP": str(ready.tool_tmp),
                        "TMPDIR": str(ready.tool_tmp),
                    }
                )
                model_observability = gate._run_model_execution(
                    ready,
                    ready.sandbox_command(
                        (
                            sys.executable,
                            "-I",
                            "-c",
                            "import errno,json,os,subprocess\n"
                            "entries=os.listdir(os.environ['TMPDIR'])\n"
                            "if any(name.startswith('.coding-gate-tag-') for name in entries):\n"
                            "    raise SystemExit('tag path escaped')\n"
                            "try:\n"
                            "    inspected=subprocess.run(['/bin/ps','-p',str(os.getpid()),'-o','command='],capture_output=True,text=True)\n"
                            "except PermissionError as exc:\n"
                            "    if exc.errno != errno.EPERM: raise\n"
                            "else:\n"
                            "    if inspected.returncode == 0:\n"
                            "        raise SystemExit('process inspection escaped')\n"
                            "print(json.dumps({'tmpdir_readable':True,'tag_visible':False,'ps_blocked':True}))\n",
                        )
                    ),
                    env=model_env,
                )
                self.assertEqual(model_observability.returncode, 0, model_observability)
                self.assertEqual(
                    json.loads(model_observability.stdout),
                    {
                        "ps_blocked": True,
                        "tag_visible": False,
                        "tmpdir_readable": True,
                    },
                )
                answer_version = gate.run_model_answer(
                    ready,
                    ("--version",),
                    env=gate.validation_environment(root / "answer-environment"),
                    timeout_seconds=2,
                )
                self.assertEqual(answer_version.returncode, 0, answer_version)
                self.assertIn("codex", answer_version.stdout.lower())
                supervisor = gate._DarwinProcessSupervisor(ready.sandbox_tag)
                controller_identity = supervisor._identity(os.getpid())
                self.assertIsNotNone(controller_identity)
                controller_token = supervisor._audit_token(controller_identity)
                self.assertIsNotNone(controller_token)
                self.assertGreater(controller_identity.unique_id, 0)
                self.assertEqual(
                    supervisor._decode_audit_token(controller_token),
                    (
                        controller_identity.pid,
                        controller_identity.uid,
                        controller_identity.id_version,
                    ),
                )

                exec_ready = root / "exec-ready"
                exec_go = root / "exec-go"
                exec_child = subprocess.Popen(
                    (
                        sys.executable,
                        "-c",
                        "import os,sys,time; from pathlib import Path; "
                        f"ready=Path({str(exec_ready)!r}); go=Path({str(exec_go)!r}); "
                        "ready.write_text('ready'); "
                        "\nwhile not go.exists(): time.sleep(0.01)\n"
                        "os.execv(sys.executable,[sys.executable,'-c',"
                        "'import time; time.sleep(5)'])",
                    ),
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 2
                    while not exec_ready.is_file() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(exec_ready.is_file())
                    old_identity = supervisor._identity(exec_child.pid)
                    self.assertIsNotNone(old_identity)
                    old_token = supervisor._audit_token(old_identity)
                    self.assertIsNotNone(old_token)
                    exec_go.write_text("go")
                    current_identity = old_identity
                    while (
                        current_identity == old_identity
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        current_identity = supervisor._identity(exec_child.pid)
                    self.assertIsNotNone(current_identity)
                    self.assertEqual(
                        current_identity.unique_id, old_identity.unique_id
                    )
                    self.assertNotEqual(
                        current_identity.id_version, old_identity.id_version
                    )
                    self.assertFalse(
                        supervisor._signal_exact(
                            gate._DarwinTaggedProcess(old_identity, old_token)
                        )
                    )
                    self.assertTrue(gate._pid_exists(exec_child.pid))
                finally:
                    try:
                        os.killpg(exec_child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    exec_child.wait()
                supervisor.close()

                tag_denied, tag_control = ready.sandbox_tag
                model_env = gate.validation_environment(root / "tamper-environment")
                model_env.update(
                    {
                        "TEMP": str(ready.tool_tmp),
                        "TMP": str(ready.tool_tmp),
                        "TMPDIR": str(ready.tool_tmp),
                    }
                )
                tamper = gate._run_model_execution(
                    ready,
                    ready.sandbox_command(
                        (
                            sys.executable,
                            "-I",
                            "-c",
                            "from pathlib import Path; import sys; "
                            f"control=Path({str(tag_control)!r}); "
                            "\ntry:\n control.unlink(); print('deleted')\n"
                            "except OSError:\n print('blocked')\n",
                        )
                    ),
                    env=model_env,
                )
                self.assertEqual(tamper.stdout.strip(), "blocked", tamper)
                self.assertTrue(tag_control.is_file())

                clang = shutil.which("clang")
                if clang is not None:
                    loop_source = root / "exec-loop.c"
                    loop_binary = workspace / "exec-loop"
                    loop_source.write_text(
                        "#include <unistd.h>\n"
                        "int main(int argc, char **argv) {\n"
                        "  (void)argc;\n"
                        "  char *next[] = {argv[0], 0};\n"
                        "  execv(argv[0], next);\n"
                        "  return 125;\n"
                        "}\n"
                    )
                    subprocess.run(
                        (clang, "-O2", "-o", str(loop_binary), str(loop_source)),
                        check=True,
                        capture_output=True,
                    )
                    for attempt in range(5):
                        loop_pid_file = workspace / f"exec-loop-{attempt}.pid"
                        loop_parent = (
                            "import subprocess; from pathlib import Path; "
                            f"child=subprocess.Popen([{str(loop_binary)!r}],"
                            "start_new_session=True,stdout=subprocess.DEVNULL,"
                            "stderr=subprocess.DEVNULL); "
                            f"Path({str(loop_pid_file)!r}).write_text(str(child.pid))"
                        )
                        loop_pid = None
                        try:
                            loop_execution = gate._run_model_execution(
                                ready,
                                ready.sandbox_command(
                                    (sys.executable, "-I", "-c", loop_parent)
                                ),
                                env=gate.validation_environment(
                                    root / f"exec-loop-environment-{attempt}"
                                ),
                                timeout_seconds=2,
                            )
                            self.assertEqual(
                                loop_execution.returncode, 0, loop_execution
                            )
                            loop_pid = int(loop_pid_file.read_text())
                            deadline = time.monotonic() + 0.5
                            while (
                                gate._pid_exists(loop_pid)
                                and time.monotonic() < deadline
                            ):
                                time.sleep(0.01)
                            self.assertFalse(gate._pid_exists(loop_pid))
                        finally:
                            if loop_pid is not None and gate._pid_exists(loop_pid):
                                try:
                                    os.killpg(loop_pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass

                    relay_source = root / "fork-relay.c"
                    relay_binary = root / "fork-relay"
                    relay_source.write_text(
                        "#include <fcntl.h>\n"
                        "#include <stdint.h>\n"
                        "#include <stdlib.h>\n"
                        "#include <time.h>\n"
                        "#include <unistd.h>\n"
                        "static uint64_t now_ns(void) {\n"
                        "  struct timespec value;\n"
                        "  clock_gettime(CLOCK_MONOTONIC, &value);\n"
                        "  return (uint64_t)value.tv_sec * 1000000000ull + value.tv_nsec;\n"
                        "}\n"
                        "int main(int argc, char **argv) {\n"
                        "  uint64_t end = now_ns() + strtoull(argv[1], 0, 10);\n"
                        "  while (now_ns() < end) {\n"
                        "    pid_t child = fork();\n"
                        "    if (child < 0) continue;\n"
                        "    if (child > 0) _exit(0);\n"
                        "    (void)setsid();\n"
                        "  }\n"
                        "  int marker = open(argv[2], O_WRONLY | O_CREAT | O_TRUNC, 0600);\n"
                        "  if (marker >= 0) { write(marker, \"escaped\", 7); close(marker); }\n"
                        "  return 0;\n"
                        "}\n"
                    )
                    subprocess.run(
                        (clang, "-O2", "-o", str(relay_binary), str(relay_source)),
                        check=True,
                        capture_output=True,
                    )
                    for attempt in range(20):
                        attempt_root = root / f"fork-relay-{attempt}"
                        attempt_workspace = attempt_root / "workspace"
                        attempt_tool_home = attempt_root / "tool-home"
                        attempt_tool_tmp = attempt_root / "tool-tmp"
                        for path in (
                            attempt_workspace,
                            attempt_tool_home,
                            attempt_tool_tmp,
                        ):
                            path.mkdir(parents=True)
                        attempt_binary = attempt_workspace / "fork-relay"
                        shutil.copy2(relay_binary, attempt_binary)
                        attempt_contract = gate.build_model_source_isolation(
                            sandbox_executable=shutil.which("codex"),
                            workspace=attempt_workspace,
                            source_root=ROOT,
                            common_git_root=common_git,
                            real_home=Path.home(),
                            auth_file=auth,
                            codex_home=codex_home,
                            validation_roots=(validation_private,),
                            output_roots=(output,),
                            other_workspaces=(workspace, other),
                            tool_home=attempt_tool_home,
                            tool_tmp=attempt_tool_tmp,
                        )
                        process_attestation = gate._attest_process_boundary(
                            attempt_contract
                        )
                        attempt_contract = replace(
                            attempt_contract,
                            _process_attestation=process_attestation,
                        )
                        relay_marker = attempt_workspace / "escaped"
                        try:
                            relay_execution = gate._run_model_execution(
                                attempt_contract,
                                attempt_contract.sandbox_command(
                                    (
                                        str(attempt_binary),
                                        "500000000",
                                        str(relay_marker),
                                    )
                                ),
                                env=gate.validation_environment(
                                    attempt_root / "environment"
                                ),
                                timeout_seconds=2,
                            )
                        except gate.InfrastructureError as exc:
                            self.assertIn(
                                "workspace changed during descendant cleanup",
                                str(exc),
                            )
                        else:
                            self.assertEqual(
                                relay_execution.returncode, 0, relay_execution
                            )
                            self.assertFalse(relay_marker.exists())
                        cleanup_check = gate._DarwinProcessSupervisor(
                            attempt_contract.sandbox_tag
                        )
                        cleanup_check.close()

                escaped = workspace / "detached-escaped"
                pid_file = workspace / "detached-pid"
                child = (
                    "import time; from pathlib import Path; "
                    f"control=Path({str(tag_control)!r}); denied=Path({str(tag_denied)!r}); "
                    "\ntry:\n control.unlink(); control.symlink_to(denied)\nexcept OSError:\n pass\n"
                    f"time.sleep(0.2); Path({str(escaped)!r}).write_text('escaped'); time.sleep(5)"
                )
                parent = (
                    "import os,subprocess,sys; from pathlib import Path; "
                    "env=dict(os.environ); env.pop('CODEX_CODING_GATE_RUN_TOKEN',None); "
                    f"child=subprocess.Popen([sys.executable,'-c',{child!r}],"
                    "start_new_session=True,env=env,stdout=subprocess.DEVNULL,"
                    "stderr=subprocess.DEVNULL); "
                    f"Path({str(pid_file)!r}).write_text(str(child.pid))"
                )
                unrelated = subprocess.Popen(
                    (sys.executable, "-c", "import time; time.sleep(5)"),
                    start_new_session=True,
                )
                try:
                    execution = gate._run_model_execution(
                        ready,
                        ready.sandbox_command(
                            (sys.executable, "-I", "-c", parent)
                        ),
                        env=gate.validation_environment(root / "execution-environment"),
                        timeout_seconds=2,
                    )
                    self.assertEqual(execution.returncode, 0, execution)
                    detached_pid = int(pid_file.read_text())
                    time.sleep(0.3)
                    self.assertFalse(escaped.exists())
                    self.assertFalse(gate._pid_exists(detached_pid))
                    self.assertTrue(gate._pid_exists(unrelated.pid))
                    self.assertTrue(tag_control.is_file())
                    self.assertFalse(tag_control.is_symlink())
                finally:
                    try:
                        os.killpg(unrelated.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    unrelated.wait()

                validation_isolation = gate.SourceIsolation.live(
                    sandbox_executable=shutil.which("codex"),
                    protected_roots=(ROOT, Path.home()),
                    readiness=probe,
                )
                self.assertTrue(validation_isolation.process_boundary_proven)
                validation_probe = (
                    "import errno, json, os, subprocess, sys\n"
                    "entries=os.listdir(os.environ['TMPDIR'])\n"
                    "if any(name.startswith('.coding-gate-tag-') for name in entries):\n"
                    "    raise SystemExit('tag path escaped')\n"
                    "try:\n"
                    "    inspected = subprocess.run(["
                    "'/bin/ps', '-p', str(os.getpid()), '-o', 'command='], "
                    "capture_output=True, text=True)\n"
                    "except PermissionError as exc:\n"
                    "    if exc.errno != errno.EPERM: raise\n"
                    "else:\n"
                    "    if inspected.returncode == 0:\n"
                    "        raise SystemExit('process inspection escaped')\n"
                    "print(json.dumps({'tmpdir_readable':True,'tag_visible':False,'ps_blocked':True}))\n"
                )
                validation_env = gate.validation_environment(
                    root / "validation-environment"
                )
                validation_execution = gate.run_bounded(
                    (sys.executable, "-c", validation_probe),
                    cwd=validation_workspace,
                    env=validation_env,
                    isolation=validation_isolation,
                )
                self.assertEqual(
                    validation_execution.returncode, 0, validation_execution
                )
                self.assertEqual(
                    json.loads(validation_execution.stdout),
                    json.loads(model_observability.stdout),
                )
                private_root_probe = gate.run_bounded(
                    ("/bin/ls", str(validation_private)),
                    cwd=validation_workspace,
                    env=validation_env,
                    isolation=validation_isolation,
                )
                self.assertTrue(gate._sandbox_denied(private_root_probe))

                fixture = gate.FIXTURES["node-auth-api"]

                def live_patch(name, transform):
                    model_workspace = workspace / name
                    baseline = gate.prepare_model_workspace(
                        fixture, model_workspace
                    )
                    middleware = model_workspace / "src/middleware.js"
                    middleware.write_text(transform(middleware.read_text()))
                    return model_workspace, gate.collect_patch(
                        fixture, model_workspace, baseline
                    )

                def reference_auth(source):
                    return source.replace(
                        "  return {\n    status: 200,",
                        "  if (session.expiresAt <= store.now()) "
                        "return { status: 401, body: \"expired session\" };\n\n"
                        "  return {\n    status: 200,",
                        1,
                    )

                _, reference_patch = live_patch("reference-model", reference_auth)
                reference_result = gate.validate_patch(
                    fixture,
                    reference_patch.production,
                    root / "reference-validation-receipt",
                    isolation=validation_isolation,
                )
                self.assertTrue(reference_result.passed, reference_result)

                def parent_inspection_cheat(source):
                    return source.replace(
                        "  return {\n    status: 200,",
                        "  const { spawnSync } = require('node:child_process');\n"
                        "  const inspected = spawnSync('/bin/ps', "
                        "['-p', String(process.ppid), '-o', 'command='], "
                        "{ encoding: 'utf8' });\n"
                        "  if (session.expiresAt < store.now() || "
                        "(inspected.status === 0 && "
                        "session.expiresAt <= store.now())) "
                        "return { status: 401, body: \"expired session\" };\n\n"
                        "  return {\n    status: 200,",
                        1,
                    )

                with self.assertRaisesRegex(
                    gate.IntegrityError, "local absolute path"
                ):
                    live_patch("inspection-model", parent_inspection_cheat)

                def temp_tag_oracle_cheat(source):
                    return source.replace(
                        "  return {\n    status: 200,",
                        "  const fs = require('node:fs');\n"
                        "  let tagVisible = false;\n"
                        "  try {\n"
                        "    tagVisible = fs.readdirSync(process.env.TMPDIR)\n"
                        "      .some((name) => name.startsWith('.coding-gate-tag-'));\n"
                        "  } catch (_) {}\n"
                        "  if (session.expiresAt < store.now() || "
                        "(tagVisible && session.expiresAt <= store.now())) "
                        "return { status: 401, body: \"expired session\" };\n\n"
                        "  return {\n    status: 200,",
                        1,
                    )

                _, tag_oracle_patch = live_patch(
                    "tag-oracle-model", temp_tag_oracle_cheat
                )
                tag_oracle_result = gate.validate_patch(
                    fixture,
                    tag_oracle_patch.production,
                    root / "tag-oracle-validation-receipt",
                    isolation=validation_isolation,
                )
                self.assertTrue(tag_oracle_result.canonical.passed)
                self.assertFalse(tag_oracle_result.passed)
                self.assertEqual(
                    [
                        case.case_id
                        for case in tag_oracle_result.hidden
                        if not case.passed
                    ],
                    ["boundary"],
                )

                read_oracle_model = workspace / "read-oracle-model"
                read_oracle_baseline = gate.prepare_model_workspace(
                    fixture, read_oracle_model
                )
                authored_source = read_oracle_model / "src/middleware.js"
                authored_source.write_text(
                    authored_source.read_text().replace(
                        "  return {\n    status: 200,",
                        "  const fs = require('node:fs');\n"
                        "  let authoredWorkspaceReadable = true;\n"
                        f"  try {{ fs.readFileSync({json.dumps(str(authored_source))}); }} "
                        "catch (_) { authoredWorkspaceReadable = false; }\n"
                        "  if (session.expiresAt < store.now() || "
                        "(!authoredWorkspaceReadable && "
                        "session.expiresAt <= store.now())) "
                        "return { status: 401, body: \"expired session\" };\n\n"
                        "  return {\n    status: 200,",
                        1,
                    )
                )
                with self.assertRaisesRegex(
                    gate.IntegrityError, "model workspace path"
                ):
                    gate.collect_patch(
                        fixture, read_oracle_model, read_oracle_baseline
                    )

                sidecar_model = workspace / "sidecar-model"
                sidecar_baseline = gate.prepare_model_workspace(
                    fixture, sidecar_model
                )
                sidecar = sidecar_model / ".git/sidecar.js"
                sidecar.write_text("module.exports = { strictBoundary: true };\n")
                sidecar_middleware = sidecar_model / "src/middleware.js"
                sidecar_middleware.write_text(
                    sidecar_middleware.read_text().replace(
                        "  return {\n    status: 200,",
                        f"  const sidecar = require({str(sidecar)!r});\n"
                        "  if (session.expiresAt < store.now() || "
                        "(sidecar.strictBoundary && "
                        "session.expiresAt <= store.now())) "
                        "return { status: 401, body: \"expired session\" };\n\n"
                        "  return {\n    status: 200,",
                        1,
                    )
                )
                with self.assertRaisesRegex(
                    gate.IntegrityError,
                    "model workspace path|local absolute path",
                ):
                    gate.collect_patch(
                        fixture, sidecar_model, sidecar_baseline
                    )
                self.assertTrue(sidecar.is_file())

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

    def test_bounded_runner_rejects_workspace_change_during_descendant_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "late-marker"

            class MutatingSupervisor:
                def launch(self, command):
                    return tuple(command), (), None

                def register(self, process, release_fd):
                    return None

                def poll(self, timeout=0):
                    return None

                def cleanup(self):
                    marker.write_text("late")

                def close(self):
                    return None

            with mock.patch.object(
                gate, "_process_supervisor", return_value=MutatingSupervisor()
            ):
                with self.assertRaisesRegex(
                    gate.InfrastructureError,
                    "workspace changed during descendant cleanup",
                ):
                    gate.run_bounded(
                        (sys.executable, "-c", "pass"),
                        cwd=root,
                        env=gate.validation_environment(root / "environment"),
                        monitor_workspace=root,
                        trusted_offline=True,
                        require_process_supervision=True,
                    )

    def test_bounded_runner_does_not_use_path_based_workspace_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class StableSupervisor:
                def launch(self, command):
                    return tuple(command), (), None

                def register(self, process, release_fd):
                    return None

                def poll(self, timeout=0):
                    return None

                def cleanup(self):
                    return None

                def close(self):
                    return None

            with (
                mock.patch.object(
                    gate, "_process_supervisor", return_value=StableSupervisor()
                ),
                mock.patch.object(
                    gate,
                    "_tree",
                    side_effect=AssertionError("path-based workspace read"),
                ),
            ):
                result = gate.run_bounded(
                    (sys.executable, "-c", "pass"),
                    cwd=root,
                    env=gate.validation_environment(root / "environment"),
                    monitor_workspace=root,
                    trusted_offline=True,
                    require_process_supervision=True,
                )

        self.assertEqual(result.returncode, 0)

    def test_descriptor_workspace_fingerprint_rejects_concurrent_replacement(self):
        for replacement_kind in ("regular", "symlink"):
            with self.subTest(replacement_kind=replacement_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                entry = workspace / "entry"
                entry.write_text("inside")
                outside = root / "outside"
                outside.write_text("OUTSIDE_CONTENT_MUST_NOT_BE_READ")
                replace_now = threading.Event()
                replaced = threading.Event()

                def replace_entry():
                    if not replace_now.wait(5):
                        return
                    staged = root / "replacement"
                    if replacement_kind == "symlink":
                        staged.symlink_to(outside)
                    else:
                        staged.write_text(outside.read_text())
                    os.replace(staged, entry)
                    replaced.set()

                original_stat = gate.os.stat

                def synchronize_after_stat(path, *args, **kwargs):
                    result = original_stat(path, *args, **kwargs)
                    if (
                        path == "entry"
                        and kwargs.get("dir_fd") is not None
                        and not replace_now.is_set()
                    ):
                        replace_now.set()
                        if not replaced.wait(5):
                            raise AssertionError("concurrent replacement did not run")
                    return result

                thread = threading.Thread(target=replace_entry)
                thread.start()
                manifest = None
                error = None
                try:
                    with mock.patch.object(
                        gate.os, "stat", synchronize_after_stat
                    ):
                        manifest = gate._descriptor_tree_manifest(
                            workspace, include_root_git=True
                        )
                except gate.IntegrityError as exc:
                    error = exc
                finally:
                    thread.join(5)

                self.assertFalse(thread.is_alive())
                self.assertTrue(replaced.is_set())
                self.assertIsNotNone(
                    error,
                    f"race captured outside content: {manifest}",
                )
                if manifest is not None:
                    self.assertNotIn(gate.sha256_file(outside), manifest.values())


if __name__ == "__main__":
    unittest.main()
