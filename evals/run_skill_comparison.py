#!/usr/bin/env python3
"""Run isolated Codex skill comparisons with resumable local evidence."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
import tempfile
import textwrap
from collections.abc import Iterable, Iterator
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


def open_directory(path: Path, *, create: bool, private: bool) -> int:
    """Open a directory by descriptor traversal without following symlinks."""
    absolute = path if path.is_absolute() else path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for name in absolute.parts[1:]:
            try:
                child = os.open(name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o700 if private else 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(name, flags, dir_fd=descriptor)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(f"unsafe symlinked path: {absolute}") from error
                raise
            os.close(descriptor)
            descriptor = child
        if private:
            os.fchmod(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def ensure_private_dir(path: Path) -> None:
    descriptor = open_directory(path, create=True, private=True)
    os.close(descriptor)


def reset_dir(path: Path) -> None:
    parent = open_directory(path.parent, create=True, private=True)
    try:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"unsafe private directory: {path}")
            shutil.rmtree(path.name, dir_fd=parent)
        os.mkdir(path.name, 0o700, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def open_private_file(path: Path, *, read_back: bool = False) -> tuple[int, int]:
    parent = open_directory(path.parent, create=True, private=True)
    flags = (os.O_RDWR if read_back else os.O_WRONLY) | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path.name, flags, 0o600, dir_fd=parent), parent
    except Exception:
        os.close(parent)
        raise


def open_existing_private_file(path: Path) -> tuple[int, int]:
    parent = open_directory(path.parent, create=False, private=True)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError(f"unsafe private file: {path}")
        return descriptor, parent
    except OSError as error:
        os.close(parent)
        if error.errno in {errno.ELOOP, errno.EISDIR}:
            raise ValueError(f"unsafe symlinked file: {path}") from error
        raise
    except Exception:
        os.close(parent)
        raise


def descriptor_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(descriptor, 64 * 1024):
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 64 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def exact_json_equal(left: object, right: object) -> bool:
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(right, sort_keys=True, separators=(",", ":"))


def atomic_write(path: Path, data: bytes, mode: int = 0o600, *, private: bool = True) -> None:
    parent = open_directory(path.parent, create=True, private=private)
    try:
        try:
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(current.st_mode):
                raise ValueError(f"unsafe symlinked file: {path}")
        for _ in range(16):
            temporary = f".{path.name}.{secrets.token_hex(8)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(temporary, flags, mode, dir_fd=parent)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                return
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        raise RuntimeError("unable to allocate safe temporary file")
    finally:
        os.close(parent)


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    atomic_write(path, "".join(json.dumps(record, sort_keys=True) + "\n" for record in records).encode())


def create_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    descriptor, parent = open_private_file(path)
    try:
        data = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records).encode()
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(parent)
    finally:
        close_descriptors(descriptor, parent)


def write_json(path: Path, value: dict[str, object]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    atomic_write(path, payload, 0o644, private=False)


def parse_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    descriptor, parent = open_existing_private_file(path)
    try:
        text = descriptor_bytes(descriptor).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"malformed JSONL: {path.name}") from error
    finally:
        os.close(descriptor)
        os.close(parent)
    for number, line in enumerate(text.splitlines(), 1):
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


def ledger_path(output: Path, project: Project, name: str) -> Path:
    return private_root(output) / "ledger" / f"{project.key}-{name}-1.jsonl"


def raw_path(output: Path, project: Project, name: str, stream: str) -> Path:
    return private_root(output) / "raw" / f"{project.key}-{name}-1.{stream}"


def prepare_private_root(output: Path) -> Path:
    root = private_root(output)
    ensure_private_dir(root)
    ensure_private_dir(root / "ledger")
    ensure_private_dir(root / "raw")
    ensure_private_dir(root / "homes")
    ensure_private_dir(root / "runs")
    return root


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def validate_raw_evidence(value: object, evidence: tuple[Path, Path] | None) -> None:
    if not isinstance(value, dict) or set(value) != {"stdout_sha256", "stderr_sha256"}:
        raise ValueError("malformed saved trace raw evidence")
    if any(not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest) for digest in value.values()):
        raise ValueError("malformed saved trace raw evidence")
    if evidence is None:
        return
    for path, key in zip(evidence, ("stdout_sha256", "stderr_sha256"), strict=True):
        descriptor, parent = open_existing_private_file(path)
        try:
            if descriptor_sha256(descriptor) != value[key]:
                raise ValueError("saved trace raw evidence mismatch")
        finally:
            os.close(descriptor)
            os.close(parent)


def validate_saved_trace(path: Path, *, evidence: tuple[Path, Path] | None = None) -> list[dict[str, object]]:
    records = parse_jsonl(path)
    if not records or set(records[0]) != {"record_type", "identity"} or records[0].get("record_type") != "identity" or not isinstance(records[0].get("identity"), dict):
        raise ValueError("malformed saved trace identity")
    identity = dict(records[0]["identity"])
    identity_id = identity.pop("id", None)
    if not isinstance(identity_id, str) or identity_id != hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest():
        raise ValueError("malformed saved trace identity")
    if len(records) == 2 and records[1] == {"record_type": "call", "status": "started"}:
        return records
    types = [record.get("record_type") for record in records]
    if len(records) < 3 or types[:2] != ["identity", "call"] or records[1] != {"record_type": "call", "status": "started"}:
        raise ValueError("malformed saved trace state")
    events_end = 2
    while events_end < len(records) and types[events_end] == "event":
        events_end += 1
    events: list[dict[str, object]] = []
    for record in records[2:events_end]:
        if set(record) != {"record_type", "event"} or not isinstance(record.get("event"), dict):
            raise ValueError("malformed saved trace event")
        events.append(record["event"])
    expected_messages = message_records(events, require_final=False)
    full = types[-2:] == ["usage", "result"]
    result = records[-1]
    if not isinstance(result, dict) or result.get("record_type") != "result" or result.get("status") not in {"completed", "failed"}:
        raise ValueError("malformed saved trace state")
    if full:
        messages = records[events_end:-2]
        if messages != expected_messages or not messages or messages[-1].get("role") != "final":
            raise ValueError("malformed saved trace messages")
        _, expected_usage = parse_codex_events("\n".join(json.dumps(event) for event in events))
        if not exact_json_equal(records[-2], {"record_type": "usage", "usage": expected_usage}):
            raise ValueError("malformed saved trace usage")
        if set(result) == {"record_type", "status", "codex_exit", "check_exit", "raw"}:
            codex_exit, check_exit = result.get("codex_exit"), result.get("check_exit")
            if type(codex_exit) is not int or type(check_exit) is not int:
                raise ValueError("malformed saved trace result")
            if result["status"] == "completed" and (codex_exit != 0 or check_exit != 0):
                raise ValueError("forged completed trace")
            if result["status"] == "failed" and codex_exit == 0 and check_exit == 0:
                raise ValueError("forged failed trace")
            validate_raw_evidence(result["raw"], evidence)
            return records
        if set(result) == {"record_type", "status", "codex_exit", "check_error", "raw"} and result["status"] == "failed":
            if type(result.get("codex_exit")) is not int or not isinstance(result.get("check_error"), str):
                raise ValueError("malformed saved trace result")
            validate_raw_evidence(result["raw"], evidence)
            return records
        raise ValueError("malformed saved trace result")
    messages = records[events_end:-1]
    if messages != expected_messages:
        raise ValueError("malformed partial trace messages")
    allowed = {"record_type", "status", "error"}
    if set(result) not in (allowed, allowed | {"raw"}) or result.get("status") != "failed" or not isinstance(result.get("error"), str):
        raise ValueError("malformed partial trace result")
    if "raw" in result:
        validate_raw_evidence(result["raw"], evidence)
    return records


def parse_codex_events(stdout: str | Iterable[str]) -> tuple[list[dict[str, object]], dict[str, object]]:
    events: list[dict[str, object]] = []
    usage: dict[str, object] = {}
    lines = stdout.splitlines() if isinstance(stdout, str) else stdout
    for number, line in enumerate(lines, 1):
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
        if not is_nonnegative_int(usage.get(key)):
            raise ValueError(f"malformed Codex JSONL: invalid usage.{key}")
    return events, usage


def message_records(events: list[dict[str, object]], *, require_final: bool) -> list[dict[str, str]]:
    texts: list[str] = []
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("malformed Codex JSONL: empty agent message")
        texts.append(text)
    if require_final and not texts:
        raise ValueError("malformed Codex JSONL: missing final agent message")
    return [{"record_type": "message", "role": "final" if index == len(texts) - 1 else "commentary", "text": text} for index, text in enumerate(texts)]


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
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for name in absolute.parts[1:]:
        current /= name
        if current.is_symlink() and current not in {Path("/tmp"), Path("/var")}:
            raise ValueError(f"unsafe symlinked output path: {current}")
    if path.is_symlink():
        raise ValueError("unsafe symlinked output path")
    resolved = path.resolve()
    descriptor = open_directory(resolved.parent, create=True, private=False)
    os.close(descriptor)
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
    if args.dry_run:
        args.codex_cli = CODEX
        return
    if any(getattr(args, name) in (None, "") for name in ("model", "effort", "max_calls")):
        parser.error("live mode requires --model, --effort, and --max-calls")
    if platform.system() != "Darwin":
        parser.error("live mode is supported only on the current macOS isolation runner")
    try:
        private = prepare_private_root(args.output_dir)
        for ledger in (private / "ledger").glob("*.jsonl"):
            if ledger.is_symlink():
                raise ValueError("unsafe symlinked ledger")
            validate_saved_trace(ledger)
    except ValueError as error:
        parser.error(str(error))
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


def prepare_run(project: Project, name: str, policy: Path | None, output: Path, seed: int) -> tuple[Path, Path, dict[str, str]]:
    run_dir, home = output / "runs" / project.key / name, output / "homes" / project.key / name
    reset_dir(run_dir)
    reset_dir(home)
    copy_seed(SEEDS / project.key, run_dir)
    for command in (["git", "init"], ["git", "add", "."], ["git", "-c", "user.name=Codex Eval", "-c", "user.email=codex-eval@example.com", "commit", "-m", "Seed failing scenario"]):
        result = run(command, cwd=run_dir)
        if result.returncode:
            raise RuntimeError(result.stderr)
    codex_home = home / ".codex"
    ensure_private_dir(codex_home)
    shutil.copy2(AUTH, codex_home / "auth.json")
    if policy:
        (codex_home / "AGENTS.md").write_text(policy.read_text(encoding="utf-8"), encoding="utf-8")
    return run_dir, home, {"HOME": str(home), "CODEX_HOME": str(codex_home), "PYTHONHASHSEED": str(seed)}


def private_path_exists(path: Path) -> bool:
    parent = open_directory(path.parent, create=False, private=True)
    try:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"unsafe symlinked file: {path}")
        return True
    finally:
        os.close(parent)


def raw_paths(output: Path, project: Project, name: str) -> tuple[Path, Path]:
    return raw_path(output, project, name, "stdout.jsonl"), raw_path(output, project, name, "stderr.txt")


def raw_evidence_exists(output: Path, project: Project, name: str) -> bool:
    directory = private_root(output) / "raw"
    descriptor = open_directory(directory, create=False, private=True)
    try:
        prefix = f"{project.key}-{name}-1."
        return any(entry.startswith(prefix) for entry in os.listdir(descriptor))
    finally:
        os.close(descriptor)


def recorded_status(path: Path, identity: dict[str, object], *, evidence: tuple[Path, Path] | None = None) -> str | None:
    if not private_path_exists(path):
        return None
    records = validate_saved_trace(path, evidence=evidence)
    if not records:
        return None
    if records[0].get("identity") != identity:
        raise ValueError("resume identity mismatch")
    for record in reversed(records):
        if record.get("record_type") == "result":
            return str(record.get("status"))
    return "started" if any(record.get("record_type") == "call" for record in records) else None


def close_descriptors(*descriptors: int) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass


def descriptor_lines(descriptor: int) -> Iterator[str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        for line in handle:
            yield line.decode("utf-8", errors="strict")


def streamed_process(command: list[str], *, cwd: Path, env: dict[str, str], stdout_path: Path, stderr_path: Path) -> tuple[int, tuple[int, int, int, int]]:
    stdout_fd, stdout_parent = open_private_file(stdout_path, read_back=True)
    try:
        stderr_fd, stderr_parent = open_private_file(stderr_path, read_back=True)
    except Exception:
        close_descriptors(stdout_fd, stdout_parent)
        raise
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout_fd, stderr=stderr_fd)
        exit_code = process.wait()
        os.fsync(stdout_fd)
        os.fsync(stderr_fd)
        os.fsync(stdout_parent)
        os.fsync(stderr_parent)
    except BaseException:
        close_descriptors(stdout_fd, stderr_fd, stdout_parent, stderr_parent)
        raise
    return exit_code, (stdout_fd, stderr_fd, stdout_parent, stderr_parent)


def parse_codex_prefix(stdout: str | Iterable[str]) -> tuple[list[dict[str, object]], str | None]:
    events: list[dict[str, object]] = []
    lines = stdout.splitlines() if isinstance(stdout, str) else stdout
    try:
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return events, f"malformed Codex JSONL:{number}"
            if not isinstance(event, dict):
                return events, f"malformed Codex JSONL:{number}"
            events.append(event)
    except UnicodeDecodeError:
        return events, "malformed Codex JSONL: invalid UTF-8"
    return events, None


def safe_partial_events(events: list[dict[str, object]]) -> tuple[list[dict[str, object]], str | None]:
    valid: list[dict[str, object]] = []
    for event in events:
        if event.get("type") == "turn.completed":
            break
        try:
            message_records([event], require_final=False)
        except ValueError as error:
            return valid, str(error)
        valid.append(event)
    return valid, None


def append_partial_records(records: list[dict[str, object]], events: list[dict[str, object]]) -> None:
    records.extend({"record_type": "event", "event": event} for event in events)
    records.extend(message_records(events, require_final=False))


def call_codex(project: Project, name: str, policy: Path | None, args: argparse.Namespace, identity: dict[str, object]) -> list[dict[str, object]]:
    ledger = ledger_path(args.output_dir, project, name)
    evidence = raw_paths(args.output_dir, project, name)
    records: list[dict[str, object]] = [{"record_type": "identity", "identity": identity}, {"record_type": "call", "status": "started"}]
    create_jsonl(ledger, records)
    home: Path | None = None
    capture: tuple[int, int, int, int] | None = None
    exit_code: int | None = None
    raw: dict[str, str] | None = None
    usage_recorded = False
    try:
        run_dir, home, env = prepare_run(project, name, policy, private_root(args.output_dir), args.seed)
        command = [CODEX, "--ask-for-approval", "never", "exec", "--ephemeral", "--skip-git-repo-check", "-C", str(run_dir), "-m", args.model, "-c", f'model_reasoning_effort="{args.effort}"', "-s", "workspace-write", "--json", project.task]
        exit_code, capture = streamed_process(command, cwd=run_dir, env={key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ} | env, stdout_path=evidence[0], stderr_path=evidence[1])
        stdout_fd, stderr_fd, _, _ = capture
        raw = {"stdout_sha256": descriptor_sha256(stdout_fd), "stderr_sha256": descriptor_sha256(stderr_fd)}
        prefix, parse_error = parse_codex_prefix(descriptor_lines(stdout_fd))
        partial_events, message_error = safe_partial_events(prefix)
        if message_error:
            append_partial_records(records, partial_events)
            raise ValueError(message_error)
        if parse_error:
            append_partial_records(records, partial_events)
            raise ValueError(parse_error)
        try:
            events, usage = parse_codex_events(descriptor_lines(stdout_fd))
            messages = message_records(events, require_final=True)
        except ValueError:
            append_partial_records(records, partial_events)
            raise
        records.extend({"record_type": "event", "event": event} for event in events)
        records.extend(messages)
        if records[2:] != [{"record_type": "event", "event": event} for event in events] + messages:
            raise ValueError("parsed message divergence")
        records.append({"record_type": "usage", "usage": usage})
        usage_recorded = True
        check = run(project.check, cwd=run_dir)
        records.append({"record_type": "result", "status": "completed" if exit_code == 0 and check.returncode == 0 else "failed", "codex_exit": exit_code, "check_exit": check.returncode, "raw": raw})
    except Exception as error:
        if usage_recorded and exit_code is not None and raw is not None:
            records.append({"record_type": "result", "status": "failed", "codex_exit": exit_code, "check_error": str(error), "raw": raw})
        else:
            result: dict[str, object] = {"record_type": "result", "status": "failed", "error": str(error)}
            if raw is not None:
                result["raw"] = raw
            records.append(result)
    finally:
        if capture is not None:
            close_descriptors(*capture)
        if home is not None and home.exists():
            shutil.rmtree(home)
    write_jsonl(ledger, records)
    validate_saved_trace(ledger, evidence=evidence)
    return records


def public_export(output: Path, records: list[dict[str, object]]) -> dict[str, object]:
    runs = []
    for record in records:
        identity = record["identity"]
        usage = record.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        runs.append({
            "id": identity["id"],
            "project": identity["project"],
            "variant": identity["variant"],
            "seed": identity["seed"],
            "trial": identity["trial"],
            "status": record["status"],
            "usage": {key: value for key, value in usage.items() if key in {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"} and is_nonnegative_int(value)},
        })
    return {"runs": runs}


def write_summary(output: Path, records: list[dict[str, object]]) -> None:
    write_json(output / "summary.json", public_export(output, records))


def summary_from_trace(raw: Path, identity: dict[str, object], status: str, *, evidence: tuple[Path, Path]) -> dict[str, object]:
    records = validate_saved_trace(raw, evidence=evidence)
    usage = next((record.get("usage") for record in records if record.get("record_type") == "usage"), {})
    return {"identity": identity, "status": status, "usage": usage if isinstance(usage, dict) else {}}


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
    output_descriptor = open_directory(args.output_dir, create=True, private=False)
    os.close(output_descriptor)
    plan = [(project, name, policy, run_identity(project, name, policy, 1, args, variants)) for project in PROJECTS for name, policy in variants]
    if not args.dry_run:
        for project, name, _, _ in plan:
            try:
                ledger_exists = private_path_exists(ledger_path(args.output_dir, project, name))
                raw_exists = raw_evidence_exists(args.output_dir, project, name)
            except ValueError as error:
                parser.error(str(error))
            if raw_exists and not ledger_exists:
                parser.error("raw evidence without a ledger; refusing a fresh or resumed model call")
            if not args.resume and (ledger_exists or raw_exists):
                parser.error("existing attempt requires --resume; choose a new --output-dir for a fresh attempt")
    resumed: dict[str, str | None] = {}
    if args.resume:
        for project, name, _, identity in plan:
            try:
                resumed[identity["id"]] = recorded_status(ledger_path(args.output_dir, project, name), identity, evidence=raw_paths(args.output_dir, project, name))
            except ValueError as error:
                parser.error(str(error))
    if args.dry_run:
        print(json.dumps({"seed": args.seed, "runs": [{"id": identity["id"], "project": project.key, "variant": name} for project, name, _, identity in plan]}, sort_keys=True))
        return 0
    verify_seeds()
    summary: list[dict[str, object]] = []
    for project, name, policy, identity in plan:
        raw = ledger_path(args.output_dir, project, name)
        evidence = raw_paths(args.output_dir, project, name)
        previous = resumed.get(identity["id"]) if args.resume else recorded_status(raw, identity, evidence=evidence)
        if args.resume and previous is not None:
            summary.append(summary_from_trace(raw, identity, previous, evidence=evidence))
            if previous != "completed":
                write_summary(args.output_dir, summary)
                return 1
            continue
        records = call_codex(project, name, policy, args, identity)
        result = next(record for record in reversed(records) if record["record_type"] == "result")
        usage = next((record.get("usage") for record in records if record.get("record_type") == "usage"), {})
        summary.append({"identity": identity, "status": result["status"], "usage": usage if isinstance(usage, dict) else {}})
        if result["status"] != "completed":
            write_summary(args.output_dir, summary)
            return 1
    write_summary(args.output_dir, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
