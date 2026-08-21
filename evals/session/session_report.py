#!/usr/bin/env python3
"""Rebuild the session benchmark report from ``trials.jsonl``.

Pairs every task across two arms and reports, per metric, the median paired
relative delta, a Wilcoxon signed-rank p-value on the raw paired differences,
the delta of arm totals, and a bootstrap CI on the median. Quality is the task
verifier's reward, compared with a two-sided sign test. Everything is computed
from the committed records each time; ``--check`` fails on any difference.

Statistics are stdlib only. The Wilcoxon p-value uses the normal approximation
with tie correction and continuity correction, adequate at the pair counts
this benchmark runs (dozens) and stated as such in the report.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

BOOTSTRAP_SEED = 20260817
BOOTSTRAP_ITERATIONS = 10_000

METRICS = (
    ("cost_usd", "cost"),
    ("total_tokens", "total tokens"),
    ("output_tokens", "output tokens"),
    ("cache_read_tokens", "cache reads"),
    ("input_tokens", "fresh input"),
    ("turns", "turns"),
    ("wall_ms", "wall-clock"),
)


def read_trials(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def usable(row: dict[str, Any]) -> bool:
    return row.get("error") is None and row.get("reward") is not None and row.get("cost_usd") is not None


def select(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """One trial per (task, arm): the latest usable one, else the latest of any.

    A one-sided infrastructure failure is retried under a ``-retry-`` job; the
    retry, if usable, replaces the failed trial. Nothing is ever chosen by outcome.
    """
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_key[(row["task"], row["arm"])].append(row)
    chosen = {}
    for key, candidates in by_key.items():
        candidates.sort(key=lambda r: r.get("started_at") or "")
        good = [r for r in candidates if usable(r)]
        chosen[key] = good[-1] if good else candidates[-1]
    return chosen


def pairs_for(chosen: dict[tuple[str, str], dict[str, Any]], baseline: str, candidate: str) -> dict[str, Any]:
    tasks = sorted({task for task, _ in chosen})
    pairs, one_sided, dropped = [], [], []
    for task in tasks:
        base = chosen.get((task, baseline))
        cand = chosen.get((task, candidate))
        if base is None or cand is None:
            continue  # not yet run on both arms
        b_ok, c_ok = usable(base), usable(cand)
        if b_ok and c_ok:
            pairs.append({"task": task, baseline: base, candidate: cand})
        elif b_ok or c_ok:
            one_sided.append({"task": task, "failed_arm": candidate if b_ok else baseline,
                              "error": (cand if b_ok else base).get("error")})
        else:
            dropped.append({"task": task, "errors": [base.get("error"), cand.get("error")]})
    return {"pairs": pairs, "one_sided": one_sided, "dropped": dropped}


def total_tokens(row: dict[str, Any]) -> float:
    return sum(row.get(k) or 0 for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))


def metric_value(row: dict[str, Any], metric: str) -> float | None:
    if metric == "total_tokens":
        return total_tokens(row)
    value = row.get(metric)
    return float(value) if value is not None else None


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def wilcoxon_p(diffs: list[float]) -> float | None:
    """Two-sided signed-rank test, normal approximation with tie and continuity correction."""
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    if n < 6:
        return None
    ranked = sorted((abs(d), i) for i, d in enumerate(nonzero))
    ranks = [0.0] * n
    i = 0
    tie_term = 0.0
    while i < n:
        j = i
        while j + 1 < n and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        avg = (i + j + 2) / 2
        for k in range(i, j + 1):
            ranks[ranked[k][1]] = avg
        t = j - i + 1
        if t > 1:
            tie_term += t**3 - t
        i = j + 1
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - tie_term / 48
    if var <= 0:
        return None
    z = (abs(w_plus - mean) - 0.5) / math.sqrt(var)
    return min(1.0, 2 * normal_sf(max(z, 0.0)))


def sign_test_p(better: int, worse: int) -> float | None:
    n = better + worse
    if n == 0:
        return None
    k = min(better, worse)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_median_ci(values: list[float], seed: int = BOOTSTRAP_SEED, iterations: int = BOOTSTRAP_ITERATIONS) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    medians = sorted(statistics.median(rng.choice(values) for _ in range(n)) for _ in range(iterations))
    return medians[int(0.025 * iterations)], medians[int(0.975 * iterations) - 1]


def compare_metric(pairs: list[dict[str, Any]], baseline: str, candidate: str, metric: str) -> dict[str, Any] | None:
    rel, diffs, base_sum, cand_sum = [], [], 0.0, 0.0
    for pair in pairs:
        b = metric_value(pair[baseline], metric)
        c = metric_value(pair[candidate], metric)
        if b is None or c is None:
            continue
        base_sum += b
        cand_sum += c
        diffs.append(c - b)
        if b:
            rel.append((c - b) / b)
    if not rel:
        return None
    low, high = bootstrap_median_ci(rel)
    return {
        "metric": metric,
        "n": len(rel),
        "median_rel_delta": statistics.median(rel),
        "ci_low": low,
        "ci_high": high,
        "wilcoxon_p": wilcoxon_p(diffs),
        "totals_rel_delta": (cand_sum - base_sum) / base_sum if base_sum else None,
        "baseline_median": statistics.median(metric_value(p[baseline], metric) or 0 for p in pairs),
        "candidate_median": statistics.median(metric_value(p[candidate], metric) or 0 for p in pairs),
    }


def compare_quality(pairs: list[dict[str, Any]], baseline: str, candidate: str) -> dict[str, Any]:
    better = sum(1 for p in pairs if p[candidate]["reward"] > p[baseline]["reward"])
    worse = sum(1 for p in pairs if p[candidate]["reward"] < p[baseline]["reward"])
    tie = len(pairs) - better - worse
    return {
        "n": len(pairs),
        "better": better,
        "worse": worse,
        "tie": tie,
        "sign_p": sign_test_p(better, worse),
        "baseline_mean": statistics.fmean(p[baseline]["reward"] for p in pairs) if pairs else None,
        "candidate_mean": statistics.fmean(p[candidate]["reward"] for p in pairs) if pairs else None,
        "baseline_pass": sum(1 for p in pairs if p[baseline]["reward"] >= 1) ,
        "candidate_pass": sum(1 for p in pairs if p[candidate]["reward"] >= 1),
    }


def delivery(rows: list[dict[str, Any]], arms: list[str]) -> dict[str, Any]:
    out = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        out[arm] = {"trials": len(mine), "delivered": sum(1 for r in mine if r.get("delivered"))}
    return out


def by_day(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    days: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        day = (row.get("started_at") or "unknown")[:10]
        days[day][row["arm"]] += 1
    return {day: dict(counts) for day, counts in sorted(days.items())}


def build(trials: list[dict[str, Any]], prereg: dict[str, Any]) -> dict[str, Any]:
    chosen = select(trials)
    arms = list(prereg["arms"])
    comparisons = []
    for baseline, candidate in prereg["comparisons"]:
        split = pairs_for(chosen, baseline, candidate)
        pairs = split["pairs"]
        comparisons.append({
            "baseline": baseline,
            "candidate": candidate,
            "n_pairs": len(pairs),
            "one_sided": split["one_sided"],
            "dropped": split["dropped"],
            "metrics": [m for m in (compare_metric(pairs, baseline, candidate, key) for key, _ in METRICS) if m],
            "quality": compare_quality(pairs, baseline, candidate),
            "tasks": [p["task"] for p in pairs],
        })
    return {
        "schema": "session-report-v1",
        "model": sorted({r.get("model") for r in trials if r.get("model")}),
        "cli_versions": sorted({r.get("cli_version") for r in trials if r.get("cli_version")}),
        "registered": {k: prereg[k] for k in ("model", "reasoning_effort", "agent", "seed") if k in prereg},
        "skillsbench_commit": prereg["skillsbench"]["commit"],
        "trials": len(trials),
        "per_arm": {arm: sum(1 for r in trials if r["arm"] == arm) for arm in arms},
        "delivery": delivery(trials, arms),
        "by_day": by_day(trials),
        "comparisons": comparisons,
        "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in trials), 2),
    }


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def pval(p: float | None) -> str:
    return "—" if p is None else ("<0.001" if p < 0.001 else f"{p:.3f}")


def render(summary: dict[str, Any]) -> str:
    out = ["# Session benchmark report", ""]
    out.append(f"Generated from `trials.jsonl` by `evals/session/session_report.py` (schema `{summary['schema']}`).")
    out.append("")
    reg = summary["registered"]
    out.append(f"- SkillsBench commit: `{summary['skillsbench_commit']}`")
    out.append(f"- Registered: model `{reg.get('model')}`, effort `{reg.get('reasoning_effort')}`, agent `{reg.get('agent', {}).get('name')}@{reg.get('agent', {}).get('version')}`")
    out.append(f"- Observed: model {', '.join(f'`{m}`' for m in summary['model']) or '—'}; CLI {', '.join(f'`{v}`' for v in summary['cli_versions']) or '—'}")
    out.append(f"- Trials: {summary['trials']} ({', '.join(f'{a} {n}' for a, n in summary['per_arm'].items())}); metered cost ${summary['total_cost_usd']:.2f} (Claude Code's own estimate; billed to the subscription)")
    out.append("")
    out.append("## Delivery (mechanical)")
    out.append("")
    out.append("| arm | trials | payload reached the agent command |")
    out.append("|---|--:|--:|")
    for arm, d in summary["delivery"].items():
        out.append(f"| {arm} | {d['trials']} | {d['delivered']}/{d['trials']} |")
    out.append("")
    out.append("## Trials by day")
    out.append("")
    for day, counts in summary["by_day"].items():
        out.append(f"- {day}: " + ", ".join(f"{a} {n}" for a, n in sorted(counts.items())))
    out.append("")
    for comp in summary["comparisons"]:
        b, c = comp["baseline"], comp["candidate"]
        out.append(f"## {c} vs {b} — {comp['n_pairs']} clean pairs")
        out.append("")
        out.append("| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median {b} → {c} |".replace("{b}", b).replace("{c}", c))
        out.append("|---|--:|--:|--:|--:|--:|")
        labels = dict(METRICS)
        for m in comp["metrics"]:
            out.append(
                f"| {labels[m['metric']]} | {pct(m['median_rel_delta'])} | [{pct(m['ci_low'])}, {pct(m['ci_high'])}] "
                f"| {pval(m['wilcoxon_p'])} | {pct(m['totals_rel_delta'])} | {m['baseline_median']:.4g} → {m['candidate_median']:.4g} |"
            )
        q = comp["quality"]
        out.append(
            f"| quality (reward) | {q['better']}↑ / {q['worse']}↓ / {q['tie']} tie | — | sign {pval(q['sign_p'])} | — "
            f"| mean {q['baseline_mean']:.3f} → {q['candidate_mean']:.3f}; pass {q['baseline_pass']} → {q['candidate_pass']} |"
            if q["n"] else "| quality (reward) | — | — | — | — | — |"
        )
        out.append("")
        if comp["one_sided"]:
            out.append("One-sided failures (pending retry, not counted): " + ", ".join(f"`{x['task']}` ({x['failed_arm']}: {x['error']})" for x in comp["one_sided"]))
            out.append("")
        if comp["dropped"]:
            out.append("Dropped symmetrically (failed on both arms): " + ", ".join(f"`{x['task']}`" for x in comp["dropped"]))
            out.append("")
    out.append("Wilcoxon p-values use the normal approximation with tie and continuity correction; the sign test is exact and two-sided. Negative deltas mean the candidate used less.")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=Path, required=True)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--write", type=Path, help="write the rendered report here (and .json beside it)")
    parser.add_argument("--check", type=Path, help="fail unless this file matches a fresh rebuild")
    parser.add_argument("--json", action="store_true", help="print the summary JSON instead of markdown")
    args = parser.parse_args(argv)

    prereg = json.loads(args.prereg.read_text(encoding="utf-8"))
    summary = build(read_trials(args.trials), prereg)
    rendered = render(summary)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
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
        args.write.with_suffix(".json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.write}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
