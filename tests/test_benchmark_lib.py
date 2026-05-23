import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import benchmark_lib as bench  # noqa: E402
import run_codex  # noqa: E402
import run_skill_comparison  # noqa: E402


class BenchmarkLibTests(unittest.TestCase):
    def test_aggregates_median_usage_and_net_savings(self):
        snapshot = {
            "runs": [
                {
                    "prompt_id": "p1",
                    "arm": "control",
                    "trial": 1,
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "text": "control answer",
                },
                {
                    "prompt_id": "p1",
                    "arm": "control",
                    "trial": 2,
                    "usage": {"input_tokens": 110, "output_tokens": 60},
                    "text": "control answer",
                },
                {
                    "prompt_id": "p1",
                    "arm": "control",
                    "trial": 3,
                    "usage": {"input_tokens": 90, "output_tokens": 40},
                    "text": "control answer",
                },
                {
                    "prompt_id": "p1",
                    "arm": "simple_man_runtime",
                    "trial": 1,
                    "usage": {"input_tokens": 120, "output_tokens": 10},
                    "text": "simple answer",
                },
                {
                    "prompt_id": "p1",
                    "arm": "simple_man_runtime",
                    "trial": 2,
                    "usage": {"input_tokens": 130, "output_tokens": 20},
                    "text": "simple answer",
                },
                {
                    "prompt_id": "p1",
                    "arm": "simple_man_runtime",
                    "trial": 3,
                    "usage": {"input_tokens": 110, "output_tokens": 30},
                    "text": "simple answer",
                },
            ]
        }

        table = bench.build_prompt_table(snapshot)
        row = table[0]

        self.assertEqual(row.prompt_id, "p1")
        self.assertEqual(row.arms["control"].median_total, 150)
        self.assertEqual(row.arms["simple_man_runtime"].median_total, 140)
        self.assertAlmostEqual(row.net_savings_vs_control, 1 - 140 / 150)
        self.assertAlmostEqual(row.output_savings_vs_control, 1 - 20 / 50)

    def test_visible_token_fields_override_raw_codex_usage_for_primary_metric(self):
        snapshot = {
            "runs": [
                {
                    "prompt_id": "p1",
                    "arm": "control",
                    "trial": 1,
                    "visible_input_tokens": 20,
                    "visible_output_tokens": 10,
                    "visible_total_tokens": 30,
                    "usage": {"input_tokens": 1000, "output_tokens": 1000},
                    "text": "control answer",
                },
                {
                    "prompt_id": "p1",
                    "arm": "simple_man_runtime",
                    "trial": 1,
                    "visible_input_tokens": 24,
                    "visible_output_tokens": 3,
                    "visible_total_tokens": 27,
                    "usage": {"input_tokens": 2000, "output_tokens": 2000},
                    "text": "simple answer",
                },
            ]
        }

        row = bench.build_prompt_table(snapshot)[0]

        self.assertEqual(row.arms["control"].median_total, 30)
        self.assertEqual(row.arms["control"].median_codex_total, 2000)
        self.assertEqual(row.arms["simple_man_runtime"].median_total, 27)
        self.assertAlmostEqual(row.net_savings_vs_control, 0.1)

    def test_arm_comparison_reports_output_first_turn_and_amortized_savings(self):
        baseline = bench.ArmStats(
            median_input=100,
            median_cached_input=0,
            median_output=100,
            median_reasoning_output=0,
            median_total=200,
            median_codex_total=200,
            trials=1,
        )
        skill = bench.ArmStats(
            median_input=150,
            median_cached_input=0,
            median_output=50,
            median_reasoning_output=0,
            median_total=200,
            median_codex_total=200,
            trials=1,
        )

        comparison = bench.compare_arm(skill, baseline, amortize_turns=10)

        self.assertAlmostEqual(comparison.output_savings, 0.5)
        self.assertAlmostEqual(comparison.first_turn_net_savings, 0.0)
        self.assertAlmostEqual(comparison.amortized_net_savings, 1 - 155 / 200)

    def test_category_summary_averages_comparisons_for_named_arm(self):
        snapshot = {
            "prompts": [
                {"id": "p1", "category": "status", "prompt": "one"},
                {"id": "p2", "category": "status", "prompt": "two"},
            ],
            "runs": [
                {
                    "prompt_id": "p1",
                    "category": "status",
                    "arm": "control",
                    "trial": 1,
                    "visible_input_tokens": 100,
                    "visible_output_tokens": 100,
                    "visible_total_tokens": 200,
                    "usage": {},
                    "text": "control",
                },
                {
                    "prompt_id": "p1",
                    "category": "status",
                    "arm": "caveman",
                    "trial": 1,
                    "visible_input_tokens": 150,
                    "visible_output_tokens": 50,
                    "visible_total_tokens": 200,
                    "usage": {},
                    "text": "cave",
                },
                {
                    "prompt_id": "p2",
                    "category": "status",
                    "arm": "control",
                    "trial": 1,
                    "visible_input_tokens": 100,
                    "visible_output_tokens": 50,
                    "visible_total_tokens": 150,
                    "usage": {},
                    "text": "control",
                },
                {
                    "prompt_id": "p2",
                    "category": "status",
                    "arm": "caveman",
                    "trial": 1,
                    "visible_input_tokens": 150,
                    "visible_output_tokens": 25,
                    "visible_total_tokens": 175,
                    "usage": {},
                    "text": "cave",
                },
            ],
        }

        summary = bench.build_category_summary(
            bench.build_prompt_table(snapshot),
            arm="caveman",
            baseline_arm="control",
            amortize_turns=10,
        )

        self.assertEqual(summary[0].category, "status")
        self.assertEqual(summary[0].prompts, 2)
        self.assertAlmostEqual(summary[0].mean_output_savings, 0.5)

    def test_quality_check_requires_any_term_per_group(self):
        prompt = {
            "id": "jwt-exp",
            "checks": {
                "must_include_any": [["seconds", "multiply by 1000"], ["exp"]],
                "forbidden": ["ignore validation"],
            },
        }
        run = {
            "prompt_id": "jwt-exp",
            "arm": "simple_man",
            "text": "JWT exp is in seconds; compare it after multiplying by 1000.",
        }

        failures = bench.check_run_quality(prompt, run)

        self.assertEqual(failures, [])

    def test_quality_check_normalizes_punctuation_and_plural_forms(self):
        prompt = {
            "id": "status",
            "checks": {
                "must_include_any": [
                    ["unit tests pass"],
                    ["integration tests fail"],
                    ["tool calls"],
                    ["builder"],
                    ["table drop"],
                    ["object"],
                    ["reference"],
                    ["fallback"],
                ],
            },
        }
        run = {
            "prompt_id": "status",
            "arm": "simple_man",
            "text": (
                "Unit tests: pass. Integration tests: fail. "
                "tool_call_id recorded. AS build. before dropping any table. "
                "Inline obj prop creates new ref by identity. "
                "Shows Something went wrong inside role alert."
            ),
        }

        failures = bench.check_run_quality(prompt, run)

        self.assertEqual(failures, [])

    def test_quality_check_reports_missing_and_forbidden_terms(self):
        prompt = {
            "id": "sql-review",
            "checks": {
                "must_include": ["parameterized"],
                "must_include_any": [["SQL injection", "injection"]],
                "forbidden": ["string interpolation is fine"],
            },
        }
        run = {
            "prompt_id": "sql-review",
            "arm": "simple_man",
            "text": "String interpolation is fine here.",
        }

        failures = bench.check_run_quality(prompt, run)

        self.assertIn("missing required term: parameterized", failures)
        self.assertIn("missing one of: SQL injection | injection", failures)
        self.assertIn("forbidden term present: string interpolation is fine", failures)

    def test_quality_check_treats_idor_as_access_control(self):
        prompt = {
            "id": "sql-review",
            "checks": {"must_include_any": [["authorization", "access control", "auth"]]},
        }
        run = {
            "prompt_id": "sql-review",
            "arm": "simple_man_runtime",
            "text": "Potential IDOR: ensure the requester is allowed to access this user.",
        }

        self.assertEqual(bench.check_run_quality(prompt, run), [])

    def test_snapshot_hash_validation_detects_stale_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text("current skill\n")
            snapshot = {
                "metadata": {
                    "skill_sha256": bench.sha256_text("old skill\n"),
                    "prompt_corpus_sha256": "unused",
                }
            }

            errors = bench.validate_snapshot_freshness(
                snapshot=snapshot,
                skill_path=skill,
                runtime_path=None,
                prompts_path=None,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("skill_sha256 mismatch", errors[0])

    def test_jsonl_prompt_loader_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts = Path(tmp) / "prompts.jsonl"
            prompts.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "dup", "category": "x", "prompt": "one"}),
                        json.dumps({"id": "dup", "category": "x", "prompt": "two"}),
                    ]
                )
            )

            with self.assertRaisesRegex(ValueError, "duplicate prompt id"):
                bench.load_prompts(prompts)

    def test_canonical_prompt_corpus_has_expected_size_and_checks(self):
        prompts = bench.load_prompts(ROOT / "evals" / "prompts" / "coding_tasks.jsonl")

        self.assertEqual(len(prompts), 40)
        self.assertTrue(all(prompt.get("checks") for prompt in prompts))

    def test_reference_prompt_corpus_matches_caveman_readme_suite(self):
        prompts = bench.load_prompts(
            ROOT / "evals" / "prompts" / "reference_compression.jsonl"
        )

        self.assertEqual(len(prompts), 10)
        self.assertEqual(prompts[0]["id"], "react-rerender")
        self.assertEqual(prompts[-1]["id"], "error-boundary")
        self.assertTrue(all(prompt.get("checks") for prompt in prompts))

    def test_skill_comparison_seed_fixtures_are_real_failing_projects(self):
        for project in run_skill_comparison.PROJECTS:
            with self.subTest(project=project.key):
                project_root = run_skill_comparison.SEEDS / project.key
                self.assertTrue(project_root.exists())

                result = run_skill_comparison.run(project.check, cwd=project_root)

                self.assertNotEqual(result.returncode, 0)

    def test_reports_do_not_include_local_user_paths(self):
        paths = [
            ROOT / "evals" / "reports" / "codex-skill-comparison.md",
            ROOT / "evals" / "reports" / "gpt-pro-benchmark-brief.md",
            ROOT / "evals" / "run_codex.py",
            ROOT / "evals" / "run_skill_comparison.py",
        ]

        for path in paths:
            with self.subTest(path=path):
                text = path.read_text()
                self.assertNotIn("/" + "Users/", text)
                self.assertNotIn("za" + "dro", text)

    def test_readme_examples_are_wrapped_comparison_tables(self):
        readme = (ROOT / "README.md").read_text()
        examples = readme.split("## Examples", 1)[1].split("## Agent support", 1)[0]

        self.assertIn("<table>", examples)
        self.assertIn("No Simple Man", examples)
        self.assertIn("With Simple Man", examples)
        self.assertNotIn("```", examples)
        self.assertNotIn("<pre", examples.lower())

    def test_installer_is_idempotent_and_enables_global_codex_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HOME"] = tmp
            env.pop("CODEX_HOME", None)

            for _ in range(2):
                result = subprocess.run(
                    ["bash", str(ROOT / "install.sh")],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=True,
                )

            home = Path(tmp) / ".codex"
            skill = home / "skills" / "simple-man" / "SKILL.md"
            metadata = home / "skills" / "simple-man" / "agents" / "openai.yaml"
            agents = (home / "AGENTS.md").read_text()

            self.assertTrue(skill.exists())
            self.assertTrue(metadata.exists())
            self.assertIn("Simple Man is always-on after install", result.stdout)
            self.assertEqual(agents.count("simple-man-always-on-begin"), 1)
            self.assertEqual(agents.count("simple-man-always-on-end"), 1)
            self.assertIn("Apply Simple Man to user-facing responses by default.", agents)

    def test_codex_plugin_skill_copy_matches_canonical_skill(self):
        canonical = ROOT / "skills" / "simple-man"
        plugin_skill = ROOT / "plugins" / "simple-man" / "skills" / "simple-man"

        for rel in ("SKILL.md", "agents/openai.yaml"):
            with self.subTest(rel=rel):
                self.assertEqual(
                    (plugin_skill / rel).read_text(),
                    (canonical / rel).read_text(),
                )

        manifest = json.loads(
            (ROOT / "plugins" / "simple-man" / ".codex-plugin" / "plugin.json").read_text()
        )
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())

        self.assertEqual(manifest["name"], "simple-man")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(marketplace["name"], "simple-man")
        self.assertEqual(marketplace["plugins"][0]["name"], "simple-man")

    def test_snapshot_age_warning_uses_generated_at(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).isoformat()
        snapshot = {"metadata": {"generated_at": old}}

        warnings = bench.validate_snapshot_age(snapshot, max_age_days=30)

        self.assertEqual(len(warnings), 1)
        self.assertIn("older than 30 days", warnings[0])

    def test_codex_jsonl_parser_extracts_final_text_and_usage(self):
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "final answer"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 4,
                            "reasoning_output_tokens": 1,
                        },
                    }
                ),
            ]
        )

        text, usage = run_codex.parse_codex_jsonl(stdout)

        self.assertEqual(text, "final answer")
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 4)

    def test_planned_runs_crosses_prompts_arms_and_trials(self):
        prompts = [
            {"id": "a", "category": "x", "prompt": "A"},
            {"id": "b", "category": "x", "prompt": "B"},
        ]

        runs = run_codex.planned_runs(prompts, ["control", "simple_man_runtime"], 3)

        self.assertEqual(len(runs), 12)
        self.assertEqual(runs[0], (prompts[0], "control", 1))
        self.assertEqual(runs[-1], (prompts[1], "simple_man_runtime", 3))

    def test_select_prompts_filters_by_explicit_ids_preserving_requested_order(self):
        prompts = [
            {"id": "a", "category": "x", "prompt": "A"},
            {"id": "b", "category": "x", "prompt": "B"},
            {"id": "c", "category": "x", "prompt": "C"},
        ]

        selected = run_codex.select_prompts(prompts, ["c", "a"], limit=0)

        self.assertEqual([prompt["id"] for prompt in selected], ["c", "a"])

    def test_select_prompts_rejects_unknown_id(self):
        prompts = [{"id": "a", "category": "x", "prompt": "A"}]

        with self.assertRaisesRegex(SystemExit, "unknown prompt id"):
            run_codex.select_prompts(prompts, ["missing"], limit=0)

    def test_default_arms_include_caveman_when_skill_path_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            caveman = Path(tmp) / "caveman.md"
            caveman.write_text("cave skill")

            arms = run_codex.default_arms(caveman)

        self.assertIn("caveman", arms)
        self.assertIn("simple_man_runtime", arms)
        self.assertIn("simple_man_candidate", arms)
        self.assertIn("simple_man_skill", arms)

    def test_reference_default_arms_use_normal_and_explicit_caveman_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            caveman = Path(tmp) / "caveman.md"
            caveman.write_text("cave skill")

            arms = run_codex.default_arms(caveman, suite=bench.SUITE_REFERENCE)

        self.assertEqual(
            arms,
            [
                "normal",
                "caveman_full",
                "caveman_ultra",
                "simple_man_runtime",
                "simple_man_candidate",
                "simple_man_skill",
            ],
        )

    def test_runtime_policy_has_no_skill_reference(self):
        snippet = (ROOT / "AGENTS.md.snippet").read_text()

        self.assertNotIn("$simple-man", snippet)
        self.assertIn("Simple Man runtime policy", snippet)
        self.assertIn("Delete water only", snippet)
        self.assertIn("No compression may remove a material fact", snippet)

    def test_arm_instructions_split_runtime_and_full_skill(self):
        skill_texts = {
            "simple_man_runtime": "runtime rules",
            "simple_man_candidate": "candidate rules",
            "simple_man_skill": "full skill rules",
        }

        runtime = run_codex.arm_instructions("simple_man_runtime", skill_texts)
        candidate = run_codex.arm_instructions("simple_man_candidate", skill_texts)
        full_skill = run_codex.arm_instructions("simple_man_skill", skill_texts)

        self.assertIn("<simple_man_runtime_policy>", runtime)
        self.assertIn("runtime rules", runtime)
        self.assertIn("<simple_man_candidate_runtime_policy>", candidate)
        self.assertIn("candidate rules", candidate)
        self.assertNotIn("runtime rules", candidate)
        self.assertNotIn("full skill rules", runtime)
        self.assertIn("<simple_man_skill>", full_skill)
        self.assertIn("full skill rules", full_skill)

    def test_candidate_policy_is_quality_first_and_water_only(self):
        policy = (ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md").read_text()

        self.assertNotIn("$simple-man", policy)
        self.assertIn("No compression may remove a material fact", policy)
        self.assertIn("Delete water only", policy)

    def test_runtime_quality_gate_includes_candidate_when_available(self):
        import measure

        snapshot = {
            "metadata": {
                "suite": bench.SUITE_RUNTIME,
                "arms": [
                    "control",
                    "simple_man_runtime",
                    "simple_man_candidate",
                    "simple_man_skill",
                ],
            }
        }

        self.assertEqual(
            measure.default_quality_arms(snapshot),
            ["simple_man_runtime", "simple_man_candidate", "simple_man_skill"],
        )

    def test_reference_quality_gate_excludes_external_caveman_by_default(self):
        import measure

        snapshot = {
            "metadata": {
                "suite": bench.SUITE_REFERENCE,
                "arms": [
                    "normal",
                    "caveman_ultra",
                    "simple_man_runtime",
                    "simple_man_candidate",
                ],
            }
        }

        self.assertEqual(
            measure.default_quality_arms(snapshot),
            ["simple_man_runtime", "simple_man_candidate"],
        )

    def test_reference_instructions_compare_against_normal_not_terse(self):
        skill_texts = {
            "simple_man_runtime": "runtime rules",
            "simple_man_candidate": "candidate rules",
            "simple_man_skill": "full skill rules",
            "caveman_full": "cave rules",
            "caveman_ultra": "cave rules",
        }

        normal = run_codex.arm_instructions("normal", skill_texts, suite=bench.SUITE_REFERENCE)
        runtime = run_codex.arm_instructions(
            "simple_man_runtime", skill_texts, suite=bench.SUITE_REFERENCE
        )
        candidate = run_codex.arm_instructions(
            "simple_man_candidate", skill_texts, suite=bench.SUITE_REFERENCE
        )
        caveman_ultra = run_codex.arm_instructions(
            "caveman_ultra", skill_texts, suite=bench.SUITE_REFERENCE
        )

        self.assertIn("You are a helpful assistant.", normal)
        self.assertIn("Answer thoroughly", normal)
        self.assertIn("You are a helpful assistant.", runtime)
        self.assertIn("Answer thoroughly", runtime)
        self.assertIn("<simple_man_runtime_policy>", runtime)
        self.assertIn("<simple_man_candidate_runtime_policy>", candidate)
        self.assertIn("candidate rules", candidate)
        self.assertNotIn("Be concise.", runtime)
        self.assertIn("<caveman_skill>", caveman_ultra)
        self.assertIn("Use /caveman ultra", caveman_ultra)

    def test_reference_prompt_wrapper_forces_standalone_q_and_a(self):
        prompt = run_codex.build_prompt(
            {"id": "p1", "category": "x", "prompt": "Implement a React component."},
            "instructions",
            suite=bench.SUITE_REFERENCE,
        )

        self.assertIn("standalone Q&A benchmark", prompt)
        self.assertIn("Do not inspect, modify, or mention", prompt)
        self.assertIn("<question id=\"p1\" category=\"x\">", prompt)
        self.assertNotIn("<task id=\"p1\"", prompt)

    def test_reference_report_uses_output_only_vs_normal(self):
        import measure

        snapshot = {
            "metadata": {
                "suite": "reference_compression",
                "generated_at": "2026-05-22T00:00:00+00:00",
                "runner": "codex exec",
                "codex_cli_version": "codex-test",
                "model": "test",
                "prompt_count": 1,
                "trials": 1,
                "arms": [
                    "normal",
                    "caveman_ultra",
                    "simple_man_runtime",
                    "simple_man_candidate",
                ],
            },
            "prompts": [{"id": "p1", "category": "x", "prompt": "P"}],
            "runs": [
                {
                    "prompt_id": "p1",
                    "category": "x",
                    "arm": "normal",
                    "trial": 1,
                    "visible_input_tokens": 20,
                    "visible_output_tokens": 100,
                    "visible_total_tokens": 120,
                    "usage": {},
                    "text": "normal",
                },
                {
                    "prompt_id": "p1",
                    "category": "x",
                    "arm": "caveman_ultra",
                    "trial": 1,
                    "visible_input_tokens": 200,
                    "visible_output_tokens": 35,
                    "visible_total_tokens": 235,
                    "usage": {},
                    "text": "cave",
                },
                {
                    "prompt_id": "p1",
                    "category": "x",
                    "arm": "simple_man_runtime",
                    "trial": 1,
                    "visible_input_tokens": 60,
                    "visible_output_tokens": 50,
                    "visible_total_tokens": 110,
                    "usage": {},
                    "text": "simple",
                },
                {
                    "prompt_id": "p1",
                    "category": "x",
                    "arm": "simple_man_candidate",
                    "trial": 1,
                    "visible_input_tokens": 65,
                    "visible_output_tokens": 45,
                    "visible_total_tokens": 110,
                    "usage": {},
                    "text": "candidate",
                },
            ],
        }

        report = measure.render_report(snapshot)

        self.assertIn("Reference Output Compression", report)
        self.assertIn("| Caveman ultra | +65.0% | 35 |", report)
        self.assertIn("| Simple Man runtime | +50.0% | 50 |", report)
        self.assertIn("| Simple Man candidate | +55.0% | 45 |", report)
        self.assertNotIn("First-turn net", report)

    def test_reference_sanity_requires_caveman_to_compress_like_readme(self):
        import measure

        snapshot = {
            "metadata": {"suite": "reference_compression"},
            "prompts": [{"id": "p1", "category": "x", "prompt": "P"}],
            "runs": [
                {
                    "prompt_id": "p1",
                    "arm": "normal",
                    "trial": 1,
                    "visible_output_tokens": 100,
                    "usage": {},
                    "text": "normal",
                },
                {
                    "prompt_id": "p1",
                    "arm": "caveman_full",
                    "trial": 1,
                    "visible_output_tokens": 70,
                    "usage": {},
                    "text": "cave",
                },
                {
                    "prompt_id": "p1",
                    "arm": "caveman_ultra",
                    "trial": 1,
                    "visible_output_tokens": 65,
                    "usage": {},
                    "text": "cave",
                },
            ],
        }

        failures = measure.reference_sanity_failures(snapshot, min_savings=0.50)

        self.assertEqual(len(failures), 1)
        self.assertIn("Caveman reference sanity failed", failures[0])

    def test_snapshot_hash_validation_checks_runtime_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            runtime = Path(tmp) / "AGENTS.md.snippet"
            candidate = Path(tmp) / "candidate.md"
            skill.write_text("skill\n")
            runtime.write_text("runtime current\n")
            candidate.write_text("candidate current\n")
            snapshot = {
                "metadata": {
                    "skill_hashes": {
                        "simple_man_skill": bench.sha256_text("skill\n"),
                        "simple_man_runtime": bench.sha256_text("runtime old\n"),
                        "simple_man_candidate": bench.sha256_text("candidate old\n"),
                    },
                    "prompt_corpus_sha256": "unused",
                }
            }

            errors = bench.validate_snapshot_freshness(
                snapshot=snapshot,
                skill_path=skill,
                runtime_path=runtime,
                candidate_path=candidate,
                prompts_path=None,
            )

        self.assertEqual(len(errors), 2)
        self.assertIn("runtime_sha256 mismatch", errors[0])
        self.assertIn("candidate_sha256 mismatch", errors[1])


if __name__ == "__main__":
    unittest.main()
