from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
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
        "GIT_TERMINAL_PROMPT": "0",
    }


def _child_limits() -> None:
    if resource is None:
        return
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


def run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_text: str | None = None,
    monitor_workspace: Path | None = None,
    isolation: SourceIsolation | None = None,
    trusted_offline: bool = False,
    timeout_seconds: int = TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> CommandResult:
    if isolation is None and not trusted_offline:
        raise IntegrityError("unisolated command execution is allowed only for offline self-checks")
    actual = isolation.wrap(command, cwd) if isolation else tuple(command)
    stdout_fd, stdout_name = tempfile.mkstemp(prefix="coding-gate-stdout-")
    stderr_fd, stderr_name = tempfile.mkstemp(prefix="coding-gate-stderr-")
    stdout_path = Path(stdout_name)
    stderr_path = Path(stderr_name)
    started = time.monotonic()
    timed_out = output_limited = tree_limited = False
    process: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(stdout_fd, "wb") as stdout, os.fdopen(stderr_fd, "wb") as stderr:
            try:
                process = subprocess.Popen(
                    actual,
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    preexec_fn=_child_limits if os.name == "posix" else None,
                )
            except OSError as exc:
                raise InfrastructureError(f"cannot start command: {actual[0]}") from exc
            if process.stdin is not None:
                try:
                    if input_text is not None:
                        process.stdin.write(input_text.encode("utf-8"))
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
    _copy_fixture(spec, destination)
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
        return _git(("rev-parse", "HEAD"), cwd=destination, env=env).stdout.strip()


def collect_patch(spec: FixtureSpec, workspace: Path, baseline: str) -> Patch:
    _tree(workspace)
    with tempfile.TemporaryDirectory(prefix="coding-gate-collect-") as temporary:
        env = validation_environment(Path(temporary))
        resolved = _git(("rev-parse", f"{baseline}^{{commit}}"), cwd=workspace, env=env).stdout.strip()
        if resolved != baseline:
            raise IntegrityError("baseline commit mismatch")
        untracked = _git(
            ("ls-files", "--others", "--exclude-standard", "-z"),
            cwd=workspace,
            env=env,
        ).stdout.split("\0")
        untracked = [path for path in untracked if path]
        if untracked:
            _git(("add", "--intent-to-add", "--", *untracked), cwd=workspace, env=env)
        changed = tuple(
            sorted(
                path
                for path in _git(
                    ("diff", "--name-only", "-z", baseline, "--"),
                    cwd=workspace,
                    env=env,
                ).stdout.split("\0")
                if path
            )
        )
        if not changed:
            raise IntegrityError("production patch is empty")
        changed_set = set(changed)
        if not changed_set.isdisjoint(spec.immutable_tests):
            raise IntegrityError("baseline tests are immutable")
        if not changed_set.issubset(spec.production_paths):
            raise IntegrityError("patch changes non-production fixture paths")
        diff_check = subprocess.run(
            ("git", "diff", "--check", baseline, "--"),
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff_check.returncode != 0:
            raise IntegrityError("git diff --check rejected the patch")
        production = _git(
            ("diff", "--binary", "--no-ext-diff", baseline, "--", *spec.production_paths),
            cwd=workspace,
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


def run_hidden_cases(
    spec: FixtureSpec,
    workspace: Path,
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
    pristine = _tree(workspace)
    worker_hash = sha256_file(spec.worker)
    results: list[HiddenResult] = []
    for case in spec.hidden_cases:
        if {"schema_version", "case_id"}.intersection(case.request):
            raise IntegrityError("hidden request contains reserved fields")
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
    source_manifest = _copy_fixture(spec, validation_root)
    apply = subprocess.run(
        ("git", "apply", "--whitespace=nowarn", "-"),
        cwd=validation_root,
        input=production_patch,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if apply.returncode != 0:
        raise IntegrityError(f"production patch replay failed: {apply.stderr.strip()}")
    patched_manifest = _tree(validation_root)
    changed = {
        path
        for path in source_manifest.keys() | patched_manifest.keys()
        if source_manifest.get(path) != patched_manifest.get(path)
    }
    if not changed or not changed.issubset(spec.production_paths):
        raise IntegrityError("replayed patch is not production-only")
    with tempfile.TemporaryDirectory(prefix="coding-gate-validation-") as temporary:
        env = validation_environment(Path(temporary))
        execution = run_bounded(
            spec.command,
            cwd=validation_root,
            env=env,
            monitor_workspace=validation_root,
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
        hidden = (
            run_hidden_cases(
                spec,
                validation_root,
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
