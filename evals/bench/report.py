#!/usr/bin/env python3
"""Recompute every benchmark number from raw run records.

Nothing here trusts a stored summary: the report is derived from the raw JSONL
each time it runs, and ``--check`` regenerates it and fails on any difference
from the committed copy. A published number that cannot be rebuilt from the raw
evidence is treated as a defect, not a rounding difference.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

import eval_v2_lib as lib  # noqa: E402

BOOTSTRAP_SEED = 20260817
BOOTSTRAP_ITERATIONS = 10000


def _read(path: Path) -> list[dict[str, Any]]:
    return lib.strict_jsonl(path) if path.exists() else []


def load_run(directory: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        "output": _read(directory / "output.jsonl"),
        "activation": _read(directory / "activation.jsonl"),
        "judge": _read(directory / "judge.jsonl"),
    }


def arm_output_tokens(records: list[dict]) -> dict[str, list[int]]:
    per_arm: dict[str, list[int]] = defaultdict(list)
    for record in records:
        per_arm[record["arm"]].append(record["output_tokens"])
    return dict(per_arm)


def pair_records(records: list[dict], baseline: str, candidate: str) -> list[dict[str, Any]]:
    """Pair two arms per case, in the shape ``clustered_bootstrap_ci`` expects.

    ``lib.pair_measurements`` is not reused here because it restricts arm names
    to the four-arm vocabulary of the older eval, which cannot express this
    benchmark's five arms. The bootstrap itself is arm-name agnostic and is
    reused unchanged.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["arm"] not in (baseline, candidate):
            continue
        bucket = grouped.setdefault(
            record["case_id"], {"cluster_id": record["cluster_id"], "sides": {}}
        )
        if record["arm"] in bucket["sides"]:
            raise ValueError(f"duplicate {record['arm']} record for {record['case_id']}")
        bucket["sides"][record["arm"]] = {
            "visible_output_tokens": record["output_tokens"],
            "input_tokens": record["input_tokens"],
            "latency_ms": record["latency_ms"],
        }
    pairs = []
    for case_id, bucket in sorted(grouped.items()):
        if set(bucket["sides"]) != {baseline, candidate}:
            continue
        pairs.append(
            {
                "case_id": case_id,
                "cluster_id": bucket["cluster_id"],
                "baseline_arm": baseline,
                "candidate_arm": candidate,
                baseline: bucket["sides"][baseline],
                candidate: bucket["sides"][candidate],
            }
        )
    return pairs


def pairwise(records: list[dict], baseline: str, candidate: str) -> dict[str, Any] | None:
    """Median relative output reduction of ``candidate`` against ``baseline``."""
    pairs = pair_records(records, baseline, candidate)
    if not pairs:
        return None
    reductions = []
    for pair in pairs:
        base = pair[baseline]["visible_output_tokens"]
        cand = pair[candidate]["visible_output_tokens"]
        if base:
            reductions.append((base - cand) / base)
    if not reductions:
        return None
    low, high = lib.clustered_bootstrap_ci(
        pairs,
        "visible_output_tokens",
        seed=BOOTSTRAP_SEED,
        iterations=BOOTSTRAP_ITERATIONS,
    )
    return {
        "baseline": baseline,
        "candidate": candidate,
        "n": len(reductions),
        "median_reduction": statistics.median(reductions),
        "ci_low": low,
        "ci_high": high,
        "candidate_median_tokens": statistics.median(
            pair[candidate]["visible_output_tokens"] for pair in pairs
        ),
        "baseline_median_tokens": statistics.median(
            pair[baseline]["visible_output_tokens"] for pair in pairs
        ),
    }


def retention(cases: list[dict], records: list[dict]) -> dict[str, Any]:
    """Material-fact retention per arm, using each case's own fact checklist."""
    by_id = {case["id"]: case for case in cases}
    per_arm: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"passed": 0, "total": 0, "missing": [], "forbidden": []}
    )
    for record in records:
        case = by_id.get(record["case_id"])
        if not case:
            continue
        result = lib.check_critical_facts(case, record["response"])
        bucket = per_arm[record["arm"]]
        bucket["total"] += 1
        bucket["passed"] += 1 if result["passed"] else 0
        for fact in result["missing"]:
            bucket["missing"].append(f"{record['case_id']}:{fact}")
        for claim in result["forbidden"]:
            bucket["forbidden"].append(f"{record['case_id']}:{claim}")
    for bucket in per_arm.values():
        bucket["rate"] = bucket["passed"] / bucket["total"] if bucket["total"] else None
    return dict(per_arm)


def activation(cases: list[dict], records: list[dict]) -> dict[str, Any]:
    by_description: dict[str, dict[str, bool]] = defaultdict(dict)
    for record in records:
        by_description[record["description"]][record["case_id"]] = record["predicted_activate"]
    seen = {case["id"] for case in cases}
    out: dict[str, Any] = {}
    for name, predictions in by_description.items():
        subset = [case for case in cases if case["id"] in predictions and case["id"] in seen]
        out[name] = lib.activation_confusion_matrix(
            subset, {case["id"]: predictions[case["id"]] for case in subset}
        )
    return out


def blind(records: list[dict]) -> dict[str, Any]:
    """Position-consistent wins only, plus an explicit position-bias readout.

    A pair counts for an arm only when it wins in both orderings. Disagreement
    between orderings is a tie, not a coin flip: it means the judge could not
    separate the answers except by position.
    """
    by_pair: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    left_choices = 0
    decided = 0
    flags: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if not record.get("judgment"):
            continue
        by_pair[(record["comparison"], record["case_id"])][record["position"]] = record
        if record["judgment"]["quality"] in ("left", "right"):
            decided += 1
            left_choices += 1 if record["judgment"]["quality"] == "left" else 0
        for side in ("left", "right"):
            arm = record[f"{side}_arm"]
            for flag in record["judgment"]["flags"][side]:
                flags[arm].append(f"{record['case_id']}:{flag}")

    results: dict[str, Any] = {}
    for (comparison, case_id), positions in by_pair.items():
        if len(positions) != 2:
            continue
        winners = set()
        for record in positions.values():
            choice = record["judgment"]["quality"]
            winners.add(record[f"{choice}_arm"] if choice in ("left", "right") else None)
        bucket = results.setdefault(
            comparison, {"wins": defaultdict(int), "ties": 0, "cases": 0}
        )
        bucket["cases"] += 1
        if len(winners) == 1 and None not in winners:
            bucket["wins"][winners.pop()] += 1
        else:
            bucket["ties"] += 1
    for bucket in results.values():
        bucket["wins"] = dict(bucket["wins"])
    return {
        "comparisons": results,
        "position_bias": {
            "decided": decided,
            "left_share": (left_choices / decided) if decided else None,
        },
        "flags": {arm: sorted(items) for arm, items in flags.items()},
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:+.1f}%"


def render(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Simple Man benchmark")
    add("")
    meta = summary["meta"]
    add(f"Model: `{meta['model']}`  Judge: `{meta['judge_model']}`  CLI: `{meta['cli']}`")
    add(f"Output cases: {meta['output_cases']}  Activation cases: {meta['activation_cases']}")
    add(f"Live calls: {meta['calls']}  Reported cost: ${meta['cost_usd']:.2f}")
    add("")
    add("Every number below is recomputed from the raw records by")
    add("`evals/bench/report.py`; `make bench-v3-check` fails if it cannot be rebuilt.")
    add("")

    add("## Output length")
    add("")
    add("| Comparison | n | Median reduction | 95% CI | Median tokens |")
    add("| --- | ---: | ---: | --- | ---: |")
    for row in summary["pairwise"]:
        add(
            f"| {row['candidate']} vs {row['baseline']} | {row['n']} | "
            f"{_pct(row['median_reduction'])} | "
            f"[{_pct(row['ci_low'])}, {_pct(row['ci_high'])}] | "
            f"{row['candidate_median_tokens']:.0f} vs {row['baseline_median_tokens']:.0f} |"
        )
    add("")

    add("## Material fact retention")
    add("")
    add("Share of cases where every required fact survived and no forbidden claim appeared.")
    add("")
    add("| Arm | Retention | Cases |")
    add("| --- | ---: | ---: |")
    for arm, bucket in sorted(summary["retention"].items()):
        rate = "n/a" if bucket["rate"] is None else f"{100 * bucket['rate']:.1f}%"
        add(f"| {arm} | {rate} | {bucket['passed']}/{bucket['total']} |")
    add("")

    if summary["activation"]:
        add("## Activation")
        add("")
        add("| Description | Implicit recall | Precision | Explicit | Protected FP |")
        add("| --- | ---: | ---: | ---: | ---: |")
        for name, matrix in sorted(summary["activation"].items()):
            protected = sum(matrix["protected_false_positives"].values())
            add(
                f"| {name} | {_rate(matrix['implicit_recall'])} | "
                f"{_rate(matrix['precision'])} | {_rate(matrix['explicit_accuracy'])} | "
                f"{protected} |"
            )
        add("")

    blind_summary = summary["blind"]
    if blind_summary["comparisons"]:
        add("## Blind pairwise preference")
        add("")
        add("Each case judged in both orderings; a win requires winning both.")
        add("")
        add("| Comparison | Wins | Ties | Cases |")
        add("| --- | --- | ---: | ---: |")
        for comparison, bucket in sorted(blind_summary["comparisons"].items()):
            wins = ", ".join(f"{arm} {count}" for arm, count in sorted(bucket["wins"].items()))
            add(f"| {comparison} | {wins or 'none'} | {bucket['ties']} | {bucket['cases']} |")
        add("")
        bias = blind_summary["position_bias"]
        if bias["left_share"] is not None:
            add(
                f"Judge chose the left position in {100 * bias['left_share']:.1f}% of "
                f"{bias['decided']} decided judgments. Far from 50% would indicate "
                "position bias rather than a real preference."
            )
            add("")

    add("## Limits")
    add("")
    add(f"- One model (`{meta['model']}`), one CLI version, one run per case. No repeats.")
    add("- The CLI exposes no temperature or seed control, so runs are not reproducible")
    add("  bit-for-bit. Confidence intervals are clustered on case id.")
    add("- Activation is measured as a routing decision from the skill description,")
    add("  not as a live end-to-end dispatch inside an agent session.")
    add("- Results are scoped to this corpus, model and CLI. They are not a universal")
    add("  claim about token cost.")
    return "\n".join(lines) + "\n"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def build(directory: Path, cases_path: Path, activation_path: Path) -> dict[str, Any]:
    run = load_run(directory)
    output_cases = lib.strict_jsonl(cases_path)
    activation_cases = lib.strict_jsonl(activation_path)

    arms = sorted(arm_output_tokens(run["output"]))
    comparisons = [(b, c) for b in ("N", "A", "G") for c in ("B",) if b in arms and c in arms]
    comparisons += [(b, c) for b, c in (("N", "A"), ("N", "C"), ("N", "G")) if b in arms and c in arms]

    pairwise_rows = [row for row in (pairwise(run["output"], b, c) for b, c in comparisons) if row]

    calls = len(run["output"]) + len(run["activation"]) + len(run["judge"])
    cost = sum(float(r.get("cost_usd") or 0) for group in run.values() for r in group)
    model = next((r["model"] for r in run["output"]), "unknown")
    judge_model = next((r["model"] for r in run["judge"]), "unknown")
    cli = next((r["cli"] for group in run.values() for r in group), "unknown")

    return {
        "meta": {
            "model": model,
            "judge_model": judge_model,
            "cli": cli,
            "calls": calls,
            "cost_usd": cost,
            "output_cases": len({r["case_id"] for r in run["output"]}),
            "activation_cases": len({r["case_id"] for r in run["activation"]}),
        },
        "pairwise": pairwise_rows,
        "retention": retention(output_cases, run["output"]),
        "activation": activation(activation_cases, run["activation"]),
        "blind": blind(run["judge"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-cases", type=Path, default=ROOT / "evals/cases/bench-output.jsonl")
    parser.add_argument(
        "--activation-cases", type=Path, default=ROOT / "evals/cases/bench-activation.jsonl"
    )
    parser.add_argument("--write", type=Path, help="write the rendered report here")
    parser.add_argument("--check", type=Path, help="fail unless this file matches a fresh rebuild")
    args = parser.parse_args(argv)

    summary = build(args.run_dir, args.output_cases, args.activation_cases)
    rendered = render(summary)

    if args.check:
        current = args.check.read_text() if args.check.exists() else ""
        if current != rendered:
            print(f"{args.check} does not match a rebuild from raw records", file=sys.stderr)
            return 1
        print(f"{args.check} matches the raw records")
        return 0

    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(rendered)
        print(f"wrote {args.write}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
