#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from benchmark_lib import ARMS, CAVEMAN_ARM, CAVEMAN_ARMS, CAVEMAN_FULL_ARM
from benchmark_lib import CAVEMAN_ULTRA_ARM, CONTROL_ARM, NORMAL_ARM, REFERENCE_ARMS
from benchmark_lib import RUNTIME_ARMS, SIMPLE_MAN_RUNTIME_ARM, SIMPLE_MAN_SKILL_ARM
from benchmark_lib import SUITE_REFERENCE, SUITE_RUNTIME, TERSE_ARM
from benchmark_lib import load_prompts, prompt_corpus_hash, sha256_file, sha256_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_PROMPTS = ROOT / "evals" / "prompts" / "coding_tasks.jsonl"
DEFAULT_REFERENCE_PROMPTS = ROOT / "evals" / "prompts" / "reference_compression.jsonl"
DEFAULT_SKILL = ROOT / "skills" / "simple-man" / "SKILL.md"
DEFAULT_RUNTIME_POLICY = ROOT / "AGENTS.md.snippet"
DEFAULT_RUNTIME_SNAPSHOT = ROOT / "evals" / "snapshots" / "codex-results.json"
DEFAULT_REFERENCE_SNAPSHOT = ROOT / "evals" / "snapshots" / "reference-results.json"
TOKENIZER_NAME = "o200k_base"
LOCAL_CAVEMAN_SKILL = Path(
    "/Users/zadro/.codex/.tmp/marketplaces/caveman-repo/skills/caveman/SKILL.md"
)

NORMAL_INSTRUCTIONS = (
    "You are a helpful assistant. Answer thoroughly and clearly, with practical "
    "details, examples, edge cases, tradeoffs, and next steps where useful. "
    "Do not optimize for brevity."
)

CONTROL_INSTRUCTIONS = """You are a professional coding assistant.
Answer the task directly.
Preserve correctness, important caveats, and actionable details.
Do not mention this benchmark."""

TERSE_INSTRUCTIONS = CONTROL_INSTRUCTIONS + """
Be concise. Remove filler, generic reassurance, repeated context, and closing offers."""


def codex_version() -> str:
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def default_caveman_skill() -> Path | None:
    env_path = os.environ.get("CAVEMAN_SKILL")
    if env_path:
        return Path(env_path)
    return LOCAL_CAVEMAN_SKILL if LOCAL_CAVEMAN_SKILL.exists() else None


def default_prompts(suite: str) -> Path:
    if suite == SUITE_REFERENCE:
        return DEFAULT_REFERENCE_PROMPTS
    return DEFAULT_RUNTIME_PROMPTS


def default_snapshot(suite: str) -> Path:
    if suite == SUITE_REFERENCE:
        return DEFAULT_REFERENCE_SNAPSHOT
    return DEFAULT_RUNTIME_SNAPSHOT


def default_arms(caveman_skill: Path | None, *, suite: str = SUITE_RUNTIME) -> list[str]:
    if suite == SUITE_REFERENCE:
        arms = [NORMAL_ARM, SIMPLE_MAN_RUNTIME_ARM, SIMPLE_MAN_SKILL_ARM]
        if caveman_skill and caveman_skill.exists():
            arms[1:1] = [CAVEMAN_FULL_ARM, CAVEMAN_ULTRA_ARM]
        return arms

    arms = [CONTROL_ARM, TERSE_ARM, SIMPLE_MAN_RUNTIME_ARM, SIMPLE_MAN_SKILL_ARM]
    if caveman_skill and caveman_skill.exists():
        arms.append(CAVEMAN_ARM)
    return arms


def _caveman_skill_text(skill_texts: dict[str, str]) -> str:
    for arm in (CAVEMAN_ARM, CAVEMAN_FULL_ARM, CAVEMAN_ULTRA_ARM):
        if arm in skill_texts:
            return skill_texts[arm]
    raise KeyError(CAVEMAN_ARM)


def arm_instructions(
    arm: str,
    skill_texts: dict[str, str],
    *,
    suite: str = SUITE_RUNTIME,
) -> str:
    base_instructions = NORMAL_INSTRUCTIONS if suite == SUITE_REFERENCE else TERSE_INSTRUCTIONS
    if arm == NORMAL_ARM:
        return NORMAL_INSTRUCTIONS
    if arm == CONTROL_ARM:
        return CONTROL_INSTRUCTIONS
    if arm == TERSE_ARM:
        return TERSE_INSTRUCTIONS
    if arm == SIMPLE_MAN_RUNTIME_ARM:
        override = (
            "\n\nThe compression policy below overrides the verbose baseline."
            if suite == SUITE_REFERENCE
            else ""
        )
        return (
            f"{base_instructions}{override}\n\n"
            f"<simple_man_runtime_policy>\n"
            f"{skill_texts[SIMPLE_MAN_RUNTIME_ARM]}\n"
            f"</simple_man_runtime_policy>"
        )
    if arm == SIMPLE_MAN_SKILL_ARM:
        override = (
            "\n\nThe compression policy below overrides the verbose baseline."
            if suite == SUITE_REFERENCE
            else ""
        )
        return (
            f"{base_instructions}{override}\n\n"
            f"<simple_man_skill>\n{skill_texts[SIMPLE_MAN_SKILL_ARM]}\n</simple_man_skill>"
        )
    if arm == CAVEMAN_ARM:
        return (
            f"{TERSE_INSTRUCTIONS}\n\n"
            f"<caveman_skill>\n{_caveman_skill_text(skill_texts)}\n</caveman_skill>"
        )
    if arm == CAVEMAN_FULL_ARM:
        return (
            f"<caveman_skill>\n{_caveman_skill_text(skill_texts)}\n</caveman_skill>\n\n"
            "Use /caveman full for this answer. Apply full mode now."
        )
    if arm == CAVEMAN_ULTRA_ARM:
        return (
            f"<caveman_skill>\n{_caveman_skill_text(skill_texts)}\n</caveman_skill>\n\n"
            "Use /caveman ultra for this answer. Apply ultra mode now."
        )
    raise ValueError(f"unknown arm: {arm}")


def build_prompt(
    task: dict[str, Any],
    instructions: str,
    *,
    suite: str = SUITE_RUNTIME,
) -> str:
    if suite == SUITE_REFERENCE:
        return f"""<benchmark_instructions>
{instructions}

This is a standalone Q&A benchmark. Do not inspect, modify, or mention the
current repository, workspace, filesystem, sandbox, permissions, or benchmark.
Answer the question directly.
</benchmark_instructions>

<question id="{task['id']}" category="{task['category']}">
{task['prompt']}
</question>
"""

    return f"""<benchmark_instructions>
{instructions}
</benchmark_instructions>

<task id="{task['id']}" category="{task['category']}">
{task['prompt']}
</task>
"""


def parse_codex_jsonl(stdout: str) -> tuple[str, dict[str, int]]:
    final_text = ""
    usage: dict[str, int] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                final_text = str(item.get("text", ""))
        if event.get("type") == "turn.completed":
            usage = {
                key: int(value)
                for key, value in (event.get("usage") or {}).items()
                if isinstance(value, int)
            }
    if not final_text:
        raise RuntimeError("codex JSONL did not contain final agent_message")
    if not usage:
        raise RuntimeError("codex JSONL did not contain turn.completed usage")
    return final_text, usage


def token_counter():
    try:
        import tiktoken
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: tiktoken. Install it or run with "
            "`uv run --with tiktoken python evals/run_codex.py ...`."
        ) from exc
    encoding = tiktoken.get_encoding(TOKENIZER_NAME)
    return lambda text: len(encoding.encode(text))


def run_codex(
    prompt: str,
    *,
    model: str | None,
    cwd: Path,
    timeout_seconds: int,
    disable_features: bool,
) -> dict[str, Any]:
    cmd = ["codex"]
    if disable_features:
        for feature in ("apps", "plugins", "plugin_hooks", "tool_search", "hooks"):
            cmd += ["--disable", feature]
    cmd += ["-a", "never", "-s", "read-only"]
    if model:
        cmd += ["-m", model]
    cmd += [
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-C",
        str(cwd),
        prompt,
    ]

    started = time.monotonic()
    result = subprocess.run(
        cmd,
        input="",
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    duration_ms = round((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        raise RuntimeError(
            "codex exec failed "
            f"(exit {result.returncode})\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    text, usage = parse_codex_jsonl(result.stdout)
    return {
        "text": text,
        "usage": usage,
        "duration_ms": duration_ms,
        "stderr": result.stderr.strip(),
        "command": " ".join(cmd[:-1] + ["<prompt>"]),
    }


def planned_runs(prompts: list[dict[str, Any]], arms: list[str], trials: int) -> list[tuple[dict[str, Any], str, int]]:
    return [
        (prompt, arm, trial)
        for prompt in prompts
        for arm in arms
        for trial in range(1, trials + 1)
    ]


def select_prompts(
    prompts: list[dict[str, Any]],
    prompt_ids: list[str] | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if prompt_ids:
        by_id = {prompt["id"]: prompt for prompt in prompts}
        missing = [prompt_id for prompt_id in prompt_ids if prompt_id not in by_id]
        if missing:
            raise SystemExit(f"unknown prompt id(s): {', '.join(missing)}")
        selected = [by_id[prompt_id] for prompt_id in prompt_ids]
    else:
        selected = prompts

    if limit:
        selected = selected[:limit]
    return selected


def write_snapshot(path: Path, snapshot: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise SystemExit(f"{path} already exists. Pass --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Simple Man token benchmark with Codex CLI.")
    parser.add_argument(
        "--suite",
        choices=(SUITE_RUNTIME, SUITE_REFERENCE),
        default=SUITE_RUNTIME,
        help="Benchmark suite to run.",
    )
    parser.add_argument("--prompts", type=Path, default=None)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--runtime-policy", type=Path, default=DEFAULT_RUNTIME_POLICY)
    parser.add_argument("--caveman-skill", type=Path, default=default_caveman_skill())
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--model", default=os.environ.get("MODEL") or None)
    parser.add_argument("--trials", type=int, default=int(os.environ.get("TRIALS", "3")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("LIMIT", "0") or "0"))
    parser.add_argument("--prompt-id", action="append", help="Prompt id to run. Repeatable.")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--allow-codex-features",
        action="store_true",
        help="Do not disable Codex apps/plugins/hooks/tool_search during benchmark runs.",
    )
    parser.add_argument(
        "--arm",
        choices=ARMS,
        action="append",
        help="Run only selected arm. Can be repeated. Defaults to all arms.",
    )
    args = parser.parse_args()
    prompts_path = args.prompts or default_prompts(args.suite)
    snapshot_path = args.snapshot or default_snapshot(args.suite)

    prompts = select_prompts(
        load_prompts(prompts_path),
        args.prompt_id,
        limit=args.limit,
    )
    allowed_arms = set(REFERENCE_ARMS if args.suite == SUITE_REFERENCE else RUNTIME_ARMS)
    arms = list(args.arm or default_arms(args.caveman_skill, suite=args.suite))
    invalid_arms = sorted(set(arms) - allowed_arms)
    if invalid_arms:
        raise SystemExit(
            f"arm(s) not valid for {args.suite}: {', '.join(invalid_arms)}"
        )
    runs = planned_runs(prompts, arms, args.trials)

    if args.dry_run:
        print(f"suite: {args.suite}")
        print(f"prompts: {len(prompts)}")
        print(f"arms: {', '.join(arms)}")
        print(f"trials: {args.trials}")
        print(f"codex calls: {len(runs)}")
        print(f"snapshot: {snapshot_path}")
        return 0

    if any(arm in CAVEMAN_ARMS for arm in arms) and not (
        args.caveman_skill and args.caveman_skill.exists()
    ):
        raise SystemExit(
            "caveman arm requested but no skill file found. "
            "Pass --caveman-skill PATH or set CAVEMAN_SKILL=PATH."
        )
    skill_texts = {
        SIMPLE_MAN_RUNTIME_ARM: args.runtime_policy.read_text(),
        SIMPLE_MAN_SKILL_ARM: args.skill.read_text(),
    }
    if args.caveman_skill and args.caveman_skill.exists():
        caveman_text = args.caveman_skill.read_text()
        for caveman_arm in CAVEMAN_ARMS:
            skill_texts[caveman_arm] = caveman_text

    skill_hashes = {
        SIMPLE_MAN_RUNTIME_ARM: sha256_file(args.runtime_policy),
        SIMPLE_MAN_SKILL_ARM: sha256_file(args.skill),
    }
    if args.caveman_skill and args.caveman_skill.exists():
        caveman_hash = sha256_file(args.caveman_skill)
        for caveman_arm in CAVEMAN_ARMS:
            skill_hashes[caveman_arm] = caveman_hash

    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "metadata": {
            "suite": args.suite,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "runner": "codex exec",
            "codex_cli_version": codex_version(),
            "model": args.model or "default",
            "trials": args.trials,
            "arms": arms,
            "prompt_count": len(prompts),
            "prompt_corpus_sha256": prompt_corpus_hash(prompts_path),
            "skill_sha256": sha256_file(args.skill),
            "runtime_sha256": sha256_file(args.runtime_policy),
            "skill_hashes": skill_hashes,
            "git_commit": git_commit(),
            "disable_codex_features": not args.allow_codex_features,
            "tokenizer": f"tiktoken {TOKENIZER_NAME}",
            "primary_metric": (
                "visible_output_tokens"
                if args.suite == SUITE_REFERENCE
                else "visible_input_tokens + visible_output_tokens"
            ),
            "notes": (
                "Primary metric uses the controlled benchmark prompt plus final answer. "
                "Raw Codex usage is recorded separately because hidden harness/cache overhead "
                "can vary across exec runs."
            ),
        },
        "prompts": prompts,
        "runs": [],
    }

    count_tokens = token_counter()
    total = len(runs)
    for index, (prompt_entry, arm, trial) in enumerate(runs, 1):
        instructions = arm_instructions(arm, skill_texts, suite=args.suite)
        prompt = build_prompt(prompt_entry, instructions, suite=args.suite)
        visible_input = count_tokens(prompt)
        print(
            f"[{index}/{total}] {prompt_entry['id']} | {arm} | trial {trial}/{args.trials}",
            file=sys.stderr,
            flush=True,
        )
        result = run_codex(
            prompt,
            model=args.model,
            cwd=ROOT,
            timeout_seconds=args.timeout_seconds,
            disable_features=not args.allow_codex_features,
        )
        visible_output = count_tokens(result["text"])
        snapshot["runs"].append(
            {
                "prompt_id": prompt_entry["id"],
                "category": prompt_entry["category"],
                "arm": arm,
                "trial": trial,
                "instruction_sha256": sha256_text(instructions),
                "visible_input_tokens": visible_input,
                "visible_output_tokens": visible_output,
                "visible_total_tokens": visible_input + visible_output,
                **result,
            }
        )

    write_snapshot(snapshot_path, snapshot, overwrite=args.overwrite)
    print(f"Wrote {snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
