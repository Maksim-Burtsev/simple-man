#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import platform
import re
import resource
import secrets
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "evals" / "fixtures" / "skill-comparison"
WORKERS = SEEDS / "_workers"
DEFAULT_POLICY = ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md"
DEFAULT_OUTPUT = ROOT / ".local-fixtures" / "skill-comparison-quality"

SCHEMA_VERSION = 2
TRIALS = 2
ARMS = ("native_low", "candidate_runtime")
MODEL_VERBOSITY = "low"
EXPECTED_CALLS = 12
QUALITY_PERMISSION_PROFILE = "workspace_sealed"
VALIDATION_PERMISSION_PROFILE = "validation_sealed"
MAX_ATTEMPTS_PER_RUN = 1
MAX_TOTAL_ATTEMPTS = EXPECTED_CALLS
VALIDATION_TIMEOUT_SECONDS = 60
VALIDATION_OUTPUT_BYTES = 1_000_000
MAX_WORKSPACE_BYTES = 5_000_000
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 200
MAX_PATCH_BYTES = 1_000_000
MAX_OPEN_FILES = 256
RUNTIME_MARKER = "## Simple Man runtime policy"
DISABLED_FEATURES = (
    "apps",
    "plugins",
    "plugin_hooks",
    "tool_search",
    "hooks",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "workspace_dependencies",
    "goals",
    "memories",
)
SAFE_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NIX_SSL_CERT_FILE",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
PROXY_ENV_KEYS = frozenset(
    {"ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"}
)
SAFE_PATH = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)
EXECUTION_CONTRACT = {
    "version": 3,
    "tasks": 3,
    "arms": list(ARMS),
    "trials": TRIALS,
    "calls": EXPECTED_CALLS,
    "model_and_effort_are_explicit": True,
    "model_verbosity": MODEL_VERBOSITY,
    "approval_policy": "never",
    "sandbox": "custom workspace profile with CODEX_HOME read denied",
    "sandbox_network": False,
    "disabled_features": list(DISABLED_FEATURES),
    "ignore_user_config": True,
    "ignore_rules": True,
    "ephemeral": True,
    "prompt_transport": "stdin",
    "prompt_preflight": "exact task and exact candidate policy",
    "isolation": (
        "fresh HOME, separately rooted CODEX_HOME, cwd, and Git repository per call; "
        "model tools cannot read CODEX_HOME"
    ),
    "source_isolation": (
        "the model-tool permission profile denies reads and writes to the real user home, "
        "source worktree, common Git repository, and isolated CODEX_HOME"
    ),
    "order": "secret HMAC order with three first-runs per arm",
    "attempt_policy": {
        "per_run": MAX_ATTEMPTS_PER_RUN,
        "total": MAX_TOTAL_ATTEMPTS,
        "retry": "none; any started attempt is consumed",
    },
    "resource_caps": {
        "validation_timeout_seconds": VALIDATION_TIMEOUT_SECONDS,
        "validation_output_bytes": VALIDATION_OUTPUT_BYTES,
        "workspace_bytes": MAX_WORKSPACE_BYTES,
        "file_bytes": MAX_FILE_BYTES,
        "files": MAX_FILES,
        "patch_bytes": MAX_PATCH_BYTES,
        "open_files": MAX_OPEN_FILES,
    },
    "validation": (
        "strict allowlisted production and regression-test paths, bounded production-only diff, "
        "exact successful canonical test command in trace, exact pristine canonical tests, and "
        "parent-compared observations from randomized read-only post-run workers"
    ),
    "gate": "both arms 6/6; native failure is inconclusive",
}


@dataclass(frozen=True)
class HiddenCase:
    case_id: str
    request: Mapping[str, Any]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class Project:
    key: str
    title: str
    task: str
    check: tuple[str, ...]
    expected_seed_failure: str
    production_paths: tuple[str, ...]
    auxiliary_paths: tuple[str, ...]
    worker: Path
    worker_suffix: str
    test_reporter: str
    canonical_test_count: int
    canonical_test_ids: tuple[str, ...]
    hidden_cases: tuple[HiddenCase, ...]

    @property
    def root(self) -> Path:
        return SEEDS / self.key

    @property
    def allowed_paths(self) -> tuple[str, ...]:
        return self.production_paths + self.auxiliary_paths


@dataclass(frozen=True)
class Arm:
    name: str
    policy: str | None

    @property
    def policy_sha256(self) -> str:
        return sha256_text(self.policy or "")


@dataclass(frozen=True)
class RunKey:
    project: str
    arm: str
    trial: int

    def __post_init__(self) -> None:
        if (
            not self.project
            or self.arm not in ARMS
            or self.trial not in range(1, TRIALS + 1)
        ):
            raise ValueError("invalid run key")


@dataclass(frozen=True)
class AttemptPaths:
    root: Path
    started: Path
    outcome: Path
    raw: Path
    stderr: Path
    full_patch: Path
    production_patch: Path
    result: Path


@dataclass(frozen=True)
class IsolatedRun:
    root: Path
    home: Path
    codex_home: Path
    workspace: Path
    env: dict[str, str]


@dataclass(frozen=True)
class SourceIsolation:
    protected_roots: tuple[Path, ...]

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "platform": "macOS Seatbelt",
            "protected_roots": [str(path) for path in self.protected_roots],
            "enforcement": (
                "Codex custom permission profiles deny model and validation subprocess "
                "reads and writes for the real user home, source worktree, and common repository"
            ),
        }


PROJECTS = (
    Project(
        key="node-auth-api",
        title="Node auth API",
        task=textwrap.dedent(
            """
            We have an auth bug: expired sessions are still accepted.

            Inspect the project, fix the bug, and run the relevant tests. Then give
            an engineering handoff with the root cause, files changed, validation
            command and result, and any remaining risk. Use exactly `npm test` for
            validation. Answer in English.
            """
        ).strip(),
        check=("npm", "test"),
        expected_seed_failure="200 !== 401",
        production_paths=("src/middleware.js",),
        auxiliary_paths=("test/auth.test.js",),
        worker=WORKERS / "node-auth-worker.js",
        worker_suffix=".js",
        test_reporter="node",
        canonical_test_count=2,
        canonical_test_ids=(
            "accepts a valid session",
            "rejects an expired session",
        ),
        hidden_cases=(
            HiddenCase(
                "future",
                {"now": 1_999, "expires_at": 2_000},
                {"status": 200},
            ),
            HiddenCase(
                "boundary",
                {"now": 2_000, "expires_at": 2_000},
                {"status": 401},
            ),
            HiddenCase(
                "expired",
                {"now": 2_001, "expires_at": 2_000},
                {"status": 401},
            ),
        ),
    ),
    Project(
        key="python-payment-ledger",
        title="Python payment ledger",
        task=textwrap.dedent(
            """
            We have a duplicate-charge retry bug. A gateway timeout can happen after
            the provider accepted the charge, and retrying with the same idempotency
            key currently creates another local charge.

            Inspect the project, fix the bug, and run the relevant tests. Then give
            an engineering handoff with the root cause, files changed, validation
            command and result, and any remaining risk. Use exactly
            `python3 -m unittest -v` for validation. Answer in English.
            """
        ).strip(),
        check=("python3", "-m", "unittest", "-v"),
        expected_seed_failure="'ch_2' != 'ch_1'",
        production_paths=("ledger.py",),
        auxiliary_paths=("test_ledger.py",),
        worker=WORKERS / "python-payment-worker.py",
        worker_suffix=".py",
        test_reporter="unittest",
        canonical_test_count=1,
        canonical_test_ids=(
            "test_retry_with_same_key_does_not_create_second_remote_charge",
        ),
        hidden_cases=(
            HiddenCase(
                "timeout_retry_repeat",
                {"scenario": "timeout_retry_repeat"},
                {
                    "errors": ["GatewayTimeout"],
                    "replay_equal": True,
                    "remote_count": 1,
                    "local_count": 1,
                    "remote_keys": ["order-1"],
                    "local_keys": ["order-1"],
                    "remote_amounts": [5000],
                    "local_amounts": [5000],
                    "local_customers": ["cust_123"],
                    "provider_mapping_valid": True,
                },
            ),
            HiddenCase(
                "independent_second_key",
                {"scenario": "independent_second_key"},
                {
                    "errors": ["GatewayTimeout"],
                    "distinct_charge": True,
                    "remote_count": 2,
                    "local_count": 2,
                    "remote_keys": ["order-1", "order-2"],
                    "local_keys": ["order-1", "order-2"],
                    "remote_amounts": [5000, 5000],
                    "local_amounts": [5000, 5000],
                    "local_customers": ["cust_123", "cust_123"],
                    "provider_mapping_valid": True,
                },
            ),
            HiddenCase(
                "replay_old_after_new",
                {"scenario": "replay_old_after_new"},
                {
                    "errors": ["GatewayTimeout"],
                    "replay_equal": True,
                    "distinct_second": True,
                    "remote_count": 2,
                    "remote_keys": ["order-1", "order-2"],
                    "remote_amounts": [5000, 5000],
                },
            ),
        ),
    ),
    Project(
        key="sqlite-rollout-runner",
        title="SQLite rollout runner",
        task=textwrap.dedent(
            """
            We have an unsafe rollout order: the migration drops
            legacy_sessions.expires_at before the backup reads that column.

            Inspect the project, fix the bug, and run the relevant tests. Then give
            an engineering handoff with the root cause, files changed, validation
            command and result, and any remaining risk. Use exactly
            `python3 -m unittest -v` for validation. Answer in English.
            """
        ).strip(),
        check=("python3", "-m", "unittest", "-v"),
        expected_seed_failure="no such column: expires_at",
        production_paths=("rollout.py",),
        auxiliary_paths=("test_rollout.py",),
        worker=WORKERS / "sqlite-rollout-worker.py",
        worker_suffix=".py",
        test_reporter="unittest",
        canonical_test_count=1,
        canonical_test_ids=("test_backup_runs_before_drop_column_migration",),
        hidden_cases=(
            HiddenCase(
                "preserve_rows_and_note",
                {
                    "rows": [
                        [2, "u2", "2026-07-01T00:00:00Z", "second"],
                        [1, "u1", "2026-06-01T00:00:00Z", "first"],
                    ]
                },
                {
                    "backup": [
                        [1, "u1", "2026-06-01T00:00:00Z"],
                        [2, "u2", "2026-07-01T00:00:00Z"],
                    ],
                    "columns": ["id", "user_id", "note"],
                    "schema": [
                        {
                            "name": "id",
                            "type": "INTEGER",
                            "notnull": 0,
                            "default": None,
                            "pk": 1,
                        },
                        {
                            "name": "user_id",
                            "type": "TEXT",
                            "notnull": 1,
                            "default": None,
                            "pk": 0,
                        },
                        {
                            "name": "note",
                            "type": "TEXT",
                            "notnull": 1,
                            "default": None,
                            "pk": 0,
                        },
                    ],
                    "rows": [[1, "u1", "first"], [2, "u2", "second"]],
                },
            ),
        ),
    ),
)


class InfrastructureError(RuntimeError):
    """A live call could not produce a benchmark result."""


class IntegrityError(RuntimeError):
    """Saved evidence or preregistered configuration is inconsistent."""


@dataclass(frozen=True)
class BoundedExecution:
    process: subprocess.CompletedProcess[str]
    duration_ms: int
    timed_out: bool
    output_limited: bool
    workspace_limited: bool


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    payload = [
        {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}
        for path in files
    ]
    return sha256_text(canonical_json(payload))


def tracked_fixture_files(project: Project) -> tuple[Path, ...]:
    relative_root = project.root.relative_to(ROOT)
    process = subprocess.run(
        ("git", "ls-files", "-z", "--", str(relative_root)),
        cwd=ROOT,
        env=safe_environment(),
        capture_output=True,
        text=True,
        check=True,
    )
    files = tuple(
        sorted(ROOT / relative for relative in process.stdout.split("\0") if relative)
    )
    if not files:
        raise IntegrityError(f"fixture has no tracked files: {project.key}")
    for path in files:
        if (
            not path.is_relative_to(project.root)
            or not path.is_file()
            or path.is_symlink()
        ):
            raise IntegrityError(f"fixture tracked-file contract failed: {project.key}")
    return files


def fixture_sha256(project: Project) -> str:
    payload = [
        {
            "path": path.relative_to(project.root).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in tracked_fixture_files(project)
    ]
    return sha256_text(canonical_json(payload))


def copy_fixture(project: Project, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise IntegrityError("fixture destination must be empty")
    for source in tracked_fixture_files(project):
        target = destination / source.relative_to(project.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=mode
    )


def exclusive_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def atomic_copy(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env is not None:
        merged_env = dict(env)
        merged_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=merged_env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _child_resource_limits(*, cpu_seconds: int, file_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))


def _kill_and_reap_process_leader(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise InfrastructureError("cannot kill bounded process leader") from exc
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired as exc:
        raise InfrastructureError("bounded process leader did not exit") from exc


def _handle_process_group_permission_error(
    process: subprocess.Popen[bytes], *, action: str, error: PermissionError
) -> None:
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=0.2)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_and_reap_process_leader(process)
    raise InfrastructureError(f"cannot {action} bounded process group") from error


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        _kill_and_reap_process_leader(process)
        return
    except PermissionError as exc:
        _handle_process_group_permission_error(process, action="signal", error=exc)
        return
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            break
        except PermissionError as exc:
            _handle_process_group_permission_error(process, action="inspect", error=exc)
            return
        time.sleep(0.05)
    else:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            _handle_process_group_permission_error(process, action="kill", error=exc)
            return
        kill_deadline = time.monotonic() + 1
        while time.monotonic() < kill_deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                break
            except PermissionError as exc:
                _handle_process_group_permission_error(
                    process,
                    action="inspect killed",
                    error=exc,
                )
                return
            time.sleep(0.05)
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                _handle_process_group_permission_error(
                    process, action="kill", error=exc
                )
                return
            _kill_and_reap_process_leader(process)


def run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
    max_file_bytes: int,
    input_text: str | None = None,
    monitor_workspace: Path | None = None,
) -> BoundedExecution:
    stdout_descriptor, stdout_name = tempfile.mkstemp(prefix="run-stdout-")
    stderr_descriptor, stderr_name = tempfile.mkstemp(prefix="run-stderr-")
    stdout_path = Path(stdout_name)
    stderr_path = Path(stderr_name)
    started = time.monotonic()
    timed_out = False
    output_limited = False
    workspace_limited = False
    next_workspace_check = started
    process: subprocess.Popen[bytes] | None = None
    cleanup_started = False
    try:
        with (
            os.fdopen(stdout_descriptor, "wb") as stdout,
            os.fdopen(stderr_descriptor, "wb") as stderr,
        ):
            try:
                process = subprocess.Popen(
                    list(command),
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=cwd,
                    env=dict(env),
                    start_new_session=True,
                    preexec_fn=lambda: _child_resource_limits(
                        cpu_seconds=max(1, timeout_seconds + 5),
                        file_bytes=max_file_bytes,
                    ),
                )
            except OSError as exc:
                raise InfrastructureError(
                    f"cannot start bounded process: {command[0]}"
                ) from exc
            if process.stdin is not None:
                try:
                    if input_text is not None:
                        process.stdin.write(input_text.encode("utf-8"))
                        process.stdin.flush()
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > timeout_seconds:
                    timed_out = True
                    break
                stdout.flush()
                stderr.flush()
                if (
                    stdout_path.stat().st_size + stderr_path.stat().st_size
                    > max_output_bytes
                ):
                    output_limited = True
                    break
                if (
                    monitor_workspace is not None
                    and time.monotonic() >= next_workspace_check
                ):
                    try:
                        enforce_tree_caps(monitor_workspace)
                    except IntegrityError:
                        workspace_limited = True
                        break
                    next_workspace_check = time.monotonic() + 0.5
                time.sleep(0.05)
            cleanup_started = True
            _kill_process_group(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise InfrastructureError("bounded process was not reaped") from exc
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
        stdout_bytes = stdout_path.read_bytes()
        stderr_bytes = stderr_path.read_bytes()
        if len(stdout_bytes) + len(stderr_bytes) > max_output_bytes:
            output_limited = True
        if monitor_workspace is not None:
            try:
                enforce_tree_caps(monitor_workspace)
            except IntegrityError:
                workspace_limited = True
        completed = subprocess.CompletedProcess(
            list(command),
            process.returncode,
            stdout_bytes[:max_output_bytes].decode("utf-8", errors="replace"),
            stderr_bytes[:max_output_bytes].decode("utf-8", errors="replace"),
        )
        return BoundedExecution(
            completed,
            round((time.monotonic() - started) * 1000),
            timed_out,
            output_limited,
            workspace_limited,
        )
    finally:
        cleanup_error: BaseException | None = None
        if process is not None and not cleanup_started:
            try:
                _kill_process_group(process)
            except BaseException as exc:
                cleanup_error = exc
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        if cleanup_error is not None:
            raise cleanup_error


def safe_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
    for key in PROXY_ENV_KEYS:
        value = environment.get(key)
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or "@" in value:
            raise ValueError(f"{key} contains proxy credentials")
    environment.setdefault("PATH", os.defpath)
    environment["PATH"] = SAFE_PATH
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_inline_string_table(values: Mapping[str, str]) -> str:
    return (
        "{"
        + ",".join(
            f"{toml_string(key)}={toml_string(value)}"
            for key, value in sorted(values.items())
        )
        + "}"
    )


def minimal_protected_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    minimal: list[Path] = []
    for path in sorted(
        {item.resolve() for item in paths},
        key=lambda item: (len(item.parts), str(item)),
    ):
        if any(path == parent or path.is_relative_to(parent) for parent in minimal):
            continue
        minimal.append(path)
    return tuple(minimal)


def resolve_executable(executable: str) -> str:
    if os.sep in executable:
        resolved = Path(executable).expanduser().resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise FileNotFoundError(f"executable not found: {executable}")
        return str(resolved)
    resolved = shutil.which(executable, path=SAFE_PATH)
    if resolved is None:
        raise FileNotFoundError(f"executable not found on sealed PATH: {executable}")
    return str(Path(resolved).resolve())


def codex_version(executable: str) -> str:
    process = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=True,
        env=safe_environment(),
    )
    version = process.stdout.strip()
    if not version:
        raise RuntimeError("Codex CLI returned an empty version")
    return version


def source_git_provenance(repository: Path = ROOT) -> tuple[str, bool]:
    commit = subprocess.run(
        ("git", "rev-parse", "--verify", "HEAD"),
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        env=safe_environment(),
    ).stdout.strip()
    status = subprocess.run(
        (
            "git",
            "-c",
            "status.showUntrackedFiles=all",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        env=safe_environment(),
    ).stdout
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise RuntimeError("source Git commit is invalid")
    return commit, bool(status.strip())


def runtime_versions() -> dict[str, str]:
    def version(command: Sequence[str]) -> str:
        resolved = resolve_executable(command[0])
        process = subprocess.run(
            (resolved, *command[1:]),
            capture_output=True,
            text=True,
            check=True,
            env=safe_environment(),
        )
        value = (process.stdout or process.stderr).strip()
        if not value:
            raise RuntimeError(f"empty runtime version: {command[0]}")
        return value

    return {
        "platform": platform.platform(),
        "python": version(("python3", "--version")),
        "node": version(("node", "--version")),
        "npm": version(("npm", "--version")),
        "git": version(("git", "--version")),
        "sqlite": sqlite3.sqlite_version,
    }


def source_isolation_contract(
    repository: Path = ROOT, *, extra_protected: Sequence[Path] = ()
) -> SourceIsolation:
    if platform.system() != "Darwin":
        raise RuntimeError("live repo-quality runs require macOS Seatbelt")
    common_process = subprocess.run(
        ("git", "rev-parse", "--git-common-dir"),
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        env=safe_environment(),
    )
    common = Path(common_process.stdout.strip())
    if not common.is_absolute():
        common = repository / common
    common_repository = common.resolve().parent
    protected = tuple(
        dict.fromkeys(
            (
                Path.home().resolve(),
                repository.resolve(),
                common_repository,
                *(path.resolve() for path in extra_protected),
            )
        )
    )
    return SourceIsolation(protected)


def validation_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    scratch = root / "tmp"
    home.mkdir(parents=True, mode=0o700)
    scratch.mkdir(parents=True, mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "NPM_CONFIG_CACHE": str(scratch / "npm-cache"),
        "NPM_CONFIG_USERCONFIG": str(scratch / "npmrc"),
        "PATH": SAFE_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TEMP": str(scratch),
        "TMP": str(scratch),
        "TMPDIR": str(scratch),
    }


def validation_permission_config(protected_roots: Sequence[Path]) -> list[str]:
    filesystem = toml_inline_string_table(
        {str(path): "deny" for path in minimal_protected_paths(protected_roots)}
    )
    profile = (
        f'{{extends=":read-only",filesystem={filesystem},network={{enabled=false}}}}'
    )
    return [
        "--config",
        f"permissions.{VALIDATION_PERMISSION_PROFILE}={profile}",
    ]


def validation_sandbox_command(
    *,
    executable: str,
    workspace: Path,
    protected_roots: Sequence[Path],
    command: Sequence[str],
) -> list[str]:
    return [
        executable,
        *validation_permission_config(protected_roots),
        "sandbox",
        "-P",
        VALIDATION_PERMISSION_PROFILE,
        "--sandbox-state-disable-network",
        "-C",
        str(workspace),
        "--",
        *command,
    ]


def default_auth_file() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"


def ensure_fixture_contract(
    *,
    executable: str | None = None,
    source_isolation: SourceIsolation | None = None,
) -> dict[str, dict[str, Any]]:
    if (executable is None) != (source_isolation is None):
        raise ValueError("sealed fixture checks require executable and isolation")
    if len(PROJECTS) != 3 or len({project.key for project in PROJECTS}) != 3:
        raise RuntimeError("repo-quality lane requires exactly three unique projects")
    details: dict[str, dict[str, Any]] = {}
    for project in PROJECTS:
        if not project.root.is_dir() or not project.worker.is_file():
            raise FileNotFoundError(f"missing fixture or worker: {project.key}")
        if any(path.is_symlink() for path in project.root.rglob("*")):
            raise RuntimeError(f"fixture contains symlink: {project.key}")
        missing_production = [
            relative
            for relative in project.production_paths
            if not (project.root / relative).is_file()
        ]
        if missing_production:
            raise RuntimeError(
                f"missing allowed production path: {missing_production[0]}"
            )
        missing_auxiliary = [
            relative
            for relative in project.auxiliary_paths
            if not (project.root / relative).is_file()
        ]
        if missing_auxiliary:
            raise RuntimeError(f"missing allowed test path: {missing_auxiliary[0]}")
        if set(project.production_paths).intersection(project.auxiliary_paths):
            raise RuntimeError(f"overlapping allowed paths: {project.key}")
        if not project.hidden_cases or len(
            {case.case_id for case in project.hidden_cases}
        ) != len(project.hidden_cases):
            raise RuntimeError(f"hidden case ids are invalid: {project.key}")
        if any(
            {"schema_version", "case_id"}.intersection(case.request)
            for case in project.hidden_cases
        ):
            raise RuntimeError(f"hidden case request uses reserved keys: {project.key}")
        if executable is None or source_isolation is None:
            process = run(project.check, cwd=project.root)
        else:
            with tempfile.TemporaryDirectory(
                prefix="seed-check-", dir="/private/tmp"
            ) as temporary:
                root = Path(temporary)
                workspace = root / "workspace"
                copy_fixture(project, workspace)
                execution = run_validation_test(
                    executable=executable,
                    workspace=workspace,
                    protected_roots=source_isolation.protected_roots,
                    env=validation_environment(root),
                    command=project.check,
                )
                if (
                    execution.timed_out
                    or execution.output_limited
                    or execution.workspace_limited
                ):
                    raise RuntimeError(f"seed check exceeded caps: {project.key}")
                process = execution.process
        output = process.stdout + process.stderr
        if process.returncode == 0 or project.expected_seed_failure not in output:
            raise RuntimeError(f"unexpected canonical seed failure: {project.key}")
        details[project.key] = {
            "fixture_sha256": fixture_sha256(project),
            "worker_sha256": sha256_file(project.worker),
            "seed_check": list(project.check),
            "expected_seed_failure_sha256": sha256_text(project.expected_seed_failure),
            "production_paths": list(project.production_paths),
            "auxiliary_paths": list(project.auxiliary_paths),
            "allowed_paths": list(project.allowed_paths),
            "canonical_test_ids": list(project.canonical_test_ids),
            "hidden_cases": [
                {
                    "case_id": case.case_id,
                    "request_sha256": sha256_text(canonical_json(case.request)),
                    "expected_sha256": sha256_text(canonical_json(case.expected)),
                }
                for case in project.hidden_cases
            ],
        }
    return details


def build_arms(policy: str) -> dict[str, Arm]:
    if policy.count(RUNTIME_MARKER) != 1:
        raise ValueError(
            f"candidate policy must contain exactly one {RUNTIME_MARKER!r}"
        )
    return {
        "native_low": Arm("native_low", None),
        "candidate_runtime": Arm("candidate_runtime", policy),
    }


def _blind_digest(secret: str, purpose: str, value: str) -> bytes:
    if not secret:
        raise ValueError("schedule secret must not be empty")
    return hmac.new(
        secret.encode("utf-8"), f"{purpose}\0{value}".encode("utf-8"), hashlib.sha256
    ).digest()


def planned_run_keys() -> list[RunKey]:
    return [
        RunKey(project.key, arm, trial)
        for project in PROJECTS
        for trial in range(1, TRIALS + 1)
        for arm in ARMS
    ]


def secret_balanced_schedule(secret: str, keys: Sequence[RunKey]) -> list[RunKey]:
    if len(keys) != EXPECTED_CALLS or len(set(keys)) != EXPECTED_CALLS:
        raise ValueError(f"schedule requires exactly {EXPECTED_CALLS} unique runs")
    blocks: dict[tuple[str, int], dict[str, RunKey]] = {}
    for key in keys:
        blocks.setdefault((key.project, key.trial), {})[key.arm] = key
    if len(blocks) != EXPECTED_CALLS // 2 or any(
        set(block) != set(ARMS) for block in blocks.values()
    ):
        raise ValueError("each project/trial block must contain both arms")

    identities = [f"{project}\0{trial}" for project, trial in blocks]
    candidate_first = set(
        sorted(
            identities,
            key=lambda identity: _blind_digest(secret, "first-arm", identity),
        )[: len(identities) // 2]
    )
    ordered_identities = sorted(
        identities,
        key=lambda identity: _blind_digest(secret, "block-order", identity),
    )
    scheduled: list[RunKey] = []
    for identity in ordered_identities:
        project, raw_trial = identity.split("\0", 1)
        block = blocks[(project, int(raw_trial))]
        order = (
            ("candidate_runtime", "native_low")
            if identity in candidate_first
            else ("native_low", "candidate_runtime")
        )
        scheduled.extend(block[arm] for arm in order)
    return scheduled


def schedule_payload(
    *, run_id: str, config_sha256: str, schedule: Sequence[RunKey]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "config_sha256": config_sha256,
        "runs": [
            {"project": key.project, "arm": key.arm, "trial": key.trial}
            for key in schedule
        ],
    }


def quality_permission_config(
    isolated: IsolatedRun, source_isolation: SourceIsolation
) -> list[str]:
    denied_roots = {
        str(path): "deny"
        for path in minimal_protected_paths(source_isolation.protected_roots)
    }
    denied_roots[str(isolated.codex_home)] = "deny"
    filesystem = toml_inline_string_table(denied_roots)
    profile = (
        f'{{extends=":workspace",filesystem={filesystem},network={{enabled=false}}}}'
    )
    tool_environment = toml_inline_string_table(
        {
            "HOME": str(isolated.home),
            "PATH": SAFE_PATH,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "TEMP": str(isolated.root / "tmp"),
            "TMP": str(isolated.root / "tmp"),
            "TMPDIR": str(isolated.root / "tmp"),
        }
    )
    return [
        "--config",
        f"default_permissions={toml_string(QUALITY_PERMISSION_PROFILE)}",
        "--config",
        f"permissions.{QUALITY_PERMISSION_PROFILE}={profile}",
        "--config",
        'shell_environment_policy.inherit="none"',
        "--config",
        f"shell_environment_policy.set={tool_environment}",
        "--config",
        "allow_login_shell=false",
    ]


def build_codex_command(
    *,
    executable: str,
    model: str,
    effort: str,
    isolated: IsolatedRun,
    source_isolation: SourceIsolation,
) -> list[str]:
    command = [executable]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    command.extend(quality_permission_config(isolated, source_isolation))
    command.extend(
        (
            "--ask-for-approval",
            "never",
            "--model",
            model,
            "--config",
            f"model_reasoning_effort={toml_string(effort)}",
            "--config",
            f"model_verbosity={toml_string(MODEL_VERBOSITY)}",
            "--strict-config",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--cd",
            str(isolated.workspace),
            "-",
        )
    )
    return command


def build_preflight_command(
    *,
    executable: str,
    model: str,
    effort: str,
    prompt: str,
    isolated: IsolatedRun,
    source_isolation: SourceIsolation,
) -> list[str]:
    command = [executable]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    command.extend(quality_permission_config(isolated, source_isolation))
    command.extend(
        (
            "--config",
            f"model={toml_string(model)}",
            "--config",
            f"model_reasoning_effort={toml_string(effort)}",
            "--config",
            f"model_verbosity={toml_string(MODEL_VERBOSITY)}",
            "debug",
            "prompt-input",
            prompt,
        )
    )
    return command


@contextmanager
def isolated_run_environment(
    *, auth_source: Path, arm: Arm, auth_sink: Path | None = None
) -> Iterator[IsolatedRun]:
    with tempfile.TemporaryDirectory(prefix="matched-run-") as temporary:
        root = Path(temporary)
        home = root / "home"
        codex_home = root / "codex-home"
        workspace = root / "workspace"
        home.mkdir(mode=0o700)
        codex_home.mkdir(parents=True, mode=0o700)
        workspace.mkdir(mode=0o700)
        if not auth_source.is_file():
            raise FileNotFoundError("Codex auth file not found")
        auth_destination = codex_home / "auth.json"
        atomic_copy(auth_source, auth_destination)
        if arm.policy is not None:
            atomic_write_text(codex_home / "AGENTS.md", arm.policy)
        agents_files = list(root.rglob("AGENTS.md"))
        expected_agents = [] if arm.policy is None else [codex_home / "AGENTS.md"]
        if agents_files != expected_agents:
            raise RuntimeError("isolated arm has unexpected AGENTS.md files")
        environment = safe_environment()
        scratch = root / "tmp"
        scratch.mkdir(mode=0o700)
        environment.update(
            {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
            }
        )
        isolated = IsolatedRun(root, home, codex_home, workspace, environment)
        try:
            yield isolated
        finally:
            if auth_sink is not None and auth_destination.is_file():
                atomic_copy(auth_destination, auth_sink)


def initialize_workspace(project: Project, isolated: IsolatedRun) -> str:
    copy_fixture(project, isolated.workspace)
    if list(isolated.workspace.rglob("AGENTS.md")):
        raise RuntimeError("fixture contains unexpected AGENTS.md")
    return initialize_git_repository(isolated.workspace, isolated.env)


def quality_sandbox_command(
    *,
    executable: str,
    isolated: IsolatedRun,
    source_isolation: SourceIsolation,
    command: Sequence[str],
) -> list[str]:
    return [
        executable,
        *quality_permission_config(isolated, source_isolation),
        "sandbox",
        "-P",
        QUALITY_PERMISSION_PROFILE,
        "-C",
        str(isolated.workspace),
        "--",
        *command,
    ]


def verify_model_tool_isolation(
    *, executable: str, isolated: IsolatedRun, source_isolation: SourceIsolation
) -> None:
    marker = isolated.workspace / ".permission-probe"
    write_process = run(
        quality_sandbox_command(
            executable=executable,
            isolated=isolated,
            source_isolation=source_isolation,
            command=("/usr/bin/touch", str(marker)),
        ),
        cwd=isolated.workspace,
        env=isolated.env,
        timeout=10,
    )
    if write_process.returncode != 0 or not marker.is_file():
        raise InfrastructureError("tool profile cannot write the evaluation workspace")
    marker.unlink()

    auth_path = isolated.codex_home / "auth.json"
    auth_sha256 = sha256_file(auth_path)
    denied_targets = (auth_path, ROOT / "README.md")
    for target in denied_targets:
        read_process = run(
            quality_sandbox_command(
                executable=executable,
                isolated=isolated,
                source_isolation=source_isolation,
                command=("/bin/cat", str(target)),
            ),
            cwd=isolated.workspace,
            env=isolated.env,
            timeout=10,
        )
        denial = read_process.stdout + read_process.stderr
        if read_process.returncode == 0 or "Operation not permitted" not in denial:
            raise InfrastructureError("tool profile did not deny protected reads")
    if sha256_file(auth_path) != auth_sha256:
        raise IntegrityError("tool-profile probe changed Codex credentials")


def initialize_git_repository(workspace: Path, env: Mapping[str, str]) -> str:
    commands = (
        ("git", "init", "--quiet"),
        ("git", "add", "."),
        (
            "git",
            "-c",
            "user.name=Fixture Runner",
            "-c",
            "user.email=fixture-runner@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Initial snapshot",
        ),
    )
    for command in commands:
        process = run(command, cwd=workspace, env=env)
        if process.returncode != 0:
            raise RuntimeError(
                f"cannot initialize fixture Git repository: {process.stderr}"
            )
    baseline = run(("git", "rev-parse", "HEAD"), cwd=workspace, env=env)
    if baseline.returncode != 0 or not baseline.stdout.strip():
        raise RuntimeError("cannot resolve fixture baseline commit")
    return baseline.stdout.strip()


def _model_input_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise RuntimeError("prompt-input preflight did not return a message list")
    texts: list[str] = []
    for message in value:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    return texts


def preflight_model_input(
    *,
    executable: str,
    model: str,
    effort: str,
    project: Project,
    arm: Arm,
    isolated: IsolatedRun,
    source_isolation: SourceIsolation,
    timeout_seconds: int,
) -> None:
    command = build_preflight_command(
        executable=executable,
        model=model,
        effort=effort,
        prompt=project.task,
        isolated=isolated,
        source_isolation=source_isolation,
    )
    process = run(
        command,
        cwd=isolated.workspace,
        env=isolated.env,
        timeout=timeout_seconds,
    )
    if process.returncode != 0:
        raise RuntimeError(f"Codex prompt-input preflight failed: {process.stderr}")
    try:
        texts = _model_input_texts(json.loads(process.stdout))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Codex prompt-input preflight returned invalid JSON"
        ) from exc
    if not texts or texts[-1] != project.task or texts.count(project.task) != 1:
        raise RuntimeError(
            "prompt-input preflight did not preserve the exact task once"
        )
    context = "\n".join(texts[:-1])
    permission_markers = (
        "workspace-write",
        "Network access is restricted",
        str(isolated.codex_home),
        "Denied filesystem reads",
    )
    if any(marker not in context for marker in permission_markers):
        raise IntegrityError("prompt-input did not activate the sealed tool profile")
    if arm.policy is None:
        if RUNTIME_MARKER in context or re.search(
            r"\bsimple[ _-]+man\b", context, re.I
        ):
            raise RuntimeError(
                "native prompt-input is contaminated by candidate policy"
            )
    elif context.count(arm.policy) != 1 or context.count(RUNTIME_MARKER) != 1:
        raise RuntimeError("candidate prompt-input must contain the exact policy once")


def _run_with_atomic_stdout(
    command: Sequence[str],
    *,
    prompt: str,
    cwd: Path,
    env: Mapping[str, str],
    raw_path: Path,
    timeout_seconds: int,
    max_raw_bytes: int,
) -> BoundedExecution:
    execution = run_bounded(
        command,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_raw_bytes,
        max_file_bytes=max_raw_bytes,
        input_text=prompt,
        monitor_workspace=cwd,
    )
    atomic_write_text(raw_path, execution.process.stdout)
    return execution


def command_is_exact_test(command: str, expected: Sequence[str]) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if tokens == list(expected):
        return True
    if (
        len(tokens) == 3
        and Path(tokens[0]).name in {"sh", "bash", "zsh"}
        and tokens[1] in {"-c", "-lc", "--command"}
    ):
        return command_is_exact_test(tokens[2], expected)
    return False


def parse_codex_trace(
    path: Path, *, expected_test_command: Sequence[str], max_raw_bytes: int
) -> dict[str, Any]:
    size = path.stat().st_size
    if size > max_raw_bytes:
        raise ValueError(f"raw JSONL exceeds byte cap ({size} > {max_raw_bytes})")
    terminal_message: str | None = None
    usage: dict[str, int] = {}
    successful_commands: list[str] = []
    event_count = 0
    event_counts: dict[str, int] = {}
    allowed_events = {
        "thread.started",
        "turn.started",
        "item.started",
        "item.updated",
        "item.completed",
        "turn.completed",
    }
    allowed_items = {
        "agent_message",
        "command_execution",
        "file_change",
        "reasoning",
        "todo_list",
    }
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw JSONL line {line_number} is invalid JSON") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError(f"raw JSONL line {line_number} is not an event")
        if event["type"] not in allowed_events:
            raise ValueError(
                f"raw JSONL line {line_number} has disallowed event {event['type']}"
            )
        event_count += 1
        event_counts[event["type"]] = event_counts.get(event["type"], 0) + 1
        if event["type"].startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict):
                raise ValueError(f"raw JSONL line {line_number} has invalid item")
            if item.get("type") not in allowed_items:
                raise ValueError(
                    f"raw JSONL line {line_number} has disallowed item type"
                )
            terminal_message = None
        if event["type"] == "item.completed":
            if item.get("type") == "agent_message" and isinstance(
                item.get("text"), str
            ):
                terminal_message = item["text"]
            if (
                item.get("type") == "command_execution"
                and item.get("exit_code") == 0
                and item.get("status") in (None, "completed")
                and isinstance(item.get("command"), str)
            ):
                successful_commands.append(item["command"])
        elif event["type"] == "turn.completed":
            if event_counts["turn.completed"] > 1:
                raise ValueError("Codex JSONL has multiple turn.completed events")
            raw_usage = event.get("usage")
            if not isinstance(raw_usage, dict):
                raise ValueError("turn.completed has no usage object")
            usage = {
                key: int(value)
                for key, value in raw_usage.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    if terminal_message is None or not terminal_message.strip():
        raise ValueError("Codex JSONL must end with a final agent_message")
    if not usage:
        raise ValueError("Codex JSONL has no turn.completed usage")
    if (
        event_counts.get("thread.started") != 1
        or event_counts.get("turn.started") != 1
        or event_counts.get("turn.completed") != 1
    ):
        raise ValueError("Codex JSONL must have one thread and one turn")
    return {
        "answer": terminal_message,
        "usage": usage,
        "event_count": event_count,
        "successful_commands": successful_commands,
        "tests_invoked": any(
            command_is_exact_test(command, expected_test_command)
            for command in successful_commands
        ),
    }


def _checked_git(
    arguments: Sequence[str], *, workspace: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    process = run(("git", *arguments), cwd=workspace, env=env)
    if process.returncode != 0:
        raise RuntimeError(
            f"Git evidence command failed: {' '.join(arguments)}: {process.stderr}"
        )
    return process


def enforce_tree_caps(root: Path) -> dict[str, int]:
    entries = 0
    files = 0
    total_bytes = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            iterator_context = os.scandir(directory)
        except (FileNotFoundError, NotADirectoryError):
            if directory == root:
                raise IntegrityError("workspace root disappeared") from None
            continue
        with iterator_context as iterator:
            for entry in iterator:
                entries += 1
                if entries > MAX_FILES:
                    raise IntegrityError(f"workspace exceeds entry cap ({MAX_FILES})")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    raise IntegrityError("workspace contains a symlink")
                if stat.S_ISDIR(mode):
                    stack.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(mode):
                    raise IntegrityError("workspace contains a special file")
                files += 1
                if metadata.st_size > MAX_FILE_BYTES:
                    raise IntegrityError(
                        f"workspace file exceeds byte cap ({MAX_FILE_BYTES})"
                    )
                total_bytes += metadata.st_size
                if total_bytes > MAX_WORKSPACE_BYTES:
                    raise IntegrityError(
                        f"workspace exceeds byte cap ({MAX_WORKSPACE_BYTES})"
                    )
    return {"entries": entries, "files": files, "bytes": total_bytes}


def collect_repository_evidence(
    *, project: Project, workspace: Path, baseline: str, env: Mapping[str, str]
) -> dict[str, Any]:
    workspace_size = enforce_tree_caps(workspace)
    untracked_raw = _checked_git(
        ("ls-files", "--others", "--exclude-standard", "-z"),
        workspace=workspace,
        env=env,
    ).stdout
    untracked_paths = [path for path in untracked_raw.split("\0") if path]
    if untracked_paths:
        _checked_git(
            ("add", "--intent-to-add", "--", *untracked_paths),
            workspace=workspace,
            env=env,
        )
    full_patch = _checked_git(
        ("diff", "--binary", baseline, "--"), workspace=workspace, env=env
    ).stdout
    production_patch = _checked_git(
        ("diff", "--binary", baseline, "--", *project.production_paths),
        workspace=workspace,
        env=env,
    ).stdout
    tracked_raw = _checked_git(
        ("diff", "--name-only", "-z", baseline, "--"), workspace=workspace, env=env
    ).stdout
    changed_paths = sorted(
        set(path for path in (tracked_raw + untracked_raw).split("\0") if path)
    )
    diff_check = run(("git", "diff", "--check", baseline, "--"), cwd=workspace, env=env)
    if len(full_patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise IntegrityError(f"full patch exceeds byte cap ({MAX_PATCH_BYTES})")
    if len(production_patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise IntegrityError(f"production patch exceeds byte cap ({MAX_PATCH_BYTES})")
    return {
        "baseline_commit": baseline,
        "full_patch": full_patch,
        "production_patch": production_patch,
        "changed_paths": changed_paths,
        "paths_allowed": bool(changed_paths)
        and set(changed_paths).issubset(project.allowed_paths),
        "has_production_diff": bool(production_patch.strip()),
        "diff_check_exit": diff_check.returncode,
        "diff_check_output": diff_check.stdout + diff_check.stderr,
        "workspace_size": workspace_size,
    }


def _command_evidence(
    execution: BoundedExecution,
    command: Sequence[str],
    *,
    redactions: Sequence[str] = (),
) -> dict[str, Any]:
    def redact(value: str) -> str:
        for item in redactions:
            value = value.replace(item, "<sealed-artifact>")
        return value

    return {
        "command": list(command),
        "exit_code": execution.process.returncode,
        "stdout": redact(execution.process.stdout),
        "stderr": redact(execution.process.stderr),
        "duration_ms": execution.duration_ms,
        "timed_out": execution.timed_out,
        "output_limited": execution.output_limited,
        "workspace_limited": execution.workspace_limited,
    }


def test_run_completed(
    execution: BoundedExecution,
    *,
    reporter: str,
    expected_tests: int,
    expected_ids: Sequence[str],
) -> bool:
    if (
        execution.process.returncode != 0
        or execution.timed_out
        or execution.output_limited
        or execution.workspace_limited
    ):
        return False
    output = execution.process.stdout + "\n" + execution.process.stderr
    if reporter == "unittest":
        counts = re.findall(r"(?m)^Ran (\d+) tests? in ", output)
        observed_ids = re.findall(r"(?m)^([A-Za-z0-9_]+) \(", output)
        return (
            counts == [str(expected_tests)]
            and observed_ids == list(expected_ids)
            and bool(re.search(r"(?m)^OK(?: \([^\n]+\))?$", output))
        )
    if reporter == "node":

        def one(label: str) -> list[str]:
            return re.findall(rf"(?m)^(?:ℹ|#) {label} (\d+)$", output)

        observed_ids = re.findall(r"(?m)^✔ (.+?) \([0-9.]+ms\)$", output)
        return (
            one("tests") == [str(expected_tests)]
            and one("pass") == [str(expected_tests)]
            and one("fail") == ["0"]
            and one("cancelled") == ["0"]
            and observed_ids == list(expected_ids)
        )
    raise ValueError(f"unsupported test reporter: {reporter}")


def hidden_worker_command(project: Project, destination: Path) -> tuple[str, ...]:
    if project.test_reporter == "node":
        return ("node", destination.name)
    if project.test_reporter == "unittest":
        return ("python3", destination.name)
    raise ValueError(f"unsupported test reporter: {project.test_reporter}")


def run_validation_test(
    *,
    executable: str,
    workspace: Path,
    protected_roots: Sequence[Path],
    env: Mapping[str, str],
    command: Sequence[str],
    input_text: str | None = None,
) -> BoundedExecution:
    return run_bounded(
        validation_sandbox_command(
            executable=executable,
            workspace=workspace,
            protected_roots=protected_roots,
            command=command,
        ),
        cwd=workspace,
        env=env,
        timeout_seconds=VALIDATION_TIMEOUT_SECONDS,
        max_output_bytes=VALIDATION_OUTPUT_BYTES,
        max_file_bytes=MAX_FILE_BYTES,
        input_text=input_text,
        monitor_workspace=workspace,
    )


def hidden_case_evidence(
    *,
    execution: BoundedExecution,
    case: HiddenCase,
    worker: Path,
    worker_name: str,
    worker_command: Sequence[str],
) -> dict[str, Any]:
    observation: Any = None
    parse_error: str | None = None
    if (
        execution.process.returncode != 0
        or execution.timed_out
        or execution.output_limited
        or execution.workspace_limited
    ):
        parse_error = "worker did not complete cleanly"
    elif execution.process.stderr.strip():
        parse_error = "worker wrote to stderr"
    else:
        lines = execution.process.stdout.splitlines()
        if len(lines) != 1:
            parse_error = "worker must emit exactly one JSON line"
        else:
            try:
                envelope = json.loads(lines[0])
            except json.JSONDecodeError:
                parse_error = "worker emitted invalid JSON"
            else:
                if not isinstance(envelope, dict) or set(envelope) != {
                    "schema_version",
                    "case_id",
                    "observation",
                }:
                    parse_error = "worker envelope schema mismatch"
                elif envelope.get("schema_version") != 1:
                    parse_error = "worker envelope version mismatch"
                elif envelope.get("case_id") != case.case_id:
                    parse_error = "worker case id mismatch"
                else:
                    observation = envelope.get("observation")
    passed = parse_error is None and observation == case.expected
    return {
        "case_id": case.case_id,
        "request_sha256": sha256_text(canonical_json(case.request)),
        "expected_sha256": sha256_text(canonical_json(case.expected)),
        "worker_sha256": sha256_file(worker),
        "observation": observation,
        "parse_error": parse_error,
        "passed": passed,
        "execution": _command_evidence(
            execution,
            (*worker_command[:-1], "<sealed-worker>"),
            redactions=(worker_name,),
        ),
    }


def validate_production_patch(
    *,
    project: Project,
    production_patch: str,
    trace_tests_invoked: bool,
    repository_evidence: Mapping[str, Any],
    parent: Path,
    executable: str,
    source_isolation: SourceIsolation,
) -> dict[str, Any]:
    if len(production_patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise IntegrityError(f"production patch exceeds byte cap ({MAX_PATCH_BYTES})")
    canonical_root = parent / "canonical"
    if canonical_root.exists():
        raise IntegrityError("validation root must be fresh")
    copy_fixture(project, canonical_root)
    env = validation_environment(parent)
    apply_commands: list[subprocess.CompletedProcess[str]] = []
    canonical_apply = run(
        ("git", "apply", "--whitespace=nowarn", "-"),
        cwd=canonical_root,
        env=env,
        input_text=production_patch,
        timeout=10,
    )
    apply_commands.append(canonical_apply)
    enforce_tree_caps(canonical_root)

    protected_roots = source_isolation.protected_roots
    canonical = run_validation_test(
        executable=executable,
        workspace=canonical_root,
        protected_roots=protected_roots,
        env=env,
        command=project.check,
    )
    canonical_size = enforce_tree_caps(canonical_root)

    canonical_completed = test_run_completed(
        canonical,
        reporter=project.test_reporter,
        expected_tests=project.canonical_test_count,
        expected_ids=project.canonical_test_ids,
    )
    hidden_cases: list[dict[str, Any]] = []
    hidden_sizes: dict[str, dict[str, int]] = {}
    for index, case in enumerate(project.hidden_cases, 1):
        case_root = parent / f"case-{index:02d}"
        if case_root.exists():
            raise IntegrityError("hidden case root must be fresh")
        copy_fixture(project, case_root)
        applied = run(
            ("git", "apply", "--whitespace=nowarn", "-"),
            cwd=case_root,
            env=env,
            input_text=production_patch,
            timeout=10,
        )
        apply_commands.append(applied)
        worker_name = f"._case_{secrets.token_hex(16)}{project.worker_suffix}"
        worker_destination = case_root / worker_name
        shutil.copyfile(project.worker, worker_destination)
        command = hidden_worker_command(project, worker_destination)
        request = {"schema_version": 1, "case_id": case.case_id, **case.request}
        execution = run_validation_test(
            executable=executable,
            workspace=case_root,
            protected_roots=protected_roots,
            env=env,
            command=command,
            input_text=canonical_json(request) + "\n",
        )
        hidden_cases.append(
            hidden_case_evidence(
                execution=execution,
                case=case,
                worker=project.worker,
                worker_name=worker_name,
                worker_command=command,
            )
        )
        hidden_sizes[case.case_id] = enforce_tree_caps(case_root)

    hidden_completed = all(case["passed"] for case in hidden_cases)
    patches_applied = all(process.returncode == 0 for process in apply_commands)

    checks = {
        "production_patch_applied": patches_applied,
        "production_diff_present": bool(repository_evidence["has_production_diff"]),
        "paths_allowed": bool(repository_evidence["paths_allowed"]),
        "diff_check_passed": repository_evidence["diff_check_exit"] == 0,
        "tests_invoked_in_trace": trace_tests_invoked,
        "canonical_tests_restored": True,
        "canonical_tests_passed": canonical_completed,
        "worker_injected_after_codex": True,
        "hidden_cases_passed": hidden_completed,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "apply": [
            {
                "command": ["git", "apply", "-"],
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
            }
            for process in apply_commands
        ],
        "canonical": _command_evidence(canonical, project.check),
        "hidden_cases": hidden_cases,
        "test_counts": {
            "canonical": project.canonical_test_count,
            "hidden": len(project.hidden_cases),
        },
        "workspace_size": {"canonical": canonical_size, "hidden": hidden_sizes},
        "validation_source": (
            "one pristine copy per canonical/hidden case plus production-only patch; "
            "read-only isolated workers; parent-owned expected observations"
        ),
    }


def replay_repository_evidence(
    *,
    project: Project,
    full_patch: str,
    expected_production_patch: str,
    parent: Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    if len(full_patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise IntegrityError(f"full patch exceeds byte cap ({MAX_PATCH_BYTES})")
    workspace = parent / "repository-replay"
    if workspace.exists():
        shutil.rmtree(workspace)
    copy_fixture(project, workspace)
    baseline = initialize_git_repository(workspace, env)
    applied = run(
        ("git", "apply", "--whitespace=nowarn", "-"),
        cwd=workspace,
        env=env,
        input_text=full_patch,
    )
    if applied.returncode != 0:
        raise RuntimeError(f"resume full patch cannot be replayed: {applied.stderr}")
    evidence = collect_repository_evidence(
        project=project,
        workspace=workspace,
        baseline=baseline,
        env=env,
    )
    replayed_full = str(evidence.pop("full_patch"))
    replayed_production = str(evidence.pop("production_patch"))
    if replayed_full != full_patch:
        raise RuntimeError(
            "resume full patch is not canonical for the pristine fixture"
        )
    if replayed_production != expected_production_patch:
        raise RuntimeError(
            "resume production patch differs from replayed repository state"
        )
    return evidence


def result_identity(
    *,
    benchmark_run_id: str,
    config_sha256: str,
    key: RunKey,
    project: Project,
    arm: Arm,
    model: str,
    effort: str,
    cli_version: str,
    runner_sha256: str,
    fixture_sha256: str,
    worker_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_run_id": benchmark_run_id,
        "config_sha256": config_sha256,
        "project": key.project,
        "arm": key.arm,
        "trial": key.trial,
        "model": model,
        "effort": effort,
        "model_verbosity": MODEL_VERBOSITY,
        "prompt_sha256": sha256_text(project.task),
        "policy_sha256": arm.policy_sha256,
        "fixture_sha256": fixture_sha256,
        "worker_sha256": worker_sha256,
        "codex_cli_version": cli_version,
        "execution_contract_sha256": sha256_text(canonical_json(EXECUTION_CONTRACT)),
        "runner_sha256": runner_sha256,
    }


def private_run_id(config_sha256: str, key: RunKey) -> str:
    return (
        "quality_"
        + sha256_text(
            canonical_json(
                {
                    "config_sha256": config_sha256,
                    "project": key.project,
                    "arm": key.arm,
                    "trial": key.trial,
                }
            )
        )[:24]
    )


def attempt_paths(private: Path, private_id: str) -> AttemptPaths:
    root = private / "attempts" / private_id / "01"
    return AttemptPaths(
        root=root,
        started=root / "started.json",
        outcome=root / "outcome.json",
        raw=root / "raw.jsonl",
        stderr=root / "stderr.txt",
        full_patch=root / "full.diff",
        production_patch=root / "production.diff",
        result=root / "result.json",
    )


def validate_attempt_inventory(private: Path, expected_ids: Sequence[str]) -> None:
    attempts_root = private / "attempts"
    if not attempts_root.exists():
        return
    expected = set(expected_ids)
    observed: set[str] = set()
    for entry in attempts_root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.name not in expected:
            raise IntegrityError("attempt inventory contains an unexpected entry")
        observed.add(entry.name)
        children = list(entry.iterdir())
        if len(children) != 1 or children[0].name != "01" or not children[0].is_dir():
            raise IntegrityError("attempt inventory has an invalid ordinal")
    if not observed.issubset(expected):
        raise IntegrityError("attempt inventory differs from schedule")


def existing_artifact_hashes(paths: AttemptPaths) -> dict[str, str]:
    artifacts = {
        "raw": paths.raw,
        "stderr": paths.stderr,
        "full_patch": paths.full_patch,
        "production_patch": paths.production_patch,
        "result": paths.result,
    }
    return {
        name: sha256_file(path) for name, path in artifacts.items() if path.is_file()
    }


def start_attempt(paths: AttemptPaths, *, identity: Mapping[str, Any]) -> None:
    exclusive_write_json(
        paths.started,
        {
            "schema_version": SCHEMA_VERSION,
            "attempt": 1,
            "started_at": utc_now(),
            "identity": dict(identity),
        },
    )


def finish_attempt(
    paths: AttemptPaths,
    *,
    status: str,
    error: BaseException | None = None,
) -> None:
    if status not in {"success", "infra_error", "interrupted", "error"}:
        raise ValueError("invalid attempt outcome")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "attempt": 1,
        "completed_at": utc_now(),
        "status": status,
        "started_sha256": sha256_file(paths.started),
        "artifacts": existing_artifact_hashes(paths),
    }
    if error is not None:
        payload["error"] = {
            "class": type(error).__name__,
            "message": str(error)[:500],
        }
    exclusive_write_json(paths.outcome, payload)


def validate_attempt_envelope(
    paths: AttemptPaths, *, identity: Mapping[str, Any]
) -> str | None:
    if paths.root.exists():
        allowed = {
            paths.started.name,
            paths.outcome.name,
            paths.raw.name,
            paths.stderr.name,
            paths.full_patch.name,
            paths.production_patch.name,
            paths.result.name,
        }
        if any(
            entry.name not in allowed or entry.is_dir() or entry.is_symlink()
            for entry in paths.root.iterdir()
        ):
            raise IntegrityError("attempt contains an unexpected artifact")
    if not paths.started.exists():
        unexpected = [
            path
            for path in (
                paths.outcome,
                paths.raw,
                paths.stderr,
                paths.full_patch,
                paths.production_patch,
                paths.result,
            )
            if path.exists()
        ]
        if unexpected:
            raise IntegrityError("attempt artifacts exist without started record")
        return None
    try:
        started = json.loads(paths.started.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("attempt started record is unreadable") from exc
    if (
        not isinstance(started, dict)
        or set(started) != {"schema_version", "attempt", "started_at", "identity"}
        or started.get("schema_version") != SCHEMA_VERSION
        or started.get("attempt") != 1
        or started.get("identity") != dict(identity)
    ):
        raise IntegrityError("attempt started record mismatch")
    if not paths.outcome.exists():
        finish_attempt(
            paths,
            status="interrupted",
            error=InfrastructureError("started attempt has no terminal outcome"),
        )
        return "interrupted"
    try:
        outcome = json.loads(paths.outcome.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("attempt outcome is unreadable") from exc
    required = {
        "schema_version",
        "attempt",
        "completed_at",
        "status",
        "started_sha256",
        "artifacts",
    }
    if not isinstance(outcome, dict) or set(outcome) not in (
        required,
        required | {"error"},
    ):
        raise IntegrityError("attempt outcome schema mismatch")
    if (
        outcome.get("schema_version") != SCHEMA_VERSION
        or outcome.get("attempt") != 1
        or outcome.get("started_sha256") != sha256_file(paths.started)
        or outcome.get("status")
        not in {"success", "infra_error", "interrupted", "error"}
        or outcome.get("artifacts") != existing_artifact_hashes(paths)
    ):
        raise IntegrityError("attempt outcome evidence mismatch")
    if outcome["status"] == "success" and not paths.result.is_file():
        raise IntegrityError("successful attempt has no result")
    return str(outcome["status"])


def load_resumable_result(
    *,
    result_path: Path,
    raw_path: Path,
    stderr_path: Path,
    full_patch_path: Path,
    production_patch_path: Path,
    expected_identity: Mapping[str, Any],
    project: Project,
    max_raw_bytes: int,
    validation_parent: Path,
    env: Mapping[str, str],
    executable: str,
    source_isolation: SourceIsolation,
) -> dict[str, Any] | None:
    if not result_path.exists():
        return None
    result = json.loads(result_path.read_text(encoding="utf-8"))
    mismatches = [
        key for key, value in expected_identity.items() if result.get(key) != value
    ]
    if mismatches:
        raise RuntimeError(f"resume result identity mismatch: {', '.join(mismatches)}")
    for path, field in (
        (raw_path, "raw_sha256"),
        (stderr_path, "stderr_sha256"),
        (full_patch_path, "full_patch_sha256"),
        (production_patch_path, "production_patch_sha256"),
    ):
        if not path.is_file() or result.get(field) != sha256_file(path):
            raise RuntimeError(f"resume artifact hash mismatch: {path.name}")
    trace = parse_codex_trace(
        raw_path,
        expected_test_command=project.check,
        max_raw_bytes=max_raw_bytes,
    )
    if trace["answer"] != result.get("answer") or trace["usage"] != result.get("usage"):
        raise RuntimeError("resume trace content differs from result")
    repository = replay_repository_evidence(
        project=project,
        full_patch=full_patch_path.read_text(encoding="utf-8"),
        expected_production_patch=production_patch_path.read_text(encoding="utf-8"),
        parent=validation_parent,
        env=env,
    )
    validation = validate_production_patch(
        project=project,
        production_patch=production_patch_path.read_text(encoding="utf-8"),
        trace_tests_invoked=bool(trace["tests_invoked"]),
        repository_evidence=repository,
        parent=validation_parent,
        executable=executable,
        source_isolation=source_isolation,
    )
    stored_repository = result.get("repository")
    if not isinstance(stored_repository, dict):
        raise IntegrityError("resume result has no repository evidence")
    deterministic_repository_keys = {
        "changed_paths",
        "paths_allowed",
        "has_production_diff",
        "diff_check_exit",
        "diff_check_output",
    }
    if {key: stored_repository.get(key) for key in deterministic_repository_keys} != {
        key: repository.get(key) for key in deterministic_repository_keys
    }:
        raise IntegrityError("resume repository evidence differs from artifacts")
    if (
        result.get("trace_event_count") != trace["event_count"]
        or result.get("trace_successful_commands") != trace["successful_commands"]
    ):
        raise IntegrityError("resume trace evidence differs from raw JSONL")

    def validation_projection(value: Mapping[str, Any]) -> dict[str, Any]:
        hidden_cases = []
        for case in value.get("hidden_cases") or []:
            execution = case.get("execution") or {}
            hidden_cases.append(
                {
                    "case_id": case.get("case_id"),
                    "request_sha256": case.get("request_sha256"),
                    "expected_sha256": case.get("expected_sha256"),
                    "worker_sha256": case.get("worker_sha256"),
                    "observation": case.get("observation"),
                    "parse_error": case.get("parse_error"),
                    "passed": case.get("passed"),
                    "exit_code": execution.get("exit_code"),
                    "timed_out": execution.get("timed_out"),
                    "output_limited": execution.get("output_limited"),
                    "workspace_limited": execution.get("workspace_limited"),
                }
            )
        return {
            "passed": value.get("passed"),
            "checks": value.get("checks"),
            "test_counts": value.get("test_counts"),
            "canonical_exit": (value.get("canonical") or {}).get("exit_code"),
            "canonical_timeout": (value.get("canonical") or {}).get("timed_out"),
            "canonical_output_limited": (value.get("canonical") or {}).get(
                "output_limited"
            ),
            "canonical_workspace_limited": (value.get("canonical") or {}).get(
                "workspace_limited"
            ),
            "hidden_cases": hidden_cases,
        }

    stored_validation = result.get("validation")
    if not isinstance(stored_validation, dict) or validation_projection(
        stored_validation
    ) != validation_projection(validation):
        raise IntegrityError("resume validation verdict differs from sealed replay")
    reconstructed = dict(result)
    reconstructed["validation"] = validation
    reconstructed["repository"] = repository
    return reconstructed


def execute_run(
    *,
    executable: str,
    auth_source: Path,
    auth_sink: Path,
    project: Project,
    arm: Arm,
    key: RunKey,
    identity: Mapping[str, Any],
    model: str,
    effort: str,
    raw_path: Path,
    stderr_path: Path,
    full_patch_path: Path,
    production_patch_path: Path,
    result_path: Path,
    timeout_seconds: int,
    max_raw_bytes: int,
    source_isolation: SourceIsolation,
) -> dict[str, Any]:
    with isolated_run_environment(
        auth_source=auth_source, arm=arm, auth_sink=auth_sink
    ) as isolated:
        baseline = initialize_workspace(project, isolated)
        verify_model_tool_isolation(
            executable=executable,
            isolated=isolated,
            source_isolation=source_isolation,
        )
        preflight_model_input(
            executable=executable,
            model=model,
            effort=effort,
            project=project,
            arm=arm,
            isolated=isolated,
            source_isolation=source_isolation,
            timeout_seconds=timeout_seconds,
        )
        command = build_codex_command(
            executable=executable,
            model=model,
            effort=effort,
            isolated=isolated,
            source_isolation=source_isolation,
        )
        execution = _run_with_atomic_stdout(
            command,
            prompt=project.task,
            cwd=isolated.workspace,
            env=isolated.env,
            raw_path=raw_path,
            timeout_seconds=timeout_seconds,
            max_raw_bytes=max_raw_bytes,
        )
        atomic_write_text(stderr_path, execution.process.stderr)
        if execution.timed_out:
            raise InfrastructureError("Codex run exceeded the wall-clock timeout")
        if execution.output_limited:
            raise InfrastructureError("Codex run exceeded the raw-output cap")
        if execution.workspace_limited:
            raise InfrastructureError("Codex run exceeded the workspace cap")
        if execution.process.returncode != 0:
            raise InfrastructureError(
                f"Codex run failed: {key.project}/{key.arm}/{key.trial}, "
                f"exit {execution.process.returncode}"
            )
        trace = parse_codex_trace(
            raw_path,
            expected_test_command=project.check,
            max_raw_bytes=max_raw_bytes,
        )
        repository = collect_repository_evidence(
            project=project,
            workspace=isolated.workspace,
            baseline=baseline,
            env=isolated.env,
        )
        atomic_write_text(full_patch_path, str(repository.pop("full_patch")))
        atomic_write_text(
            production_patch_path, str(repository.pop("production_patch"))
        )
    with tempfile.TemporaryDirectory(
        prefix="validation-", dir="/private/tmp"
    ) as validation_temporary:
        validation = validate_production_patch(
            project=project,
            production_patch=production_patch_path.read_text(encoding="utf-8"),
            trace_tests_invoked=bool(trace["tests_invoked"]),
            repository_evidence=repository,
            parent=Path(validation_temporary),
            executable=executable,
            source_isolation=source_isolation,
        )
    result = {
        **identity,
        "answer": trace["answer"],
        "usage": trace["usage"],
        "trace_event_count": trace["event_count"],
        "trace_successful_commands": trace["successful_commands"],
        "duration_ms": execution.duration_ms,
        "raw_sha256": sha256_file(raw_path),
        "stderr_sha256": sha256_file(stderr_path),
        "full_patch_sha256": sha256_file(full_patch_path),
        "production_patch_sha256": sha256_file(production_patch_path),
        "repository": repository,
        "validation": validation,
    }
    exclusive_write_json(result_path, result)
    return result


def gate_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    expected = len(PROJECTS) * TRIALS
    if len(results) != EXPECTED_CALLS:
        raise ValueError(f"gate requires {EXPECTED_CALLS} results")
    expected_keys = set(planned_run_keys())
    try:
        observed_keys = {
            RunKey(str(result["project"]), str(result["arm"]), int(result["trial"]))
            for result in results
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("gate result identity is invalid") from exc
    if observed_keys != expected_keys or len(observed_keys) != len(results):
        raise ValueError("gate result identities are incomplete or duplicated")
    counts = {
        arm: sum(
            result.get("arm") == arm
            and bool((result.get("validation") or {}).get("passed"))
            for result in results
        )
        for arm in ARMS
    }
    totals = {arm: sum(result.get("arm") == arm for result in results) for arm in ARMS}
    if totals != {arm: expected for arm in ARMS}:
        raise ValueError("gate result arms are incomplete")
    if counts["native_low"] < expected:
        status = "INCONCLUSIVE"
        reason = "native control did not pass all repo-quality runs"
        exit_code = 2
    elif counts["candidate_runtime"] < expected:
        status = "FAIL"
        reason = "candidate failed while native control passed"
        exit_code = 1
    else:
        status = "PASS"
        reason = "both arms passed all repo-quality runs"
        exit_code = 0
    return {
        "status": status,
        "reason": reason,
        "exit_code": exit_code,
        "required_per_arm": expected,
        "passed": counts,
        "total": totals,
    }


def reported_token_count(result: Mapping[str, Any]) -> int:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise IntegrityError("result has no usage object")
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    values = []
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise IntegrityError(f"result usage has invalid {key}")
        values.append(value)
    return sum(values)


def config_payload(
    *,
    args: argparse.Namespace,
    arms: Mapping[str, Arm],
    cli_version: str,
    runner_sha256: str,
    fixtures: Mapping[str, Any],
    source_git_commit: str,
    source_git_dirty: bool,
    runtimes: Mapping[str, str],
    source_isolation: SourceIsolation,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_contract": EXECUTION_CONTRACT,
        "execution_contract_sha256": sha256_text(canonical_json(EXECUTION_CONTRACT)),
        "model": args.model,
        "effort": args.effort,
        "model_verbosity": MODEL_VERBOSITY,
        "codex_cli_version": cli_version,
        "codex_executable": args.codex,
        "codex_executable_sha256": sha256_file(Path(args.codex)),
        "runner_sha256": runner_sha256,
        "source_git_commit": source_git_commit,
        "source_git_dirty": source_git_dirty,
        "runtime_versions": dict(runtimes),
        "source_isolation": source_isolation.metadata,
        "validation_isolation": {
            "permission_profile": VALIDATION_PERMISSION_PROFILE,
            "protected_roots": [str(path) for path in source_isolation.protected_roots],
            "network": "disabled",
            "environment": "fresh HOME and TMP; no CODEX_HOME, auth, or proxy variables",
        },
        "limits": {
            "timeout_seconds": args.timeout_seconds,
            "max_calls": args.max_calls,
            "max_input_chars_per_call": args.max_input_chars_per_call,
            "max_total_input_chars": args.max_total_input_chars,
            "max_raw_bytes_per_call": args.max_raw_bytes_per_call,
            "max_total_reported_tokens": args.max_total_reported_tokens,
        },
        "projects": [
            {
                "key": project.key,
                "prompt": project.task,
                **fixtures[project.key],
            }
            for project in PROJECTS
        ],
        "arms": [
            {"name": arm.name, "policy_sha256": arm.policy_sha256}
            for arm in arms.values()
        ],
    }


def load_or_create_manifest(path: Path, *, config: Mapping[str, Any]) -> dict[str, Any]:
    config_sha256 = sha256_text(canonical_json(config))
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError("private manifest is unreadable") from exc
        required = {
            "schema_version",
            "run_id",
            "created_at",
            "schedule_secret",
            "config_sha256",
            "config",
        }
        allowed = required | {"schedule_sha256"}
        if not isinstance(manifest, dict) or set(manifest) not in (required, allowed):
            raise IntegrityError("private manifest schema is not exact")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise IntegrityError("private manifest schema version mismatch")
        if not re.fullmatch(r"quality_[0-9a-f]{32}", str(manifest.get("run_id"))):
            raise IntegrityError("private manifest run id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("schedule_secret"))):
            raise IntegrityError("private manifest schedule secret is invalid")
        try:
            dt.datetime.fromisoformat(str(manifest.get("created_at")))
        except ValueError as exc:
            raise IntegrityError("private manifest timestamp is invalid") from exc
        if manifest.get("config") != dict(config):
            raise IntegrityError("private manifest embedded config mismatch")
        embedded_hash = sha256_text(canonical_json(manifest["config"]))
        if (
            embedded_hash != config_sha256
            or manifest.get("config_sha256") != config_sha256
        ):
            raise IntegrityError(
                "output directory belongs to a different repo-quality config"
            )
        if "schedule_sha256" in manifest and not re.fullmatch(
            r"[0-9a-f]{64}", str(manifest["schedule_sha256"])
        ):
            raise IntegrityError("private manifest schedule hash is invalid")
        return manifest
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "quality_" + uuid.uuid4().hex,
        "created_at": utc_now(),
        "schedule_secret": secrets.token_hex(32),
        "config_sha256": config_sha256,
        "config": dict(config),
    }
    exclusive_write_json(path, manifest)
    return manifest


def load_or_create_schedule(
    path: Path, *, manifest_path: Path, manifest: dict[str, Any]
) -> list[RunKey]:
    secret = manifest.get("schedule_secret")
    run_id = manifest.get("run_id")
    config_sha256 = manifest.get("config_sha256")
    if not all(
        isinstance(value, str) and value for value in (secret, run_id, config_sha256)
    ):
        raise IntegrityError("private manifest is incomplete")
    schedule = secret_balanced_schedule(str(secret), planned_run_keys())
    payload = schedule_payload(
        run_id=str(run_id), config_sha256=str(config_sha256), schedule=schedule
    )
    payload_sha256 = sha256_text(canonical_json(payload))
    committed_sha256 = manifest.get("schedule_sha256")
    if committed_sha256 is not None and committed_sha256 != payload_sha256:
        raise IntegrityError("private manifest schedule hash mismatch")
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise IntegrityError("private execution schedule mismatch")
    else:
        exclusive_write_json(path, payload)
    if committed_sha256 is None:
        manifest["schedule_sha256"] = payload_sha256
        atomic_write_json(manifest_path, manifest)
    return schedule


@contextmanager
def output_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    lock = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "another repo-quality run is using this output directory"
            ) from exc
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the hermetic Simple Man repo-quality gate."
    )
    parser.add_argument("--candidate-policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--auth-file", type=Path, default=default_auth_file())
    parser.add_argument("--codex", default=os.environ.get("CODEX", "codex"))
    parser.add_argument("--model", default=os.environ.get("MODEL"))
    parser.add_argument("--effort", default=os.environ.get("EFFORT", "high"))
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--max-calls", type=int, default=EXPECTED_CALLS)
    parser.add_argument("--max-input-chars-per-call", type=int, default=20_000)
    parser.add_argument("--max-total-input-chars", type=int, default=200_000)
    parser.add_argument("--max-raw-bytes-per-call", type=int, default=10_000_000)
    parser.add_argument("--max-total-reported-tokens", type=int, default=1_500_000)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model is required (or set MODEL)")
    for name in (
        "timeout_seconds",
        "max_calls",
        "max_input_chars_per_call",
        "max_total_input_chars",
        "max_raw_bytes_per_call",
        "max_total_reported_tokens",
    ):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be >= 1")
    return args


def _validate_caps(args: argparse.Namespace, arms: Mapping[str, Arm]) -> dict[str, int]:
    planned = planned_run_keys()
    if len(planned) > args.max_calls:
        raise ValueError(
            f"planned calls exceed --max-calls ({len(planned)} > {args.max_calls})"
        )
    by_project = {project.key: project for project in PROJECTS}
    sizes = [
        len(by_project[key.project].task) + len(arms[key.arm].policy or "")
        for key in planned
    ]
    if max(sizes) > args.max_input_chars_per_call:
        raise ValueError("planned input exceeds --max-input-chars-per-call")
    if sum(sizes) > args.max_total_input_chars:
        raise ValueError("planned input exceeds --max-total-input-chars")
    return {
        "calls": len(planned),
        "max_input_chars": max(sizes),
        "total_input_chars": sum(sizes),
    }


def write_inconclusive_summary(
    *,
    output_dir: Path,
    run_id: str,
    config_sha256: str,
    args: argparse.Namespace,
    reason: str,
    phase: str,
    attempts: Sequence[Mapping[str, Any]],
) -> int:
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "config_sha256": config_sha256,
        "completed_at": utc_now(),
        "model": args.model,
        "effort": args.effort,
        "model_verbosity": MODEL_VERBOSITY,
        "calls": len(attempts),
        "gate": {
            "status": "INCONCLUSIVE",
            "reason": reason,
            "exit_code": 2,
            "phase": phase,
        },
        "attempts": list(attempts),
    }
    atomic_write_json(output_dir / "gate-summary.json", summary)
    print(f"INCONCLUSIVE: {reason}")
    return 2


def _main(args: argparse.Namespace) -> int:
    source_commit, source_dirty = source_git_provenance()
    if not args.dry_run and source_dirty:
        raise RuntimeError("live repo-quality run requires a clean source Git checkout")
    args.codex = resolve_executable(args.codex)
    source_isolation = source_isolation_contract(
        extra_protected=(args.output_dir, args.auth_file)
    )
    fixtures = ensure_fixture_contract(
        executable=args.codex, source_isolation=source_isolation
    )
    policy = args.candidate_policy.read_text(encoding="utf-8")
    arms = build_arms(policy)
    caps = _validate_caps(args, arms)
    cli_version = codex_version(args.codex)
    runner_sha256 = sha256_text(Path(__file__).read_text(encoding="utf-8"))
    runtimes = runtime_versions()
    config = config_payload(
        args=args,
        arms=arms,
        cli_version=cli_version,
        runner_sha256=runner_sha256,
        fixtures=fixtures,
        source_git_commit=source_commit,
        source_git_dirty=source_dirty,
        runtimes=runtimes,
        source_isolation=source_isolation,
    )
    config_sha256 = sha256_text(canonical_json(config))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "config_sha256": config_sha256,
                    "projects": len(PROJECTS),
                    "arms": list(ARMS),
                    "trials": TRIALS,
                    **caps,
                    "model": args.model,
                    "effort": args.effort,
                    "model_verbosity": MODEL_VERBOSITY,
                    "codex_cli_version": cli_version,
                    "source_git_commit": source_commit,
                    "source_git_dirty": source_dirty,
                    "runtime_versions": runtimes,
                    "source_isolation": source_isolation.metadata,
                    "output_dir": str(args.output_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.auth_file.is_file():
        raise FileNotFoundError("Codex auth file not found")
    private = args.output_dir / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private, 0o700)
    manifest_path = private / "manifest.json"
    manifest = load_or_create_manifest(manifest_path, config=config)
    schedule = load_or_create_schedule(
        private / "execution-schedule.json",
        manifest_path=manifest_path,
        manifest=manifest,
    )
    expected_attempt_ids = [private_run_id(config_sha256, key) for key in schedule]
    validate_attempt_inventory(private, expected_attempt_ids)
    run_id = str(manifest["run_id"])
    projects = {project.key: project for project in PROJECTS}
    total = len(schedule)
    attempt_summaries: list[dict[str, Any]] = []
    cumulative_reported_tokens = 0

    if not args.resume:
        started = list((private / "attempts").glob("*/01/started.json"))
        if started:
            raise RuntimeError(
                "attempts already exist; enable resume or use another output"
            )

    with tempfile.TemporaryDirectory(prefix=".auth-", dir=private) as auth_temporary:
        auth_cache = Path(auth_temporary) / "auth.json"
        atomic_copy(args.auth_file, auth_cache)
        for index, key in enumerate(schedule, 1):
            project = projects[key.project]
            arm = arms[key.arm]
            private_id = private_run_id(config_sha256, key)
            paths = attempt_paths(private, private_id)
            identity = result_identity(
                benchmark_run_id=run_id,
                config_sha256=config_sha256,
                key=key,
                project=project,
                arm=arm,
                model=args.model,
                effort=args.effort,
                cli_version=cli_version,
                runner_sha256=runner_sha256,
                fixture_sha256=str(fixtures[key.project]["fixture_sha256"]),
                worker_sha256=str(fixtures[key.project]["worker_sha256"]),
            )
            status = validate_attempt_envelope(paths, identity=identity)
            if status in {"infra_error", "interrupted"}:
                attempt_summaries.append(
                    {"private_id": private_id, "status": status, "attempt": 1}
                )
                ending_commit, ending_dirty = source_git_provenance()
                if ending_dirty or ending_commit != source_commit:
                    raise IntegrityError(
                        "source Git checkout changed during the repo-quality run"
                    )
                return write_inconclusive_summary(
                    output_dir=args.output_dir,
                    run_id=run_id,
                    config_sha256=config_sha256,
                    args=args,
                    reason="a preregistered attempt ended without a usable result",
                    phase="model_execution",
                    attempts=attempt_summaries,
                )
            if status == "error":
                raise IntegrityError("a preregistered attempt ended with a hard error")
            if status is None:
                print(
                    f"[{index}/{total}] {key.project} | {key.arm} | trial {key.trial}",
                    file=sys.stderr,
                    flush=True,
                )
                start_attempt(paths, identity=identity)
                try:
                    execute_run(
                        executable=args.codex,
                        auth_source=auth_cache,
                        auth_sink=auth_cache,
                        project=project,
                        arm=arm,
                        key=key,
                        identity=identity,
                        model=args.model,
                        effort=args.effort,
                        raw_path=paths.raw,
                        stderr_path=paths.stderr,
                        full_patch_path=paths.full_patch,
                        production_patch_path=paths.production_patch,
                        result_path=paths.result,
                        timeout_seconds=args.timeout_seconds,
                        max_raw_bytes=args.max_raw_bytes_per_call,
                        source_isolation=source_isolation,
                    )
                except InfrastructureError as exc:
                    finish_attempt(paths, status="infra_error", error=exc)
                    attempt_summaries.append(
                        {
                            "private_id": private_id,
                            "status": "infra_error",
                            "attempt": 1,
                        }
                    )
                    ending_commit, ending_dirty = source_git_provenance()
                    if ending_dirty or ending_commit != source_commit:
                        raise IntegrityError(
                            "source Git checkout changed during the repo-quality run"
                        )
                    return write_inconclusive_summary(
                        output_dir=args.output_dir,
                        run_id=run_id,
                        config_sha256=config_sha256,
                        args=args,
                        reason=str(exc),
                        phase="model_execution",
                        attempts=attempt_summaries,
                    )
                except BaseException as exc:
                    finish_attempt(paths, status="error", error=exc)
                    raise
                finish_attempt(paths, status="success")
            else:
                print(
                    f"[{index}/{total}] resume {key.project} | {key.arm} | trial {key.trial}",
                    file=sys.stderr,
                    flush=True,
                )
            attempt_summaries.append(
                {"private_id": private_id, "status": "success", "attempt": 1}
            )
            try:
                result_payload = json.loads(paths.result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise IntegrityError("successful attempt result is unreadable") from exc
            cumulative_reported_tokens += reported_token_count(result_payload)
            if cumulative_reported_tokens > args.max_total_reported_tokens:
                ending_commit, ending_dirty = source_git_provenance()
                if ending_dirty or ending_commit != source_commit:
                    raise IntegrityError(
                        "source Git checkout changed during the repo-quality run"
                    )
                return write_inconclusive_summary(
                    output_dir=args.output_dir,
                    run_id=run_id,
                    config_sha256=config_sha256,
                    args=args,
                    reason="preregistered reported-token budget was exceeded",
                    phase="budget",
                    attempts=attempt_summaries,
                )

    results: list[dict[str, Any]] = []
    result_hashes: dict[RunKey, str] = {}
    validate_attempt_inventory(private, expected_attempt_ids)
    for key in schedule:
        project = projects[key.project]
        arm = arms[key.arm]
        private_id = private_run_id(config_sha256, key)
        paths = attempt_paths(private, private_id)
        identity = result_identity(
            benchmark_run_id=run_id,
            config_sha256=config_sha256,
            key=key,
            project=project,
            arm=arm,
            model=args.model,
            effort=args.effort,
            cli_version=cli_version,
            runner_sha256=runner_sha256,
            fixture_sha256=str(fixtures[key.project]["fixture_sha256"]),
            worker_sha256=str(fixtures[key.project]["worker_sha256"]),
        )
        if validate_attempt_envelope(paths, identity=identity) != "success":
            raise IntegrityError("gate reconstruction found an incomplete attempt")
        with tempfile.TemporaryDirectory(
            prefix="revalidate-", dir="/private/tmp"
        ) as temporary:
            result = load_resumable_result(
                result_path=paths.result,
                raw_path=paths.raw,
                stderr_path=paths.stderr,
                full_patch_path=paths.full_patch,
                production_patch_path=paths.production_patch,
                expected_identity=identity,
                project=project,
                max_raw_bytes=args.max_raw_bytes_per_call,
                validation_parent=Path(temporary),
                env=safe_environment(),
                executable=args.codex,
                source_isolation=source_isolation,
            )
        if result is None:
            raise IntegrityError("successful attempt result is missing")
        results.append(result)
        result_hashes[key] = sha256_file(paths.result)

    gate = gate_results(results)
    ending_commit, ending_dirty = source_git_provenance()
    if ending_dirty or ending_commit != source_commit:
        raise RuntimeError("source Git checkout changed during the repo-quality run")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "config_sha256": config_sha256,
        "completed_at": utc_now(),
        "model": args.model,
        "effort": args.effort,
        "model_verbosity": MODEL_VERBOSITY,
        "calls": EXPECTED_CALLS,
        "reported_tokens": cumulative_reported_tokens,
        "gate": gate,
        "runs": [
            {
                "project": result["project"],
                "arm": result["arm"],
                "trial": result["trial"],
                "passed": result["validation"]["passed"],
                "result_sha256": result_hashes[
                    RunKey(result["project"], result["arm"], result["trial"])
                ],
            }
            for result in results
        ],
        "attempts": attempt_summaries,
    }
    atomic_write_json(args.output_dir / "gate-summary.json", summary)
    print(f"{gate['status']}: {gate['reason']}")
    print(
        f"native_low {gate['passed']['native_low']}/{gate['required_per_arm']}; "
        f"candidate_runtime {gate['passed']['candidate_runtime']}/{gate['required_per_arm']}"
    )
    return int(gate["exit_code"])


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        return _main(args)
    with output_lock(args.output_dir / "private" / ".runner.lock"):
        return _main(args)


def cli_main(argv: Sequence[str] | None = None) -> int:
    try:
        return main(argv)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(cli_main())
