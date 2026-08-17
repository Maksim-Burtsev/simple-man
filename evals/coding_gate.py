from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
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


_ATTESTATION_KEY = secrets.token_bytes(32)
_SUPERVISOR_GENERATION = "kernel-descendants-v2"


@dataclass(frozen=True)
class _ProcessAttestation:
    system: str
    nonce: str
    tag_denied: str
    tag_control: str
    signature: str


@dataclass(frozen=True)
class _ReadinessAttestation:
    contract_sha256: str
    process_signature: str
    signature: str


def _attestation_signature(kind: str, payload: Mapping[str, str]) -> str:
    encoded = json.dumps(
        {"kind": kind, **payload}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hmac.new(_ATTESTATION_KEY, encoded, hashlib.sha256).hexdigest()


def _verify_process_attestation(attestation: object) -> bool:
    if not isinstance(attestation, _ProcessAttestation):
        return False
    expected = _attestation_signature(
        "process",
        {
            "system": attestation.system,
            "nonce": attestation.nonce,
            "supervisor": _SUPERVISOR_GENERATION,
            "tag_denied": attestation.tag_denied,
            "tag_control": attestation.tag_control,
        },
    )
    if attestation.system == "Darwin":
        denied = Path(attestation.tag_denied)
        control = Path(attestation.tag_control)
        tag_valid = (
            denied.is_absolute()
            and control.is_absolute()
            and denied.parent == control.parent
            and denied != control
            and denied.is_file()
            and control.is_file()
            and not denied.is_symlink()
            and not control.is_symlink()
        )
    else:
        tag_valid = not attestation.tag_denied and not attestation.tag_control
    return (
        attestation.system == platform.system()
        and tag_valid
        and hmac.compare_digest(attestation.signature, expected)
    )


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
    _process_attestation: _ProcessAttestation | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def process_boundary_proven(self) -> bool:
        return _verify_process_attestation(self._process_attestation)

    @classmethod
    def live(
        cls,
        *,
        sandbox_executable: str,
        protected_roots: Sequence[Path],
        readiness: IsolationProbe | None,
    ) -> SourceIsolation:
        if platform.system() != "Darwin":
            raise UnsupportedPlatformError(
                "live coding validation requires the pinned macOS sandbox contract"
            )
        executable = shutil.which(sandbox_executable)
        if executable is None:
            raise InfrastructureError("live coding validation sandbox is unavailable")
        ready = require_live_model_isolation(readiness)
        resolved_executable = str(Path(executable).resolve())
        if resolved_executable != ready.sandbox_executable:
            raise IntegrityError("model and validation must use the same sandbox executable")
        roots = tuple(dict.fromkeys(path.expanduser().resolve() for path in protected_roots))
        required = (ROOT.resolve(), WORKER_ROOT.resolve(), Path.home().resolve())
        if any(not any(item == root or item.is_relative_to(root) for root in roots) for item in required):
            raise IntegrityError("live source isolation must protect source, workers, and real home")
        return cls(resolved_executable, roots, ready._process_attestation)

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
        tag_denied = Path(self._process_attestation.tag_denied)
        tag_control = Path(self._process_attestation.tag_control)
        filesystem = ",".join(
            [f"{json.dumps(str(root))}=\"deny\"" for root in roots]
            + [
                f"{json.dumps(str(tag_denied))}=\"deny\"",
                f"{json.dumps(str(tag_control))}=\"read\"",
            ]
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
    sandbox_tag: tuple[Path, Path] | None = None
    _process_attestation: _ProcessAttestation | None = field(
        default=None, repr=False, compare=False
    )
    _readiness_attestation: _ReadinessAttestation | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def process_boundary_proven(self) -> bool:
        return _verify_process_attestation(self._process_attestation)

    @property
    def ready(self) -> bool:
        return _verify_model_readiness(self)

    @property
    def protected_roots(self) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                path for _, roots in self.categories for path in roots
            )
        )

    @property
    def profile_arguments(self) -> tuple[str, ...]:
        filesystem_entries = [
            f"{json.dumps(str(path))}=\"deny\"" for path in self.protected_roots
        ]
        if self.sandbox_tag is not None:
            tag_denied, tag_control = self.sandbox_tag
            filesystem_entries.extend(
                (
                    f"{json.dumps(str(tag_denied))}=\"deny\"",
                    f"{json.dumps(str(tag_control))}=\"read\"",
                )
            )
        filesystem = ",".join(filesystem_entries)
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

    def answer_command(self, arguments: Sequence[str]) -> tuple[str, ...]:
        return (
            self.sandbox_executable,
            *self.profile_arguments,
            "exec",
            *arguments,
        )

    def category_roots(self, category: str) -> tuple[Path, ...]:
        return dict(self.categories).get(category, ())


@dataclass(frozen=True)
class IsolationProbe:
    status: str
    filesystem_passed: bool
    network_passed: bool
    descendant_passed: bool
    denied_categories: frozenset[str]
    cli_version: str
    reason: str
    _ready_contract: ModelSourceIsolation | None = field(
        default=None, repr=False, compare=False
    )


def _model_contract_digest(contract: ModelSourceIsolation) -> str:
    try:
        executable = Path(contract.sandbox_executable)
        identity = executable.stat()
    except OSError:
        return ""
    payload = {
        "sandbox_executable": str(executable),
        "sandbox_identity": [
            identity.st_dev,
            identity.st_ino,
            identity.st_size,
            identity.st_mtime_ns,
        ],
        "workspace": str(contract.workspace),
        "categories": [
            [category, [str(path) for path in roots]]
            for category, roots in contract.categories
        ],
        "tool_home": str(contract.tool_home),
        "tool_tmp": str(contract.tool_tmp),
        "sandbox_tag": (
            [str(path) for path in contract.sandbox_tag]
            if contract.sandbox_tag is not None
            else None
        ),
        "profile_arguments": list(contract.profile_arguments),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _verify_model_readiness(contract: ModelSourceIsolation) -> bool:
    readiness = contract._readiness_attestation
    process = contract._process_attestation
    if (
        not isinstance(readiness, _ReadinessAttestation)
        or not _verify_process_attestation(process)
    ):
        return False
    contract_sha256 = _model_contract_digest(contract)
    expected = _attestation_signature(
        "readiness",
        {
            "contract_sha256": contract_sha256,
            "process_signature": process.signature,
        },
    )
    return (
        bool(contract_sha256)
        and contract.sandbox_tag is not None
        and tuple(str(path) for path in contract.sandbox_tag)
        == (process.tag_denied, process.tag_control)
        and readiness.contract_sha256 == contract_sha256
        and readiness.process_signature == process.signature
        and hmac.compare_digest(readiness.signature, expected)
    )


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
    tag_root = tool_tmp.resolve() / f".coding-gate-tag-{secrets.token_hex(16)}"
    tag_root.mkdir(mode=0o700)
    tag_denied = tag_root / "denied"
    tag_control = tag_root / "control"
    tag_denied.write_text("coding-gate denied tag\n")
    tag_control.write_text("coding-gate allowed control\n")
    tag_denied.chmod(0o400)
    tag_control.chmod(0o400)
    return ModelSourceIsolation(
        sandbox_executable=str(Path(executable).resolve()),
        workspace=resolved_workspace,
        categories=categories,
        tool_home=tool_home.resolve(),
        tool_tmp=tool_tmp.resolve(),
        sandbox_tag=(tag_denied.resolve(), tag_control.resolve()),
    )


def _sandbox_denied(process: CommandResult) -> bool:
    output = process.stdout + process.stderr
    return process.returncode != 0 and "Operation not permitted" in output


def _run_model_execution(
    contract: ModelSourceIsolation,
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    input_text: str | None = None,
    timeout_seconds: int = TIMEOUT_SECONDS,
    monitor_workspace: bool = True,
) -> CommandResult:
    if not contract.process_boundary_proven:
        raise InfrastructureError(
            "model execution is INCONCLUSIVE without process attestation"
        )
    return run_bounded(
        command,
        cwd=contract.workspace,
        env=env,
        input_text=input_text,
        monitor_workspace=contract.workspace if monitor_workspace else None,
        _process_attestation=contract._process_attestation,
        timeout_seconds=timeout_seconds,
    )


def run_model_answer(
    contract: ModelSourceIsolation,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str],
    input_text: str | None = None,
    timeout_seconds: int = TIMEOUT_SECONDS,
) -> CommandResult:
    if not contract.ready:
        raise InfrastructureError(
            "live coding answer is INCONCLUSIVE without full isolation readiness"
        )
    return _run_model_execution(
        contract,
        contract.answer_command(arguments),
        env=env,
        input_text=input_text,
        timeout_seconds=timeout_seconds,
    )


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
    try:
        process_attestation = _attest_process_boundary(contract)
    except (InfrastructureError, UnsupportedPlatformError) as exc:
        return IsolationProbe(
            "INCONCLUSIVE",
            False,
            False,
            False,
            frozenset(),
            "unprobed",
            str(exc),
        )
    attested = replace(contract, _process_attestation=process_attestation)
    with tempfile.TemporaryDirectory(prefix="coding-gate-profile-probe-") as temporary:
        env = validation_environment(Path(temporary))
        version = _run_model_execution(
            attested,
            (attested.sandbox_executable, "--version"),
            env=env,
            timeout_seconds=10,
            monitor_workspace=False,
        )
        cli_version = (version.stdout or version.stderr).strip()
        marker = attested.workspace / f".profile-probe-{secrets.token_hex(8)}"
        allowed = _run_model_execution(
            attested,
            attested.sandbox_command(("/usr/bin/touch", str(marker))),
            env=env,
            timeout_seconds=10,
        )
        workspace_passed = allowed.returncode == 0 and marker.is_file()
        marker.unlink(missing_ok=True)
        denied: set[str] = set()
        for category, target in denied_targets.items():
            process = _run_model_execution(
                attested,
                attested.sandbox_command(
                    ("/usr/bin/head", "-c", "1", str(target.resolve()))
                ),
                env=env,
                timeout_seconds=10,
            )
            if _sandbox_denied(process):
                denied.add(category)
        server = socket.socket()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            network = _run_model_execution(
                attested,
                attested.sandbox_command(
                    (
                        "/usr/bin/python3",
                        "-c",
                        "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 1)",
                        str(port),
                    )
                ),
                env=env,
                timeout_seconds=10,
            )
            network_passed = _sandbox_denied(network)
        finally:
            server.close()
    filesystem_passed = workspace_passed and denied == expected_categories
    ready = filesystem_passed and network_passed
    ready_contract = None
    if ready:
        contract_sha256 = _model_contract_digest(attested)
        if not contract_sha256:
            raise InfrastructureError(
                "model sandbox executable changed during readiness probe"
            )
        process_signature = process_attestation.signature
        signature = _attestation_signature(
            "readiness",
            {
                "contract_sha256": contract_sha256,
                "process_signature": process_signature,
            },
        )
        readiness = _ReadinessAttestation(
            contract_sha256, process_signature, signature
        )
        ready_contract = replace(attested, _readiness_attestation=readiness)
    reason = (
        "source, secret, workspace, network, and detached descendant probes passed"
        if ready
        else "model source isolation profile probe failed"
    )
    return IsolationProbe(
        "READY" if ready else "INCONCLUSIVE",
        filesystem_passed,
        network_passed,
        True,
        frozenset(denied),
        cli_version,
        reason,
        ready_contract,
    )


def require_live_model_isolation(
    probe: IsolationProbe | None,
) -> ModelSourceIsolation:
    if probe is None or probe.status != "READY" or probe._ready_contract is None:
        status = probe.status if probe is not None else "INCONCLUSIVE"
        reason = probe.reason if probe is not None else "readiness probe is absent"
        raise InfrastructureError(f"live coding lane is {status}: {reason}")
    if not probe._ready_contract.ready:
        raise InfrastructureError("live coding lane readiness attestation is invalid")
    return probe._ready_contract


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


def _descriptor_tree_manifest(
    workspace: Path, *, include_root_git: bool
) -> dict[str, str]:
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
            if root and name == ".git" and not include_root_git:
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


def _model_fixture_manifest(workspace: Path) -> dict[str, str]:
    return _descriptor_tree_manifest(workspace, include_root_git=False)


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


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _ProcUniqIdentifierInfo(ctypes.Structure):
    _fields_ = [
        ("p_uuid", ctypes.c_uint8 * 16),
        ("p_uniqueid", ctypes.c_uint64),
        ("p_puniqueid", ctypes.c_uint64),
        ("p_idversion", ctypes.c_int32),
        ("p_orig_ppidversion", ctypes.c_int32),
        ("p_reserve2", ctypes.c_uint64),
        ("p_reserve3", ctypes.c_uint64),
    ]


class _ProcBsdInfoWithUniqId(ctypes.Structure):
    _fields_ = [
        ("pbsd", _ProcBsdInfo),
        ("p_uniqidentifier", _ProcUniqIdentifierInfo),
    ]


class _AuditToken(ctypes.Structure):
    _fields_ = [("values", ctypes.c_uint32 * 8)]


@dataclass(frozen=True)
class _DarwinIdentity:
    pid: int
    uid: int
    real_uid: int
    start_seconds: int
    start_microseconds: int
    unique_id: int
    id_version: int


@dataclass(frozen=True)
class _DarwinTaggedProcess:
    identity: _DarwinIdentity
    audit_token: tuple[int, ...]


class _DarwinProcessSupervisor:
    _PROC_RUID_ONLY = 5
    _PROC_PIDT_BSDINFOWITHUNIQID = 18
    _SANDBOX_FILTER_PATH = 1
    _TASK_AUDIT_TOKEN = 15
    _TASK_AUDIT_TOKEN_COUNT = 8

    def __init__(self, tag: tuple[Path, Path] | None) -> None:
        if tag is None:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: Darwin sandbox tag is absent"
            )
        self.tag_denied, self.tag_control = (path.resolve() for path in tag)
        if (
            self.tag_denied.parent != self.tag_control.parent
            or self.tag_denied == self.tag_control
            or not self.tag_denied.is_file()
            or not self.tag_control.is_file()
            or self.tag_denied.is_symlink()
            or self.tag_control.is_symlink()
        ):
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: Darwin sandbox tag is invalid"
            )
        sandbox_library = ctypes.util.find_library("sandbox")
        proc_library = ctypes.util.find_library("proc")
        bsm_library = ctypes.util.find_library("bsm")
        if sandbox_library is None or proc_library is None or bsm_library is None:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: Darwin process SPI is unavailable"
            )
        try:
            self.sandbox = ctypes.CDLL(sandbox_library, use_errno=True)
            self.proc = ctypes.CDLL(proc_library, use_errno=True)
            self.bsm = ctypes.CDLL(bsm_library, use_errno=True)
            self.system = ctypes.CDLL(None, use_errno=True)
            self.mach_task_self = ctypes.c_uint32.in_dll(
                self.system, "mach_task_self_"
            ).value
        except (OSError, ValueError) as exc:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: cannot load Darwin process SPI"
            ) from exc
        expected_sizes = {
            _ProcBsdInfo: 136,
            _ProcUniqIdentifierInfo: 56,
            _ProcBsdInfoWithUniqId: 192,
            _AuditToken: 32,
        }
        if any(ctypes.sizeof(kind) != size for kind, size in expected_sizes.items()):
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: Darwin process ABI is incompatible"
            )
        self._configure_spi()
        try:
            no_report = ctypes.c_uint32.in_dll(
                self.sandbox, "SANDBOX_CHECK_NO_REPORT"
            ).value
        except ValueError as exc:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: sandbox no-report flag is unavailable"
            ) from exc
        self.sandbox_filter = self._SANDBOX_FILTER_PATH | no_report
        self.real_uid = os.getuid()
        self.effective_uid = os.geteuid()
        if self.real_uid == 0 or self.effective_uid == 0:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE for a root Darwin controller"
            )
        controller = self._identity(os.getpid())
        if (
            controller is None
            or controller.real_uid != self.real_uid
            or controller.uid != self.effective_uid
        ):
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: cannot identify controller"
            )
        self.baseline = frozenset(self._stable_real_uid_snapshot())
        self._preflight_no_tagged_processes()

    def _configure_spi(self) -> None:
        try:
            self._configure_spi_unchecked()
        except (AttributeError, TypeError, ValueError) as exc:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: Darwin process SPI is unavailable"
            ) from exc

    def _configure_spi_unchecked(self) -> None:
        self.sandbox.sandbox_check.restype = ctypes.c_int
        # sandbox_check is variadic. Only its three fixed arguments belong here.
        self.sandbox.sandbox_check.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        self.sandbox.sandbox_check_by_audit_token.restype = ctypes.c_int
        self.sandbox.sandbox_check_by_audit_token.argtypes = [
            _AuditToken,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        self.proc.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.proc.proc_listpids.restype = ctypes.c_int
        self.proc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.proc.proc_pidinfo.restype = ctypes.c_int
        self.proc.proc_signal_with_audittoken.argtypes = [
            ctypes.POINTER(_AuditToken),
            ctypes.c_int,
        ]
        self.proc.proc_signal_with_audittoken.restype = ctypes.c_int
        self.bsm.audit_token_to_pid.argtypes = [_AuditToken]
        self.bsm.audit_token_to_pid.restype = ctypes.c_int
        self.bsm.audit_token_to_euid.argtypes = [_AuditToken]
        self.bsm.audit_token_to_euid.restype = ctypes.c_uint32
        self.bsm.audit_token_to_pidversion.argtypes = [_AuditToken]
        self.bsm.audit_token_to_pidversion.restype = ctypes.c_int
        self.system.task_name_for_pid.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.system.task_name_for_pid.restype = ctypes.c_int
        self.system.task_info.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.system.task_info.restype = ctypes.c_int
        self.system.mach_port_deallocate.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.system.mach_port_deallocate.restype = ctypes.c_int

    def _pids(self) -> tuple[int, ...]:
        int_size = ctypes.sizeof(ctypes.c_int)
        required_bytes = self.proc.proc_listpids(
            self._PROC_RUID_ONLY, self.real_uid, None, 0
        )
        if required_bytes <= 0 or required_bytes % int_size:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: cannot list Darwin processes"
            )
        capacity = required_bytes // int_size + 64
        for _ in range(3):
            values = (ctypes.c_int * capacity)()
            result_bytes = self.proc.proc_listpids(
                self._PROC_RUID_ONLY,
                self.real_uid,
                values,
                ctypes.sizeof(values),
            )
            if result_bytes <= 0 or result_bytes % int_size:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: cannot list Darwin processes"
                )
            if result_bytes > ctypes.sizeof(values):
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: malformed Darwin process list"
                )
            count = result_bytes // int_size
            if count < len(values):
                pids = tuple(pid for pid in values[:count] if pid > 0)
                if not pids:
                    raise InfrastructureError(
                        "process supervision is INCONCLUSIVE: cannot list Darwin processes"
                    )
                return pids
            capacity *= 2
        raise InfrastructureError(
            "process supervision is INCONCLUSIVE: Darwin process list is unstable"
        )

    def _identity(self, pid: int) -> _DarwinIdentity | None:
        info = _ProcBsdInfoWithUniqId()
        result = self.proc.proc_pidinfo(
            pid,
            self._PROC_PIDT_BSDINFOWITHUNIQID,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if result <= 0:
            return None
        if result != ctypes.sizeof(info) or info.pbsd.pbi_pid != pid:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: malformed Darwin process identity"
            )
        return _DarwinIdentity(
            pid,
            int(info.pbsd.pbi_uid),
            int(info.pbsd.pbi_ruid),
            int(info.pbsd.pbi_start_tvsec),
            int(info.pbsd.pbi_start_tvusec),
            int(info.p_uniqidentifier.p_uniqueid),
            int(info.p_uniqidentifier.p_idversion),
        )

    def _snapshot_identities(
        self, pids: Sequence[int]
    ) -> tuple[tuple[_DarwinIdentity, ...], bool]:
        identities = []
        unstable = False
        for pid in pids:
            identity = self._identity(pid)
            if identity is None or identity.real_uid != self.real_uid:
                unstable = True
                continue
            identities.append(identity)
        return tuple(identities), unstable

    def _same_real_uid_snapshot(
        self,
    ) -> tuple[tuple[_DarwinIdentity, ...], bool]:
        return self._snapshot_identities(self._pids())

    def _stable_real_uid_snapshot(self) -> tuple[_DarwinIdentity, ...]:
        deadline = time.monotonic() + 0.25
        while True:
            identities, unstable = self._same_real_uid_snapshot()
            if not unstable:
                return identities
            if time.monotonic() >= deadline:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: Darwin process list is unstable"
                )
            time.sleep(0.001)

    def _sandbox_decision(self, pid: int, path: Path) -> int:
        ctypes.set_errno(0)
        return self.sandbox.sandbox_check(
            pid,
            b"file-read-data",
            self.sandbox_filter,
            ctypes.c_char_p(os.fsencode(path)),
        )

    def _sandbox_decision_by_token(
        self, token_values: tuple[int, ...], path: Path
    ) -> int:
        token = self._make_audit_token(token_values)
        ctypes.set_errno(0)
        return self.sandbox.sandbox_check_by_audit_token(
            token,
            b"file-read-data",
            self.sandbox_filter,
            ctypes.c_char_p(os.fsencode(path)),
        )

    @staticmethod
    def _make_audit_token(token_values: tuple[int, ...]) -> _AuditToken:
        if len(token_values) != _DarwinProcessSupervisor._TASK_AUDIT_TOKEN_COUNT:
            raise InfrastructureError(
                "process supervision is INCONCLUSIVE: malformed process audit token"
            )
        return _AuditToken((ctypes.c_uint32 * len(token_values))(*token_values))

    def _decode_audit_token(
        self, token_values: tuple[int, ...]
    ) -> tuple[int, int, int]:
        token = self._make_audit_token(token_values)
        return (
            int(self.bsm.audit_token_to_pid(token)),
            int(self.bsm.audit_token_to_euid(token)),
            int(self.bsm.audit_token_to_pidversion(token)),
        )

    def _audit_token_for_pid(self, pid: int) -> tuple[int, ...] | None:
        task = ctypes.c_uint32()
        if self.system.task_name_for_pid(
            self.mach_task_self, pid, ctypes.byref(task)
        ) != 0:
            return None
        try:
            token = _AuditToken()
            count = ctypes.c_uint32(self._TASK_AUDIT_TOKEN_COUNT)
            result = self.system.task_info(
                task.value,
                self._TASK_AUDIT_TOKEN,
                ctypes.byref(token),
                ctypes.byref(count),
            )
            if result != 0 or count.value != self._TASK_AUDIT_TOKEN_COUNT:
                return None
            return tuple(int(value) for value in token.values)
        finally:
            if self.system.mach_port_deallocate(
                self.mach_task_self, task.value
            ) != 0:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: cannot release Mach task port"
                )

    def _audit_token(self, identity: _DarwinIdentity) -> tuple[int, ...] | None:
        return self._audit_token_for_pid(identity.pid)

    def _terminate_new_snapshot_processes(
        self, pids: Sequence[int]
    ) -> tuple[int, bool, frozenset[int]]:
        baseline_pids = {identity.pid for identity in self.baseline}
        matches = 0
        unstable = False
        terminated: set[int] = set()
        for pid in pids:
            if pid in baseline_pids:
                continue
            token = self._audit_token_for_pid(pid)
            if token is None:
                unstable = True
                continue
            token_pid, token_euid, token_pidversion = self._decode_audit_token(token)
            if token_pid != pid:
                unstable = True
                continue
            denied = self._sandbox_decision_by_token(token, self.tag_denied)
            control = self._sandbox_decision_by_token(token, self.tag_control)
            tagged = denied == 1 and control == 0
            if tagged:
                matches += 1
            identity = self._identity(pid)
            if (
                identity is None
                or identity.real_uid != self.real_uid
                or identity.uid != token_euid
                or identity.id_version != token_pidversion
            ):
                unstable = True
                continue
            if denied not in (0, 1) or control not in (0, 1):
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: audit-token tag query failed"
                )
            if not tagged:
                continue
            if self._signal_exact(_DarwinTaggedProcess(identity, token)):
                terminated.add(pid)
            else:
                unstable = True
        return matches, unstable, frozenset(terminated)

    def _scan_tagged(
        self, *, include_baseline: bool = False, terminate: bool = False
    ) -> tuple[int, bool]:
        matches = 0
        strict_churn = not include_baseline or terminate
        pids = self._pids()
        if terminate:
            fast_matches, fast_unstable, terminated = (
                self._terminate_new_snapshot_processes(pids)
            )
            matches += fast_matches
        else:
            fast_unstable = False
            terminated = frozenset()
        identities, snapshot_unstable = self._snapshot_identities(
            tuple(pid for pid in pids if pid not in terminated)
        )
        unstable = snapshot_unstable
        unstable = unstable or fast_unstable
        for identity in identities:
            if not include_baseline and identity in self.baseline:
                continue
            denied = self._sandbox_decision(identity.pid, self.tag_denied)
            if self._identity(identity.pid) != identity:
                unstable = unstable or strict_churn or denied == 1
                continue
            if denied not in (0, 1):
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: sandbox tag query failed"
                )
            control = self._sandbox_decision(identity.pid, self.tag_control)
            if self._identity(identity.pid) != identity:
                unstable = unstable or strict_churn or (denied == 1 and control == 0)
                continue
            if control not in (0, 1):
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: sandbox tag query failed"
                )
            if denied != 1 or control != 0:
                continue
            matches += 1
            token = self._audit_token(identity)
            if self._identity(identity.pid) != identity:
                unstable = True
                continue
            if token is None:
                if terminate:
                    unstable = True
                    continue
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: invalid process audit token"
                )
            token_pid, token_euid, token_pidversion = self._decode_audit_token(token)
            if (
                token_pid != identity.pid
                or token_euid != identity.uid
                or token_pidversion != identity.id_version
            ):
                if self._identity(identity.pid) != identity:
                    unstable = True
                    continue
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: invalid process audit token"
                )
            denied = self._sandbox_decision_by_token(token, self.tag_denied)
            control = self._sandbox_decision_by_token(token, self.tag_control)
            if denied != 1 or control != 0:
                if self._identity(identity.pid) != identity:
                    unstable = True
                    continue
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: audit-token tag changed"
                )
            if self._identity(identity.pid) != identity:
                unstable = True
                continue
            if terminate and not self._signal_exact(
                _DarwinTaggedProcess(identity, token)
            ):
                unstable = True
        return matches, unstable

    def _preflight_no_tagged_processes(self) -> None:
        deadline = time.monotonic() + 0.25
        while True:
            matches, unstable = self._scan_tagged(include_baseline=True)
            if matches:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: sandbox tag collision"
                )
            if not unstable:
                return
            if time.monotonic() >= deadline:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: Darwin process list is unstable"
                )
            time.sleep(0.001)

    def _signal_exact(self, process: _DarwinTaggedProcess) -> bool:
        token = self._make_audit_token(process.audit_token)
        result = self.proc.proc_signal_with_audittoken(
            ctypes.byref(token), signal.SIGKILL
        )
        if result == 0:
            return True
        if result == errno.ESRCH:
            return False
        raise InfrastructureError(
            "process supervision is INCONCLUSIVE: cannot signal tagged process"
        )

    def launch(
        self, command: Sequence[str]
    ) -> tuple[tuple[str, ...], tuple[int, ...], int | None]:
        return tuple(command), (), None

    def register(self, process: subprocess.Popen[bytes], release_fd: int | None) -> None:
        if release_fd is not None:
            raise InfrastructureError("unexpected Darwin supervisor release descriptor")

    def poll(self, timeout: float = 0) -> None:
        if timeout:
            time.sleep(timeout)

    def cleanup(self) -> None:
        deadline = time.monotonic() + 2
        quiet = 0
        while quiet < 2:
            matches, unstable = self._cleanup_scan()
            quiet = quiet + 1 if matches == 0 and not unstable else 0
            if time.monotonic() >= deadline:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: tagged processes survived cleanup"
                )
            time.sleep(0.001 if matches or unstable else 0.01)

    def _cleanup_scan(self) -> tuple[int, bool]:
        return self._scan_tagged(terminate=True)

    def close(self) -> None:
        pass


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


def _process_supervisor(
    sandbox_tag: tuple[Path, Path] | None = None,
) -> _DarwinProcessSupervisor | _LinuxProcessSupervisor:
    system = platform.system()
    if system == "Darwin":
        return _DarwinProcessSupervisor(sandbox_tag)
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
    _process_attestation: _ProcessAttestation | None = None,
    _sandbox_tag: tuple[Path, Path] | None = None,
    timeout_seconds: int = TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> CommandResult:
    if isolation is not None and _process_attestation is not None:
        raise IntegrityError("process attestation must come from the isolation contract")
    attestation = isolation._process_attestation if isolation else _process_attestation
    if attestation is not None and not _verify_process_attestation(attestation):
        raise InfrastructureError("process supervision attestation is invalid")
    if isolation is None and not trusted_offline and attestation is None:
        raise IntegrityError("unisolated command execution is allowed only for offline self-checks")
    if attestation is not None:
        attested_tag = (
            (Path(attestation.tag_denied), Path(attestation.tag_control))
            if attestation.tag_denied and attestation.tag_control
            else None
        )
        if _sandbox_tag is not None and _sandbox_tag != attested_tag:
            raise IntegrityError("sandbox tag must come from the process attestation")
        _sandbox_tag = attested_tag
    actual = isolation.wrap(command, cwd) if isolation else tuple(command)
    supervisor = (
        _process_supervisor(_sandbox_tag)
        if isolation is not None
        or require_process_supervision
        or _verify_process_attestation(attestation)
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
    workspace_after_group: dict[str, str] | None = None
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
                        _descriptor_tree_manifest(
                            monitor_workspace, include_root_git=True
                        )
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
                if monitor_workspace is not None:
                    try:
                        workspace_after_group = _descriptor_tree_manifest(
                            monitor_workspace, include_root_git=True
                        )
                    except IntegrityError:
                        tree_limited = True
                supervisor.cleanup()
                supervisor.close()
                supervisor_closed = True
                if workspace_after_group is not None:
                    try:
                        workspace_after_cleanup = _descriptor_tree_manifest(
                            monitor_workspace, include_root_git=True
                        )
                    except IntegrityError as exc:
                        raise InfrastructureError(
                            "workspace changed during descendant cleanup"
                        ) from exc
                    if workspace_after_cleanup != workspace_after_group:
                        raise InfrastructureError(
                            "workspace changed during descendant cleanup"
                        )
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


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _attest_process_boundary(
    contract: ModelSourceIsolation | None = None,
) -> _ProcessAttestation:
    system = platform.system()
    with tempfile.TemporaryDirectory(prefix="coding-gate-boundary-probe-") as temporary:
        environment_root = Path(temporary)
        if system == "Darwin":
            if contract is None or contract.sandbox_tag is None:
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: model sandbox tag is absent"
                )
            root = contract.workspace
            sandbox_tag = contract.sandbox_tag
        else:
            root = environment_root
            sandbox_tag = None
        suffix = secrets.token_hex(8)
        marker = root / f".boundary-escaped-{suffix}"
        pid_file = root / f".boundary-pid-{suffix}"
        child = (
            "import time; from pathlib import Path; "
            f"time.sleep(0.2); Path({str(marker)!r}).write_text('escaped'); time.sleep(2)"
        )
        parent = (
            "import os,subprocess,sys; from pathlib import Path; "
            "env=dict(os.environ); env.pop('CODEX_CODING_GATE_RUN_TOKEN',None); "
            f"child=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,"
            "env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"Path({str(pid_file)!r}).write_text(str(child.pid))"
        )
        command = (sys.executable, "-I", "-c", parent)
        if contract is not None:
            command = contract.sandbox_command(command)
        env = validation_environment(environment_root / "environment")
        descendant_pid: int | None = None
        try:
            result = run_bounded(
                command,
                cwd=root,
                env=env,
                trusted_offline=True,
                require_process_supervision=True,
                _sandbox_tag=sandbox_tag,
                timeout_seconds=1,
            )
            if result.returncode != 0 or not pid_file.is_file():
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: descendant probe did not run"
                )
            descendant_pid = int(pid_file.read_text())
            time.sleep(0.25)
            if marker.exists() or (system == "Linux" and _pid_exists(descendant_pid)):
                raise InfrastructureError(
                    "process supervision is INCONCLUSIVE: detached descendant escaped"
                )
        finally:
            marker.unlink(missing_ok=True)
            pid_file.unlink(missing_ok=True)
        nonce = secrets.token_hex(16)
        tag_denied = str(sandbox_tag[0]) if sandbox_tag is not None else ""
        tag_control = str(sandbox_tag[1]) if sandbox_tag is not None else ""
        signature = _attestation_signature(
            "process",
            {
                "system": system,
                "nonce": nonce,
                "supervisor": _SUPERVISOR_GENERATION,
                "tag_denied": tag_denied,
                "tag_control": tag_control,
            },
        )
        return _ProcessAttestation(
            system, nonce, tag_denied, tag_control, signature
        )


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
