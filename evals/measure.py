#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from benchmark_lib import ARM_LABELS, CAVEMAN_ARM, CAVEMAN_FULL_ARM, CAVEMAN_ULTRA_ARM
from benchmark_lib import CONTROL_ARM, NORMAL_ARM, SIMPLE_MAN_CANDIDATE_ARM
from benchmark_lib import SIMPLE_MAN_RUNTIME_ARM
from benchmark_lib import SIMPLE_MAN_SKILL_ARM, SUITE_REFERENCE, SUITE_RUNTIME, TERSE_ARM
from benchmark_lib import build_category_summary, build_prompt_table, check_run_quality
from benchmark_lib import compare_arm, pct
from benchmark_lib import validate_snapshot_age, validate_snapshot_freshness


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS = ROOT / "evals" / "prompts" / "coding_tasks.jsonl"
DEFAULT_REFERENCE_PROMPTS = ROOT / "evals" / "prompts" / "reference_compression.jsonl"
DEFAULT_SKILL = ROOT / "skills" / "simple-man" / "SKILL.md"
DEFAULT_RUNTIME_POLICY = ROOT / "AGENTS.md.snippet"
DEFAULT_CANDIDATE_POLICY = ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md"
DEFAULT_SNAPSHOT = ROOT / "evals" / "snapshots" / "codex-results.json"
DEFAULT_REFERENCE_SNAPSHOT = ROOT / "evals" / "snapshots" / "reference-results.json"


def load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"No snapshot at {path}. Run `make bench-refresh` or pass --snapshot."
        )
    return json.loads(path.read_text())


def fmt_num(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def mean(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    return statistics.mean(clean) if clean else None


def comparison_values(rows, *, arm: str, baseline_arm: str, amortize_turns: int):
    values = []
    for row in rows:
        candidate = row.arms.get(arm)
        baseline = row.arms.get(baseline_arm)
        if candidate and baseline:
            values.append(compare_arm(candidate, baseline, amortize_turns=amortize_turns))
    return values


def arm_label(arm: str) -> str:
    return ARM_LABELS.get(arm, arm)


def _available_arms(snapshot: dict[str, Any], rows) -> set[str]:
    metadata = snapshot.get("metadata", {})
    return set(metadata.get("arms", [])) or {arm for row in rows for arm in row.arms}


def _arm_mean_output(rows, arm: str) -> float | None:
    return mean(
        [row.arms[arm].median_output for row in rows if row.arms.get(arm)]
    )


def _reference_skill_arms(available_arms: set[str]) -> list[str]:
    return [
        arm
        for arm in (
            CAVEMAN_FULL_ARM,
            CAVEMAN_ULTRA_ARM,
            SIMPLE_MAN_RUNTIME_ARM,
            SIMPLE_MAN_CANDIDATE_ARM,
            SIMPLE_MAN_SKILL_ARM,
        )
        if arm in available_arms
    ]


def render_reference_report(snapshot: dict[str, Any]) -> str:
    metadata = snapshot.get("metadata", {})
    rows = build_prompt_table(snapshot)
    available_arms = _available_arms(snapshot, rows)
    skill_arms = _reference_skill_arms(available_arms)

    lines: list[str] = []
    lines.append(f"_Generated: {metadata.get('generated_at', '?')}_")
    lines.append(
        f"_Runner: {metadata.get('runner', '?')} · "
        f"Codex: {metadata.get('codex_cli_version', '?')} · "
        f"Model: {metadata.get('model', '?')}_"
    )
    lines.append(
        f"_n = {metadata.get('prompt_count', len(rows))} prompts × "
        f"{metadata.get('trials', '?')} trials · primary = visible_output_tokens_"
    )
    lines.append(
        "_Baseline is Codex-calibrated verbose normal: helpful, thorough, clear, "
        "with examples/tradeoffs where useful._"
    )
    lines.append("")

    lines.append("**Reference Output Compression**")
    lines.append("| Arm | Output compression vs normal | Mean output tokens |")
    lines.append("|---|---:|---:|")
    for arm in skill_arms:
        values = comparison_values(
            rows,
            arm=arm,
            baseline_arm=NORMAL_ARM,
            amortize_turns=1,
        )
        lines.append(
            f"| {arm_label(arm)} | "
            f"{pct(mean([value.output_savings for value in values]))} | "
            f"{fmt_num(_arm_mean_output(rows, arm))} |"
        )

    lines.append("")
    lines.append("**Per Prompt**")
    header = "| Prompt | Normal out |"
    divider = "|---|---:|"
    for arm in skill_arms:
        label = arm_label(arm)
        header += f" {label} out | {label} vs normal |"
        divider += "---:|---:|"
    lines.append(header)
    lines.append(divider)
    for row in rows:
        normal = row.arms.get(NORMAL_ARM)
        line = f"| {row.prompt_id} | {fmt_num(normal.median_output if normal else None)} |"
        for arm in skill_arms:
            candidate = row.arms.get(arm)
            vs_normal = (
                compare_arm(candidate, normal, amortize_turns=1)
                if candidate and normal
                else None
            )
            line += (
                f" {fmt_num(candidate.median_output if candidate else None)} | "
                f"{pct(vs_normal.output_savings if vs_normal else None)} |"
            )
        lines.append(line)

    lines.append("")
    lines.append(
        "_Reference suite is output-only. It validates Caveman README-style "
        "compression before comparing Simple Man on the same prompts._"
    )
    return "\n".join(lines)


def render_report(snapshot: dict[str, Any], *, amortize_turns: int = 10) -> str:
    metadata = snapshot.get("metadata", {})
    rows = build_prompt_table(snapshot)
    if metadata.get("suite") == SUITE_REFERENCE:
        return render_reference_report(snapshot)

    available_arms = _available_arms(snapshot, rows)
    skill_arms = [
        arm
        for arm in (
            SIMPLE_MAN_RUNTIME_ARM,
            SIMPLE_MAN_CANDIDATE_ARM,
            SIMPLE_MAN_SKILL_ARM,
            CAVEMAN_ARM,
        )
        if arm in available_arms
    ]

    lines: list[str] = []
    lines.append(f"_Generated: {metadata.get('generated_at', '?')}_")
    lines.append(
        f"_Runner: {metadata.get('runner', '?')} · "
        f"Codex: {metadata.get('codex_cli_version', '?')} · "
        f"Model: {metadata.get('model', '?')}_"
    )
    lines.append(
        f"_n = {metadata.get('prompt_count', len(rows))} prompts × "
        f"{metadata.get('trials', '?')} trials · "
        f"primary = visible_input_tokens + visible_output_tokens_"
    )
    lines.append(f"_Amortized net assumes skill input overhead spread over {amortize_turns} turns._")
    lines.append("")

    runtime = SIMPLE_MAN_RUNTIME_ARM if SIMPLE_MAN_RUNTIME_ARM in available_arms else None
    if runtime:
        runtime_vs_control = comparison_values(
            rows,
            arm=runtime,
            baseline_arm=CONTROL_ARM,
            amortize_turns=amortize_turns,
        )
        lines.append("**Headline**")
        lines.append("| Metric | Value |")
        lines.append("|---|---:|")
        lines.append(
            "| Runtime output compression vs control | "
            f"{pct(mean([value.output_savings for value in runtime_vs_control]))} |"
        )
        if CAVEMAN_ARM in available_arms:
            runtime_vs_caveman = comparison_values(
                rows,
                arm=runtime,
                baseline_arm=CAVEMAN_ARM,
                amortize_turns=amortize_turns,
            )
            lines.append(
                "| Runtime output compression vs Caveman | "
                f"{pct(mean([value.output_savings for value in runtime_vs_caveman]))} |"
            )
        lines.append("")

        lines.append("**Runtime Session Net Vs Control**")
        lines.append("| Amortized turns | Session net |")
        lines.append("|---:|---:|")
        for turns in (20, 50, 100):
            values = comparison_values(
                rows,
                arm=runtime,
                baseline_arm=CONTROL_ARM,
                amortize_turns=turns,
            )
            lines.append(
                f"| {turns} | {pct(mean([value.amortized_net_savings for value in values]))} |"
            )
        lines.append("")

    lines.append("**Output Compression**")
    header = "| Prompt | Category | Control out | Terse out |"
    divider = "|---|---:|---:|---:|"
    for arm in skill_arms:
        label = arm_label(arm)
        header += f" {label} out | {label} vs control | {label} vs terse |"
        divider += "---:|---:|---:|"
    lines.append(header)
    lines.append(divider)
    for row in rows:
        control = row.arms.get(CONTROL_ARM)
        terse = row.arms.get(TERSE_ARM)
        line = (
            f"| {row.prompt_id} | {row.category} | "
            f"{fmt_num(control.median_output if control else None)} | "
            f"{fmt_num(terse.median_output if terse else None)} |"
        )
        for arm in skill_arms:
            candidate = row.arms.get(arm)
            vs_control = (
                compare_arm(candidate, control, amortize_turns=amortize_turns)
                if candidate and control
                else None
            )
            vs_terse = (
                compare_arm(candidate, terse, amortize_turns=amortize_turns)
                if candidate and terse
                else None
            )
            line += (
                f" {fmt_num(candidate.median_output if candidate else None)} | "
                f"{pct(vs_control.output_savings if vs_control else None)} | "
                f"{pct(vs_terse.output_savings if vs_terse else None)} |"
            )
        lines.append(line)

    lines.append("")
    lines.append("**Summary Vs Control**")
    lines.append("| Arm | Output compression | First-turn net | Amortized net |")
    lines.append("|---|---:|---:|---:|")
    for arm in skill_arms:
        label = arm_label(arm)
        values = comparison_values(
            rows,
            arm=arm,
            baseline_arm=CONTROL_ARM,
            amortize_turns=amortize_turns,
        )
        lines.append(
            f"| {label} | "
            f"{pct(mean([value.output_savings for value in values]))} | "
            f"{pct(mean([value.first_turn_net_savings for value in values]))} | "
            f"{pct(mean([value.amortized_net_savings for value in values]))} |"
        )

    lines.append("")
    lines.append("**Category Summary Vs Control**")
    lines.append("| Arm | Category | Prompts | Output compression | First-turn net | Amortized net |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for arm in skill_arms:
        label = arm_label(arm)
        for item in build_category_summary(
            rows,
            arm=arm,
            baseline_arm=CONTROL_ARM,
            amortize_turns=amortize_turns,
        ):
            lines.append(
                f"| {label} | {item.category} | {item.prompts} | "
                f"{pct(item.mean_output_savings)} | "
                f"{pct(item.mean_first_turn_net_savings)} | "
                f"{pct(item.mean_amortized_net_savings)} |"
            )

    lines.append("")
    lines.append(
        "_Output compression measures answer length only. First-turn net includes full "
        "visible instruction input overhead. Amortized net spreads instruction overhead across turns._"
    )
    return "\n".join(lines)


def quality_failures(snapshot: dict[str, Any], *, checked_arms: set[str]) -> list[str]:
    prompts = {prompt["id"]: prompt for prompt in snapshot.get("prompts", [])}
    failures: list[str] = []
    for run in snapshot.get("runs", []):
        if run.get("arm") not in checked_arms:
            continue
        prompt = prompts.get(run.get("prompt_id"))
        if not prompt:
            failures.append(
                f"{run.get('prompt_id')} {run.get('arm')}: prompt missing from snapshot"
            )
            continue
        for failure in check_run_quality(prompt, run):
            failures.append(
                f"{run.get('prompt_id')} {run.get('arm')} trial {run.get('trial')}: {failure}"
            )
    return failures


def default_quality_arms(snapshot: dict[str, Any]) -> list[str]:
    metadata = snapshot.get("metadata", {})
    available_arms = set(metadata.get("arms", []))
    return [
        arm
        for arm in (
            SIMPLE_MAN_RUNTIME_ARM,
            SIMPLE_MAN_CANDIDATE_ARM,
            SIMPLE_MAN_SKILL_ARM,
        )
        if not available_arms or arm in available_arms
    ]


def reference_sanity_failures(
    snapshot: dict[str, Any],
    *,
    min_savings: float,
) -> list[str]:
    if snapshot.get("metadata", {}).get("suite") != SUITE_REFERENCE:
        return []

    rows = build_prompt_table(snapshot)
    caveman_means: list[tuple[str, float]] = []
    for arm in (CAVEMAN_FULL_ARM, CAVEMAN_ULTRA_ARM):
        values = comparison_values(
            rows,
            arm=arm,
            baseline_arm=NORMAL_ARM,
            amortize_turns=1,
        )
        arm_mean = mean([value.output_savings for value in values])
        if arm_mean is not None:
            caveman_means.append((arm, arm_mean))

    if not caveman_means:
        return ["Caveman reference sanity failed: no Caveman reference arm present"]

    best_arm, best_savings = max(caveman_means, key=lambda item: item[1])
    if best_savings < min_savings:
        return [
            "Caveman reference sanity failed: "
            f"best={arm_label(best_arm)} {pct(best_savings)}, "
            f"required>={pct(min_savings)}"
        ]
    return []


def run_checks(args: argparse.Namespace, snapshot: dict[str, Any]) -> int:
    errors: list[str] = []
    errors.extend(
        validate_snapshot_freshness(
            snapshot=snapshot,
            skill_path=args.skill,
            runtime_path=args.runtime_policy,
            candidate_path=args.candidate_runtime_policy,
            prompts_path=args.prompts,
        )
    )
    errors.extend(validate_snapshot_age(snapshot, args.max_age_days))
    checked_arms = set(args.quality_arm or default_quality_arms(snapshot))
    errors.extend(quality_failures(snapshot, checked_arms=checked_arms))
    errors.extend(
        reference_sanity_failures(
            snapshot,
            min_savings=args.reference_min_caveman_savings,
        )
    )

    expected_runs = (
        int(snapshot.get("metadata", {}).get("prompt_count", 0))
        * len(snapshot.get("metadata", {}).get("arms", []))
        * int(snapshot.get("metadata", {}).get("trials", 0))
    )
    actual_runs = len(snapshot.get("runs", []))
    if expected_runs and actual_runs != expected_runs:
        errors.append(f"run count mismatch: snapshot={actual_runs} expected={expected_runs}")

    if errors:
        print("Benchmark check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Benchmark check passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Simple Man benchmark snapshot.")
    parser.add_argument(
        "--suite",
        choices=(SUITE_RUNTIME, SUITE_REFERENCE),
        default=None,
        help="Benchmark suite. Defaults to snapshot metadata.",
    )
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--runtime-policy", type=Path, default=DEFAULT_RUNTIME_POLICY)
    parser.add_argument(
        "--candidate-runtime-policy",
        type=Path,
        default=DEFAULT_CANDIDATE_POLICY,
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--quality-arm",
        action="append",
        default=None,
        help=(
            "Arm to gate with prompt quality checks. Repeatable. "
            "Defaults to Simple Man runtime/candidate/skill arms."
        ),
    )
    parser.add_argument("--max-age-days", type=int, default=90)
    parser.add_argument("--amortize-turns", type=int, default=10)
    parser.add_argument("--reference-min-caveman-savings", type=float, default=0.50)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    suite = args.suite or snapshot.get("metadata", {}).get("suite")
    if suite == SUITE_REFERENCE and args.prompts == DEFAULT_PROMPTS:
        args.prompts = DEFAULT_REFERENCE_PROMPTS
    if args.check:
        return run_checks(args, snapshot)

    print(render_report(snapshot, amortize_turns=args.amortize_turns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
