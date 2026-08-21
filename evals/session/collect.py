#!/usr/bin/env python3
"""Flatten Harbor job directories into one ``trials.jsonl``.

Layout produced by ``run_ab.py``: ``<jobs>/<job>/<task>/<task>__<id>/``. The
job name carries the arm (``pilot-N``, ``B2-b03``, ``N-retry-<task>``). Reward
comes from Harbor's ``result.json``; turns, wall-clock and cost come from the
final ``result`` event of Claude Code's own stream, which Harbor tees to
``agent/claude-code.txt``. Delivery is mechanical: the job lock records the
exact ``append_system_prompt`` the agent was started with, and its sha256 is
compared with the registered payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from pathlib import Path
from typing import Any


def arm_of(job: str) -> str:
    return job[len("pilot-") :] if job.startswith("pilot-") else job.split("-", 1)[0]


def stream_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                return event
    return {}


def lock_payload(lock: dict[str, Any]) -> str | None:
    trials = lock.get("trials") or []
    kwargs = (trials[0].get("agent") or {}).get("kwargs") or {} if trials else {}
    return kwargs.get("append_system_prompt")


def collect_trial(trial_dir: Path, job: str, payload_sha: dict[str, str | None]) -> dict[str, Any]:
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    lock = json.loads((trial_dir.parent / "lock.json").read_text(encoding="utf-8"))
    event = stream_result(trial_dir / "agent" / "claude-code.txt")
    arm = arm_of(job)
    payload = lock_payload(lock)
    if payload:
        parts = shlex.split(payload)  # run_ab hands Harbor a shell-quoted value
        payload = parts[0] if len(parts) == 1 else payload
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload else None
    agent = result.get("agent_result") or {}
    usage = event.get("usage") or {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    exc = result.get("exception_info") or {}
    timing = result.get("agent_execution") or {}
    return {
        "task": result["task_name"],
        "arm": arm,
        "job": job,
        "trial": result["trial_name"],
        "reward": rewards.get("reward"),
        "cost_usd": event.get("total_cost_usd", agent.get("cost_usd")),
        "input_tokens": usage.get("input_tokens", agent.get("n_input_tokens")),
        "cache_read_tokens": usage.get("cache_read_input_tokens", agent.get("n_cache_tokens")),
        "cache_write_tokens": usage.get("cache_creation_input_tokens"),
        "output_tokens": usage.get("output_tokens", agent.get("n_output_tokens")),
        "turns": event.get("num_turns"),
        "wall_ms": event.get("duration_ms"),
        "started_at": timing.get("started_at") or result.get("started_at"),
        "finished_at": timing.get("finished_at") or result.get("finished_at"),
        "error": exc.get("exception_type"),
        "delivered": digest is not None and digest == payload_sha.get(arm),
        "payload_sha256": digest,
        "model": ((result.get("agent_info") or {}).get("model_info") or {}).get("name"),
        "cli_version": (result.get("agent_info") or {}).get("version"),
        "task_checksum": result.get("task_checksum"),
    }


def collect(jobs_dir: Path, payload_sha: dict[str, str | None]) -> list[dict[str, Any]]:
    rows = []
    for result in sorted(jobs_dir.glob("*/*/*/result.json")):
        trial_dir = result.parent
        rows.append(collect_trial(trial_dir, trial_dir.parent.parent.name, payload_sha))
    rows.sort(key=lambda r: (r["task"], r["arm"], r["job"], r["trial"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True, help="trials.jsonl to write")
    args = parser.parse_args(argv)
    prereg = json.loads(args.prereg.read_text(encoding="utf-8"))
    rows = collect(args.jobs_dir, prereg["payload_sha256"])
    args.write.parent.mkdir(parents=True, exist_ok=True)
    with args.write.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{len(rows)} trials -> {args.write}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
