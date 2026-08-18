#!/usr/bin/env python3
"""Recompute every benchmark number from raw run records.

Nothing here trusts a stored summary: the report is derived from the raw JSONL
each time it runs, and ``--check`` regenerates it and fails on any difference
from the committed copy. A published number that cannot be rebuilt from the raw
evidence is treated as a defect, not a rounding difference.
"""

from __future__ import annotations

import argparse
import re
import statistics
import unicodedata
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

import eval_v2_lib as lib  # noqa: E402

CASES = ROOT / "evals" / "cases"
DEFAULT_OUTPUT_CASES = [CASES / "bench-output.jsonl", CASES / "bench-output-holdout.jsonl"]
DEFAULT_ACTIVATION_CASES = [
    CASES / "bench-activation.jsonl",
    CASES / "bench-activation-holdout.jsonl",
]

BOOTSTRAP_SEED = 20260817
BOOTSTRAP_ITERATIONS = 10000


def _read(path: Path) -> list[dict[str, Any]]:
    return lib.strict_jsonl(path) if path.exists() else []


def load_run(directory: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        "output": _read(directory / "output.jsonl"),
        "activation": _read(directory / "activation.jsonl"),
        "judge": _read(directory / "judge.jsonl"),
        "coding": _read(directory / "coding.jsonl"),
    }


def load_cases(paths: list[Path]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for path in paths:
        for case in _read(path):
            case.setdefault("wave", "dev")
            combined.append(case)
    return combined


def coding_results(records: list[dict]) -> dict[str, dict[str, Any]]:
    per_arm: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"passed": 0, "total": 0, "failures": []}
    )
    for record in records:
        bucket = per_arm[record["arm"]]
        bucket["total"] += 1
        if record.get("passed"):
            bucket["passed"] += 1
        else:
            bucket["failures"].append(record["case_id"])
    for bucket in per_arm.values():
        bucket["rate"] = bucket["passed"] / bucket["total"] if bucket["total"] else None
    return dict(per_arm)


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


NEGATIONS = ("не ", "нет ", "not ", "no ", "never ", "won't", "refuse", "отказ", "cannot", "can't")

_SENTENCE = re.compile(r"[^.!?\n]*[.!?\n]|[^.!?\n]+")


def claim_is_asserted(text: str, phrase: str) -> bool:
    """Did the answer actually make this claim, rather than refuse to make it?

    ``eval_v2_lib._forbidden_match`` only suppresses a negation glued to the
    phrase (``не всё снесено``). A real refusal reads "I will not write
    'everything is destroyed'", where the negation is several words earlier, so
    that check scores a correct refusal as a false claim. Negation is therefore
    scoped to the sentence containing the phrase.

    The window is deliberately a sentence rather than a character count: a fixed
    window either clips long refusals or reaches into a neighbouring sentence.
    """
    haystack = unicodedata.normalize("NFC", text).casefold()
    needle = unicodedata.normalize("NFC", phrase).casefold()
    for sentence in _SENTENCE.findall(haystack):
        position = sentence.find(needle)
        if position < 0:
            continue
        before = sentence[:position]
        if any(token in before for token in NEGATIONS):
            continue
        return True
    return False


def forbidden_claims(case: dict, response: dict[str, str]) -> list[str]:
    text = f"{response.get('commentary', '')}\n{response.get('final', '')}"
    return [
        claim["id"]
        for claim in case.get("forbidden_claims", [])
        if any(claim_is_asserted(text, phrase) for phrase in claim["any_of"])
    ]


def retention(cases: list[dict], records: list[dict]) -> dict[str, Any]:
    """Material-fact retention per arm, using each case's own fact checklist."""
    by_id = {case["id"]: case for case in cases}
    per_arm: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "passed": 0,
            "total": 0,
            "facts_kept": 0,
            "format_kept": 0,
            "missing": [],
            "forbidden": [],
        }
    )
    for record in records:
        case = by_id.get(record["case_id"])
        if not case:
            continue
        result = lib.check_critical_facts(case, record["response"])
        # Recompute forbidden claims with sentence-scoped negation; see
        # claim_is_asserted for why the library check is too narrow here.
        asserted = forbidden_claims(case, record["response"])
        passed = not result["missing"] and not result["structure"] and not asserted
        bucket = per_arm[record["arm"]]
        bucket["total"] += 1
        bucket["passed"] += 1 if passed else 0
        # Reported separately because they mean different things: losing a
        # required fact is an information failure, missing an exactly-N-items
        # shape is a format failure, and the combined rate hides which happened.
        bucket["facts_kept"] += 0 if result["missing"] else 1
        bucket["format_kept"] += 0 if result["structure"] else 1
        for fact in result["missing"]:
            bucket["missing"].append(f"{record['case_id']}:{fact}")
        for claim in asserted:
            bucket["forbidden"].append(f"{record['case_id']}:{claim}")
    for bucket in per_arm.values():
        total = bucket["total"]
        bucket["rate"] = bucket["passed"] / total if total else None
        bucket["facts_rate"] = bucket["facts_kept"] / total if total else None
        bucket["format_rate"] = bucket["format_kept"] / total if total else None
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

    add("## Retention")
    add("")
    add("Split because the two failures mean different things. *Facts* is the share")
    add("of cases keeping every required fact and making no forbidden claim. *Format*")
    add("is the share obeying an explicitly requested shape, such as exactly four")
    add("numbered steps. *Both* is the strict combination, and is the gated metric.")
    add("")
    add("| Arm | Facts | Format | Both |")
    add("| --- | ---: | ---: | ---: |")
    for arm, bucket in sorted(summary["retention"].items()):
        add(
            f"| {arm} | {_rate(bucket['facts_rate'])} | {_rate(bucket['format_rate'])} | "
            f"{_rate(bucket['rate'])} |"
        )
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

    if summary.get("coding"):
        add("## Coding tasks")
        add("")
        add("Three fixtures with failing test suites. The arm edits production files,")
        add("then the patch is replayed against a pristine copy and against hidden")
        add("cases the model never saw. This is the only measurement here that does")
        add("not depend on anyone's opinion.")
        add("")
        add("| Arm | Passed | Failed fixtures |")
        add("| --- | ---: | --- |")
        for arm, bucket in sorted(summary["coding"].items()):
            failed = ", ".join(bucket["failures"]) or "none"
            add(f"| {arm} | {bucket['passed']}/{bucket['total']} | {failed} |")
        add("")

    waves = summary.get("waves") or {}
    if len(waves) > 1:
        add("## Dev and holdout separately")
        add("")
        add("The holdout wave was written after the first run, by authors who saw")
        add("neither its results nor any candidate policy. If a policy were tuned to")
        add("the dev corpus, the two slices would disagree.")
        add("")
        add("| Wave | Cases | Comparison | Median reduction | Facts kept |")
        add("| --- | ---: | --- | ---: | ---: |")
        for wave, bucket in sorted(waves.items()):
            for row in bucket["pairwise"]:
                arm = row["candidate"]
                facts = bucket["retention"].get(arm, {}).get("facts_rate")
                add(
                    f"| {wave} | {bucket['cases']} | {row['candidate']} vs {row['baseline']} "
                    f"| {_pct(row['median_reduction'])} | {_rate(facts)} |"
                )
        add("")

    if summary.get("by_category"):
        add("## By category")
        add("")
        add("Candidate against no policy, per case category.")
        add("")
        add("| Category | n | Median reduction |")
        add("| --- | ---: | ---: |")
        for row in summary["by_category"]:
            add(f"| {row['category']} | {row['n']} | {_pct(row['median_reduction'])} |")
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


def by_category(cases: list[dict], records: list[dict], left: str, right: str) -> list[dict]:
    """Median reduction of ``right`` against ``left`` within each category."""
    category = {case["id"]: case["category"] for case in cases}
    rows = []
    for name in sorted({c for c in category.values()}):
        subset = [r for r in records if category.get(r["case_id"]) == name]
        row = pairwise(subset, left, right)
        if row:
            row["category"] = name
            rows.append(row)
    return rows


def build(
    directory: Path,
    cases_path: Path | list[Path],
    activation_path: Path | list[Path],
) -> dict[str, Any]:
    run = load_run(directory)
    output_cases = load_cases(
        cases_path if isinstance(cases_path, list) else [cases_path]
    )
    activation_cases = load_cases(
        activation_path if isinstance(activation_path, list) else [activation_path]
    )
    wave_of = {case["id"]: case.get("wave", "dev") for case in output_cases}

    arms = sorted(arm_output_tokens(run["output"]))
    # Reference arms are the fixed points; anything else present is a candidate
    # and gets compared against each of them. Hardcoding a candidate name here
    # silently produced "n/a" for a run whose candidate was named differently.
    references = [name for name in ("N", "A", "G") if name in arms]
    candidates = [name for name in arms if name not in ("N", "A", "G", "C")]
    comparisons = [(ref, cand) for cand in candidates for ref in references]
    comparisons += [
        (b, c) for b, c in (("N", "A"), ("N", "C"), ("N", "G")) if b in arms and c in arms
    ]

    pairwise_rows = [row for row in (pairwise(run["output"], b, c) for b, c in comparisons) if row]

    calls = len(run["output"]) + len(run["activation"]) + len(run["judge"])
    cost = sum(float(r.get("cost_usd") or 0) for group in run.values() for r in group)
    model = next((r["model"] for r in run["output"]), "unknown")
    judge_model = next((r["model"] for r in run["judge"]), "unknown")
    cli = next((r["cli"] for group in run.values() for r in group), "unknown")

    waves: dict[str, Any] = {}
    for wave in sorted({w for w in wave_of.values()}):
        subset = [r for r in run["output"] if wave_of.get(r["case_id"]) == wave]
        wave_cases = [c for c in output_cases if c.get("wave", "dev") == wave]
        if not subset:
            continue
        waves[wave] = {
            "cases": len({r["case_id"] for r in subset}),
            "pairwise": [
                row
                for row in (pairwise(subset, b, c) for b, c in comparisons)
                if row
            ],
            "retention": retention(wave_cases, subset),
        }

    return {
        "waves": waves,
        "coding": coding_results(run["coding"]),
        "by_category": (
            by_category(output_cases, run["output"], "N", candidates[0])
            if candidates
            else []
        ),
        "candidate_arm": candidates[0] if candidates else None,
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
    parser.add_argument("--output-cases", type=Path, action="append", dest="output_cases")
    parser.add_argument(
        "--activation-cases", type=Path, action="append", dest="activation_cases"
    )
    parser.add_argument("--write", type=Path, help="write the rendered report here")
    parser.add_argument("--check", type=Path, help="fail unless this file matches a fresh rebuild")
    args = parser.parse_args(argv)

    summary = build(
        args.run_dir,
        args.output_cases or DEFAULT_OUTPUT_CASES,
        args.activation_cases or DEFAULT_ACTIVATION_CASES,
    )
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
