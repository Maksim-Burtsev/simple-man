#!/usr/bin/env python3
"""Drive paired SkillsBench sessions through Harbor, one arm at a time.

Every trial is a real Claude Code session in a Docker sandbox. The arms,
tasks, model, effort and payload hashes come from the preregistration, never
from flags, so a run cannot drift from what was registered.

Billing: the claude.ai subscription only. macOS keeps the login in the
Keychain, which a container cannot read, so the one subscription path into
Docker is the long-lived token from ``claude setup-token`` exported as
``CLAUDE_CODE_OAUTH_TOKEN``. API credentials are refused outright.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

#: Any of these routes the run onto API billing. Refused, not stripped.
BANNED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
#: The one credential a container can bill the subscription with.
OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


class BillingGuard(Exception):
    """Raised when a run could be billed against anything but the subscription."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_prereg(path: Path) -> dict[str, Any]:
    prereg = json.loads(path.read_text(encoding="utf-8"))
    for arm, rel in prereg["arms"].items():
        if rel is None:
            continue
        digest = sha256(ROOT / rel)
        registered = prereg["payload_sha256"][arm]
        if digest != registered:
            raise ValueError(f"payload for arm {arm} hashes to {digest}, registered {registered}")
    return prereg


def assert_subscription_billing(env: dict[str, str]) -> None:
    present = [name for name in BANNED_ENV if env.get(name)]
    if present:
        raise BillingGuard(
            "refusing to run: " + ", ".join(present) + " set; Harbor would bill the API"
        )
    if not env.get(OAUTH_ENV, "").strip():
        raise BillingGuard(
            f"refusing to run: {OAUTH_ENV} unset; run `claude setup-token` and export it"
        )


def task_order(prereg: dict[str, Any]) -> list[str]:
    """Deterministic shuffle so batches are not alphabetical slices of the corpus."""
    tasks = sorted(prereg["tasks"])
    random.Random(prereg["seed"]).shuffle(tasks)
    return tasks


def batch_tasks(prereg: dict[str, Any], batch: int) -> list[str]:
    size = prereg["batch_size"]
    tasks = task_order(prereg)[batch * size : (batch + 1) * size]
    if not tasks:
        raise ValueError(f"batch {batch} is empty")
    return tasks


def skillsbench_commit(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def task_skills(skillsbench: Path, task: str) -> list[Path]:
    """SkillsBench ships each task's skills beside it; this pin does not bake them
    into the image, so they are injected per job. Both arms get them."""
    skills_dir = skillsbench / "tasks" / task / "environment" / "skills"
    return sorted(p for p in skills_dir.iterdir() if (p / "SKILL.md").exists()) if skills_dir.exists() else []


def harbor_command(
    prereg: dict[str, Any],
    *,
    arm: str,
    task: str,
    jobs_dir: Path,
    skillsbench: Path,
) -> list[str]:
    """One Harbor job per task: ``--skill`` is job-wide, and tasks carry their own."""
    cmd = [
        "harbor",
        "run",
        "-p",
        str(skillsbench / "tasks" / task),
        "-a",
        prereg["agent"]["name"],
        "-m",
        prereg["model"],
        "--ak",
        f"version={prereg['agent']['version']}",
        "--ak",
        f"reasoning_effort={prereg['reasoning_effort']}",
        "-o",
        str(jobs_dir),
        "--job-name",
        task,
        "-n",
        "1",
        "-k",
        "1",
        "--agent-setup-timeout-multiplier",
        "3",
        "-y",
        "-q",
    ]
    payload = prereg["arms"][arm]
    if payload is not None:
        cmd += ["--ak", "append_system_prompt=" + (ROOT / payload).read_text(encoding="utf-8")]
    for skill in task_skills(skillsbench, task):
        cmd += ["--skill", str(skill)]
    return cmd


def count_trials(jobs_dir: Path) -> int:
    """Trials recorded so far under ``jobs_dir/<job>/<task>/<trial>/result.json``."""
    return sum(1 for _ in jobs_dir.glob("*/*/*/result.json")) if jobs_dir.exists() else 0


def run_jobs(commands: list[list[str]], env: dict[str, str], workers: int) -> int:
    from concurrent.futures import ThreadPoolExecutor

    def one(cmd: list[str]) -> int:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        print(f"[{cmd[cmd.index('--job-name') + 1]}] exit {proc.returncode}: {' | '.join(tail)}", file=sys.stderr)
        return proc.returncode

    with ThreadPoolExecutor(max_workers=workers) as pool:
        codes = list(pool.map(one, commands))
    return max(codes) if codes else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--arm", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--batch", type=int, help="zero-based batch index from the preregistration order")
    group.add_argument("--retry", metavar="TASK", help="re-run one task (one-sided infra failure)")
    group.add_argument("--pilot", type=int, metavar="N", help="first N tasks of the order, job name pilot-<arm>")
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--skillsbench", type=Path, required=True, help="checkout pinned to the registered commit")
    parser.add_argument("--dry-run", action="store_true", help="print the harbor command, make no calls")
    args = parser.parse_args(argv)

    prereg = load_prereg(args.prereg)
    if args.arm not in prereg["arms"]:
        parser.error(f"arm {args.arm!r} not in preregistration {sorted(prereg['arms'])}")

    if args.retry:
        tasks, job_name = [args.retry], f"{args.arm}-retry-{args.retry}"
    elif args.pilot is not None:
        tasks, job_name = task_order(prereg)[: args.pilot], f"pilot-{args.arm}"
    else:
        tasks, job_name = batch_tasks(prereg, args.batch), f"{args.arm}-b{args.batch:02d}"

    commands = [
        harbor_command(prereg, arm=args.arm, task=task, jobs_dir=args.jobs_dir / job_name, skillsbench=args.skillsbench)
        for task in tasks
    ]
    if args.dry_run:
        for cmd in commands:
            print(" ".join(shlex.quote(part) for part in cmd))
        return 0

    assert_subscription_billing(dict(os.environ))
    commit = skillsbench_commit(args.skillsbench)
    if commit != prereg["skillsbench"]["commit"]:
        raise ValueError(f"skillsbench checkout at {commit}, registered {prereg['skillsbench']['commit']}")
    done = count_trials(args.jobs_dir)
    if done + len(tasks) > prereg["max_trials"]:
        raise BillingGuard(
            f"refusing to run: {done} trials recorded + {len(tasks)} requested "
            f"exceeds the registered cap of {prereg['max_trials']}"
        )
    if (args.jobs_dir / job_name).exists():
        raise ValueError(f"job {job_name} already exists under {args.jobs_dir}")

    env = {k: v for k, v in os.environ.items() if k not in BANNED_ENV}
    env["CLAUDE_FORCE_OAUTH"] = "1"
    print(f"{job_name}: {len(tasks)} tasks, arm {args.arm}, {done} trials recorded so far", file=sys.stderr)
    return run_jobs(commands, env, prereg["n_concurrent"])


if __name__ == "__main__":
    sys.exit(main())
