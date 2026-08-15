#!/usr/bin/env python3
"""Run isolated Codex skill comparisons with resumable local evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SEEDS = REPO / "evals" / "fixtures" / "skill-comparison"
SIMPLE_MAN_SKILL = REPO / "skills" / "simple-man" / "SKILL.md"
AUTH = Path.home() / ".codex" / "auth.json"
CODEX = os.environ.get("CODEX", "codex")
SCHEMA = 1
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "simple-man-skill-comparison"


@dataclass(frozen=True)
class Project:
    key: str
    title: str
    task: str
    check: list[str]


PROJECTS = [
    Project(
        "node-auth-api", "Node auth API", textwrap.dedent("""
        We have an auth bug: expired sessions are still accepted.

        Please inspect the project, fix the bug, run the relevant tests, and give me an engineering handoff with:
        - root cause
        - files changed
        - validation command and result
        - any remaining risk

        Answer in English. Do not mention benchmark internals, isolated CODEX_HOME directories, raw logs, or absolute run-copy paths.
        """).strip(), ["npm", "test"],
    ),
    Project(
        "python-payment-ledger", "Python payment ledger", textwrap.dedent("""
        We have a duplicate-charge retry bug. A gateway timeout can happen after the provider accepted the charge, and retrying with the same idempotency key currently creates another local charge.

        Please inspect the project, fix the idempotency bug, run the relevant tests, and give me an engineering handoff with:
        - root cause
        - files changed
        - validation command and result
        - any remaining risk

        Answer in English. Do not mention benchmark internals, isolated CODEX_HOME directories, raw logs, or absolute run-copy paths.
        """).strip(), ["python3", "-m", "unittest", "-v"],
    ),
    Project(
        "sqlite-rollout-runner", "SQLite rollout runner", textwrap.dedent("""
        We have an unsafe rollout order: the migration drops legacy_sessions.expires_at before the backup reads that column.

        Please inspect the project, fix the rollout order, run the relevant tests, and give me an engineering handoff with:
        - root cause
        - files changed
        - validation command and result
        - any remaining risk

        Answer in English. Do not mention benchmark internals, isolated CODEX_HOME directories, raw logs, or absolute run-copy paths.
        """).strip(), ["python3", "-m", "unittest", "-v"],
    ),
]


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    merged.update(env or {})
    if cmd[1:3] == ["-m", "unittest"]:
        merged["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(cmd, cwd=cwd, env=merged, text=True, capture_output=True)


def reset_dir(path: Path, output: Path) -> None:
    for parent in (path, *path.parents):
        if parent == output.parent:
            break
        if parent.is_symlink():
            raise ValueError(f"unsafe symlinked output path: {parent}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"malformed JSONL: {path.name}:{number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"malformed JSONL: {path.name}:{number}")
        records.append(value)
    return records


def private_root(output: Path) -> Path:
    return output.parent / f".{output.name}-private"


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def validate_saved_trace(path: Path) -> None:
    records = parse_jsonl(path)
    if len(records) == 2 and records[0].get("record_type") == "identity" and records[1] == {"record_type": "call", "status": "started"}:
        return
    if len(records) == 3 and records[0].get("record_type") == "identity" and records[1] == {"record_type": "call", "status": "started"} and records[2].get("record_type") == "result" and records[2].get("status") == "failed" and isinstance(records[2].get("error"), str):
        return
    types = [record.get("record_type") for record in records]
    if len(records) < 5 or types[:2] != ["identity", "call"] or types[-2:] != ["usage", "result"]:
        raise ValueError("malformed saved trace state")
    if not isinstance(records[0].get("identity"), dict) or records[1].get("status") != "started":
        raise ValueError("malformed saved trace state")
    events_end = 2
    while events_end < len(records) and types[events_end] == "event":
        events_end += 1
    messages = records[events_end:-2]
    if not messages or any(record.get("record_type") != "message" for record in messages):
        raise ValueError("malformed saved trace state")
    if [record.get("role") for record in messages].count("final") != 1 or messages[-1].get("role") != "final":
        raise ValueError("malformed saved trace final message")
    events = [record["event"] for record in records[2:events_end] if isinstance(record.get("event"), dict)]
    parse_codex_events("\n".join(json.dumps(event) for event in events))
    usage = records[-2].get("usage")
    if not isinstance(usage, dict) or records[-1].get("status") not in {"completed", "failed"}:
        raise ValueError("malformed saved trace state")


def parse_codex_events(stdout: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    events: list[dict[str, object]] = []
    usage: dict[str, object] = {}
    for number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"malformed Codex JSONL:{number}") from error
        if not isinstance(event, dict):
            raise ValueError(f"malformed Codex JSONL:{number}")
        events.append(event)
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    completed = [index for index, event in enumerate(events) if event.get("type") == "turn.completed"]
    if completed != [len(events) - 1] or not usage:
        raise ValueError("malformed Codex JSONL: expected one final turn.completed usage event")
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
        if not isinstance(usage.get(key), int) or usage[key] < 0:
            raise ValueError(f"malformed Codex JSONL: invalid usage.{key}")
    return events, usage


def agent_messages(events: list[dict[str, object]]) -> list[str]:
    messages = [item["text"] for event in events if isinstance(event.get("item"), dict) and (item := event["item"]).get("type") == "agent_message" and isinstance(item.get("text"), str)]
    if not messages:
        raise ValueError("malformed Codex JSONL: missing final agent message")
    return messages


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit() -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=REPO)
    if result.returncode:
        raise RuntimeError("source commit unavailable")
    return result.stdout.strip()


def run_identity(project: Project, name: str, policy: Path | None, trial: int, args: argparse.Namespace, variants: list[tuple[str, Path | None]]) -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": source_commit(),
        "runner_sha256": sha256_file(Path(__file__)),
        "fixture_sha256": tree_hash(SEEDS / project.key),
        "project": project.key,
        "task_sha256": hashlib.sha256(project.task.encode()).hexdigest(),
        "check": project.check,
        "variant": name,
        "variants": [{"name": variant, "policy_sha256": sha256_file(source) if source else None} for variant, source in variants],
        "policy_sha256": sha256_file(policy) if policy else None,
        "seed": args.seed,
        "trial": trial,
        "model": args.model,
        "effort": args.effort,
        "codex_cli": getattr(args, "codex_cli", CODEX),
    }
    identity["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return identity


def default_variants() -> list[tuple[str, Path | None]]:
    return [("baseline", None), ("simple-man", SIMPLE_MAN_SKILL)]


def parse_variants(values: list[str]) -> list[tuple[str, Path | None]]:
    variants: list[tuple[str, Path | None]] = []
    for value in values:
        if "=" not in value:
            raise ValueError("variant must be NAME=PATH")
        name, raw_path = value.split("=", 1)
        if not name or not raw_path or not name.replace("-", "").replace("_", "").isalnum() or any(existing == name for existing, _ in variants):
            raise ValueError("variant must use a unique NAME=PATH")
        policy = Path(raw_path).expanduser()
        if not policy.is_file():
            raise ValueError(f"policy missing for variant {name}")
        variants.append((name, policy.resolve()))
    return variants or default_variants()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--resume", action="store_true")
    return parser


def safe_output_dir(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("unsafe symlinked output path")
    resolved = path.resolve()
    if resolved == REPO or resolved.is_relative_to(REPO) or resolved == SEEDS or resolved.is_relative_to(SEEDS):
        raise ValueError("output directory must be outside source and seed paths")
    return resolved


def preflight(args: argparse.Namespace, parser: argparse.ArgumentParser, variants: list[tuple[str, Path | None]]) -> None:
    try:
        args.output_dir = safe_output_dir(args.output_dir)
    except ValueError as error:
        parser.error(str(error))
    for _, policy in variants:
        if policy is not None and not policy.is_file():
            parser.error(f"policy missing: {policy}")
    raw_root = private_root(args.output_dir) / "raw"
    if raw_root.is_symlink():
        parser.error("unsafe symlinked output path")
    for raw in raw_root.glob("*.jsonl"):
        if raw.is_symlink():
            parser.error("unsafe symlinked output path")
        try:
            validate_saved_trace(raw)
        except ValueError as error:
            parser.error(str(error))
    if args.dry_run:
        args.codex_cli = CODEX
        return
    if any(getattr(args, name) in (None, "") for name in ("model", "effort", "max_calls")):
        parser.error("live mode requires --model, --effort, and --max-calls")
    if platform.system() != "Darwin":
        parser.error("live mode is supported only on the current macOS isolation runner")
    if args.max_calls <= 0:
        parser.error("--max-calls must be positive")
    planned = len(PROJECTS) * len(variants)
    if planned > args.max_calls:
        parser.error("--max-calls budget overflow before first model call")
    if args.max_usd is not None:
        parser.error("--max-usd is unavailable without a verified versioned price mapping; use --max-calls and token caps for Codex subscription runs")
    if not AUTH.is_file():
        parser.error(f"auth missing: {AUTH}")
    version = run([CODEX, "--version"], cwd=REPO)
    if version.returncode or not version.stdout.strip():
        parser.error("Codex CLI identity unavailable")
    args.codex_cli = version.stdout.strip()


def verify_seeds() -> None:
    for project in PROJECTS:
        proc = run(project.check, cwd=SEEDS / project.key)
        print(f"seed check {project.key}: exit {proc.returncode}", flush=True)
        if proc.returncode == 0:
            raise RuntimeError(f"seed must fail before fixes: {project.key}")


def copy_seed(seed: Path, target: Path) -> None:
    try:
        relative = seed.resolve().relative_to(REPO)
    except ValueError:
        files = [path for path in seed.rglob("*") if path.is_file()]
    else:
        tracked = run(["git", "ls-files", "-z", "--", str(relative)], cwd=REPO)
        if tracked.returncode:
            raise RuntimeError(tracked.stderr)
        files = [REPO / path for path in tracked.stdout.split("\0") if path]
    for source in files:
        if source.is_symlink() or ".git" in source.relative_to(seed).parts or source.name == ".env" or source.name.startswith(".env."):
            raise ValueError(f"unsafe seed file: {source.name}")
        destination = target / source.relative_to(seed)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def prepare_run(project: Project, name: str, policy: Path | None, output: Path, seed: int) -> tuple[Path, dict[str, str]]:
    run_dir, home = output / "runs" / project.key / name, output / "homes" / project.key / name
    reset_dir(run_dir, output)
    reset_dir(home, output)
    copy_seed(SEEDS / project.key, run_dir)
    for command in (["git", "init"], ["git", "add", "."], ["git", "-c", "user.name=Codex Eval", "-c", "user.email=codex-eval@example.com", "commit", "-m", "Seed failing scenario"]):
        result = run(command, cwd=run_dir)
        if result.returncode:
            raise RuntimeError(result.stderr)
    codex_home = home / ".codex"
    codex_home.mkdir()
    shutil.copy2(AUTH, codex_home / "auth.json")
    if policy:
        (codex_home / "AGENTS.md").write_text(policy.read_text(encoding="utf-8"), encoding="utf-8")
    return run_dir, {"HOME": str(home), "CODEX_HOME": str(codex_home), "PYTHONHASHSEED": str(seed)}


def recorded_status(path: Path, identity: dict[str, object]) -> str | None:
    if not path.exists():
        return None
    records = parse_jsonl(path)
    if not records:
        return None
    if records[0].get("identity") != identity:
        raise ValueError("resume identity mismatch")
    for record in reversed(records):
        if record.get("record_type") == "result":
            return str(record.get("status"))
    return "started" if any(record.get("record_type") == "call" for record in records) else None


def call_codex(project: Project, name: str, policy: Path | None, args: argparse.Namespace, identity: dict[str, object]) -> list[dict[str, object]]:
    raw_file = private_root(args.output_dir) / "raw" / f"{project.key}-{name}-1.jsonl"
    records: list[dict[str, object]] = [{"record_type": "identity", "identity": identity}, {"record_type": "call", "status": "started"}]
    write_jsonl(raw_file, records)
    try:
        run_dir, env = prepare_run(project, name, policy, private_root(args.output_dir), args.seed)
        command = [CODEX, "--ask-for-approval", "never", "exec", "--ephemeral", "--skip-git-repo-check", "-C", str(run_dir), "-m", args.model, "-c", f'model_reasoning_effort="{args.effort}"', "-s", "workspace-write", "--json", project.task]
        proc = subprocess.run(command, cwd=run_dir, env={key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ} | env, text=True, capture_output=True)
        events, usage = parse_codex_events(proc.stdout)
        messages = agent_messages(events)
        records.extend({"record_type": "event", "event": event} for event in events)
        records.extend({"record_type": "message", "role": "final" if index == len(messages) - 1 else "commentary", "text": message} for index, message in enumerate(messages))
        records.append({"record_type": "usage", "usage": usage})
        check = run(project.check, cwd=run_dir)
        records.append({"record_type": "result", "status": "completed" if proc.returncode == 0 and check.returncode == 0 else "failed", "codex_exit": proc.returncode, "check_exit": check.returncode})
    except Exception as error:
        records.append({"record_type": "result", "status": "failed", "error": str(error)})
    write_jsonl(raw_file, records)
    return records


def redact(value: object, paths: tuple[str, ...]) -> object:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"auth|token|secret|key|password", key, re.I) else redact(item, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, paths) for item in value]
    if not isinstance(value, str):
        return value
    for path in paths:
        value = value.replace(path, "[PATH]")
    value = re.sub(r"(?i)(authorization|token|secret|api[_-]?key|password)\s*[:=]\s*[^\s,]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)\b(secret|bearer)\b", "[REDACTED]", value)
    return re.sub(r"/(?:[^\s\"']+)", "[PATH]", value)


def public_export(output: Path, records: list[dict[str, object]]) -> dict[str, object]:
    paths = (str(REPO), str(output), str(private_root(output)), str(Path.home()))
    runs = []
    for record in records:
        identity = record["identity"]
        runs.append(redact({"id": identity["id"], "project": identity["project"], "variant": identity["variant"], "seed": identity["seed"], "trial": identity["trial"], "status": record["status"], "messages": record.get("messages", []), "stderr": record.get("stderr", ""), "event": record.get("event", {})}, paths))
    return {"runs": runs}


def write_summary(output: Path, records: list[dict[str, object]]) -> None:
    write_json(output / "summary.json", public_export(output, records))


def render_dry_run(argv: list[str]) -> str:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        variants = parse_variants(args.variant)
    except ValueError as error:
        parser.error(str(error))
    if not args.dry_run:
        parser.error("render_dry_run requires --dry-run")
    preflight(args, parser, variants)
    plan = [(project, name, policy, run_identity(project, name, policy, 1, args, variants)) for project in PROJECTS for name, policy in variants]
    return json.dumps({"seed": args.seed, "runs": [{"id": identity["id"], "project": project.key, "variant": name} for project, name, _, identity in plan]}, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        variants = parse_variants(args.variant)
    except ValueError as error:
        parser.error(str(error))
    preflight(args, parser, variants)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plan = [(project, name, policy, run_identity(project, name, policy, 1, args, variants)) for project in PROJECTS for name, policy in variants]
    resumed: dict[str, str | None] = {}
    if args.resume:
        for project, name, _, identity in plan:
            try:
                resumed[identity["id"]] = recorded_status(private_root(args.output_dir) / "raw" / f"{project.key}-{name}-1.jsonl", identity)
            except ValueError as error:
                parser.error(str(error))
    if args.dry_run:
        print(json.dumps({"seed": args.seed, "runs": [{"id": identity["id"], "project": project.key, "variant": name} for project, name, _, identity in plan]}, sort_keys=True))
        return 0
    verify_seeds()
    summary: list[dict[str, object]] = []
    for project, name, policy, identity in plan:
        raw = private_root(args.output_dir) / "raw" / f"{project.key}-{name}-1.jsonl"
        previous = resumed.get(identity["id"]) if args.resume else recorded_status(raw, identity)
        if args.resume and previous is not None:
            summary.append({"identity": identity, "status": previous})
            if previous != "completed":
                write_summary(args.output_dir, summary)
                return 1
            continue
        records = call_codex(project, name, policy, args, identity)
        result = next(record for record in reversed(records) if record["record_type"] == "result")
        summary.append({"identity": identity, "status": result["status"], "messages": [record["text"] for record in records if record.get("record_type") == "message"]})
        if result["status"] != "completed":
            write_summary(args.output_dir, summary)
            return 1
    write_summary(args.output_dir, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
