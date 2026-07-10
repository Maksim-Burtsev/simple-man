#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from review_lib import DEFAULT_ARMS, SCHEMA_VERSION, ArmSpec, RunKey
from review_lib import (
    atomic_copy_file,
    atomic_write_json,
    atomic_write_text,
    build_arm_specs,
)
from review_lib import build_blind_bundle, canonical_json, isolated_codex_environment
from review_lib import load_prompts, parse_codex_jsonl, private_run_id
from review_lib import prompt_corpus_sha256, sha256_file, sha256_text
from review_lib import secret_seeded_execution_schedule
from review_lib import validate_prompt_contamination


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS = ROOT / "evals" / "prompts" / "review_smoke.jsonl"
DEFAULT_POLICY = ROOT / "AGENTS.md.snippet"
DEFAULT_OUTPUT = ROOT / ".local-fixtures" / "blind-review"
DISABLED_FEATURES = (
    "apps",
    "plugins",
    "plugin_hooks",
    "tool_search",
    "hooks",
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
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
EXECUTION_CONTRACT_VERSION = 3
EXECUTION_CONTRACT = {
    "version": EXECUTION_CONTRACT_VERSION,
    "schema_version": SCHEMA_VERSION,
    "disabled_features": list(DISABLED_FEATURES),
    "approval_policy": "never",
    "sandbox": "read-only",
    "model_is_explicit": True,
    "reasoning_effort_is_explicit": True,
    "model_verbosity_is_explicit": True,
    "exec_jsonl": True,
    "ephemeral": True,
    "ignore_user_config": True,
    "ignore_rules": True,
    "skip_git_repo_check": True,
    "prompt_transport": "stdin",
    "prompt_input_preflight": "codex debug prompt-input",
    "tool_events": "fail_closed",
    "isolation": "fresh HOME, CODEX_HOME, cwd per arm/task/trial",
    "output_lock": "nonblocking advisory lock for one live writer",
    "auth_refresh": "atomic copy through temp-only run cache",
}
EXECUTION_CONTRACT_SHA256 = sha256_text(canonical_json(EXECUTION_CONTRACT))


@contextmanager
def output_directory_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    lock_file = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another blind-review run is using output directory: {path.parent.parent}"
            ) from exc
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def codex_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot determine Codex CLI version: {exc}") from exc
    version = result.stdout.strip()
    if not version:
        raise RuntimeError("Codex CLI returned an empty version")
    return version


def source_git_provenance(repository: Path) -> tuple[str, bool]:
    try:
        commit_process = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        )
        status_process = subprocess.run(
            [
                "git",
                "-c",
                "status.showUntrackedFiles=normal",
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot determine source Git provenance: {exc}") from exc
    commit = commit_process.stdout.strip()
    if not commit or "\n" in commit:
        raise RuntimeError("Git returned an invalid source commit")
    return commit, bool(status_process.stdout.strip())


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_codex_command(
    *,
    executable: str,
    model: str,
    effort: str,
    spec: ArmSpec,
    workspace: Path,
) -> list[str]:
    command = [executable]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    command.extend(
        (
            "--ask-for-approval",
            "never",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "--config",
            f"model_reasoning_effort={toml_string(effort)}",
            "--config",
            f"model_verbosity={toml_string(spec.model_verbosity)}",
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


def build_prompt_preflight_command(
    *,
    executable: str,
    model: str,
    effort: str,
    spec: ArmSpec,
    prompt: str,
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
            f"model_verbosity={toml_string(spec.model_verbosity)}",
            "debug",
            "prompt-input",
            prompt,
        )
    )
    return command


def preflight_model_visible_input(
    *,
    executable: str,
    model: str,
    effort: str,
    spec: ArmSpec,
    prompt: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
) -> None:
    command = build_prompt_preflight_command(
        executable=executable,
        model=model,
        effort=effort,
        spec=spec,
        prompt=prompt,
    )
    process = subprocess.run(
        command,
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"Codex prompt-input preflight failed with exit {process.returncode}"
        )
    try:
        model_input = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Codex prompt-input preflight returned invalid JSON"
        ) from exc
    if not isinstance(model_input, list):
        raise RuntimeError("Codex prompt-input preflight did not return a message list")

    text_parts: list[str] = []
    for message in model_input:
        if not isinstance(message, dict):
            continue
        for content in message.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                text_parts.append(content["text"])
    if not text_parts or text_parts[-1] != prompt:
        raise RuntimeError(
            "Codex prompt-input preflight did not preserve the exact task prompt"
        )

    context = "\n".join(text_parts[:-1])
    marker_count = context.count("## Simple Man runtime policy")
    name_count = len(re.findall(r"\bsimple[ _-]+man\b", context, re.IGNORECASE))
    if spec.name == "simple_man_runtime":
        exact_policy_count = context.count(spec.agents_text or "")
        if marker_count != 1 or name_count < 1 or exact_policy_count != 1:
            raise RuntimeError(
                "Simple Man prompt-input preflight expected the exact runtime policy once"
            )
    elif marker_count or name_count:
        raise RuntimeError(f"{spec.name} prompt-input is contaminated by Simple Man")


def select_prompts(
    prompts: Sequence[dict[str, Any]],
    prompt_ids: Sequence[str] | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if prompt_ids:
        by_id = {prompt["id"]: prompt for prompt in prompts}
        missing = [prompt_id for prompt_id in prompt_ids if prompt_id not in by_id]
        if missing:
            raise ValueError(f"unknown prompt id(s): {', '.join(missing)}")
        selected = [by_id[prompt_id] for prompt_id in prompt_ids]
    else:
        selected = list(prompts)
    if limit:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no prompts selected")
    return selected


def config_payload(
    *,
    prompts: Sequence[Mapping[str, Any]],
    arms: Sequence[ArmSpec],
    trials: int,
    model: str,
    effort: str,
    cli_version: str,
    runner_sha256: str,
    source_git_commit: str,
    source_git_dirty: bool,
    timeout_seconds: int = 600,
    max_calls: int = 100,
    max_total_reported_tokens: int = 1_500_000,
    require_clean_source: bool = False,
) -> dict[str, Any]:
    if not source_git_commit:
        raise ValueError("source_git_commit must not be empty")
    if not isinstance(source_git_dirty, bool):
        raise ValueError("source_git_dirty must be a boolean")
    return {
        "schema_version": SCHEMA_VERSION,
        "prompt_corpus_sha256": prompt_corpus_sha256(prompts),
        "prompt_ids": [prompt["id"] for prompt in prompts],
        "arms": [
            {
                "name": arm.name,
                "model_verbosity": arm.model_verbosity,
                "policy_sha256": arm.policy_sha256,
            }
            for arm in arms
        ],
        "trials": trials,
        "model": model,
        "effort": effort,
        "codex_cli_version": cli_version,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "execution_contract_sha256": EXECUTION_CONTRACT_SHA256,
        "execution_contract": EXECUTION_CONTRACT,
        "runner_sha256": runner_sha256,
        "source_git_commit": source_git_commit,
        "source_git_dirty": source_git_dirty,
        "limits": {
            "timeout_seconds": timeout_seconds,
            "max_calls": max_calls,
            "max_total_reported_tokens": max_total_reported_tokens,
        },
        "require_clean_source": require_clean_source,
    }


def load_or_create_manifest(
    path: Path,
    *,
    config: Mapping[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    config_sha256 = sha256_text(canonical_json(config))
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("config_sha256") != config_sha256:
            raise RuntimeError(
                "output directory belongs to a different benchmark config; use another --output-dir"
            )
        return manifest

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "blind_" + uuid.uuid4().hex,
        "created_at": utc_now(),
        "blinding_secret": secrets.token_hex(32),
        "config_sha256": config_sha256,
        "config": dict(config),
    }
    if not dry_run:
        atomic_write_json(path, manifest)
    return manifest


def execution_schedule_payload(
    *,
    run_id: str,
    config_sha256: str,
    run_keys: Sequence[RunKey],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "config_sha256": config_sha256,
        "runs": [
            {
                "task_id": key.task_id,
                "arm": key.arm,
                "trial": key.trial,
            }
            for key in run_keys
        ],
    }


def load_or_create_execution_schedule(
    path: Path,
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    config_sha256: str,
    run_keys: Sequence[RunKey],
) -> list[RunKey]:
    blinding_secret = manifest.get("blinding_secret")
    if not isinstance(blinding_secret, str) or not blinding_secret:
        raise RuntimeError("private manifest has no blinding secret")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("private manifest has no run id")

    expected_schedule = secret_seeded_execution_schedule(blinding_secret, run_keys)
    expected_payload = execution_schedule_payload(
        run_id=run_id,
        config_sha256=config_sha256,
        run_keys=expected_schedule,
    )
    expected_sha256 = sha256_text(canonical_json(expected_payload))
    committed_sha256 = manifest.get("execution_schedule_sha256")
    if committed_sha256 is not None and committed_sha256 != expected_sha256:
        raise RuntimeError("private manifest execution schedule hash mismatch")

    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("private execution schedule is invalid JSON") from exc
        if sha256_text(canonical_json(payload)) != expected_sha256:
            raise RuntimeError("private execution schedule hash mismatch")
        if payload != expected_payload:
            raise RuntimeError(
                "private execution schedule differs from seeded schedule"
            )
    else:
        if committed_sha256 is not None:
            raise RuntimeError("committed private execution schedule is missing")
        atomic_write_json(path, expected_payload)

    if committed_sha256 is None:
        manifest["execution_schedule_sha256"] = expected_sha256
        atomic_write_json(manifest_path, manifest)
    return expected_schedule


def result_identity(
    *,
    run_id: str,
    key: RunKey,
    prompt: str,
    spec: ArmSpec,
    model: str,
    effort: str,
    cli_version: str,
    runner_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": key.task_id,
        "arm": key.arm,
        "trial": key.trial,
        "model": model,
        "effort": effort,
        "model_verbosity": spec.model_verbosity,
        "prompt_sha256": sha256_text(prompt),
        "policy_sha256": spec.policy_sha256,
        "codex_cli_version": cli_version,
        "execution_contract_sha256": EXECUTION_CONTRACT_SHA256,
        "runner_sha256": runner_sha256,
    }


def load_resumable_result(
    path: Path,
    *,
    raw_path: Path,
    expected_identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"resume result is invalid JSON: {path.name}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"resume result is not an object: {path.name}")
    expected_fields = set(expected_identity) | {
        "text",
        "usage",
        "duration_ms",
        "raw_sha256",
    }
    if set(result) != expected_fields:
        raise RuntimeError(f"resume result fields differ: {path.name}")
    mismatches = [
        key
        for key, expected in expected_identity.items()
        if result.get(key) != expected
    ]
    if mismatches:
        raise RuntimeError(
            f"resume result identity mismatch ({path.name}): {', '.join(mismatches)}"
        )
    if not raw_path.is_file():
        raise RuntimeError(f"resume raw JSONL missing: {raw_path.name}")
    if result.get("raw_sha256") != sha256_file(raw_path):
        raise RuntimeError(f"resume raw JSONL hash mismatch: {raw_path.name}")
    parsed_text, parsed_usage = parse_codex_jsonl(raw_path)
    if result.get("text") != parsed_text or result.get("usage") != parsed_usage:
        raise RuntimeError(f"resume parsed result differs from raw JSONL: {path.name}")
    if not isinstance(result.get("duration_ms"), int) or result["duration_ms"] < 0:
        raise RuntimeError(f"resume duration is invalid: {path.name}")
    return result


def reported_tokens(usage: Mapping[str, int]) -> int:
    if "total_tokens" in usage:
        return usage["total_tokens"]
    if "input_tokens" in usage and "output_tokens" in usage:
        return usage["input_tokens"] + usage["output_tokens"]
    raise ValueError(
        "answer usage must contain total_tokens or both input_tokens and output_tokens"
    )


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
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{raw_path.name}.", dir=raw_path.parent
    )
    temporary = Path(temporary_name)
    started = time.monotonic()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as raw:
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
        os.replace(temporary, raw_path)
    except BaseException:
        if temporary.exists():
            os.chmod(temporary, 0o600)
            os.replace(temporary, raw_path)
        raise
    duration_ms = round((time.monotonic() - started) * 1000)
    return process, duration_ms


def execute_run(
    *,
    executable: str,
    auth_source: Path,
    auth_sink: Path,
    prompt: str,
    key: RunKey,
    spec: ArmSpec,
    run_id: str,
    model: str,
    effort: str,
    cli_version: str,
    runner_sha256: str,
    raw_path: Path,
    stderr_path: Path,
    result_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    identity = result_identity(
        run_id=run_id,
        key=key,
        prompt=prompt,
        spec=spec,
        model=model,
        effort=effort,
        cli_version=cli_version,
        runner_sha256=runner_sha256,
    )
    with isolated_codex_environment(
        auth_source=auth_source,
        auth_sink=auth_sink,
        spec=spec,
    ) as isolated:
        preflight_model_visible_input(
            executable=executable,
            model=model,
            effort=effort,
            spec=spec,
            prompt=prompt,
            cwd=isolated.workspace,
            env=isolated.env,
            timeout_seconds=timeout_seconds,
        )
        command = build_codex_command(
            executable=executable,
            model=model,
            effort=effort,
            spec=spec,
            workspace=isolated.workspace,
        )
        process, duration_ms = _run_with_atomic_stdout(
            command,
            prompt=prompt,
            cwd=isolated.workspace,
            env=isolated.env,
            raw_path=raw_path,
            timeout_seconds=timeout_seconds,
        )
    atomic_write_text(stderr_path, process.stderr, mode=0o600)
    if process.returncode != 0:
        raise RuntimeError(f"Codex run {run_id} failed with exit {process.returncode}")

    text, usage = parse_codex_jsonl(raw_path)
    result = {
        **identity,
        "text": text,
        "usage": usage,
        "duration_ms": duration_ms,
        "raw_sha256": sha256_file(raw_path),
    }
    atomic_write_json(result_path, result)
    return result


def default_auth_file() -> Path:
    configured_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return configured_home / "auth.json"


def default_prompts_file() -> Path:
    return DEFAULT_PROMPTS


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hermetic Codex answers for blind A/B review."
    )
    parser.add_argument("--prompts", type=Path, default=default_prompts_file())
    parser.add_argument("--runtime-policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--auth-file", type=Path, default=default_auth_file())
    parser.add_argument("--codex", default=os.environ.get("CODEX", "codex"))
    parser.add_argument("--model", default=os.environ.get("MODEL"))
    parser.add_argument("--effort", default=os.environ.get("EFFORT", "high"))
    parser.add_argument("--arm", action="append")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--prompt-id", action="append")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--max-total-reported-tokens", type=int, default=1_500_000)
    parser.add_argument(
        "--require-clean-source",
        action="store_true",
        help="Fail before live calls unless the source commit is clean and unchanged.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse identity-checked completed runs (default: true).",
    )
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model is required (or set MODEL)")
    if args.trials < 1:
        parser.error("--trials must be >= 1")
    if args.limit < 0:
        parser.error("--limit must be >= 0")
    for field in ("timeout_seconds", "max_calls", "max_total_reported_tokens"):
        if getattr(args, field) < 1:
            parser.error(f"--{field.replace('_', '-')} must be >= 1")
    return args


def _main(args: argparse.Namespace) -> int:
    prompts = select_prompts(
        load_prompts(args.prompts), args.prompt_id, limit=args.limit
    )
    validate_prompt_contamination(prompts)

    runtime_policy = args.runtime_policy.read_text(encoding="utf-8")
    arm_specs = build_arm_specs(runtime_policy)
    arm_names = list(args.arm or DEFAULT_ARMS)
    unknown_arms = [arm for arm in arm_names if arm not in arm_specs]
    if unknown_arms:
        raise ValueError(f"unknown arm(s): {', '.join(unknown_arms)}")
    if len(arm_names) < 2 or len(set(arm_names)) != len(arm_names):
        raise ValueError("select at least two unique arms")
    selected_specs = [arm_specs[arm] for arm in arm_names]
    total = len(prompts) * len(selected_specs) * args.trials
    if total > args.max_calls:
        raise ValueError(
            f"planned calls exceed --max-calls ({total} > {args.max_calls})"
        )

    cli_version = codex_version(args.codex)
    source_git_commit, source_git_dirty = source_git_provenance(ROOT)
    if args.require_clean_source and not args.dry_run and source_git_dirty:
        raise RuntimeError("live blind-review run requires a clean source Git checkout")
    runner_sha256 = sha256_text(
        Path(__file__).read_text(encoding="utf-8")
        + (ROOT / "evals" / "review_lib.py").read_text(encoding="utf-8")
    )
    config = config_payload(
        prompts=prompts,
        arms=selected_specs,
        trials=args.trials,
        model=args.model,
        effort=args.effort,
        cli_version=cli_version,
        runner_sha256=runner_sha256,
        source_git_commit=source_git_commit,
        source_git_dirty=source_git_dirty,
        timeout_seconds=args.timeout_seconds,
        max_calls=args.max_calls,
        max_total_reported_tokens=args.max_total_reported_tokens,
        require_clean_source=args.require_clean_source,
    )
    config_sha256 = sha256_text(canonical_json(config))
    private_root = args.output_dir / "private"
    public_root = args.output_dir / "public"
    manifest_path = private_root / "manifest.json"

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "config_sha256": config_sha256,
                    "prompts": len(prompts),
                    "arms": arm_names,
                    "trials": args.trials,
                    "codex_calls": total,
                    "pairs": len(prompts)
                    * (len(arm_names) * (len(arm_names) - 1) // 2)
                    * args.trials,
                    "model": args.model,
                    "effort": args.effort,
                    "codex_cli_version": cli_version,
                    "source_git_commit": source_git_commit,
                    "source_git_dirty": source_git_dirty,
                    "output_dir": str(args.output_dir),
                    "cost_caps": {
                        "max_calls": args.max_calls,
                        "max_total_reported_tokens": args.max_total_reported_tokens,
                        "timeout_seconds": args.timeout_seconds,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.auth_file.is_file():
        raise FileNotFoundError("Codex auth file not found")
    manifest = load_or_create_manifest(manifest_path, config=config, dry_run=False)
    if (
        not isinstance(manifest.get("blinding_secret"), str)
        or not manifest["blinding_secret"]
    ):
        raise RuntimeError("private manifest has no blinding secret")
    public_run_id = str(manifest["run_id"])
    results: list[dict[str, Any]] = []
    total_reported_tokens = 0
    planned_by_key: dict[
        RunKey,
        tuple[dict[str, Any], ArmSpec, RunKey, str, Path, Path, Path],
    ] = {}

    for prompt in prompts:
        for spec in selected_specs:
            for trial in range(1, args.trials + 1):
                key = RunKey(str(prompt["id"]), spec.name, trial)
                run_id = private_run_id(config_sha256, key)
                raw_path = private_root / "raw" / f"{run_id}.jsonl"
                stderr_path = private_root / "raw" / f"{run_id}.stderr.txt"
                result_path = private_root / "runs" / f"{run_id}.json"
                planned_by_key[key] = (
                    prompt,
                    spec,
                    key,
                    run_id,
                    raw_path,
                    stderr_path,
                    result_path,
                )

    schedule = load_or_create_execution_schedule(
        private_root / "execution-schedule.json",
        manifest_path=manifest_path,
        manifest=manifest,
        config_sha256=config_sha256,
        run_keys=list(planned_by_key),
    )
    planned = [planned_by_key[key] for key in schedule]

    if not args.resume:
        existing = [item[-1].name for item in planned if item[-1].exists()]
        if existing:
            raise RuntimeError(
                f"completed runs already exist: {', '.join(existing[:3])}"
            )

    with tempfile.TemporaryDirectory(prefix="codex-auth-") as auth_temporary:
        auth_cache = Path(auth_temporary) / "auth.json"
        atomic_copy_file(args.auth_file, auth_cache)

        for index, item in enumerate(planned, 1):
            prompt, spec, key, run_id, raw_path, stderr_path, result_path = item
            identity = result_identity(
                run_id=run_id,
                key=key,
                prompt=str(prompt["prompt"]),
                spec=spec,
                model=args.model,
                effort=args.effort,
                cli_version=cli_version,
                runner_sha256=runner_sha256,
            )
            result = (
                load_resumable_result(
                    result_path,
                    raw_path=raw_path,
                    expected_identity=identity,
                )
                if args.resume
                else None
            )
            if result is None:
                print(
                    f"[{index}/{total}] {key.task_id} | {key.arm} | trial {key.trial}",
                    file=sys.stderr,
                    flush=True,
                )
                result = execute_run(
                    executable=args.codex,
                    auth_source=auth_cache,
                    auth_sink=auth_cache,
                    prompt=str(prompt["prompt"]),
                    key=key,
                    spec=spec,
                    run_id=run_id,
                    model=args.model,
                    effort=args.effort,
                    cli_version=cli_version,
                    runner_sha256=runner_sha256,
                    raw_path=raw_path,
                    stderr_path=stderr_path,
                    result_path=result_path,
                    timeout_seconds=args.timeout_seconds,
                )
            else:
                print(
                    f"[{index}/{total}] resume {key.task_id} | {key.arm} | trial {key.trial}",
                    file=sys.stderr,
                    flush=True,
                )
            results.append(result)
            total_reported_tokens += reported_tokens(result["usage"])
            if total_reported_tokens > args.max_total_reported_tokens:
                raise RuntimeError(
                    "answer usage exceeded --max-total-reported-tokens "
                    f"({total_reported_tokens} > {args.max_total_reported_tokens})"
                )

    if args.require_clean_source:
        ending_commit, ending_dirty = source_git_provenance(ROOT)
        if ending_dirty or ending_commit != source_git_commit:
            raise RuntimeError("source Git checkout changed during blind-review run")

    public_metadata = {
        "generated_at": manifest["created_at"],
        "model": args.model,
        "effort": args.effort,
        "trials": args.trials,
        "task_count": len(prompts),
        "pair_count": len(prompts)
        * (len(selected_specs) * (len(selected_specs) - 1) // 2)
        * args.trials,
        "prompt_corpus_sha256": prompt_corpus_sha256(prompts),
        "codex_cli_version": cli_version,
    }
    public_bundle, private_key = build_blind_bundle(
        public_run_id=public_run_id,
        metadata=public_metadata,
        prompts=prompts,
        arms=arm_names,
        trials=args.trials,
        blinding_secret=str(manifest["blinding_secret"]),
        results=results,
    )
    atomic_write_json(private_root / "key.json", private_key)
    atomic_write_json(public_root / "bundle.json", public_bundle, mode=0o644)
    print(f"Wrote {public_root / 'bundle.json'}")
    print(f"Private key: {private_root / 'key.json'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        return _main(args)
    lock_path = args.output_dir / "private" / ".runner.lock"
    with output_directory_lock(lock_path):
        return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
