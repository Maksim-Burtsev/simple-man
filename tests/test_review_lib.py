import json
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import review_lib as review  # noqa: E402
import run_blind_review as runner  # noqa: E402


RUNTIME_POLICY = """## Simple Man runtime policy

Apply Simple Man to user-facing responses by default.
"""


class ReviewLibTests(unittest.TestCase):
    def test_prompt_preflight_rejects_treatment_names(self):
        prompts = [
            {
                "id": "leak",
                "category": "status",
                "prompt": "Compare Simple Man with native_low and model_verbosity.",
            }
        ]

        with self.assertRaisesRegex(
            ValueError, "prompt treatment contamination"
        ) as error:
            review.validate_prompt_contamination(prompts)

        self.assertIn("Simple Man name", str(error.exception))
        self.assertIn("native-low arm", str(error.exception))
        self.assertIn("model verbosity treatment", str(error.exception))

    def test_load_prompts_preserves_language_and_verified_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompts.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "id": "ru-status",
                        "category": "status",
                        "language": "ru",
                        "prompt": "Что сломано?",
                        "verified_context": "Tests failed.",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            prompts = review.load_prompts(path)

        self.assertEqual(prompts[0]["language"], "ru")
        self.assertEqual(prompts[0]["verified_context"], "Tests failed.")

    def test_load_prompts_requires_string_verified_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompts.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "id": "bad",
                        "category": "status",
                        "prompt": "Status?",
                        "verified_context": {"tests": "failed"},
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                ValueError, "verified_context must be a non-empty string"
            ):
                review.load_prompts(path)

    def test_private_run_ids_are_unique_for_task_arm_trial(self):
        ids = {
            review.private_run_id("config", review.RunKey("one", "native_low", 1)),
            review.private_run_id("config", review.RunKey("one", "native_low", 2)),
            review.private_run_id(
                "config", review.RunKey("one", "simple_man_runtime", 1)
            ),
            review.private_run_id("config", review.RunKey("two", "native_low", 1)),
        }

        self.assertEqual(len(ids), 4)

    def test_public_pair_id_does_not_take_arm_or_private_run_ids(self):
        pair_id = review.public_pair_id("private-secret", "blind_random", "task", 2, 1)

        self.assertRegex(pair_id, r"^pair_[0-9a-f]{24}$")
        self.assertNotIn("native", pair_id)
        self.assertNotIn("simple", pair_id)

        other_secret = review.public_pair_id(
            "other-private-secret", "blind_random", "task", 2, 1
        )
        self.assertNotEqual(pair_id, other_secret)

    def test_blind_bundle_has_exact_public_and_private_shapes(self):
        prompts = [
            {
                "id": "task",
                "category": "status",
                "language": "ru",
                "prompt": "Что произошло?",
                "verified_context": "Tests failed.",
            }
        ]
        results = [
            {
                "task_id": "task",
                "arm": "native_low",
                "trial": 1,
                "run_id": "private_native",
                "text": "Ответ N",
            },
            {
                "task_id": "task",
                "arm": "simple_man_runtime",
                "trial": 1,
                "run_id": "private_simple",
                "text": "Ответ S",
            },
        ]

        public, private = review.build_blind_bundle(
            public_run_id="blind_random",
            metadata={"model": "gpt-test", "trials": 1},
            prompts=prompts,
            arms=["native_low", "simple_man_runtime"],
            trials=1,
            blinding_secret="private-secret",
            results=results,
        )

        self.assertEqual(set(public), {"schema_version", "run_id", "metadata", "pairs"})
        pair = public["pairs"][0]
        self.assertEqual(
            set(pair),
            {
                "id",
                "task_id",
                "category",
                "language",
                "prompt",
                "left",
                "right",
                "verified_context",
            },
        )
        self.assertEqual(set(pair["left"]), {"text"})
        self.assertEqual(set(pair["right"]), {"text"})
        serialized_public = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("native_low", serialized_public)
        self.assertNotIn("simple_man_runtime", serialized_public)
        self.assertNotIn("private-secret", serialized_public)

        self.assertEqual(
            set(private),
            {"schema_version", "run_id", "commitment_nonce", "pairs"},
        )
        self.assertRegex(private["commitment_nonce"], r"[0-9a-f]{64}\Z")
        self.assertNotIn("private-secret", json.dumps(private))
        self.assertEqual(
            public["metadata"]["key_commitment_sha256"],
            review.private_key_commitment_sha256(private),
        )
        other_nonce = dict(private)
        other_nonce["commitment_nonce"] = "0" * 64
        self.assertNotEqual(
            review.private_key_commitment_sha256(private),
            review.private_key_commitment_sha256(other_nonce),
        )
        key = private["pairs"][pair["id"]]
        self.assertEqual(
            set(key),
            {"left_arm", "right_arm", "left_run_id", "right_run_id"},
        )
        self.assertEqual(
            {key["left_arm"], key["right_arm"]}, {"native_low", "simple_man_runtime"}
        )

    def test_blind_bundle_pairwise_compares_three_arms(self):
        arms = ["baseline", "native_low", "simple_man_runtime"]
        prompts = [
            {
                "id": "task",
                "category": "x",
                "prompt": "Explain it.",
                "verified_context": "Known facts.",
            }
        ]
        results = [
            {
                "task_id": "task",
                "arm": arm,
                "trial": 1,
                "run_id": f"private_{index}",
                "text": f"answer {index}",
            }
            for index, arm in enumerate(arms)
        ]

        public, private = review.build_blind_bundle(
            public_run_id="blind_random",
            metadata={"model": "test"},
            prompts=prompts,
            arms=arms,
            trials=1,
            blinding_secret="private-secret",
            results=results,
        )

        self.assertEqual(len(public["pairs"]), 3)
        self.assertEqual(len(private["pairs"]), 3)

    def test_blind_bundle_rejects_arm_name_in_public_metadata(self):
        with self.assertRaisesRegex(ValueError, "public metadata contains arm name"):
            review.build_blind_bundle(
                public_run_id="blind_random",
                metadata={"treatment": "native low"},
                prompts=[],
                arms=["native_low", "simple_man_runtime"],
                trials=1,
                blinding_secret="private-secret",
                results=[],
            )

    def test_blind_bundle_rejects_treatment_name_in_answer(self):
        prompts = [
            {
                "id": "task",
                "category": "status",
                "prompt": "Status?",
                "verified_context": "Known facts.",
            }
        ]
        results = [
            {
                "task_id": "task",
                "arm": "native_low",
                "trial": 1,
                "run_id": "private-native",
                "text": "Native low produced this answer.",
            },
            {
                "task_id": "task",
                "arm": "simple_man_runtime",
                "trial": 1,
                "run_id": "private-simple",
                "text": "Other answer.",
            },
        ]

        with self.assertRaisesRegex(
            ValueError, "public bundle contains treatment name"
        ):
            review.build_blind_bundle(
                public_run_id="blind_random",
                metadata={"model": "test"},
                prompts=prompts,
                arms=["native_low", "simple_man_runtime"],
                trials=1,
                blinding_secret="private-secret",
                results=results,
            )

    def test_blind_bundle_normalizes_formatted_treatment_names(self):
        prompts = [
            {
                "id": "task",
                "category": "status",
                "prompt": "Status?",
                "verified_context": "Known facts.",
            }
        ]
        for leaked_text in ("Simple **Man**", "Simple\nMan", "Simple\u00a0Man"):
            results = [
                {
                    "task_id": "task",
                    "arm": "native_low",
                    "trial": 1,
                    "run_id": "private-native",
                    "text": "Neutral answer.",
                },
                {
                    "task_id": "task",
                    "arm": "simple_man_runtime",
                    "trial": 1,
                    "run_id": "private-simple",
                    "text": leaked_text,
                },
            ]
            with (
                self.subTest(leaked_text=leaked_text),
                self.assertRaisesRegex(
                    ValueError, "public bundle contains treatment name"
                ),
            ):
                review.build_blind_bundle(
                    public_run_id="blind_random",
                    metadata={"model": "test"},
                    prompts=prompts,
                    arms=["native_low", "simple_man_runtime"],
                    trials=1,
                    blinding_secret="private-secret",
                    results=results,
                )

    def test_secret_side_assignment_is_deterministic_and_block_balanced(self):
        pair_ids = [f"pair_{index}" for index in range(10)]
        first = review.block_balanced_left_assignments(
            "private-secret",
            pair_ids,
            block_id="comparison-1",
        )
        second = review.block_balanced_left_assignments(
            "private-secret",
            pair_ids,
            block_id="comparison-1",
        )

        self.assertEqual(first, second)
        self.assertEqual(sum(first.values()), 5)

    def test_execution_schedule_is_secret_seeded_global_permutation(self):
        run_keys = [
            review.RunKey(task_id, arm, trial)
            for task_id in ("one", "two")
            for arm in ("baseline", "native_low", "simple_man_runtime")
            for trial in (1, 2)
        ]

        first = review.secret_seeded_execution_schedule("secret-one", run_keys)
        resumed = review.secret_seeded_execution_schedule("secret-one", run_keys)
        other_seed = review.secret_seeded_execution_schedule("secret-two", run_keys)

        self.assertEqual(first, resumed)
        self.assertCountEqual(first, run_keys)
        self.assertNotEqual(first, run_keys)
        self.assertNotEqual(first, other_seed)

    def test_private_execution_schedule_is_stored_committed_and_validated(self):
        run_keys = [
            review.RunKey("one", "native_low", 1),
            review.RunKey("one", "simple_man_runtime", 1),
            review.RunKey("two", "native_low", 1),
            review.RunKey("two", "simple_man_runtime", 1),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private"
            manifest_path = private / "manifest.json"
            schedule_path = private / "execution-schedule.json"
            manifest = {
                "schema_version": 1,
                "run_id": "blind-test",
                "blinding_secret": "private-secret",
                "config_sha256": "config-hash",
            }
            review.atomic_write_json(manifest_path, manifest)

            first = runner.load_or_create_execution_schedule(
                schedule_path,
                manifest_path=manifest_path,
                manifest=manifest,
                config_sha256="config-hash",
                run_keys=run_keys,
            )
            stored = json.loads(schedule_path.read_text())
            committed_manifest = json.loads(manifest_path.read_text())

            self.assertEqual(
                committed_manifest["execution_schedule_sha256"],
                review.sha256_text(review.canonical_json(stored)),
            )
            self.assertEqual(
                [
                    review.RunKey(item["task_id"], item["arm"], item["trial"])
                    for item in stored["runs"]
                ],
                first,
            )

            resumed = runner.load_or_create_execution_schedule(
                schedule_path,
                manifest_path=manifest_path,
                manifest=committed_manifest,
                config_sha256="config-hash",
                run_keys=run_keys,
            )
            self.assertEqual(resumed, first)

            stored["runs"].reverse()
            review.atomic_write_json(schedule_path, stored)
            with self.assertRaisesRegex(
                RuntimeError, "execution schedule hash mismatch"
            ):
                runner.load_or_create_execution_schedule(
                    schedule_path,
                    manifest_path=manifest_path,
                    manifest=committed_manifest,
                    config_sha256="config-hash",
                    run_keys=run_keys,
                )

    def test_atomic_json_write_leaves_only_complete_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "value.json"

            review.atomic_write_json(path, {"ok": True})

            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_atomic_copy_preserves_previous_auth_on_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            destination = root / "auth.json"
            source.write_bytes(b"refreshed-auth")
            destination.write_bytes(b"previous-auth")

            def interrupted_copy(_source, temporary_file):
                temporary_file.write(b"partial")
                raise OSError("interrupted")

            with mock.patch.object(
                review.shutil,
                "copyfileobj",
                side_effect=interrupted_copy,
            ):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    review.atomic_copy_file(source, destination)

            self.assertEqual(destination.read_bytes(), b"previous-auth")
            self.assertEqual(set(root.iterdir()), {source, destination})

    def test_output_directory_lock_is_nonblocking_and_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "private" / ".runner.lock"

            with runner.output_directory_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "another blind-review run"):
                    with runner.output_directory_lock(lock_path):
                        self.fail("second writer acquired the lock")

            with runner.output_directory_lock(lock_path):
                self.assertEqual(
                    stat.S_IMODE(lock_path.stat().st_mode),
                    0o600,
                )

    def test_isolation_changes_home_and_codex_home_and_cleans_them(self):
        specs = review.build_arm_specs(RUNTIME_POLICY)
        roots: list[Path] = []
        with tempfile.TemporaryDirectory() as temporary:
            auth = Path(temporary) / "auth.json"
            auth.write_text('{"token":"secret"}', encoding="utf-8")
            os.environ["SHOULD_NOT_LEAK"] = "private"

            try:
                for arm in ("native_low", "simple_man_runtime"):
                    with review.isolated_codex_environment(
                        auth_source=auth, spec=specs[arm]
                    ) as isolated:
                        roots.append(isolated.root)
                        self.assertEqual(isolated.env["HOME"], str(isolated.home))
                        self.assertEqual(
                            isolated.env["CODEX_HOME"], str(isolated.codex_home)
                        )
                        self.assertNotIn("OPENAI_API_KEY", isolated.env)
                        self.assertNotIn("SHOULD_NOT_LEAK", isolated.env)
                        self.assertIn("PATH", isolated.env)
                        self.assertEqual(
                            (isolated.codex_home / "auth.json").read_text(),
                            '{"token":"secret"}',
                        )
                        agents = isolated.codex_home / "AGENTS.md"
                        self.assertEqual(agents.exists(), arm == "simple_man_runtime")
                        if agents.exists():
                            self.assertEqual(agents.read_text(), RUNTIME_POLICY)
            finally:
                os.environ.pop("SHOULD_NOT_LEAK", None)

        self.assertEqual(len(set(roots)), 2)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_isolation_returns_refreshed_auth_only_to_temp_sink(self):
        spec = review.build_arm_specs(RUNTIME_POLICY)["native_low"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source-auth.json"
            sink = root / "temporary-auth-cache.json"
            source.write_text("old")

            with review.isolated_codex_environment(
                auth_source=source,
                auth_sink=sink,
                spec=spec,
            ) as isolated:
                (isolated.codex_home / "auth.json").write_text("refreshed")

            self.assertEqual(source.read_text(), "old")
            self.assertEqual(sink.read_text(), "refreshed")

    def test_arm_environment_rejects_simple_man_leak_in_native_low(self):
        spec = review.ArmSpec("native_low", "low", None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            codex_home = home / ".codex"
            workspace = root / "workspace"
            codex_home.mkdir(parents=True)
            workspace.mkdir()
            (codex_home / "AGENTS.md").write_text(RUNTIME_POLICY)
            environment = review.IsolatedCodexEnvironment(
                root=root,
                home=home,
                codex_home=codex_home,
                workspace=workspace,
                env={},
            )

            with self.assertRaisesRegex(ValueError, "contains Simple Man instructions"):
                review.assert_arm_environment(spec, environment)

    def test_isolation_allows_exact_neutral_policy_without_simple_man(self):
        policy = "## Blind response judge\n\nEvaluate anonymous replies.\n"
        spec = review.ArmSpec("blind_judge", "low", policy)
        with tempfile.TemporaryDirectory() as temporary:
            auth = Path(temporary) / "auth.json"
            auth.write_text('{"token":"secret"}', encoding="utf-8")

            with review.isolated_codex_environment(
                auth_source=auth, spec=spec
            ) as isolated:
                agents = isolated.codex_home / "AGENTS.md"
                self.assertEqual(agents.read_text(encoding="utf-8"), policy)

    def test_neutral_policy_rejects_simple_man_leak(self):
        spec = review.ArmSpec("blind_judge", "low", RUNTIME_POLICY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            codex_home = home / ".codex"
            workspace = root / "workspace"
            codex_home.mkdir(parents=True)
            workspace.mkdir()
            (codex_home / "AGENTS.md").write_text(RUNTIME_POLICY, encoding="utf-8")
            environment = review.IsolatedCodexEnvironment(
                root=root,
                home=home,
                codex_home=codex_home,
                workspace=workspace,
                env={},
            )

            with self.assertRaisesRegex(ValueError, "policy contains Simple Man"):
                review.assert_arm_environment(spec, environment)

    def test_safe_environment_rejects_proxy_credentials(self):
        for value in (
            "http://user:secret@proxy.example:8080",
            "user:secret@proxy.example:8080",
        ):
            with (
                self.subTest(value=value),
                mock.patch.dict(
                    os.environ,
                    {"HTTP_PROXY": value},
                    clear=False,
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError, "HTTP_PROXY contains proxy credentials"
                ):
                    review.safe_environment()

    def test_build_codex_command_sets_explicit_model_effort_and_verbosity(self):
        spec = review.ArmSpec("native_low", "low", None)

        command = runner.build_codex_command(
            executable="codex",
            model="gpt-test",
            effort="xhigh",
            spec=spec,
            workspace=Path("/tmp/workspace"),
        )

        self.assertIn("gpt-test", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn('model_verbosity="low"', command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[-1], "-")
        for feature in ("shell_tool", "unified_exec", "shell_snapshot", "multi_agent"):
            self.assertIn(feature, command)

    def test_prompt_input_preflight_requires_exact_runtime_policy_once(self):
        spec = review.build_arm_specs(RUNTIME_POLICY)["simple_man_runtime"]
        visible = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "## Simple Man runtime policy\n\nApply Simple Man differently.\n",
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Prompt"}],
            },
        ]
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=json.dumps(visible),
            stderr="",
        )

        with mock.patch.object(runner.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "exact runtime policy once"):
                runner.preflight_model_visible_input(
                    executable="codex",
                    model="gpt-test",
                    effort="high",
                    spec=spec,
                    prompt="Prompt",
                    cwd=Path("/tmp"),
                    env={},
                    timeout_seconds=1,
                )

    def test_native_low_and_simple_man_pin_same_low_verbosity(self):
        specs = review.build_arm_specs(RUNTIME_POLICY)

        self.assertEqual(specs["native_low"].model_verbosity, "low")
        self.assertEqual(specs["simple_man_runtime"].model_verbosity, "low")

    def test_execution_contract_and_runner_hash_enter_config_and_resume_identity(self):
        spec = review.build_arm_specs(RUNTIME_POLICY)["native_low"]
        prompts = [
            {
                "id": "one",
                "category": "status",
                "prompt": "Status?",
                "verified_context": "Known facts.",
            }
        ]
        config = runner.config_payload(
            prompts=prompts,
            arms=[spec],
            trials=1,
            model="gpt-test",
            effort="high",
            cli_version="codex-cli test",
            runner_sha256="runner-hash",
            source_git_commit="deadbeef",
            source_git_dirty=True,
        )
        identity = runner.result_identity(
            run_id="private-run",
            key=review.RunKey("one", "native_low", 1),
            prompt="Status?",
            spec=spec,
            model="gpt-test",
            effort="high",
            cli_version="codex-cli test",
            runner_sha256="runner-hash",
        )

        self.assertEqual(config["execution_contract"], runner.EXECUTION_CONTRACT)
        self.assertEqual(
            config["execution_contract_sha256"], runner.EXECUTION_CONTRACT_SHA256
        )
        self.assertEqual(config["runner_sha256"], "runner-hash")
        self.assertEqual(config["source_git_commit"], "deadbeef")
        self.assertIs(config["source_git_dirty"], True)
        self.assertEqual(config["limits"]["max_calls"], 100)
        self.assertEqual(
            identity["execution_contract_sha256"], runner.EXECUTION_CONTRACT_SHA256
        )
        self.assertEqual(identity["runner_sha256"], "runner-hash")
        with self.assertRaisesRegex(ValueError, "must contain total_tokens"):
            runner.reported_tokens({"cached_input_tokens": 10})

    def test_source_git_provenance_ignores_ignored_outputs_but_tracks_untracked(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            (repository / ".gitignore").write_text(
                "ignored-output/\n", encoding="utf-8"
            )
            (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Benchmark Test",
                    "-c",
                    "user.email=benchmark@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                cwd=repository,
                check=True,
            )
            expected_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            ignored = repository / "ignored-output" / "bundle.json"
            ignored.parent.mkdir()
            ignored.write_text("generated\n", encoding="utf-8")

            commit, dirty = runner.source_git_provenance(repository)
            self.assertEqual(commit, expected_commit)
            self.assertFalse(dirty)

            (repository / "untracked.txt").write_text("visible\n", encoding="utf-8")
            resumed_commit, resumed_dirty = runner.source_git_provenance(repository)
            self.assertEqual(resumed_commit, expected_commit)
            self.assertTrue(resumed_dirty)

    def test_parse_raw_jsonl_extracts_final_text_and_usage(self):
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "answer"},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            text, usage = review.parse_codex_jsonl(path)

        self.assertEqual(text, "answer")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 4})

    def test_parse_raw_jsonl_rejects_tools_and_malformed_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {"type": "command_execution"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "forbidden item type"):
                review.parse_codex_jsonl(path)

            path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                review.parse_codex_jsonl(path)

            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "answer"},
                            }
                        ),
                        json.dumps(
                            {"type": "turn.completed", "usage": {"total_tokens": 10}}
                        ),
                        json.dumps(
                            {"type": "turn.completed", "usage": {"total_tokens": 1}}
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "after turn.completed"):
                review.parse_codex_jsonl(path)

    def test_resume_requires_matching_identity_and_raw_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.jsonl"
            raw.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "answer"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {"output_tokens": 1},
                            }
                        ),
                    ]
                )
                + "\n"
            )
            expected = {"schema_version": 1, "run_id": "private"}
            result = {
                **expected,
                "text": "answer",
                "usage": {"output_tokens": 1},
                "duration_ms": 1,
                "raw_sha256": review.sha256_file(raw),
            }
            result_path = root / "result.json"
            result_path.write_text(json.dumps(result))

            loaded = runner.load_resumable_result(
                result_path,
                raw_path=raw,
                expected_identity=expected,
            )
            self.assertEqual(loaded, result)

            raw.write_text(raw.read_text() + "changed\n")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                runner.load_resumable_result(
                    result_path,
                    raw_path=raw,
                    expected_identity=expected,
                )

    def test_cli_dry_run_does_not_write_output_or_require_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = root / "prompts.jsonl"
            prompts.write_text(
                json.dumps(
                    {
                        "id": "one",
                        "category": "x",
                        "prompt": "Explain this.",
                        "verified_context": "Known facts.",
                    }
                )
                + "\n"
            )
            fake_codex = root / "codex"
            fake_codex.write_text("#!/bin/sh\necho 'codex-cli test'\n")
            fake_codex.chmod(0o755)
            output = root / "output"

            process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "evals" / "run_blind_review.py"),
                    "--dry-run",
                    "--prompts",
                    str(prompts),
                    "--runtime-policy",
                    str(ROOT / "AGENTS.md.snippet"),
                    "--output-dir",
                    str(output),
                    "--auth-file",
                    str(root / "missing-auth.json"),
                    "--codex",
                    str(fake_codex),
                    "--model",
                    "gpt-test",
                ],
                text=True,
                capture_output=True,
                check=True,
            )

        plan = json.loads(process.stdout)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["codex_calls"], 2)
        self.assertFalse(output.exists())
        self.assertNotIn("seed", process.stdout.lower())
        self.assertNotIn("secret", process.stdout.lower())

    def test_live_release_run_rejects_dirty_source_before_auth_or_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = root / "prompts.jsonl"
            prompts.write_text(
                json.dumps(
                    {
                        "id": "one",
                        "category": "status",
                        "prompt": "Report status.",
                        "verified_context": "Known status.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            fake_codex = root / "codex"
            fake_codex.write_text("#!/bin/sh\necho 'codex-cli test'\n")
            fake_codex.chmod(0o755)
            output = root / "output"
            with mock.patch.object(
                runner, "source_git_provenance", return_value=("a" * 40, True)
            ):
                with self.assertRaisesRegex(RuntimeError, "requires a clean source"):
                    runner.main(
                        [
                            "--require-clean-source",
                            "--prompts",
                            str(prompts),
                            "--runtime-policy",
                            str(
                                ROOT / "evals/policies/simple_man_candidate_runtime.md"
                            ),
                            "--output-dir",
                            str(output),
                            "--auth-file",
                            str(root / "missing-auth.json"),
                            "--codex",
                            str(fake_codex),
                            "--model",
                            "gpt-test",
                        ]
                    )
            self.assertFalse((output / "private" / "manifest.json").exists())

    def test_fake_codex_live_run_builds_bundle_and_resume_skips_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = root / "prompts.jsonl"
            prompts.write_text(
                json.dumps(
                    {
                        "id": "status",
                        "category": "status",
                        "language": "en",
                        "prompt": "Give the verified status.",
                        "verified_context": "Implementation complete; tests passed.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            auth = root / "auth.json"
            auth.write_text("{}\n", encoding="utf-8")
            counter = root / "exec-count.txt"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli fake-1.0")
    raise SystemExit(0)

agents = Path(os.environ.get("CODEX_HOME", "")) / "AGENTS.md"
if "debug" in args and "prompt-input" in args:
    context = agents.read_text(encoding="utf-8") if agents.exists() else "Neutral runtime."
    prompt = args[-1]
    print(json.dumps([
        {{"type": "message", "role": "user", "content": [{{"type": "input_text", "text": context}}]}},
        {{"type": "message", "role": "user", "content": [{{"type": "input_text", "text": prompt}}]}},
    ]))
    raise SystemExit(0)

if "exec" in args:
    prompt = sys.stdin.read()
    counter = Path({str(counter)!r})
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    answer = "Compact verified result." if agents.exists() else "Verified result with context."
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": answer}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": len(prompt), "output_tokens": 4}}}}))
    raise SystemExit(0)

print("unsupported fake Codex invocation", file=sys.stderr)
raise SystemExit(2)
""",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            output = root / "output"
            argv = [
                "--prompts",
                str(prompts),
                "--runtime-policy",
                str(ROOT / "AGENTS.md.snippet"),
                "--output-dir",
                str(output),
                "--auth-file",
                str(auth),
                "--codex",
                str(fake_codex),
                "--model",
                "gpt-test",
                "--effort",
                "high",
            ]

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(argv), 0)

            run_results = sorted((output / "private" / "runs").glob("*.json"))
            raw_runs = sorted((output / "private" / "raw").glob("*.jsonl"))
            bundle_path = output / "public" / "bundle.json"
            key_path = output / "private" / "key.json"
            first_bundle = bundle_path.read_bytes()
            bundle = json.loads(first_bundle)
            key = json.loads(key_path.read_text())

            self.assertEqual(counter.read_text(), "2")
            self.assertEqual(len(run_results), 2)
            self.assertEqual(len(raw_runs), 2)
            self.assertEqual(len(bundle["pairs"]), 1)
            self.assertEqual(set(bundle["pairs"][0]["left"]), {"text"})
            self.assertEqual(set(bundle["pairs"][0]["right"]), {"text"})
            pair_id = bundle["pairs"][0]["id"]
            self.assertEqual(
                {key["pairs"][pair_id]["left_arm"], key["pairs"][pair_id]["right_arm"]},
                {"native_low", "simple_man_runtime"},
            )
            self.assertEqual(
                bundle["metadata"]["key_commitment_sha256"],
                review.private_key_commitment_sha256(key),
            )
            manifest = json.loads((output / "private" / "manifest.json").read_text())
            schedule = json.loads(
                (output / "private" / "execution-schedule.json").read_text()
            )
            self.assertEqual(
                manifest["execution_schedule_sha256"],
                review.sha256_text(review.canonical_json(schedule)),
            )

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(argv), 0)

            self.assertEqual(counter.read_text(), "2")
            self.assertEqual(bundle_path.read_bytes(), first_bundle)


if __name__ == "__main__":
    unittest.main()
