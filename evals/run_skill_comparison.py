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
import secrets
import shutil
import sqlite3
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
VALIDATORS = SEEDS / "_validators"
DEFAULT_POLICY = ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md"
DEFAULT_OUTPUT = ROOT / ".local-fixtures" / "skill-comparison-quality"

SCHEMA_VERSION = 1
TRIALS = 2
ARMS = ("native_low", "candidate_runtime")
MODEL_VERBOSITY = "low"
EXPECTED_CALLS = 12
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
    "version": 1,
    "tasks": 3,
    "arms": list(ARMS),
    "trials": TRIALS,
    "calls": EXPECTED_CALLS,
    "model_and_effort_are_explicit": True,
    "model_verbosity": MODEL_VERBOSITY,
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "sandbox_network": False,
    "disabled_features": list(DISABLED_FEATURES),
    "ignore_user_config": True,
    "ignore_rules": True,
    "ephemeral": True,
    "prompt_transport": "stdin",
    "prompt_preflight": "exact task and exact candidate policy",
    "isolation": "fresh HOME, CODEX_HOME, cwd, and Git repository per call",
    "source_isolation": (
        "outer macOS Seatbelt denies reads and writes to the real user home, source worktree, "
        "and common Git repository"
    ),
    "order": "secret HMAC order with three first-runs per arm",
    "validation": (
        "strict production paths, production diff, diff-check, successful test command in trace, "
        "pristine canonical tests, and post-run hidden validator"
    ),
    "gate": "both arms 6/6; native failure is inconclusive",
}


@dataclass(frozen=True)
class Project:
    key: str
    title: str
    task: str
    check: tuple[str, ...]
    expected_seed_failure: str
    allowed_paths: tuple[str, ...]
    validator: Path
    validator_destination: str
    validator_check: tuple[str, ...]
    trace_test_pattern: re.Pattern[str]

    @property
    def root(self) -> Path:
        return SEEDS / self.key


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
class IsolatedRun:
    root: Path
    home: Path
    codex_home: Path
    workspace: Path
    env: dict[str, str]


@dataclass(frozen=True)
class SourceIsolation:
    executable: Path
    profile: str
    protected_roots: tuple[Path, ...]

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "platform": "macOS Seatbelt",
            "executable": str(self.executable),
            "profile_sha256": sha256_text(self.profile),
            "protected_roots": [str(path) for path in self.protected_roots],
            "enforcement": (
                "deny file-read and file-write for real user home, source worktree, "
                "and common repository"
            ),
        }


SHELL_COMMAND_BOUNDARY = r"(?:^|(?:&&|\|\||;|\n)\s*|(?:-lc|--command)\s+['\"]\s*)"
SHELL_ENV_PREFIX = r"(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)"
PYTHON_TEST_PATTERN = re.compile(
    SHELL_COMMAND_BOUNDARY
    + SHELL_ENV_PREFIX
    + r"(?:(?:python(?:3(?:\.\d+)?)?)\s+-m\s+(?:unittest|pytest)\b|pytest\b)"
)
NODE_TEST_PATTERN = re.compile(
    SHELL_COMMAND_BOUNDARY
    + SHELL_ENV_PREFIX
    + r"(?:npm\s+(?:run\s+)?test\b|node\s+--test\b)"
)

PROJECTS = (
    Project(
        key="node-auth-api",
        title="Node auth API",
        task=textwrap.dedent(
            """
            We have an auth bug: expired sessions are still accepted.

            Inspect the project, fix the bug, and run the relevant tests. Then give
            an engineering handoff with the root cause, files changed, validation
            command and result, and any remaining risk. Answer in English.
            """
        ).strip(),
        check=("npm", "test"),
        expected_seed_failure="200 !== 401",
        allowed_paths=("src/middleware.js",),
        validator=VALIDATORS / "node-auth-api.test.js",
        validator_destination="_quality_validator.test.js",
        validator_check=("node", "--test", "_quality_validator.test.js"),
        trace_test_pattern=NODE_TEST_PATTERN,
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
            command and result, and any remaining risk. Answer in English.
            """
        ).strip(),
        check=("python3", "-m", "unittest", "-v"),
        expected_seed_failure="'ch_2' != 'ch_1'",
        allowed_paths=("ledger.py",),
        validator=VALIDATORS / "python-payment-ledger.py",
        validator_destination="_quality_validator.py",
        validator_check=("python3", "_quality_validator.py", "-v"),
        trace_test_pattern=PYTHON_TEST_PATTERN,
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
            command and result, and any remaining risk. Answer in English.
            """
        ).strip(),
        check=("python3", "-m", "unittest", "-v"),
        expected_seed_failure="no such column: expires_at",
        allowed_paths=("rollout.py",),
        validator=VALIDATORS / "sqlite-rollout-runner.py",
        validator_destination="_quality_validator.py",
        validator_check=("python3", "_quality_validator.py", "-v"),
        trace_test_pattern=PYTHON_TEST_PATTERN,
    ),
)


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


def codex_version(executable: str) -> str:
    process = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True
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
    ).stdout
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise RuntimeError("source Git commit is invalid")
    return commit, bool(status.strip())


def runtime_versions() -> dict[str, str]:
    def version(command: Sequence[str]) -> str:
        process = subprocess.run(command, capture_output=True, text=True, check=True)
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


def _seatbelt_literal(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def source_isolation_contract(repository: Path = ROOT) -> SourceIsolation:
    executable = Path("/usr/bin/sandbox-exec")
    if platform.system() != "Darwin" or not executable.is_file():
        raise RuntimeError("live repo-quality runs require macOS /usr/bin/sandbox-exec")
    common_process = subprocess.run(
        ("git", "rev-parse", "--git-common-dir"),
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    common = Path(common_process.stdout.strip())
    if not common.is_absolute():
        common = repository / common
    common_repository = common.resolve().parent
    protected = tuple(
        dict.fromkeys((Path.home().resolve(), repository.resolve(), common_repository))
    )
    clauses = ["(version 1)", "(allow default)"]
    for root in protected:
        literal = _seatbelt_literal(root)
        clauses.append(f'(deny file-read* (subpath "{literal}"))')
        clauses.append(f'(deny file-write* (subpath "{literal}"))')
    profile = "".join(clauses)
    return SourceIsolation(executable, profile, protected)


def source_isolated_command(
    command: Sequence[str], isolation: SourceIsolation
) -> list[str]:
    return [str(isolation.executable), "-p", isolation.profile, *command]


def default_auth_file() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"


def ensure_fixture_contract() -> dict[str, dict[str, Any]]:
    if len(PROJECTS) != 3 or len({project.key for project in PROJECTS}) != 3:
        raise RuntimeError("repo-quality lane requires exactly three unique projects")
    details: dict[str, dict[str, Any]] = {}
    for project in PROJECTS:
        if not project.root.is_dir() or not project.validator.is_file():
            raise FileNotFoundError(f"missing fixture or validator: {project.key}")
        if any(path.is_symlink() for path in project.root.rglob("*")):
            raise RuntimeError(f"fixture contains symlink: {project.key}")
        missing_allowed = [
            relative
            for relative in project.allowed_paths
            if not (project.root / relative).is_file()
        ]
        if missing_allowed:
            raise RuntimeError(f"missing allowed production path: {missing_allowed[0]}")
        process = run(project.check, cwd=project.root)
        output = process.stdout + process.stderr
        if process.returncode == 0 or project.expected_seed_failure not in output:
            raise RuntimeError(f"unexpected canonical seed failure: {project.key}")
        details[project.key] = {
            "fixture_sha256": tree_sha256(project.root),
            "validator_sha256": sha256_file(project.validator),
            "seed_check": list(project.check),
            "expected_seed_failure_sha256": sha256_text(project.expected_seed_failure),
            "allowed_paths": list(project.allowed_paths),
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


def build_codex_command(
    *, executable: str, model: str, effort: str, workspace: Path
) -> list[str]:
    command = [executable]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    command.extend(
        (
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "--model",
            model,
            "--config",
            f"model_reasoning_effort={toml_string(effort)}",
            "--config",
            f"model_verbosity={toml_string(MODEL_VERBOSITY)}",
            "--config",
            "sandbox_workspace_write.network_access=false",
            "--strict-config",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--cd",
            str(workspace),
            "-",
        )
    )
    return command


def build_preflight_command(
    *, executable: str, model: str, effort: str, prompt: str
) -> list[str]:
    command = [executable]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    command.extend(
        (
            "--config",
            f"model={toml_string(model)}",
            "--config",
            f"model_reasoning_effort={toml_string(effort)}",
            "--config",
            f"model_verbosity={toml_string(MODEL_VERBOSITY)}",
            "--config",
            "sandbox_workspace_write.network_access=false",
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
    with tempfile.TemporaryDirectory(prefix="codex-quality-") as temporary:
        root = Path(temporary)
        home = root / "home"
        codex_home = home / ".codex"
        workspace = root / "workspace"
        codex_home.mkdir(parents=True, mode=0o700)
        workspace.mkdir(mode=0o700)
        if not auth_source.is_file():
            raise FileNotFoundError("Codex auth file not found")
        auth_destination = codex_home / "auth.json"
        atomic_copy(auth_source, auth_destination)
        if arm.policy is not None:
            atomic_write_text(codex_home / "AGENTS.md", arm.policy)
        agents_files = list(home.rglob("AGENTS.md"))
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
    shutil.copytree(project.root, isolated.workspace, dirs_exist_ok=True)
    if list(isolated.workspace.rglob("AGENTS.md")):
        raise RuntimeError("fixture contains unexpected AGENTS.md")
    return initialize_git_repository(isolated.workspace, isolated.env)


def initialize_git_repository(workspace: Path, env: Mapping[str, str]) -> str:
    commands = (
        ("git", "init", "--quiet"),
        ("git", "add", "."),
        (
            "git",
            "-c",
            "user.name=Codex Quality Eval",
            "-c",
            "user.email=codex-quality@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Pristine failing fixture",
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
        executable=executable, model=model, effort=effort, prompt=project.task
    )
    process = run(
        source_isolated_command(command, source_isolation),
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
) -> tuple[subprocess.CompletedProcess[str], int]:
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # Child Seatbelt denies the source worktree, which also contains the ignored
    # output directory. Capture through a system-temp descriptor; the parent
    # copies it into the artifact directory after the child exits.
    descriptor, temporary_name = tempfile.mkstemp(prefix="codex-quality-raw-")
    temporary = Path(temporary_name)
    started = time.monotonic()
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as raw:
            try:
                process = subprocess.run(
                    list(command),
                    input=prompt,
                    stdout=raw,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=dict(env),
                    text=True,
                    timeout=timeout_seconds,
                )
            finally:
                raw.flush()
                os.fsync(raw.fileno())
        os.chmod(temporary, 0o600)
        atomic_copy(temporary, raw_path)
        temporary.unlink()
    except BaseException:
        if temporary.exists():
            os.chmod(temporary, 0o600)
            atomic_copy(temporary, raw_path)
            temporary.unlink()
        raise
    return process, round((time.monotonic() - started) * 1000)


def parse_codex_trace(
    path: Path, *, test_pattern: re.Pattern[str], max_raw_bytes: int
) -> dict[str, Any]:
    size = path.stat().st_size
    if size > max_raw_bytes:
        raise ValueError(f"raw JSONL exceeds byte cap ({size} > {max_raw_bytes})")
    final_text = ""
    usage: dict[str, int] = {}
    successful_commands: list[str] = []
    event_count = 0
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
        event_count += 1
        if event["type"] == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise ValueError(f"raw JSONL line {line_number} has invalid item")
            if item.get("type") == "agent_message" and isinstance(
                item.get("text"), str
            ):
                final_text = item["text"]
            if (
                item.get("type") == "command_execution"
                and item.get("exit_code") == 0
                and item.get("status") in (None, "completed")
                and isinstance(item.get("command"), str)
            ):
                successful_commands.append(item["command"])
        elif event["type"] == "turn.completed":
            raw_usage = event.get("usage")
            if not isinstance(raw_usage, dict):
                raise ValueError("turn.completed has no usage object")
            usage = {
                key: int(value)
                for key, value in raw_usage.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    if not final_text.strip():
        raise ValueError("Codex JSONL has no final agent_message")
    if not usage:
        raise ValueError("Codex JSONL has no turn.completed usage")
    return {
        "answer": final_text,
        "usage": usage,
        "event_count": event_count,
        "successful_commands": successful_commands,
        "tests_invoked": any(
            test_pattern.search(command) for command in successful_commands
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


def collect_repository_evidence(
    *, project: Project, workspace: Path, baseline: str, env: Mapping[str, str]
) -> dict[str, Any]:
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
        ("diff", "--binary", baseline, "--", *project.allowed_paths),
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
    }


def _command_evidence(
    process: subprocess.CompletedProcess[str], command: Sequence[str]
) -> dict[str, Any]:
    return {
        "command": list(command),
        "exit_code": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


def validate_production_patch(
    *,
    project: Project,
    production_patch: str,
    trace_tests_invoked: bool,
    repository_evidence: Mapping[str, Any],
    parent: Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    validation = parent / "validation"
    if validation.exists():
        shutil.rmtree(validation)
    shutil.copytree(project.root, validation)
    apply_process = run(
        ("git", "apply", "--whitespace=nowarn", "-"),
        cwd=validation,
        env=env,
        input_text=production_patch,
    )
    canonical = run(project.check, cwd=validation, env=env)

    validator_destination = validation / project.validator_destination
    if validator_destination.exists():
        raise RuntimeError("validator destination exists in canonical fixture")
    shutil.copyfile(project.validator, validator_destination)
    validator = run(project.validator_check, cwd=validation, env=env)

    checks = {
        "production_patch_applied": apply_process.returncode == 0,
        "production_diff_present": bool(repository_evidence["has_production_diff"]),
        "paths_allowed": bool(repository_evidence["paths_allowed"]),
        "diff_check_passed": repository_evidence["diff_check_exit"] == 0,
        "tests_invoked_in_trace": trace_tests_invoked,
        "canonical_tests_restored": True,
        "canonical_tests_passed": canonical.returncode == 0,
        "validator_injected_after_codex": True,
        "hidden_validator_passed": validator.returncode == 0,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "apply": _command_evidence(apply_process, ("git", "apply", "-")),
        "canonical": _command_evidence(canonical, project.check),
        "hidden_validator": _command_evidence(validator, project.validator_check),
        "validation_source": "pristine fixture plus production-only patch",
    }


def replay_repository_evidence(
    *,
    project: Project,
    full_patch: str,
    expected_production_patch: str,
    parent: Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    workspace = parent / "repository-replay"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(project.root, workspace)
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
    validator_sha256: str,
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
        "validator_sha256": validator_sha256,
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
        raw_path, test_pattern=project.trace_test_pattern, max_raw_bytes=max_raw_bytes
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
        env=env,
    )
    result["validation"] = validation
    result["repository"] = repository
    result["trace_event_count"] = trace["event_count"]
    result["trace_successful_commands"] = trace["successful_commands"]
    atomic_write_json(result_path, result)
    return result


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
            workspace=isolated.workspace,
        )
        process, duration_ms = _run_with_atomic_stdout(
            source_isolated_command(command, source_isolation),
            prompt=project.task,
            cwd=isolated.workspace,
            env=isolated.env,
            raw_path=raw_path,
            timeout_seconds=timeout_seconds,
        )
        atomic_write_text(stderr_path, process.stderr)
        if process.returncode != 0:
            raise RuntimeError(
                f"Codex run failed: {key.project}/{key.arm}/{key.trial}, exit {process.returncode}"
            )
        trace = parse_codex_trace(
            raw_path,
            test_pattern=project.trace_test_pattern,
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
        validation = validate_production_patch(
            project=project,
            production_patch=production_patch_path.read_text(encoding="utf-8"),
            trace_tests_invoked=bool(trace["tests_invoked"]),
            repository_evidence=repository,
            parent=isolated.root,
            env=isolated.env,
        )
    result = {
        **identity,
        "answer": trace["answer"],
        "usage": trace["usage"],
        "trace_event_count": trace["event_count"],
        "trace_successful_commands": trace["successful_commands"],
        "duration_ms": duration_ms,
        "raw_sha256": sha256_file(raw_path),
        "stderr_sha256": sha256_file(stderr_path),
        "full_patch_sha256": sha256_file(full_patch_path),
        "production_patch_sha256": sha256_file(production_patch_path),
        "repository": repository,
        "validation": validation,
    }
    atomic_write_json(result_path, result)
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
        "runner_sha256": runner_sha256,
        "source_git_commit": source_git_commit,
        "source_git_dirty": source_git_dirty,
        "runtime_versions": dict(runtimes),
        "source_isolation": source_isolation.metadata,
        "limits": {
            "timeout_seconds": args.timeout_seconds,
            "max_calls": args.max_calls,
            "max_input_chars_per_call": args.max_input_chars_per_call,
            "max_total_input_chars": args.max_total_input_chars,
            "max_raw_bytes_per_call": args.max_raw_bytes_per_call,
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
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("config_sha256") != config_sha256:
            raise RuntimeError(
                "output directory belongs to a different repo-quality config"
            )
        return manifest
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "quality_" + uuid.uuid4().hex,
        "created_at": utc_now(),
        "schedule_secret": secrets.token_hex(32),
        "config_sha256": config_sha256,
        "config": dict(config),
    }
    atomic_write_json(path, manifest)
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
        raise RuntimeError("private manifest is incomplete")
    schedule = secret_balanced_schedule(str(secret), planned_run_keys())
    payload = schedule_payload(
        run_id=str(run_id), config_sha256=str(config_sha256), schedule=schedule
    )
    payload_sha256 = sha256_text(canonical_json(payload))
    committed_sha256 = manifest.get("schedule_sha256")
    if committed_sha256 is not None and committed_sha256 != payload_sha256:
        raise RuntimeError("private manifest schedule hash mismatch")
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError("private execution schedule mismatch")
    else:
        atomic_write_json(path, payload)
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


def _main(args: argparse.Namespace) -> int:
    fixtures = ensure_fixture_contract()
    policy = args.candidate_policy.read_text(encoding="utf-8")
    arms = build_arms(policy)
    caps = _validate_caps(args, arms)
    cli_version = codex_version(args.codex)
    runner_sha256 = sha256_text(Path(__file__).read_text(encoding="utf-8"))
    source_commit, source_dirty = source_git_provenance()
    runtimes = runtime_versions()
    source_isolation = source_isolation_contract()
    if not args.dry_run and source_dirty:
        raise RuntimeError("live repo-quality run requires a clean source Git checkout")
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
    run_id = str(manifest["run_id"])
    projects = {project.key: project for project in PROJECTS}
    total = len(schedule)
    results: list[dict[str, Any]] = []

    if not args.resume:
        completed = list((private / "runs").glob("*.json"))
        if completed:
            raise RuntimeError(
                "completed runs already exist; enable resume or use another output"
            )

    with tempfile.TemporaryDirectory(prefix="codex-quality-auth-") as auth_temporary:
        auth_cache = Path(auth_temporary) / "auth.json"
        atomic_copy(args.auth_file, auth_cache)
        for index, key in enumerate(schedule, 1):
            project = projects[key.project]
            arm = arms[key.arm]
            private_id = private_run_id(config_sha256, key)
            raw_path = private / "raw" / f"{private_id}.jsonl"
            stderr_path = private / "raw" / f"{private_id}.stderr.txt"
            full_patch_path = private / "patches" / f"{private_id}.full.diff"
            production_patch_path = (
                private / "patches" / f"{private_id}.production.diff"
            )
            result_path = private / "runs" / f"{private_id}.json"
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
                validator_sha256=str(fixtures[key.project]["validator_sha256"]),
            )
            result = None
            if args.resume:
                with tempfile.TemporaryDirectory(
                    prefix="codex-quality-revalidate-"
                ) as temporary:
                    result = load_resumable_result(
                        result_path=result_path,
                        raw_path=raw_path,
                        stderr_path=stderr_path,
                        full_patch_path=full_patch_path,
                        production_patch_path=production_patch_path,
                        expected_identity=identity,
                        project=project,
                        max_raw_bytes=args.max_raw_bytes_per_call,
                        validation_parent=Path(temporary),
                        env=safe_environment(),
                    )
            if result is None:
                print(
                    f"[{index}/{total}] {key.project} | {key.arm} | trial {key.trial}",
                    file=sys.stderr,
                    flush=True,
                )
                result = execute_run(
                    executable=args.codex,
                    auth_source=auth_cache,
                    auth_sink=auth_cache,
                    project=project,
                    arm=arm,
                    key=key,
                    identity=identity,
                    model=args.model,
                    effort=args.effort,
                    raw_path=raw_path,
                    stderr_path=stderr_path,
                    full_patch_path=full_patch_path,
                    production_patch_path=production_patch_path,
                    result_path=result_path,
                    timeout_seconds=args.timeout_seconds,
                    max_raw_bytes=args.max_raw_bytes_per_call,
                    source_isolation=source_isolation,
                )
            else:
                print(
                    f"[{index}/{total}] resume {key.project} | {key.arm} | trial {key.trial}",
                    file=sys.stderr,
                    flush=True,
                )
            results.append(result)

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
        "gate": gate,
        "runs": [
            {
                "project": result["project"],
                "arm": result["arm"],
                "trial": result["trial"],
                "passed": result["validation"]["passed"],
                "result_sha256": sha256_file(
                    private
                    / "runs"
                    / f"{private_run_id(config_sha256, RunKey(result['project'], result['arm'], result['trial']))}.json"
                ),
            }
            for result in results
        ],
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


if __name__ == "__main__":
    raise SystemExit(main())
