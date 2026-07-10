#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import auto_judge_lib as judge
from review_lib import ArmSpec, atomic_copy_file, atomic_write_json, atomic_write_text
from review_lib import (
    canonical_json,
    isolated_codex_environment,
    sha256_file,
    sha256_text,
)
from run_blind_review import (
    codex_version,
    output_directory_lock,
    source_git_provenance,
    toml_string,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / ".local-fixtures" / "blind-review" / "public" / "bundle.json"
DEFAULT_CALIBRATION = ROOT / "evals" / "prompts" / "judge_calibration.jsonl"
DEFAULT_POLICY = ROOT / "evals" / "policies" / "blind_judge.md"
DEFAULT_SCHEMA = ROOT / "evals" / "schemas" / "blind_judge.schema.json"
DEFAULT_OUTPUT = ROOT / ".local-fixtures" / "blind-review" / "private" / "auto-judge"
JUDGE_VERBOSITY = "low"
RUNNER_SCHEMA_VERSION = 2
EXECUTION_CONTRACT_VERSION = 2
ORIENTATIONS = judge.ORIENTATIONS
PREFLIGHT_FORBIDDEN = (
    re.compile(r"\bsimple[ _-]+man\b", re.IGNORECASE),
    re.compile(r"\bnative[ _-]+low\b", re.IGNORECASE),
    re.compile(r"\bsimple_man_runtime\b", re.IGNORECASE),
)
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
EXECUTION_CONTRACT = {
    "version": EXECUTION_CONTRACT_VERSION,
    "runner_schema_version": RUNNER_SCHEMA_VERSION,
    "disabled_features": list(DISABLED_FEATURES),
    "approval_policy": "never",
    "sandbox": "read-only",
    "model_is_explicit": True,
    "reasoning_effort_is_explicit": True,
    "model_verbosity": JUDGE_VERBOSITY,
    "output_schema": True,
    "exec_jsonl": True,
    "ephemeral": True,
    "ignore_user_config": True,
    "ignore_rules": True,
    "skip_git_repo_check": True,
    "prompt_transport": "stdin",
    "prompt_input_preflight": "exact policy once and exact prompt last",
    "tool_events": "fail_closed",
    "orientations": list(ORIENTATIONS),
    "trials_per_orientation": "explicit run config",
    "aggregation": (
        "verdicts and consensus flags require at least 75% canonical agreement "
        "with support in both orientations"
    ),
    "isolation": "fresh HOME, CODEX_HOME, cwd per judge call",
    "auth_refresh": "atomic copy through temp-only run cache",
    "reveal": "separate process requiring private key",
}
EXECUTION_CONTRACT_SHA256 = sha256_text(canonical_json(EXECUTION_CONTRACT))
ALLOWED_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "item.started",
    "item.completed",
    "turn.completed",
}
ALLOWED_ITEM_TYPES = {"agent_message", "reasoning"}


@dataclass(frozen=True)
class PlannedCall:
    kind: str
    subject_id: str
    orientation: str
    trial: int
    prompt: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _calibration_cases(calibration: Any) -> list[Mapping[str, Any]]:
    if isinstance(calibration, list):
        cases = calibration
    elif isinstance(calibration, dict) and isinstance(calibration.get("cases"), list):
        cases = calibration["cases"]
    else:
        raise ValueError("calibration must contain a cases list")
    if not cases or any(not isinstance(case, dict) for case in cases):
        raise ValueError("calibration cases must be a non-empty object list")
    return cases


def _case_id(case: Mapping[str, Any]) -> str:
    value = case.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("calibration case id must be a non-empty string")
    return value


def build_calls(
    *,
    bundle: Mapping[str, Any],
    calibration: Any,
    judge_trials: int = 2,
) -> tuple[list[PlannedCall], list[PlannedCall]]:
    if (
        not isinstance(judge_trials, int)
        or isinstance(judge_trials, bool)
        or judge_trials < 1
    ):
        raise ValueError("judge_trials must be a positive integer")
    calibration_calls: list[PlannedCall] = []
    for case in _calibration_cases(calibration):
        case_id = _case_id(case)
        for trial in range(1, judge_trials + 1):
            for orientation in ORIENTATIONS:
                calibration_calls.append(
                    PlannedCall(
                        kind="calibration",
                        subject_id=case_id,
                        orientation=orientation,
                        trial=trial,
                        prompt=judge.build_judge_payload(
                            prompt=case["prompt"],
                            verified_context=case["verified_context"],
                            left_text=case["response_a"],
                            right_text=case["response_b"],
                            orientation=orientation,
                        ),
                    )
                )

    pair_calls: list[PlannedCall] = []
    pairs = bundle.get("pairs")
    if not isinstance(pairs, list):
        raise ValueError("public bundle has no pairs list")
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(pair.get("id"), str):
            raise ValueError("public bundle pair has no valid id")
        for trial in range(1, judge_trials + 1):
            for orientation in ORIENTATIONS:
                pair_calls.append(
                    PlannedCall(
                        kind="benchmark",
                        subject_id=pair["id"],
                        orientation=orientation,
                        trial=trial,
                        prompt=judge.build_judge_payload(
                            prompt=pair["prompt"],
                            verified_context=pair["verified_context"],
                            left_text=pair["left"]["text"],
                            right_text=pair["right"]["text"],
                            orientation=orientation,
                        ),
                    )
                )
    return calibration_calls, pair_calls


def validate_plan(calls: Sequence[PlannedCall], args: argparse.Namespace) -> None:
    if len(calls) > args.max_calls:
        raise ValueError(
            f"planned calls exceed --max-calls ({len(calls)} > {args.max_calls})"
        )
    oversized = [
        call for call in calls if len(call.prompt) > args.max_input_chars_per_call
    ]
    if oversized:
        call = oversized[0]
        raise ValueError(
            f"judge input exceeds --max-input-chars-per-call: "
            f"{call.kind}/{call.subject_id}/{call.orientation} ({len(call.prompt)})"
        )
    total_chars = sum(len(call.prompt) for call in calls)
    if total_chars > args.max_total_input_chars:
        raise ValueError(
            "planned input exceeds --max-total-input-chars "
            f"({total_chars} > {args.max_total_input_chars})"
        )


def build_codex_command(
    *,
    executable: str,
    model: str,
    effort: str,
    workspace: Path,
    schema_path: Path,
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
            f"model_verbosity={toml_string(JUDGE_VERBOSITY)}",
            "--strict-config",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "--cd",
            str(workspace),
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
            "--config",
            f"model={toml_string(model)}",
            "--config",
            f"model_reasoning_effort={toml_string(effort)}",
            "--config",
            f"model_verbosity={toml_string(JUDGE_VERBOSITY)}",
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
    policy: str,
    prompt: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
) -> None:
    process = subprocess.run(
        build_preflight_command(
            executable=executable,
            model=model,
            effort=effort,
            prompt=prompt,
        ),
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"Codex judge prompt-input preflight failed with exit {process.returncode}"
        )
    try:
        messages = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Codex judge prompt-input preflight returned invalid JSON"
        ) from exc
    if not isinstance(messages, list):
        raise RuntimeError(
            "Codex judge prompt-input preflight did not return a message list"
        )

    text_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
    if not text_parts or text_parts[-1] != prompt:
        raise RuntimeError(
            "Codex judge preflight did not preserve the exact rendered prompt"
        )
    prior_context = "\n".join(text_parts[:-1])
    if prior_context.count(policy) != 1:
        raise RuntimeError(
            "Codex judge preflight expected the exact neutral policy once"
        )
    leaked = [
        pattern.pattern
        for pattern in PREFLIGHT_FORBIDDEN
        if pattern.search(prior_context)
    ]
    if leaked:
        raise RuntimeError("Codex judge preflight contains treatment identity")


def parse_judge_jsonl(path: Path) -> tuple[dict[str, Any], dict[str, int]]:
    final_messages: list[str] = []
    usage: dict[str, int] | None = None
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        if usage is not None:
            raise ValueError("judge JSONL contains an event after turn.completed")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"judge JSONL line {line_number} is invalid JSON") from exc
        if not isinstance(event, dict):
            raise ValueError(f"judge JSONL line {line_number} is not an object")
        event_type = event.get("type")
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"judge JSONL has unexpected event type: {event_type!r}")
        if event_type in {"item.started", "item.completed"}:
            item = event.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            if item_type not in ALLOWED_ITEM_TYPES:
                raise ValueError(
                    f"judge JSONL contains forbidden item type: {item_type!r}"
                )
            if event_type == "item.completed" and item_type == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    raise ValueError("judge agent_message text must be a string")
                final_messages.append(text)
        elif event_type == "turn.completed":
            raw_usage = event.get("usage")
            if not isinstance(raw_usage, dict) or not raw_usage:
                raise ValueError("judge turn.completed has no usage")
            normalized_usage = {
                key: value
                for key, value in raw_usage.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
            if normalized_usage != raw_usage:
                raise ValueError("judge usage must contain only non-negative integers")
            usage = normalized_usage

    if len(final_messages) != 1:
        raise ValueError(
            f"judge JSONL must contain exactly one final agent_message, got {len(final_messages)}"
        )
    if usage is None:
        raise ValueError("judge JSONL has no turn.completed usage")
    try:
        payload = json.loads(final_messages[0])
    except json.JSONDecodeError as exc:
        raise ValueError("judge final message is not a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("judge final message must be a JSON object")
    return judge.validate_judgment(payload), usage


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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{raw_path.name}.", dir=raw_path.parent
    )
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
        os.replace(temporary, raw_path)
    except BaseException:
        if temporary.exists():
            os.chmod(temporary, 0o600)
            os.replace(temporary, raw_path)
        raise
    return process, round((time.monotonic() - started) * 1000)


def call_id(config_sha256: str, call: PlannedCall) -> str:
    identity = {
        "config_sha256": config_sha256,
        "kind": call.kind,
        "subject_id": call.subject_id,
        "orientation": call.orientation,
        "trial": call.trial,
        "prompt_sha256": sha256_text(call.prompt),
    }
    return "judge_" + sha256_text(canonical_json(identity))[:24]


def call_identity(
    *,
    run_id: str,
    config_sha256: str,
    call: PlannedCall,
    model: str,
    effort: str,
    cli_version: str,
    runner_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "run_id": run_id,
        "config_sha256": config_sha256,
        "call_id": call_id(config_sha256, call),
        "kind": call.kind,
        "subject_id": call.subject_id,
        "orientation": call.orientation,
        "trial": call.trial,
        "prompt_sha256": sha256_text(call.prompt),
        "model": model,
        "effort": effort,
        "model_verbosity": JUDGE_VERBOSITY,
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
        "judgment",
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
    parsed_judgment, parsed_usage = parse_judge_jsonl(raw_path)
    if result.get("judgment") != parsed_judgment or result.get("usage") != parsed_usage:
        raise RuntimeError(f"resume parsed result differs from raw JSONL: {path.name}")
    if not isinstance(result.get("duration_ms"), int) or result["duration_ms"] < 0:
        raise RuntimeError(f"resume duration is invalid: {path.name}")
    return result


def execute_call(
    *,
    executable: str,
    auth_source: Path,
    auth_sink: Path,
    policy: str,
    schema_path: Path,
    call: PlannedCall,
    identity: Mapping[str, Any],
    model: str,
    effort: str,
    raw_path: Path,
    stderr_path: Path,
    result_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    spec = ArmSpec("blind_judge", JUDGE_VERBOSITY, policy)
    with isolated_codex_environment(
        auth_source=auth_source,
        auth_sink=auth_sink,
        spec=spec,
    ) as isolated:
        preflight_model_visible_input(
            executable=executable,
            model=model,
            effort=effort,
            policy=policy,
            prompt=call.prompt,
            cwd=isolated.workspace,
            env=isolated.env,
            timeout_seconds=timeout_seconds,
        )
        process, duration_ms = _run_with_atomic_stdout(
            build_codex_command(
                executable=executable,
                model=model,
                effort=effort,
                workspace=isolated.workspace,
                schema_path=schema_path,
            ),
            prompt=call.prompt,
            cwd=isolated.workspace,
            env=isolated.env,
            raw_path=raw_path,
            timeout_seconds=timeout_seconds,
        )
    atomic_write_text(stderr_path, process.stderr, mode=0o600)
    if process.returncode != 0:
        raise RuntimeError(
            f"Codex judge call {identity['call_id']} failed with exit {process.returncode}"
        )
    judgment, usage = parse_judge_jsonl(raw_path)
    result = {
        **identity,
        "judgment": judgment,
        "usage": usage,
        "duration_ms": duration_ms,
        "raw_sha256": sha256_file(raw_path),
    }
    atomic_write_json(result_path, result)
    return result


def reported_tokens(usage: Mapping[str, int]) -> int:
    if "total_tokens" in usage:
        return usage["total_tokens"]
    if "input_tokens" in usage and "output_tokens" in usage:
        return usage["input_tokens"] + usage["output_tokens"]
    raise ValueError(
        "judge usage must contain total_tokens or both input_tokens and output_tokens"
    )


def aggregate_results(
    calls: Sequence[PlannedCall],
    results: Mapping[tuple[str, str, str, int], Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    subject_ids = list(dict.fromkeys(call.subject_id for call in calls))
    kind = calls[0].kind if calls else ""
    aggregated: dict[str, dict[str, Any]] = {}
    for subject_id in subject_ids:
        aggregated[subject_id] = judge.aggregate_pair(
            forward=[
                results[(kind, subject_id, "forward", call.trial)]["judgment"]
                for call in calls
                if call.subject_id == subject_id and call.orientation == "forward"
            ],
            swapped=[
                results[(kind, subject_id, "swapped", call.trial)]["judgment"]
                for call in calls
                if call.subject_id == subject_id and call.orientation == "swapped"
            ],
        )
    return aggregated


def runner_component_hashes() -> dict[str, str]:
    paths = (
        Path(__file__).resolve(),
        ROOT / "evals" / "auto_judge_lib.py",
        ROOT / "evals" / "review_lib.py",
        ROOT / "evals" / "run_blind_review.py",
    )
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def config_payload(
    *,
    bundle_path: Path,
    calibration_path: Path,
    policy_path: Path,
    schema_path: Path,
    bundle: Mapping[str, Any],
    calls: Sequence[PlannedCall],
    model: str,
    effort: str,
    cli_version: str,
    runner_sha256: str,
    runner_components: Mapping[str, str],
    source_git_commit: str,
    source_git_dirty: bool,
    args: argparse.Namespace,
) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "bundle_sha256": sha256_file(bundle_path),
        "bundle_run_id": bundle["run_id"],
        "calibration_sha256": sha256_file(calibration_path),
        "policy_sha256": sha256_file(policy_path),
        "output_schema_sha256": sha256_file(schema_path),
        "model": model,
        "effort": effort,
        "model_verbosity": JUDGE_VERBOSITY,
        "codex_cli_version": cli_version,
        "orientations": list(ORIENTATIONS),
        "judge_trials_per_orientation": args.judge_trials,
        "call_count": len(calls),
        "total_input_chars": sum(len(call.prompt) for call in calls),
        "max_pairs": args.max_pairs,
        "max_calls": args.max_calls,
        "max_input_chars_per_call": args.max_input_chars_per_call,
        "max_total_input_chars": args.max_total_input_chars,
        "max_total_reported_tokens": args.max_total_reported_tokens,
        "timeout_seconds": args.timeout_seconds,
        "execution_contract": EXECUTION_CONTRACT,
        "execution_contract_sha256": EXECUTION_CONTRACT_SHA256,
        "runner_sha256": runner_sha256,
        "runner_components": dict(runner_components),
        "source_git_commit": source_git_commit,
        "source_git_dirty": source_git_dirty,
        "require_clean_source": args.require_clean_source,
    }


def load_or_create_manifest(
    path: Path,
    *,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    config_sha256 = sha256_text(canonical_json(config))
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("auto-judge manifest is invalid JSON") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("config_sha256") != config_sha256
        ):
            raise RuntimeError(
                "output directory belongs to a different auto-judge config; use another --output-dir"
            )
        if manifest.get("config") != config:
            raise RuntimeError(
                "auto-judge manifest config differs despite matching hash"
            )
        return manifest
    manifest = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "run_id": "autojudge_" + uuid.uuid4().hex,
        "created_at": utc_now(),
        "config_sha256": config_sha256,
        "config": dict(config),
    }
    atomic_write_json(path, manifest)
    return manifest


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def freeze_input(source: Path, destination: Path, *, expected_sha256: str) -> None:
    if destination.exists():
        if sha256_file(destination) != expected_sha256:
            raise RuntimeError(f"frozen input hash mismatch: {destination.name}")
        return
    atomic_copy_file(source, destination, mode=0o600)
    if sha256_file(destination) != expected_sha256:
        raise RuntimeError(f"failed to freeze exact input: {destination.name}")


def default_auth_file() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "auth.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated, position-checked automatic judge over a public blind bundle."
    )
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--judge-policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--auth-file", type=Path, default=default_auth_file())
    parser.add_argument("--codex", default=os.environ.get("CODEX", "codex"))
    parser.add_argument("--model", default=os.environ.get("JUDGE_MODEL"))
    parser.add_argument("--effort", default=os.environ.get("JUDGE_EFFORT"))
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--max-pairs", type=int, default=12)
    parser.add_argument(
        "--judge-trials",
        type=int,
        default=2,
        help="Independent calls per orientation for each case or pair (default: 2).",
    )
    parser.add_argument("--max-calls", type=int, default=96)
    parser.add_argument("--max-input-chars-per-call", type=int, default=50_000)
    parser.add_argument("--max-total-input-chars", type=int, default=500_000)
    parser.add_argument("--max-total-reported-tokens", type=int, default=500_000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--require-clean-source",
        action="store_true",
        help="Fail before live calls unless the source commit is clean and unchanged.",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse identity- and raw-hash-checked completed judge calls (default: true).",
    )
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model is required (or set JUDGE_MODEL)")
    if not args.effort:
        parser.error("--effort is required (or set JUDGE_EFFORT)")
    for field in (
        "timeout_seconds",
        "max_pairs",
        "judge_trials",
        "max_calls",
        "max_input_chars_per_call",
        "max_total_input_chars",
        "max_total_reported_tokens",
    ):
        if getattr(args, field) < 1:
            parser.error(f"--{field.replace('_', '-')} must be >= 1")
    return args


def _ensure_private_output(bundle_path: Path, output_dir: Path) -> None:
    public_dir = bundle_path.resolve().parent
    output = output_dir.resolve()
    if output == public_dir or public_dir in output.parents:
        raise ValueError("--output-dir must not be inside the public bundle directory")


def _main(args: argparse.Namespace) -> int:
    for path, label in (
        (args.bundle, "public bundle"),
        (args.calibration, "calibration"),
        (args.judge_policy, "judge policy"),
        (args.output_schema, "output schema"),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
    _ensure_private_output(args.bundle, args.output_dir)

    bundle = judge.load_public_bundle(args.bundle)
    calibration = judge.load_calibration(args.calibration)
    policy = args.judge_policy.read_text(encoding="utf-8")
    if not policy.strip():
        raise ValueError("judge policy must not be empty")
    if any(pattern.search(policy) for pattern in PREFLIGHT_FORBIDDEN):
        raise ValueError("judge policy contains treatment identity")
    schema_payload = json.loads(args.output_schema.read_text(encoding="utf-8"))
    if not isinstance(schema_payload, dict):
        raise ValueError("judge output schema must be a JSON object")

    pair_count = len(bundle["pairs"])
    if pair_count > args.max_pairs:
        raise ValueError(
            f"bundle exceeds --max-pairs ({pair_count} > {args.max_pairs})"
        )
    calibration_calls, benchmark_calls = build_calls(
        bundle=bundle,
        calibration=calibration,
        judge_trials=args.judge_trials,
    )
    all_calls = calibration_calls + benchmark_calls
    validate_plan(all_calls, args)

    cli_version = codex_version(args.codex)
    source_git_commit, source_git_dirty = source_git_provenance(ROOT)
    if args.require_clean_source and not args.dry_run and source_git_dirty:
        raise RuntimeError("live auto-judge run requires a clean source Git checkout")
    runner_components = runner_component_hashes()
    runner_sha256 = sha256_text(canonical_json(runner_components))
    config = config_payload(
        bundle_path=args.bundle,
        calibration_path=args.calibration,
        policy_path=args.judge_policy,
        schema_path=args.output_schema,
        bundle=bundle,
        calls=all_calls,
        model=args.model,
        effort=args.effort,
        cli_version=cli_version,
        runner_sha256=runner_sha256,
        runner_components=runner_components,
        source_git_commit=source_git_commit,
        source_git_dirty=source_git_dirty,
        args=args,
    )
    config_sha256 = sha256_text(canonical_json(config))

    if args.dry_run:
        print(
            json.dumps(
                {
                    "config_sha256": config_sha256,
                    "bundle_run_id": bundle["run_id"],
                    "pairs": pair_count,
                    "calibration_cases": len(_calibration_cases(calibration)),
                    "judge_trials_per_orientation": args.judge_trials,
                    "calibration_calls": len(calibration_calls),
                    "benchmark_calls": len(benchmark_calls),
                    "total_calls": len(all_calls),
                    "total_input_chars": sum(len(call.prompt) for call in all_calls),
                    "model": args.model,
                    "effort": args.effort,
                    "model_verbosity": JUDGE_VERBOSITY,
                    "codex_cli_version": cli_version,
                    "source_git_commit": source_git_commit,
                    "source_git_dirty": source_git_dirty,
                    "cost_caps": {
                        "max_pairs": args.max_pairs,
                        "max_calls": args.max_calls,
                        "max_input_chars_per_call": args.max_input_chars_per_call,
                        "max_total_input_chars": args.max_total_input_chars,
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
        raise FileNotFoundError(f"Codex auth file not found: {args.auth_file}")
    for private_dir in (
        args.output_dir,
        args.output_dir / "inputs",
        args.output_dir / "raw",
        args.output_dir / "runs",
    ):
        ensure_private_directory(private_dir)
    manifest = load_or_create_manifest(args.output_dir / "manifest.json", config=config)
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("auto-judge manifest has no valid run_id")
    frozen_inputs = {
        "bundle.json": (args.bundle, config["bundle_sha256"]),
        "calibration.jsonl": (args.calibration, config["calibration_sha256"]),
        "judge-policy.md": (args.judge_policy, config["policy_sha256"]),
        "output-schema.json": (args.output_schema, config["output_schema_sha256"]),
    }
    for filename, (source, expected_sha256) in frozen_inputs.items():
        freeze_input(
            source,
            args.output_dir / "inputs" / filename,
            expected_sha256=expected_sha256,
        )
    frozen_bundle = judge.load_public_bundle(args.output_dir / "inputs" / "bundle.json")
    frozen_calibration = judge.load_calibration(
        args.output_dir / "inputs" / "calibration.jsonl"
    )
    frozen_policy = (args.output_dir / "inputs" / "judge-policy.md").read_text(
        encoding="utf-8"
    )
    frozen_schema = json.loads(
        (args.output_dir / "inputs" / "output-schema.json").read_text(encoding="utf-8")
    )
    if frozen_bundle != bundle:
        raise RuntimeError("frozen public bundle differs from planned bundle")
    if frozen_calibration != calibration:
        raise RuntimeError("frozen calibration differs from planned calibration")
    if frozen_policy != policy:
        raise RuntimeError("frozen judge policy differs from planned policy")
    if frozen_schema != schema_payload:
        raise RuntimeError("frozen output schema differs from planned schema")
    frozen_schema_path = (args.output_dir / "inputs" / "output-schema.json").resolve()

    results: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    total_reported_tokens = 0
    with tempfile.TemporaryDirectory(prefix="codex-judge-auth-") as auth_temporary:
        auth_cache = Path(auth_temporary) / "auth.json"
        atomic_copy_file(args.auth_file, auth_cache)
        for index, call in enumerate(all_calls, 1):
            identity = call_identity(
                run_id=run_id,
                config_sha256=config_sha256,
                call=call,
                model=args.model,
                effort=args.effort,
                cli_version=cli_version,
                runner_sha256=runner_sha256,
            )
            raw_path = args.output_dir / "raw" / f"{identity['call_id']}.jsonl"
            stderr_path = args.output_dir / "raw" / f"{identity['call_id']}.stderr.txt"
            result_path = args.output_dir / "runs" / f"{identity['call_id']}.json"
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
                if not args.resume and result_path.exists():
                    raise RuntimeError(
                        f"completed judge result already exists: {result_path.name}"
                    )
                print(
                    f"[{index}/{len(all_calls)}] {call.kind} | {call.subject_id} | "
                    f"{call.orientation} | trial {call.trial}",
                    file=sys.stderr,
                    flush=True,
                )
                result = execute_call(
                    executable=args.codex,
                    auth_source=auth_cache,
                    auth_sink=auth_cache,
                    policy=policy,
                    schema_path=frozen_schema_path,
                    call=call,
                    identity=identity,
                    model=args.model,
                    effort=args.effort,
                    raw_path=raw_path,
                    stderr_path=stderr_path,
                    result_path=result_path,
                    timeout_seconds=args.timeout_seconds,
                )
            else:
                print(
                    f"[{index}/{len(all_calls)}] resume {call.kind} | "
                    f"{call.subject_id} | {call.orientation} | trial {call.trial}",
                    file=sys.stderr,
                    flush=True,
                )
            results[(call.kind, call.subject_id, call.orientation, call.trial)] = result
            total_reported_tokens += reported_tokens(result["usage"])
            if total_reported_tokens > args.max_total_reported_tokens:
                raise RuntimeError(
                    "judge usage exceeded --max-total-reported-tokens "
                    f"({total_reported_tokens} > {args.max_total_reported_tokens})"
                )

            if index == len(calibration_calls):
                calibration_aggregated = aggregate_results(calibration_calls, results)
                calibration_report = judge.grade_calibration(
                    calibration=calibration,
                    pair_results=calibration_aggregated,
                )
                atomic_write_json(
                    args.output_dir / "calibration-results.json",
                    {
                        "schema_version": RUNNER_SCHEMA_VERSION,
                        "run_id": run_id,
                        "bundle_sha256": config["bundle_sha256"],
                        "judge_config_sha256": config_sha256,
                        "aggregated_pairs": calibration_aggregated,
                        "report": calibration_report,
                    },
                )
                if not calibration_report.get("passed"):
                    raise RuntimeError(
                        "judge calibration gate failed; benchmark calls were not started"
                    )

    pair_results = aggregate_results(benchmark_calls, results)
    if args.require_clean_source:
        ending_commit, ending_dirty = source_git_provenance(ROOT)
        if ending_dirty or ending_commit != source_git_commit:
            raise RuntimeError("source Git checkout changed during auto-judge run")
    blind_results = judge.build_blind_results(
        judge_run_id=run_id,
        bundle=bundle,
        bundle_sha256=config["bundle_sha256"],
        judge_config_sha256=config_sha256,
        pair_results=pair_results,
        calibration=calibration_report,
    )
    blind_results = judge.validate_blind_results(blind_results)
    blind_results_path = args.output_dir / "blind-results.json"
    atomic_write_json(blind_results_path, blind_results)
    atomic_write_json(
        args.output_dir / "run-summary.json",
        {
            "schema_version": RUNNER_SCHEMA_VERSION,
            "judge_run_id": run_id,
            "source_run_id": bundle["run_id"],
            "bundle_sha256": config["bundle_sha256"],
            "judge_config_sha256": config_sha256,
            "model": args.model,
            "effort": args.effort,
            "model_verbosity": JUDGE_VERBOSITY,
            "codex_cli_version": cli_version,
            "total_reported_tokens": total_reported_tokens,
            "completed_at": utc_now(),
        },
    )
    print(f"Wrote blind auto-judge results: {blind_results_path}")
    print("Arm identities remain sealed; run reveal_auto_judge.py separately.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        return _main(args)
    _ensure_private_output(args.bundle, args.output_dir)
    ensure_private_directory(args.output_dir)
    with output_directory_lock(args.output_dir / ".runner.lock"):
        return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
