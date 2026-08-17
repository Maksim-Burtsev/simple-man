#!/usr/bin/env python3
"""Lean Simple Man benchmark runner for Claude Code.

Each arm is a complete system prompt: a shared neutral prelude plus one policy
file. The default Claude Code system prompt is replaced rather than appended to,
so an arm measures its policy and nothing else.

Billing is fail-closed on the subscription. Any Anthropic credential in the
environment would take precedence over the claude.ai login and silently move the
run onto API credits, so those variables are stripped from every subprocess and
the preflight refuses to start unless the CLI reports a claude.ai login.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

import eval_v2_lib as lib  # noqa: E402
from run_codex import CONTROL_INSTRUCTIONS  # noqa: E402

POLICIES = ROOT / "evals" / "policies"

#: Arm name -> policy file appended to the shared prelude. ``None`` is the bare
#: control: the prelude alone, with no communication policy at all.
ARMS: dict[str, Path | None] = {
    "N": None,
    "A": POLICIES / "v0.2" / "simple_man_runtime.md",
    "B": POLICIES / "v0.3" / "B-runtime.md",
    "G": POLICIES / "v0.3" / "generic-terse.md",
    "C": POLICIES / "external" / "caveman-SKILL.md",
}

DESCRIPTIONS: dict[str, Path] = {
    "D0": POLICIES / "v0.2" / "description.txt",
    "D1": POLICIES / "v0.3" / "D1-description.txt",
}

#: Credentials that override the claude.ai login. Stripped from every subprocess.
BANNED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

ACTIVATION_PROMPT = """A skill is available. Its description is:

<skill_description>
{description}
</skill_description>

A user sends this request:

<user_request>
{request}
</user_request>

Should the skill be applied to this request? Answer with exactly one word: YES or NO."""

ROUTER_PRELUDE = (
    "You route requests to skills. Decide only whether the described skill "
    "applies. Answer with exactly one word: YES or NO."
)


class BillingGuard(Exception):
    """Raised when a run could be billed against anything but the subscription."""


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in BANNED_ENV}
    return env


def assert_no_credentials() -> None:
    present = [name for name in BANNED_ENV if os.environ.get(name)]
    if present:
        # Stripping them from the subprocess is not enough to be safe here: their
        # presence means the operator intended a different credential path, and
        # guessing which one is worse than refusing.
        raise BillingGuard(
            "refusing to run: "
            + ", ".join(present)
            + " set; unset them so the run bills against the claude.ai subscription"
        )


def preflight(claude: str = "claude") -> dict[str, Any]:
    assert_no_credentials()
    if shutil.which(claude) is None:
        raise BillingGuard(f"{claude} not found on PATH")
    proc = subprocess.run(
        [claude, "auth", "status"],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=120,
    )
    if proc.returncode != 0:
        raise BillingGuard(f"claude auth status failed: {proc.stderr.strip()[:200]}")
    status = json.loads(proc.stdout)
    if not status.get("loggedIn") or status.get("authMethod") != "claude.ai":
        raise BillingGuard(f"expected a claude.ai login, got {status.get('authMethod')!r}")
    version = subprocess.run(
        [claude, "--version"], capture_output=True, text=True, env=clean_env(), timeout=120
    ).stdout.strip()
    return {
        "auth_method": status["authMethod"],
        "subscription": status.get("subscriptionType"),
        "cli": version,
    }


#: Tools are disabled for answer cases, so every arm is told so identically.
#: Without this the control arm sometimes reads "send a status line" as a request
#: to operate a messaging tool and declines, which would flatter any arm whose
#: policy happens to push it toward writing text instead.
NO_TOOLS_NOTE = (
    "You have no tools in this session. Write the requested text directly as your answer."
)

BENCH_PRELUDE = f"{CONTROL_INSTRUCTIONS}\n{NO_TOOLS_NOTE}"


def system_prompt(arm: str) -> str:
    policy = ARMS[arm]
    if policy is None:
        return BENCH_PRELUDE
    return f"{BENCH_PRELUDE}\n\n{policy.read_text().strip()}"


def policy_tokens(arm: str) -> int:
    """Words in the arm's policy, excluding the shared prelude."""
    policy = ARMS[arm]
    return 0 if policy is None else len(policy.read_text().split())


def call_claude(
    prompt: str,
    *,
    system: str,
    model: str,
    claude: str = "claude",
    tools: str = "",
    cwd: Path | None = None,
    extra_args: Iterable[str] = (),
    timeout: int = 600,
) -> dict[str, Any]:
    """One headless turn. Returns the parsed CLI result envelope plus latency."""
    with _temp_system_prompt(system) as system_file:
        cmd = [
            claude,
            "-p",
            prompt,
            "--model",
            model,
            "--system-prompt-file",
            str(system_file),
            "--tools",
            tools,
            "--safe-mode",
            "--strict-mcp-config",
            "--output-format",
            "json",
            "--no-session-persistence",
            *extra_args,
        ]
        started = time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=clean_env(),
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    envelope = json.loads(proc.stdout)
    envelope["latency_ms"] = latency_ms
    return envelope


class _temp_system_prompt:
    def __init__(self, text: str) -> None:
        self.text = text

    def __enter__(self) -> Path:
        import tempfile

        self._dir = tempfile.TemporaryDirectory(prefix="simple-man-bench-")
        path = Path(self._dir.name) / "system.md"
        path.write_text(self.text)
        return path

    def __exit__(self, *exc: object) -> None:
        self._dir.cleanup()


def usage_metrics(envelope: dict[str, Any]) -> dict[str, Any]:
    usage = envelope.get("usage") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    raw_input = int(usage.get("input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    return {
        # pair_measurements requires this exact field set
        "commentary_visible_tokens": 0,
        "final_visible_tokens": output,
        "input_tokens": raw_input + cache_read + cache_write,
        "cached_input_tokens": cache_read,
        "output_tokens": output,
        "latency_ms": envelope["latency_ms"],
    }


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load cases without the fixed-count assertions of the v2 loaders."""
    cases = lib.strict_jsonl(path)
    seen: set[str] = set()
    for case in cases:
        lib.validate_holdout_case(case)
        if case["id"] in seen:
            raise ValueError(f"duplicate case id {case['id']}")
        seen.add(case["id"])
    return cases


def _key(record: dict[str, Any]) -> tuple:
    return (record["phase"], record["case_id"], record.get("arm") or record.get("description"))


def load_done(path: Path) -> set[tuple]:
    if not path.exists():
        return set()
    return {_key(row) for row in lib.strict_jsonl(path)}


def append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(lib.canonical_json(record) + "\n")


def plan_output(cases: list[dict], arms: list[str]) -> list[tuple[dict, str]]:
    return [(case, arm) for case in cases for arm in arms]


def plan_activation(cases: list[dict], descriptions: list[str]) -> list[tuple[dict, str]]:
    return [(case, name) for case in cases for name in descriptions]


def run_output_phase(
    cases: list[dict],
    arms: list[str],
    *,
    out: Path,
    model: str,
    identity: dict[str, Any],
    dry_run: bool,
) -> int:
    done = load_done(out)
    calls = 0
    for case, arm in plan_output(cases, arms):
        if ("output", case["id"], arm) in done:
            continue
        calls += 1
        if dry_run:
            continue
        envelope = call_claude(case["prompt"], system=system_prompt(arm), model=model)
        final = envelope.get("result") or ""
        record = {
            "phase": "output",
            "case_id": case["id"],
            "cluster_id": case["cluster_id"],
            "arm": arm,
            "trial": 1,
            "model": model,
            "effort": "default",
            "cli": identity["cli"],
            "response": {"commentary": "", "final": final},
            "cost_usd": envelope.get("total_cost_usd"),
            **usage_metrics(envelope),
        }
        append(out, record)
    return calls


def run_activation_phase(
    cases: list[dict],
    descriptions: list[str],
    *,
    out: Path,
    model: str,
    identity: dict[str, Any],
    dry_run: bool,
) -> int:
    done = load_done(out)
    calls = 0
    for case, name in plan_activation(cases, descriptions):
        if ("activation", case["id"], name) in done:
            continue
        calls += 1
        if dry_run:
            continue
        prompt = ACTIVATION_PROMPT.format(
            description=DESCRIPTIONS[name].read_text().strip(),
            request=case["prompt"],
        )
        envelope = call_claude(prompt, system=ROUTER_PRELUDE, model=model)
        answer = (envelope.get("result") or "").strip().upper()
        predicted = answer.startswith("YES")
        record = {
            "phase": "activation",
            "case_id": case["id"],
            "description": name,
            "model": model,
            "cli": identity["cli"],
            "raw_answer": answer[:40],
            "predicted_activate": predicted,
            "expected_activate": case["expected"] == "activate",
            "cost_usd": envelope.get("total_cost_usd"),
            "latency_ms": envelope["latency_ms"],
        }
        append(out, record)
    return calls


JUDGE_SYSTEM = """You compare two answers to the same task and return one JSON object.

Judge which answer a working engineer would rather receive. Do not reward length
and do not reward brevity for its own sake. A shorter answer is better only when
it keeps every fact the reader needs to act; a longer answer is better only when
the extra words carry decision-relevant content.

Return exactly this JSON and nothing else, no code fence:
{"quality":"left|right|tie|both_bad","naturalness":"left|right|tie","flags":{"left":[],"right":[]},"rationale":"<600 chars"}

Valid flags: factual_error, missing_material_fact, false_validation_claim,
safety_or_approval_loss, detail_override_loss, constraint_violation,
unnatural_compression, unnecessary_content."""

#: Blinding is structural: the payload carries the task and two answer texts and
#: never carries an arm label, so there is nothing about arm identity to leak.
JUDGE_PROMPT = "{payload}"


def _parse_judgment(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    return lib.validate_judgment(cleaned.strip())


def run_judge_phase(
    cases: list[dict],
    records: list[dict],
    comparison: tuple[str, str],
    *,
    out: Path,
    model: str,
    identity: dict[str, Any],
    seed: str,
    dry_run: bool,
) -> int:
    """Blind pairwise judging of one arm pair, each case judged in both positions."""
    left_arm, right_arm = comparison
    by_case: dict[str, dict[str, dict]] = {}
    for record in records:
        by_case.setdefault(record["case_id"], {})[record["arm"]] = record

    label = f"{left_arm}-vs-{right_arm}"
    sides = lib.balanced_sides(cases, seed, stratum_id=label)
    done = load_done(out)
    calls = 0
    for case in cases:
        answers = by_case.get(case["id"], {})
        if left_arm not in answers or right_arm not in answers:
            continue
        for position in ("primary", "swapped"):
            key = ("judge", case["id"], f"{label}:{position}")
            if key in done:
                continue
            calls += 1
            if dry_run:
                continue
            first_is_left = sides[case["id"]] if position == "primary" else not sides[case["id"]]
            left_record = answers[left_arm] if first_is_left else answers[right_arm]
            right_record = answers[right_arm] if first_is_left else answers[left_arm]
            payload = lib.build_judge_payload(
                case,
                left_record["response"]["final"],
                right_record["response"]["final"],
            )
            envelope = call_claude(
                JUDGE_PROMPT.format(payload=lib.canonical_json(payload)),
                system=JUDGE_SYSTEM,
                model=model,
            )
            raw = envelope.get("result") or ""
            try:
                judgment = _parse_judgment(raw)
                error = None
            except ValueError as exc:
                judgment, error = None, str(exc)[:200]
            append(
                out,
                {
                    "phase": "judge",
                    "case_id": case["id"],
                    "arm": f"{label}:{position}",
                    "comparison": label,
                    "position": position,
                    # which arm actually sat on each side, recorded only after
                    # the judgment exists so the report can un-blind it
                    "left_arm": left_arm if first_is_left else right_arm,
                    "right_arm": right_arm if first_is_left else left_arm,
                    "judgment": judgment,
                    "parse_error": error,
                    "raw": None if judgment else raw[:400],
                    "model": model,
                    "cli": identity["cli"],
                    "cost_usd": envelope.get("total_cost_usd"),
                    "latency_ms": envelope["latency_ms"],
                },
            )
    return calls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("output", "activation", "judge", "all"))
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--judge-model", default="claude-haiku-4-5")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--arm", action="append", dest="arms")
    parser.add_argument("--description", action="append", dest="descriptions")
    parser.add_argument(
        "--compare",
        action="append",
        dest="comparisons",
        metavar="X:Y",
        help="arm pair to judge blind, e.g. B:A (repeatable)",
    )
    parser.add_argument("--seed", default="simple-man-v0.3")
    parser.add_argument("--output-cases", type=Path, default=ROOT / "evals/cases/bench-output.jsonl")
    parser.add_argument(
        "--activation-cases", type=Path, default=ROOT / "evals/cases/bench-activation.jsonl"
    )
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--limit", type=int, default=0, help="use only the first N cases")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    def cases_from(path: Path) -> list[dict[str, Any]]:
        cases = load_cases(path)
        return cases[: args.limit] if args.limit else cases

    arms = args.arms or list(ARMS)
    descriptions = args.descriptions or list(DESCRIPTIONS)
    for arm in arms:
        if arm not in ARMS:
            parser.error(f"unknown arm {arm!r}; known: {', '.join(ARMS)}")

    comparisons: list[tuple[str, str]] = []
    for raw in args.comparisons or ["B:A", "B:G"]:
        left, _, right = raw.partition(":")
        if left not in ARMS or right not in ARMS or left == right:
            parser.error(f"invalid --compare {raw!r}")
        comparisons.append((left, right))

    identity = {"cli": "dry-run"} if args.dry_run else preflight(args.claude)
    out_path = args.output_dir / "output.jsonl"

    def phases(dry: bool) -> int:
        total = 0
        if args.phase in ("output", "all"):
            total += run_output_phase(
                cases_from(args.output_cases),
                arms,
                out=out_path,
                model=args.model,
                identity=identity,
                dry_run=dry,
            )
        if args.phase in ("activation", "all"):
            total += run_activation_phase(
                cases_from(args.activation_cases),
                descriptions,
                out=args.output_dir / "activation.jsonl",
                model=args.model,
                identity=identity,
                dry_run=dry,
            )
        if args.phase in ("judge", "all"):
            answers = lib.strict_jsonl(out_path) if out_path.exists() else []
            for comparison in comparisons:
                total += run_judge_phase(
                    cases_from(args.output_cases),
                    answers,
                    comparison,
                    out=args.output_dir / "judge.jsonl",
                    model=args.judge_model,
                    identity=identity,
                    seed=args.seed,
                    dry_run=dry,
                )
        return total

    planned = phases(dry=True)
    if planned > args.max_calls:
        parser.error(f"plan needs {planned} calls, over --max-calls {args.max_calls}")

    print(f"planned calls: {planned}")
    print(f"model: {args.model}   judge: {args.judge_model}")
    print(f"arms: {', '.join(arms)}")
    if args.phase in ("judge", "all"):
        print(f"comparisons: {', '.join(f'{a}-vs-{b}' for a, b in comparisons)}")
    if args.dry_run:
        return 0

    print(f"cli: {identity['cli']}  auth: {identity['auth_method']}/{identity['subscription']}")
    phases(dry=False)
    print(f"done: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
