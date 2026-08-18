#!/usr/bin/env python3
"""Apply the preregistered release gates to a completed run.

The gates and their thresholds are read from the preregistration committed
before the run, never from this file, so the bar cannot be moved after seeing
the results. A failing gate is reported, not repaired.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals" / "bench"))

import report as bench_report  # noqa: E402

#: Defaults for the first release; a preregistration may name different arms,
#: which is why evaluate() reads them from it rather than from this file.
CANDIDATE = "B"
BASELINE = "A"
CONTROL = "N"
GENERIC = "G"
CANDIDATE_DESCRIPTION = "D1"


def _compare(op: str, value: Any, threshold: Any) -> bool:
    if value is None:
        return False
    if op == "eq":
        return value == threshold
    if op == "gte":
        return value >= threshold
    if op == "gt":
        return value > threshold
    raise ValueError(f"unknown operator {op!r}")


def measure(summary: dict[str, Any], prereg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reduce the report summary to the values the gate table names."""
    prereg = prereg or {}
    candidate = prereg.get("candidate_arm", CANDIDATE)
    baseline = prereg.get("baseline_arm", BASELINE)
    control = prereg.get("control_arm", CONTROL)
    generic = prereg.get("generic_arm", GENERIC)
    description = prereg.get("candidate_description", CANDIDATE_DESCRIPTION)

    pairwise = {(row["baseline"], row["candidate"]): row for row in summary["pairwise"]}
    retention = summary["retention"]
    activation = summary["activation"].get(description, {})
    blind = summary["blind"]["comparisons"]
    coding = summary.get("coding", {})

    def reduction(against: str) -> float | None:
        row = pairwise.get((against, candidate))
        return row["median_reduction"] if row else None

    def lower_bound(against: str) -> float | None:
        row = pairwise.get((against, candidate))
        return row["ci_low"] if row else None

    def margin(comparison: str, opponent: str) -> int | None:
        bucket = blind.get(comparison)
        if not bucket:
            return None
        return bucket["wins"].get(candidate, 0) - bucket["wins"].get(opponent, 0)

    candidate_retention = retention.get(candidate, {}).get("rate")
    baseline_retention = retention.get(baseline, {}).get("rate")
    candidate_coding = coding.get(candidate)

    return {
        "explicit_activation": activation.get("explicit_accuracy"),
        "implicit_recall": activation.get("implicit_recall"),
        "precision": activation.get("precision"),
        "protected_near_miss_false_positives": (
            sum(activation["protected_false_positives"].values())
            if activation.get("protected_false_positives") is not None
            else None
        ),
        "false_validation_or_lost_blocker": len(retention.get(candidate, {}).get("forbidden", [])),
        "winner_coding_failures": (
            None if candidate_coding is None
            else candidate_coding["total"] - candidate_coding["passed"]
        ),
        "retention_equal_or_better_than_A": (
            None
            if candidate_retention is None or baseline_retention is None
            else candidate_retention >= baseline_retention
        ),
        "blind_wins_minus_losses_vs_A": margin(f"{candidate}-vs-{baseline}", baseline),
        "blind_wins_minus_losses_vs_G": margin(f"{candidate}-vs-{generic}", generic),
        "median_output_reduction_vs_N": reduction(control),
        "bootstrap_95_lower_bound_vs_N": lower_bound(control),
        "median_output_reduction_vs_G": reduction(generic),
    }


def evaluate(prereg: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    measured = measure(summary, prereg)
    results = []
    for gate in prereg["gates"]:
        value = measured.get(gate["name"])
        results.append(
            {
                "name": gate["name"],
                "op": gate["op"],
                "threshold": gate["threshold"],
                "value": value,
                "passed": _compare(gate["op"], value, gate["threshold"]),
            }
        )
    failed = [row["name"] for row in results if not row["passed"]]
    return {
        "gates": results,
        "failed": failed,
        "decision": "SHIP_CANDIDATE" if not failed else "KEEP_SHIPPED_POLICY",
    }


def render(outcome: dict[str, Any]) -> str:
    lines = ["| Gate | Required | Measured | Result |", "| --- | --- | --- | --- |"]
    for row in outcome["gates"]:
        value = row["value"]
        if isinstance(value, float):
            shown = f"{value:.3f}"
        else:
            shown = "n/a" if value is None else str(value)
        lines.append(
            f"| {row['name']} | {row['op']} {row['threshold']} | {shown} | "
            f"{'pass' if row['passed'] else 'FAIL'} |"
        )
    lines.append("")
    lines.append(f"Decision: **{outcome['decision']}**")
    if outcome["failed"]:
        lines.append("")
        lines.append("Failed: " + ", ".join(outcome["failed"]))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--preregistration",
        type=Path,
        default=ROOT / "evals" / "releases" / "v0.3.0" / "preregistration.json",
    )
    parser.add_argument("--output-cases", type=Path, action="append", dest="output_cases")
    parser.add_argument("--activation-cases", type=Path, action="append", dest="activation_cases")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    prereg = json.loads(args.preregistration.read_text())
    summary = bench_report.build(
        args.run_dir,
        args.output_cases or bench_report.DEFAULT_OUTPUT_CASES,
        args.activation_cases or bench_report.DEFAULT_ACTIVATION_CASES,
    )
    outcome = evaluate(prereg, summary)

    if args.json:
        print(json.dumps(outcome, indent=2, sort_keys=True))
    else:
        print(render(outcome), end="")
    # A failed gate is a reported outcome, not a crash: exit 0 either way so the
    # result gets published rather than swallowed by a shell that stops on error.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
