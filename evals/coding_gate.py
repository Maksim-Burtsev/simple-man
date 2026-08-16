from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - live mode rejects unsupported platforms
    resource = None


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "evals/fixtures/skill-comparison"
WORKER_ROOT = ROOT / "evals/coding_workers"
MAX_PATCH_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 2_000
TIMEOUT_SECONDS = 15


def build_safe_path(ambient_path: str | None = None) -> str:
    paths = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    lookup = ambient_path if ambient_path is not None else os.environ.get("PATH", os.defpath)
    for name in ("git", "node", "npm", "python3"):
        executable = shutil.which(name, path=lookup)
        if executable is not None:
            paths.append(str(Path(executable).resolve().parent))
    return os.pathsep.join(dict.fromkeys(paths))


SAFE_PATH = build_safe_path()


class InfrastructureError(RuntimeError):
    pass


class IntegrityError(RuntimeError):
    pass


class UnsupportedPlatformError(InfrastructureError):
    pass


@dataclass(frozen=True)
class HiddenCase:
    case_id: str
    request: Mapping[str, Any]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class FixtureSpec:
    key: str
    root: Path
    production_paths: tuple[str, ...]
    immutable_tests: tuple[str, ...]
    command: tuple[str, ...]
    expected_seed_failure: str
    reporter: str
    canonical_test_ids: tuple[str, ...]
    worker: Path
    worker_runtime: str
    hidden_cases: tuple[HiddenCase, ...]


@dataclass(frozen=True)
class Patch:
    production: str
    changed_paths: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    output_limited: bool = False
    tree_limited: bool = False


@dataclass(frozen=True)
class CanonicalResult:
    passed: bool
    returncode: int
    output_sha256: str
    timed_out: bool
    output_limited: bool
    tree_limited: bool


@dataclass(frozen=True)
class HiddenResult:
    case_id: str
    passed: bool
    validator_sha256: str
    observation_sha256: str | None
    error: str | None


@dataclass(frozen=True)
class ValidationResult:
    project: str
    canonical: CanonicalResult
    hidden: tuple[HiddenResult, ...]
    patch_sha256: str

    @property
    def passed(self) -> bool:
        return self.canonical.passed and bool(self.hidden) and all(
            case.passed for case in self.hidden
        )


@dataclass(frozen=True)
class SourceIsolation:
    sandbox_executable: str
    protected_roots: tuple[Path, ...]
    process_boundary_proven: bool = False

    @classmethod
    def live(
        cls,
        *,
        sandbox_executable: str,
        protected_roots: Sequence[Path],
    ) -> SourceIsolation:
        if platform.system() != "Darwin":
            raise UnsupportedPlatformError(
                "live coding validation requires the pinned macOS sandbox contract"
            )
        executable = shutil.which(sandbox_executable)
        if executable is None:
            raise InfrastructureError("live coding validation sandbox is unavailable")
        roots = tuple(dict.fromkeys(path.expanduser().resolve() for path in protected_roots))
        required = (ROOT.resolve(), WORKER_ROOT.resolve(), Path.home().resolve())
        if any(not any(item == root or item.is_relative_to(root) for root in roots) for item in required):
            raise IntegrityError("live source isolation must protect source, workers, and real home")
        return cls(str(Path(executable).resolve()), roots)

    def wrap(self, command: Sequence[str], workspace: Path) -> tuple[str, ...]:
        if platform.system() != "Darwin":
            raise UnsupportedPlatformError(
                "live coding validation requires the pinned macOS sandbox contract"
            )
        executable = Path(self.sandbox_executable)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise InfrastructureError("live coding validation sandbox is unavailable")
        roots = tuple(path.expanduser().resolve() for path in self.protected_roots)
        required = (ROOT.resolve(), WORKER_ROOT.resolve(), Path.home().resolve())
        if any(
            not any(item == root or item.is_relative_to(root) for root in roots)
            for item in required
        ):
            raise IntegrityError("live source isolation contract is incomplete")
        if not self.process_boundary_proven:
            raise InfrastructureError(
                "live coding isolation is INCONCLUSIVE without a proven process boundary"
            )
        resolved_workspace = workspace.resolve()
        if any(
            resolved_workspace == root or resolved_workspace.is_relative_to(root)
            for root in roots
        ):
            raise IntegrityError("live validation workspace overlaps a protected root")
        filesystem = ",".join(
            f"{json.dumps(str(root))}=\"deny\"" for root in roots
        )
        profile = (
            "{extends=\":read-only\",filesystem={"
            + filesystem
            + "},network={enabled=false}}"
        )
        return (
            str(executable),
            "--config",
            f"permissions.coding_gate={profile}",
            "sandbox",
            "-P",
            "coding_gate",
            "--sandbox-state-disable-network",
            "-C",
            str(resolved_workspace),
            "--",
            *command,
        )


@dataclass(frozen=True)
class ModelSourceIsolation:
    sandbox_executable: str
    workspace: Path
    categories: tuple[tuple[str, tuple[Path, ...]], ...]
    tool_home: Path
    tool_tmp: Path
    process_boundary_proven: bool = False

    @property
    def protected_roots(self) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                path for _, roots in self.categories for path in roots
            )
        )

    @property
    def profile_arguments(self) -> tuple[str, ...]:
        filesystem = ",".join(
            f"{json.dumps(str(path))}=\"deny\"" for path in self.protected_roots
        )
        profile = (
            "{extends=\":workspace\",filesystem={"
            + filesystem
            + "},network={enabled=false}}"
        )
        tool_environment = {
            "HOME": str(self.tool_home),
            "PATH": SAFE_PATH,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "TEMP": str(self.tool_tmp),
            "TMP": str(self.tool_tmp),
            "TMPDIR": str(self.tool_tmp),
        }
        inline_environment = "{" + ",".join(
            f"{json.dumps(key)}={json.dumps(value)}"
            for key, value in tool_environment.items()
        ) + "}"
        return (
            "--config",
            'default_permissions="coding_model"',
            "--config",
            f"permissions.coding_model={profile}",
            "--config",
            'shell_environment_policy.inherit="none"',
            "--config",
            f"shell_environment_policy.set={inline_environment}",
            "--config",
            "allow_login_shell=false",
        )

    def sandbox_command(self, command: Sequence[str]) -> tuple[str, ...]:
        return (
            self.sandbox_executable,
            *self.profile_arguments,
            "sandbox",
            "-P",
            "coding_model",
            "--sandbox-state-disable-network",
            "-C",
            str(self.workspace),
            "--",
            *command,
        )

    def category_roots(self, category: str) -> tuple[Path, ...]:
        return dict(self.categories).get(category, ())


@dataclass(frozen=True)
class IsolationProbe:
    status: str
    filesystem_passed: bool
    network_passed: bool
    denied_categories: frozenset[str]
    cli_version: str
    reason: str


def build_model_source_isolation(
    *,
    sandbox_executable: str | None,
    workspace: Path,
    source_root: Path,
    common_git_root: Path,
    real_home: Path,
    auth_file: Path,
    codex_home: Path,
    workers_root: Path,
    validation_roots: Sequence[Path],
    output_roots: Sequence[Path],
    other_workspaces: Sequence[Path],
    tool_home: Path,
    tool_tmp: Path,
) -> ModelSourceIsolation:
    if platform.system() != "Darwin":
        raise UnsupportedPlatformError(
            "model source isolation requires the pinned macOS sandbox contract"
        )
    executable = shutil.which(sandbox_executable) if sandbox_executable else None
    if executable is None:
        raise InfrastructureError("model source isolation sandbox is unavailable")
    if real_home.expanduser().resolve() != Path.home().resolve():
        raise IntegrityError("model isolation must protect the real home")
    resolved_workspace = workspace.resolve()
    if workspace.is_symlink() or not resolved_workspace.is_dir():
        raise IntegrityError("model workspace must be an existing regular directory")
    categories = (
        ("source", (source_root.resolve(),)),
        ("common_git", (common_git_root.resolve(),)),
        ("home", (real_home.expanduser().resolve(),)),
        ("auth", (auth_file.expanduser().resolve(),)),
        ("codex_home", (codex_home.expanduser().resolve(),)),
        ("workers", (workers_root.resolve(),)),
        ("validation", tuple(path.resolve() for path in validation_roots)),
        ("output", tuple(path.resolve() for path in output_roots)),
        ("other_workspace", tuple(path.resolve() for path in other_workspaces)),
    )
    if any(not roots for _, roots in categories):
        raise IntegrityError("model isolation requires every protected path category")
    protected = tuple(path for _, roots in categories for path in roots)
    if any(
        resolved_workspace == path
        or resolved_workspace.is_relative_to(path)
        or path.is_relative_to(resolved_workspace)
        for path in protected
    ):
        raise IntegrityError("model workspace overlaps a protected path")
    for path in (tool_home, tool_tmp):
        if path.is_symlink() or not path.resolve().is_dir():
            raise IntegrityError("model tool HOME/TMP must be existing regular directories")
    return ModelSourceIsolation(
        sandbox_executable=str(Path(executable).resolve()),
        workspace=resolved_workspace,
        categories=categories,
        tool_home=tool_home.resolve(),
        tool_tmp=tool_tmp.resolve(),
    )


def _sandbox_denied(process: subprocess.CompletedProcess[str]) -> bool:
    output = process.stdout + process.stderr
    return process.returncode != 0 and "Operation not permitted" in output


def probe_model_source_isolation(
    contract: ModelSourceIsolation,
    *,
    denied_targets: Mapping[str, Path],
) -> IsolationProbe:
    if platform.system() != "Darwin":
        return IsolationProbe(
            "INCONCLUSIVE",
            False,
            False,
            frozenset(),
            "unsupported platform",
            "model source isolation probe requires macOS",
        )
    expected_categories = {category for category, _ in contract.categories}
    if set(denied_targets) != expected_categories:
        raise IntegrityError("probe targets must cover every protected category")
    for category, target in denied_targets.items():
        resolved = target.resolve()
        if not any(
            resolved == root or resolved.is_relative_to(root)
            for root in contract.category_roots(category)
        ):
            raise IntegrityError("probe target is outside its protected category")
    with tempfile.TemporaryDirectory(prefix="coding-gate-profile-probe-") as temporary:
        env = validation_environment(Path(temporary))
        version = subprocess.run(
            (contract.sandbox_executable, "--version"),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        cli_version = (version.stdout or version.stderr).strip()
        marker = contract.workspace / f".profile-probe-{secrets.token_hex(8)}"
        allowed = subprocess.run(
            contract.sandbox_command(("/usr/bin/touch", str(marker))),
            cwd=contract.workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        workspace_passed = allowed.returncode == 0 and marker.is_file()
        marker.unlink(missing_ok=True)
        denied: set[str] = set()
        for category, target in denied_targets.items():
            process = subprocess.run(
                contract.sandbox_command(
                    ("/usr/bin/head", "-c", "1", str(target.resolve()))
                ),
                cwd=contract.workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if _sandbox_denied(process):
                denied.add(category)
        server = socket.socket()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            network = subprocess.run(
                contract.sandbox_command(
                    (
                        "/usr/bin/python3",
                        "-c",
                        "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 1)",
                        str(port),
                    )
                ),
                cwd=contract.workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            network_passed = _sandbox_denied(network)
        finally:
            server.close()
    filesystem_passed = workspace_passed and denied == expected_categories
    ready = filesystem_passed and network_passed and contract.process_boundary_proven
    reason = (
        "source, secret, workspace, and network probes passed; detached descendant boundary is unproven"
        if filesystem_passed and network_passed
        else "model source isolation profile probe failed"
    )
    return IsolationProbe(
        "READY" if ready else "INCONCLUSIVE",
        filesystem_passed,
        network_passed,
        frozenset(denied),
        cli_version,
        reason,
    )


def require_live_model_isolation(probe: IsolationProbe) -> None:
    if probe.status != "READY":
        raise InfrastructureError(f"live coding lane is {probe.status}: {probe.reason}")


FIXTURES = {
    "node-auth-api": FixtureSpec(
        key="node-auth-api",
        root=FIXTURE_ROOT / "node-auth-api",
        production_paths=("src/middleware.js",),
        immutable_tests=("test/auth.test.js",),
        command=("npm", "test"),
        expected_seed_failure="200 !== 401",
        reporter="node",
        canonical_test_ids=("accepts a valid session", "rejects an expired session"),
        worker=WORKER_ROOT / "node_auth_worker.js",
        worker_runtime="node",
        hidden_cases=(
            HiddenCase("future", {"now": 1_999, "expires_at": 2_000}, {"status": 200}),
            HiddenCase("boundary", {"now": 2_000, "expires_at": 2_000}, {"status": 401}),
            HiddenCase("expired", {"now": 2_001, "expires_at": 2_000}, {"status": 401}),
        ),
    ),
    "python-payment-ledger": FixtureSpec(
        key="python-payment-ledger",
        root=FIXTURE_ROOT / "python-payment-ledger",
        production_paths=("ledger.py",),
        immutable_tests=("test_ledger.py",),
        command=("python3", "-m", "unittest", "-v"),
        expected_seed_failure="'ch_2' != 'ch_1'",
        reporter="unittest",
        canonical_test_ids=("test_retry_with_same_key_does_not_create_second_remote_charge",),
        worker=WORKER_ROOT / "ledger_worker.py",
        worker_runtime="python3",
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
    "sqlite-rollout-runner": FixtureSpec(
        key="sqlite-rollout-runner",
        root=FIXTURE_ROOT / "sqlite-rollout-runner",
        production_paths=("rollout.py",),
        immutable_tests=("test_rollout.py",),
        command=("python3", "-m", "unittest", "-v"),
        expected_seed_failure="no such column: expires_at",
        reporter="unittest",
        canonical_test_ids=("test_backup_runs_before_drop_column_migration",),
        worker=WORKER_ROOT / "sqlite_worker.py",
        worker_runtime="python3",
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
                        {"name": "id", "type": "INTEGER", "notnull": 0, "default": None, "pk": 1},
                        {"name": "user_id", "type": "TEXT", "notnull": 1, "default": None, "pk": 0},
                        {"name": "note", "type": "TEXT", "notnull": 1, "default": None, "pk": 0},
                    ],
                    "rows": [[1, "u1", "first"], [2, "u2", "second"]],
                },
            ),
        ),
    ),
}
WORKERS = tuple(spec.worker for spec in FIXTURES.values())


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _manifest_sha256(manifest: Mapping[str, str]) -> str:
    return sha256_bytes(canonical_json(manifest).encode("utf-8"))


def _tree(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise IntegrityError(f"invalid tree root: {root}")
    stack = [root]
    files: dict[str, str] = {}
    total = 0
    entries = 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > MAX_FILES:
                    raise IntegrityError("workspace exceeds file cap")
                metadata = child.stat(follow_symlinks=False)
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    raise IntegrityError("workspace contains a symlink")
                if stat.S_ISDIR(mode):
                    stack.append(Path(child.path))
                    continue
                if not stat.S_ISREG(mode):
                    raise IntegrityError("workspace contains a special file")
                if metadata.st_size > MAX_FILE_BYTES:
                    raise IntegrityError("workspace file exceeds byte cap")
                total += metadata.st_size
                if total > MAX_TREE_BYTES:
                    raise IntegrityError("workspace exceeds byte cap")
                path = Path(child.path)
                files[path.relative_to(root).as_posix()] = sha256_file(path)
    return dict(sorted(files.items()))


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_stable_file(fd: int) -> tuple[bytes, os.stat_result]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
        raise IntegrityError("workspace path is not a bounded regular file")
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, min(64 * 1024, MAX_FILE_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise IntegrityError("workspace file exceeds byte cap")
    if _stat_signature(os.fstat(fd)) != _stat_signature(before):
        raise IntegrityError("workspace file changed while being read")
    return b"".join(chunks), before


def _model_fixture_manifest(workspace: Path) -> dict[str, str]:
    if workspace.is_symlink():
        raise IntegrityError("model workspace must not be a symlink")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise UnsupportedPlatformError("safe descriptor-relative capture is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    nofollow = os.O_NOFOLLOW
    root_fd = os.open(workspace, directory_flags | nofollow)
    files: dict[str, str] = {}
    total = 0
    entries = 0

    def visit(directory_fd: int, prefix: str, *, root: bool = False) -> None:
        nonlocal total, entries
        before = os.fstat(directory_fd)
        for name in sorted(os.listdir(directory_fd)):
            if root and name == ".git":
                continue
            entries += 1
            if entries > MAX_FILES:
                raise IntegrityError("workspace exceeds file cap")
            item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISLNK(item.st_mode):
                raise IntegrityError("workspace contains a symlink")
            if stat.S_ISDIR(item.st_mode):
                child_fd = os.open(
                    name,
                    directory_flags | nofollow,
                    dir_fd=directory_fd,
                )
                try:
                    if _stat_signature(os.fstat(child_fd)) != _stat_signature(item):
                        raise IntegrityError("workspace directory changed while opening")
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(item.st_mode):
                raise IntegrityError("workspace contains a special file")
            file_fd = os.open(name, os.O_RDONLY | nofollow, dir_fd=directory_fd)
            try:
                if _stat_signature(os.fstat(file_fd)) != _stat_signature(item):
                    raise IntegrityError("workspace file changed while opening")
                content, _ = _read_stable_file(file_fd)
            finally:
                os.close(file_fd)
            total += len(content)
            if total > MAX_TREE_BYTES:
                raise IntegrityError("workspace exceeds byte cap")
            files[relative] = sha256_bytes(content)
        if _stat_signature(os.fstat(directory_fd)) != _stat_signature(before):
            raise IntegrityError("workspace directory changed while being read")

    try:
        visit(root_fd, "", root=True)
    except OSError as exc:
        raise IntegrityError("cannot safely read model workspace") from exc
    finally:
        os.close(root_fd)
    return dict(sorted(files.items()))


def _capture_production_files(
    workspace: Path,
    production_paths: Sequence[str],
    expected_manifest: Mapping[str, str],
) -> dict[str, tuple[bytes, int] | None]:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise UnsupportedPlatformError("safe descriptor-relative capture is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    nofollow = os.O_NOFOLLOW
    root_fd = os.open(workspace, directory_flags | nofollow)
    captured: dict[str, tuple[bytes, int] | None] = {}
    try:
        for relative in production_paths:
            parts = Path(relative).parts
            if not parts or Path(relative).is_absolute() or ".." in parts:
                raise IntegrityError("invalid production path")
            source = workspace / relative
            if source.is_symlink():
                raise IntegrityError("production path must not be a symlink")
            parent_fd = os.dup(root_fd)
            file_fd: int | None = None
            try:
                for part in parts[:-1]:
                    next_fd = os.open(
                        part,
                        directory_flags | nofollow,
                        dir_fd=parent_fd,
                    )
                    os.close(parent_fd)
                    parent_fd = next_fd
                try:
                    file_fd = os.open(
                        parts[-1],
                        os.O_RDONLY | nofollow,
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    captured[relative] = None
                    continue
                content, before = _read_stable_file(file_fd)
                if sha256_bytes(content) != expected_manifest.get(relative):
                    raise IntegrityError("production file changed before capture")
                captured[relative] = (content, stat.S_IMODE(before.st_mode))
            except OSError as exc:
                raise IntegrityError("cannot safely capture production file") from exc
            finally:
                if file_fd is not None:
                    os.close(file_fd)
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    return captured


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
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _child_limits(cpu_seconds: int) -> None:
    if resource is None:
        return
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
    cpu_limit = min(
        cpu_seconds,
        cpu_hard if cpu_hard != resource.RLIM_INFINITY else cpu_seconds,
    )
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = min(128, hard if hard != resource.RLIM_INFINITY else 128)
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, limit))


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


_BOOTSTRAP = (
    "import os,sys; fd=int(sys.argv[1]); ready=os.read(fd,1); os.close(fd); "
    "ready == b'1' or os._exit(126); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
)


class _DarwinProcessSupervisor:
    def __init__(self) -> None:
        required = (
            "kqueue",
            "kevent",
            "KQ_FILTER_PROC",
            "KQ_EV_ADD",
            "KQ_EV_DELETE",
            "KQ_EV_ENABLE",
            "KQ_EV_CLEAR",
            "KQ_EV_ERROR",
            "KQ_NOTE_FORK",
            "KQ_NOTE_TRACK",
            "KQ_NOTE_TRACKERR",
            "KQ_NOTE_CHILD",
            "KQ_NOTE_EXIT",
        )
        if any(not hasattr(select, name) for name in required):
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: Darwin kqueue tracking is unavailable"
            )
        self.queue = select.kqueue()
        self.root_pid: int | None = None
        self.alive: set[int] = set()
        self.tracking_error = False
        try:
            self._change(os.getpid(), select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR)
            self._change(os.getpid(), select.KQ_EV_DELETE)
        except BaseException:
            self.queue.close()
            raise

    @staticmethod
    def _fflags() -> int:
        return select.KQ_NOTE_FORK | select.KQ_NOTE_TRACK | select.KQ_NOTE_EXIT

    def _change(self, pid: int, flags: int) -> None:
        event = select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=flags,
            fflags=self._fflags(),
        )
        returned = self.queue.control([event], 1, 0)
        for result in returned:
            if result.flags & select.KQ_EV_ERROR and result.data:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: " + os.strerror(result.data)
                )

    def launch(self, command: Sequence[str]) -> tuple[tuple[str, ...], tuple[int, ...], int]:
        read_fd, write_fd = os.pipe()
        return (
            (sys.executable, "-I", "-c", _BOOTSTRAP, str(read_fd), *command),
            (read_fd,),
            write_fd,
        )

    def register(self, process: subprocess.Popen[bytes], release_fd: int) -> None:
        self.root_pid = process.pid
        self.alive = {process.pid}
        self._change(
            process.pid,
            select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
        )
        os.write(release_fd, b"1")

    def poll(self, timeout: float = 0) -> None:
        for event in self.queue.control(None, MAX_FILES, timeout):
            if event.flags & select.KQ_EV_ERROR:
                self.tracking_error = True
            if event.fflags & select.KQ_NOTE_TRACKERR:
                self.tracking_error = True
            if event.fflags & select.KQ_NOTE_CHILD:
                self.alive.add(int(event.ident))
            if event.fflags & select.KQ_NOTE_EXIT:
                self.alive.discard(int(event.ident))

    def cleanup(self) -> None:
        deadline = time.monotonic() + 1
        quiet = 0
        while quiet < 2:
            self.poll(0.01)
            if self.tracking_error:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: kqueue lost a descendant"
                )
            survivors = set(self.alive)
            for pid in survivors:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    self.alive.discard(pid)
                except PermissionError as exc:
                    raise InfrastructureError("cannot kill supervised descendant") from exc
            before = set(self.alive)
            self.poll(0.01)
            quiet = quiet + 1 if not self.alive and not before else 0
            if time.monotonic() >= deadline:
                raise InfrastructureError("supervised descendants survived cleanup")

    def close(self) -> None:
        self.queue.close()


def _linux_parent_map() -> dict[int, int]:
    parents: dict[int, int] = {}
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise InfrastructureError("Linux process supervision requires /proc") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        match = re.search(r"(?m)^PPid:\s+(\d+)$", status)
        if match:
            parents[int(entry.name)] = int(match.group(1))
    return parents


class _LinuxProcessSupervisor:
    def __init__(self) -> None:
        import ctypes

        if not Path("/proc/self/status").is_file():
            raise InfrastructureError("Linux process supervision requires /proc")
        self.ctypes = ctypes
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.prctl.restype = ctypes.c_int
        current = ctypes.c_int()
        if self.libc.prctl(37, ctypes.byref(current), 0, 0, 0) != 0:
            raise InfrastructureError("cannot query Linux child subreaper")
        self.previous = current.value
        if self.libc.prctl(36, 1, 0, 0, 0) != 0:
            raise InfrastructureError("cannot enable Linux child subreaper")
        self.baseline = {
            pid for pid, parent in _linux_parent_map().items() if parent == os.getpid()
        }
        self.known: set[int] = set()

    def launch(self, command: Sequence[str]) -> tuple[tuple[str, ...], tuple[int, ...], int | None]:
        return tuple(command), (), None

    def register(self, process: subprocess.Popen[bytes], release_fd: int | None) -> None:
        self.known.add(process.pid)

    def poll(self, timeout: float = 0) -> None:
        if timeout:
            time.sleep(timeout)
        parents = _linux_parent_map()
        roots = self.known | {
            pid
            for pid, parent in parents.items()
            if parent == os.getpid() and pid not in self.baseline
        }
        descendants = set(roots)
        while True:
            added = {
                pid for pid, parent in parents.items() if parent in descendants
            } - descendants
            if not added:
                break
            descendants.update(added)
        self.known.update(descendants)

    def cleanup(self) -> None:
        deadline = time.monotonic() + 1
        while True:
            self.poll()
            alive = {pid for pid in self.known if Path(f"/proc/{pid}").exists()}
            if not alive:
                return
            for pid in alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    raise InfrastructureError("cannot kill supervised descendant") from exc
            for pid in alive:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, ProcessLookupError):
                    pass
            if time.monotonic() >= deadline:
                raise InfrastructureError("supervised descendants survived cleanup")
            time.sleep(0.01)

    def close(self) -> None:
        if self.libc.prctl(36, self.previous, 0, 0, 0) != 0:
            raise InfrastructureError("cannot restore Linux child subreaper")


def _process_supervisor() -> _DarwinProcessSupervisor | _LinuxProcessSupervisor:
    system = platform.system()
    if system == "Darwin":
        return _DarwinProcessSupervisor()
    if system == "Linux":
        return _LinuxProcessSupervisor()
    raise UnsupportedPlatformError("kernel descendant supervision is unsupported")


def run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_text: str | None = None,
    monitor_workspace: Path | None = None,
    isolation: SourceIsolation | None = None,
    trusted_offline: bool = False,
    require_process_supervision: bool = False,
    timeout_seconds: int = TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> CommandResult:
    if isolation is None and not trusted_offline:
        raise IntegrityError("unisolated command execution is allowed only for offline self-checks")
    actual = isolation.wrap(command, cwd) if isolation else tuple(command)
    supervisor = (
        _process_supervisor()
        if isolation is not None or require_process_supervision
        else None
    )
    inherited_fds: tuple[int, ...] = ()
    release_fd: int | None = None
    launch = actual
    if supervisor is not None:
        launch, inherited_fds, release_fd = supervisor.launch(actual)
    stdout_fd, stdout_name = tempfile.mkstemp(prefix="coding-gate-stdout-")
    stderr_fd, stderr_name = tempfile.mkstemp(prefix="coding-gate-stderr-")
    stdout_path = Path(stdout_name)
    stderr_path = Path(stderr_name)
    started = time.monotonic()
    timed_out = output_limited = tree_limited = False
    process: subprocess.Popen[bytes] | None = None
    group_cleaned = False
    supervisor_closed = False
    try:
        with os.fdopen(stdout_fd, "wb") as stdout, os.fdopen(stderr_fd, "wb") as stderr:
            try:
                process = subprocess.Popen(
                    launch,
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    pass_fds=inherited_fds,
                    preexec_fn=(
                        lambda: _child_limits(max(1, math.ceil(timeout_seconds) + 1))
                        if os.name == "posix"
                        else None
                    ),
                )
            except OSError as exc:
                raise InfrastructureError(f"cannot start command: {launch[0]}") from exc
            finally:
                for descriptor in inherited_fds:
                    os.close(descriptor)
                inherited_fds = ()
            if supervisor is not None:
                descriptor = release_fd
                release_fd = None
                try:
                    supervisor.register(process, descriptor)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            if process.stdin is not None:
                try:
                    if input_text is not None:
                        process.stdin.write(input_text.encode("utf-8"))
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
            while process.poll() is None:
                if supervisor is not None:
                    supervisor.poll()
                elapsed = time.monotonic() - started
                if elapsed > timeout_seconds:
                    timed_out = True
                    break
                stdout.flush()
                stderr.flush()
                if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
                    output_limited = True
                    break
                if monitor_workspace is not None:
                    try:
                        _tree(monitor_workspace)
                    except IntegrityError:
                        tree_limited = True
                        break
                time.sleep(0.01)
            if timed_out or output_limited or tree_limited:
                _kill_group(process)
            else:
                process.wait()
                _kill_group(process)
            group_cleaned = True
            if supervisor is not None:
                supervisor.cleanup()
                supervisor.close()
                supervisor_closed = True
            stdout.flush()
            stderr.flush()
        stdout_bytes = stdout_path.read_bytes()
        stderr_bytes = stderr_path.read_bytes()
        if len(stdout_bytes) + len(stderr_bytes) > max_output_bytes:
            output_limited = True
        try:
            decoded_stdout = stdout_bytes[:max_output_bytes].decode("utf-8")
            decoded_stderr = stderr_bytes[:max_output_bytes].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrityError("command output is not valid UTF-8") from exc
        return CommandResult(
            returncode=process.returncode,
            stdout=decoded_stdout,
            stderr=decoded_stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            output_limited=output_limited,
            tree_limited=tree_limited,
        )
    finally:
        for descriptor in inherited_fds:
            os.close(descriptor)
        if release_fd is not None:
            os.close(release_fd)
        if process is not None and not group_cleaned:
            _kill_group(process)
        if supervisor is not None and not supervisor_closed:
            try:
                supervisor.cleanup()
            finally:
                supervisor.close()
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


def _copy_fixture(spec: FixtureSpec, destination: Path) -> dict[str, str]:
    if destination.exists() or destination.is_symlink():
        raise IntegrityError("workspace destination must be fresh")
    source_manifest = _tree(spec.root)
    shutil.copytree(spec.root, destination)
    if _tree(destination) != source_manifest:
        raise IntegrityError("fixture copy does not match pristine source")
    return source_manifest


def _git(arguments: Sequence[str], *, cwd: Path, env: Mapping[str, str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env=dict(env),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if process.returncode != 0:
        raise IntegrityError(f"git {' '.join(arguments)} failed: {process.stderr.strip()}")
    return process


def _canonical_passed(spec: FixtureSpec, result: CommandResult) -> bool:
    if result.returncode != 0 or result.timed_out or result.output_limited or result.tree_limited:
        return False
    output = result.stdout + "\n" + result.stderr
    if spec.reporter == "unittest":
        counts = re.findall(r"(?m)^Ran (\d+) tests? in ", output)
        ids = re.findall(r"(?m)^([A-Za-z0-9_]+) \(", output)
        return (
            counts == [str(len(spec.canonical_test_ids))]
            and ids == list(spec.canonical_test_ids)
            and bool(re.search(r"(?m)^OK$", output))
        )
    if spec.reporter == "node":
        counts = re.findall(r"(?m)^(?:ℹ|#) tests (\d+)$", output)
        failures = re.findall(r"(?m)^(?:ℹ|#) fail (\d+)$", output)
        ids = re.findall(r"(?m)^✔ (.+?) \([0-9.]+ms\)$", output)
        return (
            counts == [str(len(spec.canonical_test_ids))]
            and failures == ["0"]
            and ids == list(spec.canonical_test_ids)
        )
    raise IntegrityError(f"unsupported canonical reporter: {spec.reporter}")


def prepare_model_workspace(spec: FixtureSpec, destination: Path) -> str:
    source_manifest = _copy_fixture(spec, destination)
    with tempfile.TemporaryDirectory(prefix="coding-gate-seed-") as temporary:
        result = run_bounded(
            spec.command,
            cwd=destination,
            env=validation_environment(Path(temporary)),
            monitor_workspace=destination,
            trusted_offline=True,
        )
    output = result.stdout + "\n" + result.stderr
    if (
        result.returncode == 0
        or result.timed_out
        or result.output_limited
        or result.tree_limited
        or spec.expected_seed_failure not in output
    ):
        raise IntegrityError(f"unexpected pristine seed result: {spec.key}")
    with tempfile.TemporaryDirectory(prefix="coding-gate-git-") as temporary:
        env = validation_environment(Path(temporary))
        _git(("init", "-q"), cwd=destination, env=env)
        _git(("add", "-A"), cwd=destination, env=env)
        _git(
            (
                "-c",
                "user.name=Coding Gate",
                "-c",
                "user.email=coding-gate.invalid",
                "commit",
                "-qm",
                "pristine fixture",
            ),
            cwd=destination,
            env=env,
        )
        _git(("rev-parse", "HEAD"), cwd=destination, env=env)
    return _manifest_sha256(source_manifest)


def collect_patch(spec: FixtureSpec, workspace: Path, baseline: str) -> Patch:
    source_manifest = _tree(spec.root)
    if baseline != _manifest_sha256(source_manifest):
        raise IntegrityError("controller baseline manifest mismatch")
    workspace_manifest = _model_fixture_manifest(workspace)
    changed = tuple(
        sorted(
            path
            for path in source_manifest.keys() | workspace_manifest.keys()
            if source_manifest.get(path) != workspace_manifest.get(path)
        )
    )
    if not changed:
        raise IntegrityError("production patch is empty")
    changed_set = set(changed)
    if not changed_set.isdisjoint(spec.immutable_tests):
        raise IntegrityError("baseline tests are immutable")
    if not changed_set.issubset(spec.production_paths):
        raise IntegrityError("patch changes non-production fixture paths")
    captured = _capture_production_files(
        workspace,
        spec.production_paths,
        workspace_manifest,
    )
    if _model_fixture_manifest(workspace) != workspace_manifest:
        raise IntegrityError("model workspace changed during patch capture")
    with tempfile.TemporaryDirectory(prefix="coding-gate-collect-") as temporary:
        control = Path(temporary)
        repository = control / "repository"
        _copy_fixture(spec, repository)
        env = validation_environment(control / "environment")
        _git(("init", "-q"), cwd=repository, env=env)
        _git(("add", "-A"), cwd=repository, env=env)
        _git(
            (
                "-c",
                "user.name=Coding Gate",
                "-c",
                "user.email=coding-gate.invalid",
                "commit",
                "-qm",
                "controller baseline",
            ),
            cwd=repository,
            env=env,
        )
        controller_baseline = _git(
            ("rev-parse", "HEAD"), cwd=repository, env=env
        ).stdout.strip()
        for relative in spec.production_paths:
            destination = repository / relative
            snapshot = captured[relative]
            if snapshot is None:
                destination.unlink(missing_ok=True)
            else:
                content, mode = snapshot
                destination.write_bytes(content)
                destination.chmod(mode)
        diff_check = subprocess.run(
            (
                "git",
                "--no-pager",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--check",
                controller_baseline,
                "--",
                *spec.production_paths,
            ),
            cwd=repository,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff_check.returncode != 0:
            raise IntegrityError("git diff --check rejected the patch")
        production = _git(
            (
                "--no-pager",
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                controller_baseline,
                "--",
                *spec.production_paths,
            ),
            cwd=repository,
            env=env,
        ).stdout
    encoded = production.encode("utf-8")
    if not production.strip() or len(encoded) > MAX_PATCH_BYTES:
        raise IntegrityError("invalid production patch size")
    return Patch(production, changed, sha256_bytes(encoded))


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError("worker JSON contains duplicate keys")
        result[key] = value
    return result


def parse_worker_output(case: HiddenCase, stdout: str, stderr: str) -> Mapping[str, Any]:
    if stderr.strip():
        raise IntegrityError("worker wrote to stderr")
    lines = stdout.splitlines()
    if len(lines) != 1:
        raise IntegrityError("worker must emit exactly one JSON line")
    try:
        envelope = json.loads(lines[0], object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, IntegrityError) as exc:
        raise IntegrityError("worker emitted invalid JSON") from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version",
        "case_id",
        "observation",
    }:
        raise IntegrityError("worker envelope schema mismatch")
    if envelope["schema_version"] != 1 or envelope["case_id"] != case.case_id:
        raise IntegrityError("worker envelope identity mismatch")
    observation = envelope["observation"]
    if not isinstance(observation, dict):
        raise IntegrityError("worker observation must be an object")
    return observation


def _inject_worker(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise IntegrityError("hidden validator source is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(source.read_bytes())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _replay_production_patch(
    spec: FixtureSpec,
    production_patch: str,
    destination: Path,
    *,
    env: Mapping[str, str],
) -> dict[str, str]:
    source_manifest = _copy_fixture(spec, destination)
    apply = subprocess.run(
        ("git", "apply", "--whitespace=nowarn", "-"),
        cwd=destination,
        env=dict(env),
        input=production_patch,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if apply.returncode != 0:
        raise IntegrityError(f"production patch replay failed: {apply.stderr.strip()}")
    patched_manifest = _tree(destination)
    changed = {
        path
        for path in source_manifest.keys() | patched_manifest.keys()
        if source_manifest.get(path) != patched_manifest.get(path)
    }
    if not changed or not changed.issubset(spec.production_paths):
        raise IntegrityError("replayed patch is not production-only")
    return patched_manifest


def run_hidden_cases(
    spec: FixtureSpec,
    production_patch: str,
    validation_root: Path,
    *,
    env: Mapping[str, str],
    isolation: SourceIsolation | None = None,
    trusted_offline: bool = False,
) -> tuple[HiddenResult, ...]:
    if isolation is not None and trusted_offline:
        raise IntegrityError("choose live isolation or trusted offline validation")
    if isolation is None and not trusted_offline:
        raise IntegrityError("hidden validation requires source isolation or explicit fixture trust")
    if (
        spec.worker == FIXTURE_ROOT
        or spec.worker.is_relative_to(FIXTURE_ROOT)
        or not spec.worker.is_file()
        or spec.worker.is_symlink()
    ):
        raise IntegrityError("hidden validator must live outside the fixture root")
    if validation_root.exists() or validation_root.is_symlink():
        raise IntegrityError("hidden validation root must be fresh")
    validation_root.mkdir(parents=True)
    worker_hash = sha256_file(spec.worker)
    results: list[HiddenResult] = []
    for index, case in enumerate(spec.hidden_cases, 1):
        if {"schema_version", "case_id"}.intersection(case.request):
            raise IntegrityError("hidden request contains reserved fields")
        workspace = validation_root / f"case-{index:02d}"
        pristine = _replay_production_patch(
            spec,
            production_patch,
            workspace,
            env=env,
        )
        suffix = spec.worker.suffix
        destination = workspace / f"._validator_{secrets.token_hex(16)}{suffix}"
        _inject_worker(spec.worker, destination)
        request = {"schema_version": 1, "case_id": case.case_id, **case.request}
        try:
            execution = run_bounded(
                (spec.worker_runtime, destination.name),
                cwd=workspace,
                env=env,
                input_text=canonical_json(request) + "\n",
                monitor_workspace=workspace,
                isolation=isolation,
                trusted_offline=trusted_offline,
            )
        finally:
            destination.unlink(missing_ok=True)
        observation: Mapping[str, Any] | None = None
        error: str | None = None
        if (
            execution.returncode != 0
            or execution.timed_out
            or execution.output_limited
            or execution.tree_limited
        ):
            error = "worker did not complete within the execution contract"
        else:
            try:
                observation = parse_worker_output(case, execution.stdout, execution.stderr)
            except IntegrityError as exc:
                error = str(exc)
        if _tree(workspace) != pristine:
            raise IntegrityError("hidden validation changed the patched fixture")
        observed_hash = (
            sha256_bytes(canonical_json(observation).encode("utf-8"))
            if observation is not None
            else None
        )
        results.append(
            HiddenResult(
                case_id=case.case_id,
                passed=error is None and observation == case.expected,
                validator_sha256=worker_hash,
                observation_sha256=observed_hash,
                error=error,
            )
        )
    return tuple(results)


def validate_patch(
    spec: FixtureSpec,
    production_patch: str,
    validation_root: Path,
    *,
    isolation: SourceIsolation | None = None,
    trusted_offline: bool = False,
) -> ValidationResult:
    if isolation is not None and trusted_offline:
        raise IntegrityError("choose live isolation or trusted offline validation")
    if isolation is None and not trusted_offline:
        raise IntegrityError("model-generated validation requires source isolation")
    encoded = production_patch.encode("utf-8")
    if not production_patch.strip() or len(encoded) > MAX_PATCH_BYTES:
        raise IntegrityError("invalid production patch size")
    if validation_root.exists() or validation_root.is_symlink():
        raise IntegrityError("validation root must be fresh")
    validation_root.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="coding-gate-validation-") as temporary:
        env = validation_environment(Path(temporary))
        canonical_root = validation_root / "canonical"
        canonical_manifest = _replay_production_patch(
            spec,
            production_patch,
            canonical_root,
            env=env,
        )
        execution = run_bounded(
            spec.command,
            cwd=canonical_root,
            env=env,
            monitor_workspace=canonical_root,
            isolation=isolation,
            trusted_offline=trusted_offline,
        )
        output = (execution.stdout + "\n" + execution.stderr).encode("utf-8")
        canonical = CanonicalResult(
            passed=_canonical_passed(spec, execution),
            returncode=execution.returncode,
            output_sha256=sha256_bytes(output),
            timed_out=execution.timed_out,
            output_limited=execution.output_limited,
            tree_limited=execution.tree_limited,
        )
        if _tree(canonical_root) != canonical_manifest:
            raise IntegrityError("canonical execution mutated the patched fixture")
        hidden = (
            run_hidden_cases(
                spec,
                production_patch,
                validation_root / "hidden",
                env=env,
                isolation=isolation,
                trusted_offline=trusted_offline,
            )
            if canonical.passed
            else ()
        )
    return ValidationResult(spec.key, canonical, hidden, sha256_bytes(encoded))


def self_check() -> dict[str, Any]:
    workers = tuple(path for path in WORKER_ROOT.glob("*") if path.is_file())
    if len(FIXTURES) != 3 or set(workers) != set(WORKERS):
        raise IntegrityError("coding gate requires exactly three fixtures and workers")
    for spec in FIXTURES.values():
        if not spec.hidden_cases or not set(spec.immutable_tests).isdisjoint(spec.production_paths):
            raise IntegrityError(f"invalid fixture contract: {spec.key}")
        _tree(spec.root)
        if not spec.worker.is_file() or spec.worker.is_symlink():
            raise IntegrityError(f"invalid hidden validator: {spec.key}")
        if spec.worker.is_relative_to(spec.root):
            raise IntegrityError(f"hidden validator is inside fixture: {spec.key}")
    with tempfile.TemporaryDirectory(prefix="coding-gate-self-check-") as temporary:
        root = Path(temporary)
        for spec in FIXTURES.values():
            prepare_model_workspace(spec, root / spec.key)
    return {"passed": True, "fixtures": len(FIXTURES), "workers": len(workers)}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if arguments != ["self-check"]:
        raise SystemExit("usage: coding_gate.py self-check")
    print(canonical_json(self_check()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
