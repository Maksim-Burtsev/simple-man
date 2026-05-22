#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from benchmark_lib import ARM_LABELS, CAVEMAN_ARM, CONTROL_ARM
from benchmark_lib import SIMPLE_MAN_RUNTIME_ARM, SIMPLE_MAN_SKILL_ARM, TERSE_ARM
from benchmark_lib import build_category_summary, build_prompt_table, check_run_quality
from benchmark_lib import compare_arm, pct
from benchmark_lib import validate_snapshot_age, validate_snapshot_freshness


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS = ROOT / "evals" / "prompts" / "coding_tasks.jsonl"
DEFAULT_SKILL = ROOT / "skills" / "simple-man" / "SKILL.md"
DEFAULT_RUNTIME_POLICY = ROOT / "AGENTS.md.snippet"
DEFAULT_SNAPSHOT = ROOT / "evals" / "snapshots" / "codex-results.json"


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


def render_report(snapshot: dict[str, Any], *, amortize_turns: int = 10) -> str:
    metadata = snapshot.get("metadata", {})
    rows = build_prompt_table(snapshot)
    available_arms = set(metadata.get("arms", [])) or {
        arm for row in rows for arm in row.arms
    }
    skill_arms = [
        arm
        for arm in (SIMPLE_MAN_RUNTIME_ARM, SIMPLE_MAN_SKILL_ARM, CAVEMAN_ARM)
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


def run_checks(args: argparse.Namespace, snapshot: dict[str, Any]) -> int:
    errors: list[str] = []
    errors.extend(
        validate_snapshot_freshness(
            snapshot=snapshot,
            skill_path=args.skill,
            runtime_path=args.runtime_policy,
            prompts_path=args.prompts,
        )
    )
    errors.extend(validate_snapshot_age(snapshot, args.max_age_days))
    checked_arms = set(
        args.quality_arm or [SIMPLE_MAN_RUNTIME_ARM, SIMPLE_MAN_SKILL_ARM]
    )
    errors.extend(quality_failures(snapshot, checked_arms=checked_arms))

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
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--runtime-policy", type=Path, default=DEFAULT_RUNTIME_POLICY)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--quality-arm",
        action="append",
        default=None,
        help=(
            "Arm to gate with prompt quality checks. Repeatable. "
            "Defaults to simple_man_runtime and simple_man_skill."
        ),
    )
    parser.add_argument("--max-age-days", type=int, default=90)
    parser.add_argument("--amortize-turns", type=int, default=10)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    if args.check:
        return run_checks(args, snapshot)

    print(render_report(snapshot, amortize_turns=args.amortize_turns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
