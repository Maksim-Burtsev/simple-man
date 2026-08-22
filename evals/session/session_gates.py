#!/usr/bin/env python3
"""Apply the preregistered gates to a session benchmark summary.

Gates, metrics and thresholds are read from the preregistration committed
before the run. A failing gate is reported, never repaired; the exit code is
always 0 so a failure gets published with the rest of the record.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_report  # noqa: E402


def measure(summary: dict[str, Any]) -> dict[str, Any]:
    """Flatten the summary into the dotted names a gate table can reference."""
    flat: dict[str, Any] = {}
    for arm, d in summary["delivery"].items():
        flat[f"delivery.{arm}.fraction"] = d["delivered"] / d["trials"] if d["trials"] else None
    for comp in summary["comparisons"]:
        key = f"{comp['baseline']}:{comp['candidate']}"
        q = comp["quality"]
        flat[f"quality.{key}.n"] = q["n"]
        flat[f"quality.{key}.better"] = q["better"]
        flat[f"quality.{key}.worse"] = q["worse"]
        flat[f"quality.{key}.sign_p"] = q["sign_p"]
        flat[f"quality.{key}.worse_significant"] = bool(
            q["worse"] > q["better"] and q["sign_p"] is not None and q["sign_p"] < 0.05
        )
        for m in comp["metrics"]:
            flat[f"metric.{key}.{m['metric']}.median_rel_delta"] = m["median_rel_delta"]
            flat[f"metric.{key}.{m['metric']}.wilcoxon_p"] = m["wilcoxon_p"]
    return flat


def _compare(op: str, value: Any, threshold: Any) -> bool:
    if value is None:
        return False
    return {"eq": value == threshold, "gte": value >= threshold, "lte": value <= threshold, "gt": value > threshold}[op]


def evaluate(summary: dict[str, Any], prereg: dict[str, Any]) -> dict[str, Any]:
    flat = measure(summary)
    rows = []
    for gate in prereg["gates"]:
        value = flat.get(gate["metric"])
        rows.append({**gate, "value": value, "passed": _compare(gate["op"], value, gate["threshold"])})
    failed = [r["name"] for r in rows if not r["passed"]]
    return {"gates": rows, "failed": failed, "decision": "KEEP_SHIPPED_POLICY" if not failed else "REVIEW_POLICY"}


def render(result: dict[str, Any]) -> str:
    out = ["# Session benchmark gates", "", "| gate | metric | rule | value | result |", "|---|---|---|--:|---|"]
    for g in result["gates"]:
        value = g["value"]
        shown = "—" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))
        out.append(f"| {g['name']} | `{g['metric']}` | {g['op']} {g['threshold']} | {shown} | {'PASS' if g['passed'] else 'FAIL'} |")
    out += ["", f"Decision: **{result['decision']}**" + (f" — failed: {', '.join(result['failed'])}" if result["failed"] else ""), ""]
    out.append("`KEEP_SHIPPED_POLICY` means the shipped policy survives real sessions; `REVIEW_POLICY` means a gate failed and the policy needs a candidate revision before the next release.")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=Path, required=True)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--write", type=Path)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args(argv)
    prereg = json.loads(args.prereg.read_text(encoding="utf-8"))
    summary = session_report.build(session_report.read_trials(args.trials), prereg)
    rendered = render(evaluate(summary, prereg))
    if args.check:
        current = args.check.read_text() if args.check.exists() else ""
        if current != rendered:
            print(f"{args.check} does not match a rebuild from raw records", file=sys.stderr)
            return 1
        print(f"{args.check} matches the raw records")
        return 0
    if args.write:
        args.write.write_text(rendered)
        print(f"wrote {args.write}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
