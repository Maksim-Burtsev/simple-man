import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import benchmark_lib as bench  # noqa: E402
import run_codex  # noqa: E402


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
                ],
            },
        }
        run = {
            "prompt_id": "status",
            "arm": "simple_man",
            "text": (
                "Unit tests: pass. Integration tests: fail. "
                "tool_call_id recorded. AS build. before dropping any table."
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
        self.assertIn("simple_man_skill", arms)

    def test_runtime_policy_has_no_skill_reference(self):
        snippet = (ROOT / "AGENTS.md.snippet").read_text()

        self.assertNotIn("$simple-man", snippet)
        self.assertIn("Simple Man runtime policy", snippet)

    def test_arm_instructions_split_runtime_and_full_skill(self):
        skill_texts = {
            "simple_man_runtime": "runtime rules",
            "simple_man_skill": "full skill rules",
        }

        runtime = run_codex.arm_instructions("simple_man_runtime", skill_texts)
        full_skill = run_codex.arm_instructions("simple_man_skill", skill_texts)

        self.assertIn("<simple_man_runtime_policy>", runtime)
        self.assertIn("runtime rules", runtime)
        self.assertNotIn("full skill rules", runtime)
        self.assertIn("<simple_man_skill>", full_skill)
        self.assertIn("full skill rules", full_skill)

    def test_snapshot_hash_validation_checks_runtime_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            runtime = Path(tmp) / "AGENTS.md.snippet"
            skill.write_text("skill\n")
            runtime.write_text("runtime current\n")
            snapshot = {
                "metadata": {
                    "skill_hashes": {
                        "simple_man_skill": bench.sha256_text("skill\n"),
                        "simple_man_runtime": bench.sha256_text("runtime old\n"),
                    },
                    "prompt_corpus_sha256": "unused",
                }
            }

            errors = bench.validate_snapshot_freshness(
                snapshot=snapshot,
                skill_path=skill,
                runtime_path=runtime,
                prompts_path=None,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("runtime_sha256 mismatch", errors[0])


if __name__ == "__main__":
    unittest.main()
