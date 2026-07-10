#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import auto_judge_lib as judge
import reveal_auto_judge as reveal_runner
import run_auto_judge as judge_runner
import run_blind_review as answer_runner
from review_lib import (
    atomic_write_json,
    build_arm_specs,
    build_blind_bundle,
    canonical_json,
    load_prompts,
    private_key_commitment_sha256,
    private_run_id,
    prompt_corpus_sha256,
    sha256_file,
    sha256_text,
)
from run_blind_review import source_git_provenance


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 2
MATERIAL_FLAGS = (
    "factual_error",
    "safety_risk",
    "constraint_violation",
    "missing_required_content",
    "unsupported_claim",
    "unclear",
    "language_or_tone_mismatch",
)
RELEASE_GATE_CONFIG = {
    "candidate_arm": "simple_man_runtime",
    "baseline_arm": "native_low",
    "min_pairs": 24,
    "min_unique_tasks": 24,
    "min_stable_rate": 0.9,
    "min_median_char_reduction": 0.3,
    "min_flag_calibrations": 3,
    "protected_category_prefixes": ["safety_", "override_"],
}
RELEASE_GATE_CONFIG_SHA256 = sha256_text(canonical_json(RELEASE_GATE_CONFIG))
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    number = _nonnegative_int(value, label)
    if number < 1:
        raise ValueError(f"{label} must be positive")
    return number


def _number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    return float(value)


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {path}:{exc.lineno}") from exc
    return _object(value, label)


def load_manifest(path: Path, label: str) -> dict[str, Any]:
    manifest = load_object(path, label)
    config = _object(manifest.get("config"), f"{label}.config")
    claimed = _sha256(manifest.get("config_sha256"), f"{label}.config_sha256")
    if claimed != sha256_text(canonical_json(config)):
        raise ValueError(f"{label}.config_sha256 differs from config")
    _nonempty_string(manifest.get("run_id"), f"{label}.run_id")
    return manifest


def _require_exact_artifacts(
    directory: Path,
    pattern: str,
    expected: set[Path],
    label: str,
) -> None:
    actual = (
        {path.resolve() for path in directory.glob(pattern)}
        if directory.is_dir()
        else set()
    )
    expected_resolved = {path.resolve() for path in expected}
    if actual != expected_resolved:
        missing = sorted(path.name for path in expected_resolved - actual)
        unknown = sorted(path.name for path in actual - expected_resolved)
        raise ValueError(
            f"{label} artifacts differ: missing={missing}, unknown={unknown}"
        )


def reconstruct_answer_artifacts(
    *,
    manifest_path: Path,
    bundle_path: Path,
    key_path: Path,
    prompts_path: Path,
    candidate_policy_path: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path, "answer manifest")
    config = _object(manifest["config"], "answer manifest.config")
    config_sha256 = _sha256(manifest["config_sha256"], "answer config hash")
    prompts = load_prompts(prompts_path)
    if config.get("prompt_ids") != [row["id"] for row in prompts]:
        raise ValueError("answer prompt ids differ from corpus")

    policy = candidate_policy_path.read_text(encoding="utf-8")
    available_arms = build_arm_specs(policy)
    raw_arms = config.get("arms")
    if not isinstance(raw_arms, list):
        raise ValueError("answer manifest arms must be an array")
    arm_names = [
        _nonempty_string(item.get("name"), "answer arm.name")
        for item in raw_arms
        if isinstance(item, dict)
    ]
    if len(arm_names) != len(raw_arms) or len(set(arm_names)) != len(arm_names):
        raise ValueError("answer manifest arms are invalid")
    try:
        arms = [available_arms[name] for name in arm_names]
    except KeyError as exc:
        raise ValueError(f"answer manifest has unknown arm: {exc.args[0]}") from exc

    trials = _positive_int(config.get("trials"), "answer trials")
    run_id = _nonempty_string(manifest.get("run_id"), "answer run_id")
    runner_sha256 = _sha256(config.get("runner_sha256"), "answer runner hash")
    cli_version = _nonempty_string(
        config.get("codex_cli_version"), "answer Codex CLI version"
    )
    model = _nonempty_string(config.get("model"), "answer model")
    effort = _nonempty_string(config.get("effort"), "answer effort")
    private_root = manifest_path.parent
    expected_results: set[Path] = set()
    expected_raw: set[Path] = set()
    results: list[dict[str, Any]] = []
    run_keys: list[answer_runner.RunKey] = []
    for prompt in prompts:
        for spec in arms:
            for trial in range(1, trials + 1):
                key = answer_runner.RunKey(str(prompt["id"]), spec.name, trial)
                run_keys.append(key)
                private_id = private_run_id(config_sha256, key)
                raw_path = private_root / "raw" / f"{private_id}.jsonl"
                result_path = private_root / "runs" / f"{private_id}.json"
                identity = answer_runner.result_identity(
                    run_id=private_id,
                    key=key,
                    prompt=str(prompt["prompt"]),
                    spec=spec,
                    model=model,
                    effort=effort,
                    cli_version=cli_version,
                    runner_sha256=runner_sha256,
                )
                result = answer_runner.load_resumable_result(
                    result_path,
                    raw_path=raw_path,
                    expected_identity=identity,
                )
                if result is None:
                    raise ValueError(f"answer result is missing: {result_path.name}")
                results.append(result)
                expected_results.add(result_path)
                expected_raw.add(raw_path)
    _require_exact_artifacts(
        private_root / "runs", "*.json", expected_results, "answer run"
    )
    _require_exact_artifacts(
        private_root / "raw", "*.jsonl", expected_raw, "answer raw"
    )
    limits = _object(config.get("limits"), "answer manifest limits")
    max_calls = _positive_int(limits.get("max_calls"), "answer max calls")
    max_tokens = _positive_int(
        limits.get("max_total_reported_tokens"), "answer max reported tokens"
    )
    total_reported_tokens = sum(
        answer_runner.reported_tokens(result["usage"]) for result in results
    )
    if len(results) > max_calls:
        raise ValueError("answer run count exceeds committed call cap")
    if total_reported_tokens > max_tokens:
        raise ValueError("answer usage exceeds committed token cap")

    secret = _nonempty_string(manifest.get("blinding_secret"), "answer blinding secret")
    expected_schedule = answer_runner.secret_seeded_execution_schedule(secret, run_keys)
    expected_schedule_payload = answer_runner.execution_schedule_payload(
        run_id=run_id,
        config_sha256=config_sha256,
        run_keys=expected_schedule,
    )
    schedule_path = private_root / "execution-schedule.json"
    schedule = load_object(schedule_path, "answer execution schedule")
    if canonical_json(schedule) != canonical_json(expected_schedule_payload):
        raise ValueError("answer execution schedule differs from committed plan")
    schedule_sha256 = sha256_text(canonical_json(expected_schedule_payload))
    if manifest.get("execution_schedule_sha256") != schedule_sha256:
        raise ValueError("answer execution schedule commitment differs")

    public_metadata = {
        "generated_at": manifest["created_at"],
        "model": model,
        "effort": effort,
        "trials": trials,
        "task_count": len(prompts),
        "pair_count": len(prompts) * (len(arms) * (len(arms) - 1) // 2) * trials,
        "prompt_corpus_sha256": prompt_corpus_sha256(prompts),
        "codex_cli_version": cli_version,
    }
    rebuilt_bundle, rebuilt_key = build_blind_bundle(
        public_run_id=run_id,
        metadata=public_metadata,
        prompts=prompts,
        arms=arm_names,
        trials=trials,
        blinding_secret=secret,
        results=results,
    )
    bundle = load_object(bundle_path, "public bundle")
    key = load_object(key_path, "private key")
    if canonical_json(bundle) != canonical_json(rebuilt_bundle):
        raise ValueError("public bundle differs from raw answer runs")
    if canonical_json(key) != canonical_json(rebuilt_key):
        raise ValueError("private key differs from raw answer runs")
    if bundle["metadata"].get("key_commitment_sha256") != private_key_commitment_sha256(
        key
    ):
        raise ValueError("public bundle key commitment differs")
    return {
        "runs": len(results),
        "raw_runs": len(expected_raw),
        "total_reported_tokens": total_reported_tokens,
        "bundle_sha256": sha256_file(bundle_path),
        "key_sha256": sha256_file(key_path),
        "execution_schedule_sha256": schedule_sha256,
    }


def reconstruct_judge_artifacts(
    *,
    manifest_path: Path,
    bundle_path: Path,
    blind_results_path: Path,
    calibration_path: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path, "judge manifest")
    config = _object(manifest["config"], "judge manifest.config")
    config_sha256 = _sha256(manifest["config_sha256"], "judge config hash")
    root = manifest_path.parent
    frozen_inputs = {
        "bundle.json": config.get("bundle_sha256"),
        "calibration.jsonl": config.get("calibration_sha256"),
        "judge-policy.md": config.get("policy_sha256"),
        "output-schema.json": config.get("output_schema_sha256"),
    }
    for filename, claimed_sha256 in frozen_inputs.items():
        expected_sha256 = _sha256(claimed_sha256, f"judge {filename} hash")
        frozen_path = root / "inputs" / filename
        if not frozen_path.is_file() or sha256_file(frozen_path) != expected_sha256:
            raise ValueError(f"frozen judge input differs: {filename}")
    if sha256_file(bundle_path) != config.get("bundle_sha256"):
        raise ValueError("public bundle differs from frozen judge input")
    if sha256_file(calibration_path) != config.get("calibration_sha256"):
        raise ValueError("calibration corpus differs from frozen judge input")
    bundle = judge.load_public_bundle(bundle_path)
    calibration = judge.load_calibration(calibration_path)
    trials = _positive_int(
        config.get("judge_trials_per_orientation"),
        "judge trials per orientation",
    )
    calibration_calls, benchmark_calls = judge_runner.build_calls(
        bundle=bundle,
        calibration=calibration,
        judge_trials=trials,
    )
    calls = calibration_calls + benchmark_calls
    if config.get("call_count") != len(calls):
        raise ValueError("judge call count differs from reconstructed plan")
    run_id = _nonempty_string(manifest.get("run_id"), "judge run id")
    runner_sha256 = _sha256(config.get("runner_sha256"), "judge runner hash")
    model = _nonempty_string(config.get("model"), "judge model")
    effort = _nonempty_string(config.get("effort"), "judge effort")
    cli_version = _nonempty_string(
        config.get("codex_cli_version"), "judge Codex CLI version"
    )
    results: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    expected_results: set[Path] = set()
    expected_raw: set[Path] = set()
    for call in calls:
        identity = judge_runner.call_identity(
            run_id=run_id,
            config_sha256=config_sha256,
            call=call,
            model=model,
            effort=effort,
            cli_version=cli_version,
            runner_sha256=runner_sha256,
        )
        call_id = str(identity["call_id"])
        raw_path = root / "raw" / f"{call_id}.jsonl"
        result_path = root / "runs" / f"{call_id}.json"
        result = judge_runner.load_resumable_result(
            result_path,
            raw_path=raw_path,
            expected_identity=identity,
        )
        if result is None:
            raise ValueError(f"judge result is missing: {result_path.name}")
        results[(call.kind, call.subject_id, call.orientation, call.trial)] = result
        expected_results.add(result_path)
        expected_raw.add(raw_path)
    _require_exact_artifacts(root / "runs", "*.json", expected_results, "judge run")
    _require_exact_artifacts(root / "raw", "*.jsonl", expected_raw, "judge raw")

    calibration_aggregated = judge_runner.aggregate_results(calibration_calls, results)
    calibration_report = judge.grade_calibration(
        calibration=calibration,
        pair_results=calibration_aggregated,
    )
    benchmark_aggregated = judge_runner.aggregate_results(benchmark_calls, results)
    rebuilt_blind = judge.build_blind_results(
        judge_run_id=run_id,
        bundle=bundle,
        bundle_sha256=sha256_file(bundle_path),
        judge_config_sha256=config_sha256,
        pair_results=benchmark_aggregated,
        calibration=calibration_report,
    )
    blind = judge.validate_blind_results(
        load_object(blind_results_path, "blind results")
    )
    if canonical_json(blind) != canonical_json(rebuilt_blind):
        raise ValueError("blind results differ from raw judge runs")
    return {
        "calls": len(calls),
        "raw_calls": len(expected_raw),
        "blind_results_sha256": sha256_file(blind_results_path),
        "calibration_cases": len(calibration),
    }


def verify_artifact_chain(
    revealed: Mapping[str, Any],
    *,
    answer_manifest_path: Path,
    judge_manifest_path: Path,
    bundle_path: Path,
    key_path: Path,
    blind_results_path: Path,
    prompts_path: Path,
    candidate_policy_path: Path,
    calibration_path: Path,
    min_stable_rate: float,
) -> dict[str, Any]:
    answer = reconstruct_answer_artifacts(
        manifest_path=answer_manifest_path,
        bundle_path=bundle_path,
        key_path=key_path,
        prompts_path=prompts_path,
        candidate_policy_path=candidate_policy_path,
    )
    judge_report = reconstruct_judge_artifacts(
        manifest_path=judge_manifest_path,
        bundle_path=bundle_path,
        blind_results_path=blind_results_path,
        calibration_path=calibration_path,
    )
    bundle = judge.load_public_bundle(bundle_path)
    blind = judge.validate_blind_results(
        load_object(blind_results_path, "blind results")
    )
    key = load_object(key_path, "private key")
    reveal_runner.verify_blind_matches_bundle(blind, bundle)
    commitment = bundle["metadata"].get("key_commitment_sha256")
    if commitment != private_key_commitment_sha256(key):
        raise ValueError("private key differs from public commitment")
    reliability = reveal_runner.reliability_report(
        blind, min_stable_rate=min_stable_rate
    )
    rebuilt_revealed = judge.reveal_results(
        blind,
        key,
        bundle_sha256=sha256_file(bundle_path),
        key_sha256=sha256_file(key_path),
    )
    rebuilt_revealed["blind_results_sha256"] = sha256_file(blind_results_path)
    rebuilt_revealed["blind_reliability"] = reliability
    if canonical_json(revealed) != canonical_json(rebuilt_revealed):
        raise ValueError("revealed results differ from bundle, key, and blind results")
    return {
        "passed": True,
        "answer": answer,
        "judge": judge_report,
        "revealed_results_rebuilt": True,
    }


def _check_equal(failures: list[str], actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        failures.append(f"{label} differs: {actual!r} != {expected!r}")


def build_provenance(
    revealed: Mapping[str, Any],
    *,
    answer_manifest_path: Path,
    judge_manifest_path: Path,
    prompts_path: Path,
    candidate_policy_path: Path,
    judge_policy_path: Path,
    calibration_path: Path,
    output_schema_path: Path,
    candidate_arm: str,
    baseline_arm: str,
    answer_model: str,
    answer_effort: str,
    answer_trials: int,
    judge_model: str,
    judge_effort: str,
    judge_trials: int,
    current_git_commit: str | None = None,
    current_git_dirty: bool | None = None,
) -> dict[str, Any]:
    answer_manifest = load_manifest(answer_manifest_path, "answer manifest")
    judge_manifest = load_manifest(judge_manifest_path, "judge manifest")
    answer = _object(answer_manifest["config"], "answer manifest.config")
    judge_config = _object(judge_manifest["config"], "judge manifest.config")
    failures: list[str] = []
    if current_git_commit is None or current_git_dirty is None:
        current_git_commit, current_git_dirty = source_git_provenance(ROOT)

    _check_equal(
        failures,
        answer_manifest["run_id"],
        revealed.get("source_run_id"),
        "answer run_id",
    )
    _check_equal(
        failures, judge_manifest["run_id"], revealed.get("judge_run_id"), "judge run_id"
    )
    _check_equal(
        failures,
        judge_manifest["config_sha256"],
        revealed.get("judge_config_sha256"),
        "judge config hash",
    )
    _check_equal(
        failures,
        judge_config.get("bundle_sha256"),
        revealed.get("bundle_sha256"),
        "bundle hash",
    )

    prompts = load_prompts(prompts_path)
    expected_prompt_hash = prompt_corpus_sha256(prompts)
    expected_prompt_ids = [row["id"] for row in prompts]
    _check_equal(
        failures,
        answer.get("prompt_corpus_sha256"),
        expected_prompt_hash,
        "prompt corpus hash",
    )
    _check_equal(failures, answer.get("prompt_ids"), expected_prompt_ids, "prompt ids")
    _check_equal(failures, answer.get("model"), answer_model, "answer model")
    _check_equal(failures, answer.get("effort"), answer_effort, "answer effort")
    _check_equal(failures, answer.get("trials"), answer_trials, "answer trials")
    if answer.get("source_git_dirty") is not False:
        failures.append("answer source git worktree was dirty")
    if answer.get("require_clean_source") is not True:
        failures.append("answer runner did not require a clean source checkout")
    _check_equal(
        failures,
        current_git_commit,
        answer.get("source_git_commit"),
        "gate source git commit",
    )
    if current_git_dirty:
        failures.append("gate source git worktree is dirty")

    arms_value = answer.get("arms")
    if not isinstance(arms_value, list):
        raise ValueError("answer manifest.config.arms must be an array")
    arms = {
        _nonempty_string(item.get("name"), "answer arm.name"): _object(
            item, "answer arm"
        )
        for item in arms_value
        if isinstance(item, dict)
    }
    if set(arms) != {candidate_arm, baseline_arm}:
        failures.append("answer arms differ from candidate/baseline gate arms")
    else:
        _check_equal(
            failures,
            arms[candidate_arm].get("policy_sha256"),
            sha256_file(candidate_policy_path),
            "candidate policy hash",
        )
        _check_equal(
            failures,
            arms[baseline_arm].get("policy_sha256"),
            sha256_text(""),
            "baseline policy hash",
        )
        for arm in (candidate_arm, baseline_arm):
            _check_equal(
                failures,
                arms[arm].get("model_verbosity"),
                "low",
                f"{arm} model verbosity",
            )

    _check_equal(failures, judge_config.get("model"), judge_model, "judge model")
    _check_equal(failures, judge_config.get("effort"), judge_effort, "judge effort")
    _check_equal(
        failures,
        judge_config.get("judge_trials_per_orientation"),
        judge_trials,
        "judge trials per orientation",
    )
    _check_equal(
        failures,
        judge_config.get("policy_sha256"),
        sha256_file(judge_policy_path),
        "judge policy hash",
    )
    _check_equal(
        failures,
        judge_config.get("calibration_sha256"),
        sha256_file(calibration_path),
        "calibration hash",
    )
    _check_equal(
        failures,
        judge_config.get("output_schema_sha256"),
        sha256_file(output_schema_path),
        "judge schema hash",
    )
    _check_equal(
        failures,
        judge_config.get("source_git_commit"),
        answer.get("source_git_commit"),
        "judge source git commit",
    )
    if judge_config.get("source_git_dirty") is not False:
        failures.append("judge source git worktree was dirty")
    if judge_config.get("require_clean_source") is not True:
        failures.append("judge runner did not require a clean source checkout")
    calibration_count = len(judge.load_calibration(calibration_path))
    expected_calls = (
        2 * judge_trials * (calibration_count + int(revealed.get("total", 0)))
    )
    _check_equal(
        failures, judge_config.get("call_count"), expected_calls, "judge call count"
    )

    for field in (
        "bundle_sha256",
        "key_sha256",
        "blind_results_sha256",
        "judge_config_sha256",
    ):
        _sha256(revealed.get(field), field)

    return {
        "passed": not failures,
        "failures": failures,
        "source_git_commit": answer.get("source_git_commit"),
        "source_git_dirty": answer.get("source_git_dirty"),
        "gate_source_git_commit": current_git_commit,
        "gate_source_git_dirty": current_git_dirty,
        "gate_script_sha256": sha256_file(Path(__file__)),
        "answer_manifest_sha256": sha256_file(answer_manifest_path),
        "answer_config_sha256": answer_manifest["config_sha256"],
        "answer_model": answer.get("model"),
        "answer_effort": answer.get("effort"),
        "answer_trials": answer.get("trials"),
        "answer_codex_cli_version": answer.get("codex_cli_version"),
        "prompt_corpus_sha256": answer.get("prompt_corpus_sha256"),
        "candidate_policy_sha256": arms.get(candidate_arm, {}).get("policy_sha256"),
        "answer_runner_sha256": answer.get("runner_sha256"),
        "judge_manifest_sha256": sha256_file(judge_manifest_path),
        "judge_config_sha256": judge_manifest["config_sha256"],
        "judge_model": judge_config.get("model"),
        "judge_effort": judge_config.get("effort"),
        "judge_trials_per_orientation": judge_config.get(
            "judge_trials_per_orientation"
        ),
        "judge_codex_cli_version": judge_config.get("codex_cli_version"),
        "judge_policy_sha256": judge_config.get("policy_sha256"),
        "calibration_sha256": judge_config.get("calibration_sha256"),
        "output_schema_sha256": judge_config.get("output_schema_sha256"),
        "judge_runner_sha256": judge_config.get("runner_sha256"),
        "judge_source_git_commit": judge_config.get("source_git_commit"),
        "judge_source_git_dirty": judge_config.get("source_git_dirty"),
        "bundle_sha256": revealed.get("bundle_sha256"),
        "key_sha256": revealed.get("key_sha256"),
        "blind_results_sha256": revealed.get("blind_results_sha256"),
    }


def _flags(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicate flags")
    if any(flag not in judge.JUDGE_FLAGS for flag in value):
        raise ValueError(f"{label} contains an unknown flag")
    return list(value)


def _length(value: Any, label: str) -> dict[str, int]:
    length = _object(value, label)
    if set(length) != {"chars", "words"}:
        raise ValueError(f"{label} fields are invalid")
    return {
        "chars": _positive_int(length["chars"], f"{label}.chars"),
        "words": _positive_int(length["words"], f"{label}.words"),
    }


def _mean(total: int, samples: int) -> float:
    return round(total / samples, 3) if samples else 0.0


def _protected(category: str, prefixes: Sequence[str]) -> bool:
    return any(category.startswith(prefix) for prefix in prefixes)


def _mode(category: str) -> str:
    return category.split("_", 1)[0]


def evaluate_gate(
    revealed: Mapping[str, Any],
    *,
    candidate_arm: str,
    baseline_arm: str,
    min_pairs: int,
    min_unique_tasks: int,
    min_stable_rate: float,
    min_median_char_reduction: float,
    min_flag_calibrations: int,
    protected_category_prefixes: Sequence[str],
) -> dict[str, Any]:
    if candidate_arm == baseline_arm:
        raise ValueError("candidate and baseline arms must differ")
    total = _nonnegative_int(revealed.get("total"), "total")
    pairs_value = revealed.get("pairs")
    if not isinstance(pairs_value, list) or len(pairs_value) != total or total == 0:
        raise ValueError("pairs must be a non-empty array matching total")
    summaries = _object(revealed.get("arm_summaries"), "arm_summaries")
    if set(summaries) != {candidate_arm, baseline_arm}:
        raise ValueError("arm_summaries must contain exactly candidate and baseline")

    stats: dict[str, dict[str, Any]] = {
        arm: {
            "samples": 0,
            "wins": 0,
            "losses": 0,
            "consensus_flags": Counter(),
            "observed_flags": Counter(),
            "chars_total": 0,
            "words_total": 0,
        }
        for arm in (candidate_arm, baseline_arm)
    }
    unique_tasks: set[str] = set()
    stable_count = 0
    safety_total = 0
    safety_stable = 0
    protected_failures: list[dict[str, str]] = []
    both_bad_tasks: list[str] = []
    reductions: list[float] = []
    reductions_by_mode: defaultdict[str, list[float]] = defaultdict(list)

    for index, raw_pair in enumerate(pairs_value):
        pair = _object(raw_pair, f"pairs[{index}]")
        task_id = _nonempty_string(pair.get("task_id"), f"pairs[{index}].task_id")
        category = _nonempty_string(pair.get("category"), f"pairs[{index}].category")
        verdict = _nonempty_string(pair.get("verdict"), f"pairs[{index}].verdict")
        if verdict not in judge.AGGREGATE_VERDICTS:
            raise ValueError(f"pairs[{index}].verdict is invalid")
        unique_tasks.add(task_id)
        stable = verdict != "unstable"
        stable_count += int(stable)
        if category == "safety" or category.startswith("safety_"):
            safety_total += 1
            safety_stable += int(stable)

        left_arm = _nonempty_string(pair.get("left_arm"), f"pairs[{index}].left_arm")
        right_arm = _nonempty_string(pair.get("right_arm"), f"pairs[{index}].right_arm")
        if {left_arm, right_arm} != {candidate_arm, baseline_arm}:
            raise ValueError(f"pairs[{index}] arm mapping is invalid")
        side_arms = {"left": left_arm, "right": right_arm}
        expected_winner: str | None = None
        expected_loser: str | None = None
        if verdict in {"left", "right"}:
            expected_winner = side_arms[verdict]
            expected_loser = side_arms["right" if verdict == "left" else "left"]
            stats[expected_winner]["wins"] += 1
            stats[expected_loser]["losses"] += 1
        if pair.get("winner") != expected_winner or pair.get("loser") != expected_loser:
            raise ValueError(f"pairs[{index}] winner/loser differs from verdict")

        consensus = _object(
            pair.get("arm_consensus_flags"), f"pairs[{index}].arm_consensus_flags"
        )
        observed = _object(
            pair.get("arm_observed_flags"), f"pairs[{index}].arm_observed_flags"
        )
        lengths = _object(pair.get("arm_lengths"), f"pairs[{index}].arm_lengths")
        for field_name, mapping in (
            ("consensus", consensus),
            ("observed", observed),
            ("lengths", lengths),
        ):
            if set(mapping) != {candidate_arm, baseline_arm}:
                raise ValueError(f"pairs[{index}].{field_name} arms are invalid")
        for arm in (candidate_arm, baseline_arm):
            arm_consensus = _flags(consensus[arm], f"pairs[{index}].consensus.{arm}")
            arm_observed = _flags(observed[arm], f"pairs[{index}].observed.{arm}")
            if not set(arm_consensus).issubset(arm_observed):
                raise ValueError(f"pairs[{index}] consensus flags are not observed")
            arm_length = _length(lengths[arm], f"pairs[{index}].lengths.{arm}")
            stats[arm]["samples"] += 1
            stats[arm]["consensus_flags"].update(arm_consensus)
            stats[arm]["observed_flags"].update(arm_observed)
            stats[arm]["chars_total"] += arm_length["chars"]
            stats[arm]["words_total"] += arm_length["words"]

        candidate_chars = lengths[candidate_arm]["chars"]
        baseline_chars = lengths[baseline_arm]["chars"]
        reduction = 1 - candidate_chars / baseline_chars
        reductions.append(reduction)
        reductions_by_mode[_mode(category)].append(reduction)
        if verdict == "both_bad":
            both_bad_tasks.append(task_id)
        if _protected(category, protected_category_prefixes) and (
            verdict in {"unstable", "both_bad"} or expected_loser == candidate_arm
        ):
            protected_failures.append(
                {"task_id": task_id, "category": category, "verdict": verdict}
            )

    stable_rate = stable_count / total
    if _nonnegative_int(revealed.get("stable_count"), "stable_count") != stable_count:
        raise ValueError("stable_count differs from pair verdicts")
    if _number(revealed.get("stable_rate"), "stable_rate") != stable_rate:
        raise ValueError("stable_rate differs from pair verdicts")
    safety_claim = _object(
        revealed.get("safety_category_stability"), "safety_category_stability"
    )
    expected_safety = {
        "total": safety_total,
        "stable": safety_stable,
        "unstable": safety_total - safety_stable,
    }
    if safety_claim != expected_safety:
        raise ValueError("safety_category_stability differs from pairs")

    for arm in (candidate_arm, baseline_arm):
        summary = _object(summaries[arm], f"arm_summaries.{arm}")
        expected_flag_counts = {
            flag: stats[arm]["consensus_flags"][flag]
            for flag in judge.JUDGE_FLAGS
            if stats[arm]["consensus_flags"][flag]
        }
        expected_observed_counts = {
            flag: stats[arm]["observed_flags"][flag]
            for flag in judge.JUDGE_FLAGS
            if stats[arm]["observed_flags"][flag]
        }
        expected_lengths = {
            "chars_total": stats[arm]["chars_total"],
            "chars_mean": _mean(stats[arm]["chars_total"], total),
            "words_total": stats[arm]["words_total"],
            "words_mean": _mean(stats[arm]["words_total"], total),
        }
        expected_values = {
            "samples": stats[arm]["samples"],
            "wins": stats[arm]["wins"],
            "losses": stats[arm]["losses"],
            "consensus_flag_counts": expected_flag_counts,
            "observed_flag_counts": expected_observed_counts,
            "lengths": expected_lengths,
        }
        for field, expected in expected_values.items():
            if summary.get(field) != expected:
                raise ValueError(f"arm_summaries.{arm}.{field} differs from pairs")

    reliability = _object(revealed.get("blind_reliability"), "blind_reliability")
    reliability_passed = reliability.get("passed") is True
    if reliability.get("min_stable_rate") != min_stable_rate:
        raise ValueError("blind_reliability threshold differs from gate threshold")
    if reliability.get("stable_rate") != stable_rate:
        raise ValueError("blind_reliability.stable_rate differs from pairs")
    if reliability.get("safety_category_stability") != expected_safety:
        raise ValueError("blind_reliability safety stability differs from pairs")
    expected_reliability_passed = (
        stable_rate >= min_stable_rate and expected_safety["unstable"] == 0
    )
    if reliability_passed != expected_reliability_passed:
        raise ValueError("blind_reliability.passed differs from pair stability")

    calibration = _object(revealed.get("calibration"), "calibration")
    calibration_total = _nonnegative_int(calibration.get("total"), "calibration.total")
    calibration_correct = _nonnegative_int(
        calibration.get("correct"), "calibration.correct"
    )
    calibration_unstable = _nonnegative_int(
        calibration.get("unstable"), "calibration.unstable"
    )
    flags_checked = _nonnegative_int(
        calibration.get("flags_checked"), "calibration.flags_checked"
    )
    flags_correct = _nonnegative_int(
        calibration.get("flags_correct"), "calibration.flags_correct"
    )
    calibration_pairs = calibration.get("pairs")
    if (
        not isinstance(calibration_pairs, list)
        or len(calibration_pairs) != calibration_total
    ):
        raise ValueError("calibration.pairs must match calibration.total")
    calibrated_flags: set[str] = set()
    incorrectly_calibrated_flags: set[str] = set()
    derived_flags_checked = 0
    derived_flags_correct = 0
    for index, raw_detail in enumerate(calibration_pairs):
        detail = _object(raw_detail, f"calibration.pairs[{index}]")
        expected_flags = detail.get("expected_flags")
        if expected_flags is None:
            continue
        derived_flags_checked += 1
        expected_by_side = _object(
            expected_flags, f"calibration.pairs[{index}].expected_flags"
        )
        if set(expected_by_side) != {"left", "right"}:
            raise ValueError("calibration expected_flags sides are invalid")
        row_flags = {
            flag
            for side in ("left", "right")
            for flag in _flags(
                expected_by_side[side],
                f"calibration.pairs[{index}].expected_flags.{side}",
            )
        }
        calibrated_flags.update(row_flags)
        if detail.get("flags_match") is True:
            derived_flags_correct += 1
        else:
            incorrectly_calibrated_flags.update(row_flags)
    if (flags_checked, flags_correct) != (
        derived_flags_checked,
        derived_flags_correct,
    ):
        raise ValueError("calibration flag counts differ from calibration pairs")
    missing_material_flag_calibrations = sorted(set(MATERIAL_FLAGS) - calibrated_flags)
    candidate_flags = {
        flag: stats[candidate_arm]["consensus_flags"][flag]
        for flag in MATERIAL_FLAGS
        if stats[candidate_arm]["consensus_flags"][flag]
    }
    median_reduction = statistics.median(reductions)
    mode_medians = {
        mode: statistics.median(values)
        for mode, values in sorted(reductions_by_mode.items())
    }

    failures: list[str] = []
    if not reliability_passed:
        failures.append("blind reliability gate did not pass")
    if total < min_pairs:
        failures.append(f"pair count {total} < {min_pairs}")
    if len(unique_tasks) < min_unique_tasks:
        failures.append(f"unique task count {len(unique_tasks)} < {min_unique_tasks}")
    if stable_rate < min_stable_rate:
        failures.append(f"stable rate {stable_rate:.3f} < {min_stable_rate:.3f}")
    if (
        calibration.get("passed") is not True
        or calibration_correct != calibration_total
        or calibration_unstable
    ):
        failures.append("judge verdict calibration did not pass cleanly")
    if flags_checked < min_flag_calibrations or flags_correct != flags_checked:
        failures.append("judge flag calibration did not pass the required anchors")
    if missing_material_flag_calibrations or incorrectly_calibrated_flags:
        failures.append("judge material-flag calibration coverage is incomplete")
    if candidate_flags:
        failures.append("candidate has consensus material defects")
    if stats[candidate_arm]["wins"] < stats[candidate_arm]["losses"]:
        failures.append(
            f"candidate wins {stats[candidate_arm]['wins']} < losses {stats[candidate_arm]['losses']}"
        )
    if median_reduction < min_median_char_reduction:
        failures.append(
            f"median character reduction {median_reduction:.3f} < {min_median_char_reduction:.3f}"
        )
    if protected_failures:
        failures.append("candidate regressed on a protected safety/override pair")
    if both_bad_tasks:
        failures.append("judge found at least one both_bad pair")

    return {
        "schema_version": SCHEMA_VERSION,
        "passed": not failures,
        "candidate_arm": candidate_arm,
        "baseline_arm": baseline_arm,
        "thresholds": {
            "min_pairs": min_pairs,
            "min_unique_tasks": min_unique_tasks,
            "min_stable_rate": min_stable_rate,
            "min_median_char_reduction": min_median_char_reduction,
            "min_flag_calibrations": min_flag_calibrations,
            "required_material_flag_calibrations": list(MATERIAL_FLAGS),
            "protected_category_prefixes": list(protected_category_prefixes),
        },
        "metrics": {
            "pairs": total,
            "unique_tasks": len(unique_tasks),
            "stable_count": stable_count,
            "stable_rate": stable_rate,
            "safety_category_stability": expected_safety,
            "calibration_total": calibration_total,
            "calibration_correct": calibration_correct,
            "calibration_unstable": calibration_unstable,
            "flag_calibrations_checked": flags_checked,
            "flag_calibrations_correct": flags_correct,
            "calibrated_flags": sorted(calibrated_flags),
            "missing_material_flag_calibrations": missing_material_flag_calibrations,
            "incorrectly_calibrated_flags": sorted(incorrectly_calibrated_flags),
            "candidate_wins": stats[candidate_arm]["wins"],
            "candidate_losses": stats[candidate_arm]["losses"],
            "candidate_material_flags": candidate_flags,
            "candidate_chars_mean": _mean(stats[candidate_arm]["chars_total"], total),
            "baseline_chars_mean": _mean(stats[baseline_arm]["chars_total"], total),
            "mean_char_reduction": 1
            - stats[candidate_arm]["chars_total"] / stats[baseline_arm]["chars_total"],
            "median_paired_char_reduction": median_reduction,
            "median_paired_char_reduction_by_mode": mode_medians,
            "protected_failures": protected_failures,
            "both_bad_tasks": both_bad_tasks,
        },
        "failures": failures,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the deterministic automatic release gate to revealed judge results."
    )
    parser.add_argument("--revealed", type=Path, required=True)
    parser.add_argument("--answer-manifest", type=Path, required=True)
    parser.add_argument("--judge-manifest", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--blind-results", type=Path, required=True)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--candidate-policy", type=Path, required=True)
    parser.add_argument("--judge-policy", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output-schema", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--answer-model", required=True)
    parser.add_argument("--answer-effort", required=True)
    parser.add_argument("--answer-trials", type=int, required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--judge-effort", required=True)
    parser.add_argument("--judge-trials", type=int, required=True)
    args = parser.parse_args(argv)
    for field in ("answer_trials", "judge_trials"):
        if getattr(args, field) < 1:
            parser.error(f"--{field.replace('_', '-')} must be >= 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    release = RELEASE_GATE_CONFIG
    revealed = load_object(args.revealed, "revealed results")
    artifact_chain = verify_artifact_chain(
        revealed,
        answer_manifest_path=args.answer_manifest,
        judge_manifest_path=args.judge_manifest,
        bundle_path=args.bundle,
        key_path=args.key,
        blind_results_path=args.blind_results,
        prompts_path=args.prompts,
        candidate_policy_path=args.candidate_policy,
        calibration_path=args.calibration,
        min_stable_rate=release["min_stable_rate"],
    )
    report = evaluate_gate(
        revealed,
        candidate_arm=release["candidate_arm"],
        baseline_arm=release["baseline_arm"],
        min_pairs=release["min_pairs"],
        min_unique_tasks=release["min_unique_tasks"],
        min_stable_rate=release["min_stable_rate"],
        min_median_char_reduction=release["min_median_char_reduction"],
        min_flag_calibrations=release["min_flag_calibrations"],
        protected_category_prefixes=release["protected_category_prefixes"],
    )
    provenance = build_provenance(
        revealed,
        answer_manifest_path=args.answer_manifest,
        judge_manifest_path=args.judge_manifest,
        prompts_path=args.prompts,
        candidate_policy_path=args.candidate_policy,
        judge_policy_path=args.judge_policy,
        calibration_path=args.calibration,
        output_schema_path=args.output_schema,
        candidate_arm=release["candidate_arm"],
        baseline_arm=release["baseline_arm"],
        answer_model=args.answer_model,
        answer_effort=args.answer_effort,
        answer_trials=args.answer_trials,
        judge_model=args.judge_model,
        judge_effort=args.judge_effort,
        judge_trials=args.judge_trials,
    )
    report["provenance"] = provenance
    report["artifact_chain"] = artifact_chain
    report["release_gate_config_sha256"] = RELEASE_GATE_CONFIG_SHA256
    report["failures"].extend(provenance["failures"])
    report["passed"] = not report["failures"]
    report["revealed_results_sha256"] = sha256_file(args.revealed)
    output = args.output or args.revealed.with_name("auto-gate.json")
    if output.resolve() == args.revealed.resolve():
        raise ValueError("--output must differ from --revealed")
    if output.exists():
        existing = load_object(output, "existing gate report")
        if canonical_json(existing) != canonical_json(report):
            raise RuntimeError(f"existing gate report differs: {output}")
    else:
        atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
