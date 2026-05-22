from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUITE_RUNTIME = "runtime_economics"
SUITE_REFERENCE = "reference_compression"

NORMAL_ARM = "normal"
CONTROL_ARM = "control"
TERSE_ARM = "terse"
SIMPLE_MAN_RUNTIME_ARM = "simple_man_runtime"
SIMPLE_MAN_SKILL_ARM = "simple_man_skill"
CAVEMAN_ARM = "caveman"
CAVEMAN_FULL_ARM = "caveman_full"
CAVEMAN_ULTRA_ARM = "caveman_ultra"
CAVEMAN_ARMS = (CAVEMAN_ARM, CAVEMAN_FULL_ARM, CAVEMAN_ULTRA_ARM)
RUNTIME_ARMS = (
    CONTROL_ARM,
    TERSE_ARM,
    SIMPLE_MAN_RUNTIME_ARM,
    SIMPLE_MAN_SKILL_ARM,
    CAVEMAN_ARM,
)
REFERENCE_ARMS = (
    NORMAL_ARM,
    CAVEMAN_FULL_ARM,
    CAVEMAN_ULTRA_ARM,
    SIMPLE_MAN_RUNTIME_ARM,
    SIMPLE_MAN_SKILL_ARM,
)
ARMS = tuple(dict.fromkeys((*RUNTIME_ARMS, *REFERENCE_ARMS)))
ARM_LABELS = {
    NORMAL_ARM: "Normal",
    SIMPLE_MAN_RUNTIME_ARM: "Simple Man runtime",
    SIMPLE_MAN_SKILL_ARM: "Simple Man skill",
    CAVEMAN_ARM: "Caveman",
    CAVEMAN_FULL_ARM: "Caveman full",
    CAVEMAN_ULTRA_ARM: "Caveman ultra",
}


@dataclass(frozen=True)
class ArmStats:
    median_input: float
    median_cached_input: float
    median_output: float
    median_reasoning_output: float
    median_total: float
    median_codex_total: float
    trials: int


@dataclass(frozen=True)
class PromptStats:
    prompt_id: str
    category: str
    arms: dict[str, ArmStats]
    net_savings_vs_control: float | None
    net_savings_vs_terse: float | None
    output_savings_vs_control: float | None
    output_savings_vs_terse: float | None


@dataclass(frozen=True)
class ComparisonStats:
    output_savings: float | None
    first_turn_net_savings: float | None
    amortized_net_savings: float | None
    amortized_total: float | None


@dataclass(frozen=True)
class CategorySummary:
    category: str
    arm: str
    baseline_arm: str
    prompts: int
    mean_output_savings: float | None
    mean_first_turn_net_savings: float | None
    mean_amortized_net_savings: float | None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_prompts(path: Path) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            prompt = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc

        prompt_id = prompt.get("id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ValueError(f"{path}:{line_number}: prompt id must be a non-empty string")
        if prompt_id in seen:
            raise ValueError(f"{path}:{line_number}: duplicate prompt id: {prompt_id}")
        if not isinstance(prompt.get("category"), str) or not prompt["category"]:
            raise ValueError(f"{path}:{line_number}: category must be a non-empty string")
        if not isinstance(prompt.get("prompt"), str) or not prompt["prompt"].strip():
            raise ValueError(f"{path}:{line_number}: prompt must be a non-empty string")

        seen.add(prompt_id)
        prompts.append(prompt)

    if not prompts:
        raise ValueError(f"{path}: no prompts found")
    return prompts


def prompt_corpus_hash(path: Path) -> str:
    prompts = load_prompts(path)
    normalized = "\n".join(
        json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for prompt in prompts
    )
    return sha256_text(normalized + "\n")


def usage_total(usage: dict[str, Any]) -> int:
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


def visible_input_tokens(run: dict[str, Any]) -> int:
    if "visible_input_tokens" in run:
        return int(run["visible_input_tokens"])
    return int(run.get("usage", {}).get("input_tokens", 0))


def visible_output_tokens(run: dict[str, Any]) -> int:
    if "visible_output_tokens" in run:
        return int(run["visible_output_tokens"])
    return int(run.get("usage", {}).get("output_tokens", 0))


def visible_total_tokens(run: dict[str, Any]) -> int:
    if "visible_total_tokens" in run:
        return int(run["visible_total_tokens"])
    return visible_input_tokens(run) + visible_output_tokens(run)


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _arm_stats(runs: list[dict[str, Any]]) -> ArmStats:
    return ArmStats(
        median_input=_median([visible_input_tokens(run) for run in runs]),
        median_cached_input=_median(
            [int(run.get("usage", {}).get("cached_input_tokens", 0)) for run in runs]
        ),
        median_output=_median([visible_output_tokens(run) for run in runs]),
        median_reasoning_output=_median(
            [int(run.get("usage", {}).get("reasoning_output_tokens", 0)) for run in runs]
        ),
        median_total=_median([visible_total_tokens(run) for run in runs]),
        median_codex_total=_median([usage_total(run.get("usage", {})) for run in runs]),
        trials=len(runs),
    )


def _savings(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return 1 - numerator / denominator


def compare_arm(
    arm: ArmStats,
    baseline: ArmStats,
    *,
    amortize_turns: int,
) -> ComparisonStats:
    overhead = max(arm.median_input - baseline.median_input, 0)
    amortized_total = baseline.median_input + (overhead / amortize_turns) + arm.median_output
    return ComparisonStats(
        output_savings=_savings(arm.median_output, baseline.median_output),
        first_turn_net_savings=_savings(arm.median_total, baseline.median_total),
        amortized_net_savings=_savings(amortized_total, baseline.median_total),
        amortized_total=amortized_total,
    )


def _mean(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    return statistics.mean(clean) if clean else None


def build_prompt_table(snapshot: dict[str, Any]) -> list[PromptStats]:
    prompts_by_id = {
        prompt["id"]: prompt for prompt in snapshot.get("prompts", []) if "id" in prompt
    }
    runs_by_prompt: dict[str, list[dict[str, Any]]] = {}
    for run in snapshot.get("runs", []):
        runs_by_prompt.setdefault(run["prompt_id"], []).append(run)

    rows: list[PromptStats] = []
    for prompt_id in sorted(runs_by_prompt):
        runs = runs_by_prompt[prompt_id]
        category = prompts_by_id.get(prompt_id, {}).get("category", runs[0].get("category", ""))
        by_arm = {
            arm: [run for run in runs if run.get("arm") == arm]
            for arm in sorted({run.get("arm") for run in runs if run.get("arm")})
        }
        arm_stats = {arm: _arm_stats(arm_runs) for arm, arm_runs in by_arm.items()}
        control = arm_stats.get(CONTROL_ARM)
        terse = arm_stats.get(TERSE_ARM)
        simple = arm_stats.get(SIMPLE_MAN_RUNTIME_ARM)

        rows.append(
            PromptStats(
                prompt_id=prompt_id,
                category=category,
                arms=arm_stats,
                net_savings_vs_control=(
                    _savings(simple.median_total, control.median_total)
                    if simple and control
                    else None
                ),
                net_savings_vs_terse=(
                    _savings(simple.median_total, terse.median_total)
                    if simple and terse
                    else None
                ),
                output_savings_vs_control=(
                    _savings(simple.median_output, control.median_output)
                    if simple and control
                    else None
                ),
                output_savings_vs_terse=(
                    _savings(simple.median_output, terse.median_output)
                    if simple and terse
                    else None
                ),
            )
        )
    return rows


def build_category_summary(
    rows: list[PromptStats],
    *,
    arm: str,
    baseline_arm: str,
    amortize_turns: int,
) -> list[CategorySummary]:
    by_category: dict[str, list[ComparisonStats]] = {}
    for row in rows:
        candidate = row.arms.get(arm)
        baseline = row.arms.get(baseline_arm)
        if not candidate or not baseline:
            continue
        by_category.setdefault(row.category, []).append(
            compare_arm(candidate, baseline, amortize_turns=amortize_turns)
        )

    summaries: list[CategorySummary] = []
    for category, comparisons in sorted(by_category.items()):
        summaries.append(
            CategorySummary(
                category=category,
                arm=arm,
                baseline_arm=baseline_arm,
                prompts=len(comparisons),
                mean_output_savings=_mean([item.output_savings for item in comparisons]),
                mean_first_turn_net_savings=_mean(
                    [item.first_turn_net_savings for item in comparisons]
                ),
                mean_amortized_net_savings=_mean(
                    [item.amortized_net_savings for item in comparisons]
                ),
            )
        )
    return summaries


def _normalize_quality_text(text: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    stems: list[str] = []
    for token in tokens:
        if len(token) > 6 and token.endswith("pping"):
            token = token[:-4]
        elif len(token) > 5 and token.endswith("er"):
            token = token[:-2]
        elif len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        stems.append(token)
    return " ".join(stems)


def _quality_term_matches(term: str, text: str, normalized_text: str) -> bool:
    lowered = term.lower()
    if lowered in text:
        return True
    normalized_term = _normalize_quality_text(term)
    if not normalized_term:
        return False
    aliases = {
        "object": ("obj",),
        "reference": ("ref", "identity"),
        "fallback": ("fallback ui", "something went wrong", "role alert"),
    }
    if any(alias in normalized_text for alias in aliases.get(normalized_term, ())):
        return True
    if normalized_term in normalized_text:
        return True
    term_tokens = normalized_term.split()
    if len(term_tokens) > 1:
        text_tokens = set(normalized_text.split())
        return all(token in text_tokens for token in term_tokens)
    return False


def check_run_quality(prompt: dict[str, Any], run: dict[str, Any]) -> list[str]:
    checks = prompt.get("checks") or {}
    text = str(run.get("text", "")).lower()
    normalized_text = _normalize_quality_text(text)
    failures: list[str] = []

    for term in checks.get("must_include", []):
        if not _quality_term_matches(str(term), text, normalized_text):
            failures.append(f"missing required term: {term}")

    for group in checks.get("must_include_any", []):
        terms = [str(term) for term in group]
        if not any(_quality_term_matches(term, text, normalized_text) for term in terms):
            failures.append(f"missing one of: {' | '.join(terms)}")

    for term in checks.get("forbidden", []):
        if str(term).lower() in text:
            failures.append(f"forbidden term present: {term}")

    return failures


def validate_snapshot_freshness(
    snapshot: dict[str, Any],
    skill_path: Path,
    runtime_path: Path | None,
    prompts_path: Path | None,
) -> list[str]:
    metadata = snapshot.get("metadata", {})
    errors: list[str] = []
    skill_hashes = metadata.get("skill_hashes") or {}

    expected_skill = sha256_file(skill_path)
    actual_skill = skill_hashes.get(SIMPLE_MAN_SKILL_ARM) or metadata.get("skill_sha256")
    if actual_skill != expected_skill:
        errors.append(f"skill_sha256 mismatch: snapshot={actual_skill} current={expected_skill}")

    if runtime_path is not None:
        expected_runtime = sha256_file(runtime_path)
        actual_runtime = skill_hashes.get(SIMPLE_MAN_RUNTIME_ARM) or metadata.get(
            "runtime_sha256"
        )
        if actual_runtime != expected_runtime:
            errors.append(
                "runtime_sha256 mismatch: "
                f"snapshot={actual_runtime} current={expected_runtime}"
            )

    if prompts_path is not None:
        expected_prompts = prompt_corpus_hash(prompts_path)
        actual_prompts = metadata.get("prompt_corpus_sha256")
        if actual_prompts != expected_prompts:
            errors.append(
                "prompt_corpus_sha256 mismatch: "
                f"snapshot={actual_prompts} current={expected_prompts}"
            )

    return errors


def validate_snapshot_age(snapshot: dict[str, Any], max_age_days: int) -> list[str]:
    generated_at = snapshot.get("metadata", {}).get("generated_at")
    if not generated_at:
        return ["snapshot metadata is missing generated_at"]
    try:
        generated = dt.datetime.fromisoformat(generated_at)
    except ValueError:
        return [f"snapshot generated_at is not ISO-8601: {generated_at}"]
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - generated.astimezone(dt.timezone.utc)
    if age > dt.timedelta(days=max_age_days):
        return [f"snapshot is older than {max_age_days} days: {generated_at}"]
    return []


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else "+"
    return f"{sign}{abs(value) * 100:.1f}%"
