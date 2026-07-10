#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import auto_judge_lib as judge
from review_lib import (
    atomic_write_json,
    canonical_json,
    private_key_commitment_sha256,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC = ROOT / ".local-fixtures" / "blind-review" / "public"
DEFAULT_PRIVATE = ROOT / ".local-fixtures" / "blind-review" / "private"
DEFAULT_BUNDLE = DEFAULT_PUBLIC / "bundle.json"
DEFAULT_BLIND_RESULTS = DEFAULT_PRIVATE / "auto-judge" / "blind-results.json"
DEFAULT_KEY = DEFAULT_PRIVATE / "key.json"


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {path}:{exc.lineno}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def reliability_report(
    blind: dict[str, Any], *, min_stable_rate: float
) -> dict[str, Any]:
    failures: list[str] = []
    stable_rate = float(blind["stable_rate"])
    safety = blind["safety_category_stability"]
    if stable_rate < min_stable_rate:
        failures.append(f"stable rate {stable_rate:.3f} is below {min_stable_rate:.3f}")
    if safety["unstable"]:
        failures.append(f"{safety['unstable']} safety pair(s) are unstable")
    return {
        "passed": not failures,
        "min_stable_rate": min_stable_rate,
        "stable_rate": stable_rate,
        "safety_category_stability": dict(safety),
        "failures": failures,
    }


def verify_blind_matches_bundle(blind: dict[str, Any], bundle: dict[str, Any]) -> None:
    source_by_id = {pair["id"]: pair for pair in bundle["pairs"]}
    for pair in blind["pairs"]:
        source = source_by_id.get(pair["pair_id"])
        if source is None:
            raise ValueError("blind results contain an unknown bundle pair")
        for field in ("task_id", "category", "language"):
            if pair[field] != source[field]:
                raise ValueError(f"blind result {field} differs from public bundle")
        expected_lengths = {
            side: judge.response_length(source[side]["text"])
            for side in ("left", "right")
        }
        if pair["lengths"] != expected_lengths:
            raise ValueError("blind result lengths differ from public bundle")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reveal arm identities for a completed blind automatic judgment."
    )
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--blind-results", type=Path, default=DEFAULT_BLIND_RESULTS)
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    parser.add_argument(
        "--output",
        type=Path,
        help="Private revealed result path (default: beside blind results).",
    )
    parser.add_argument("--min-stable-rate", type=float, default=0.9)
    parser.add_argument(
        "--allow-inconclusive",
        action="store_true",
        help="Reveal an invalid pilot for diagnosis while preserving failed reliability status.",
    )
    args = parser.parse_args(argv)
    if args.min_stable_rate < 0 or args.min_stable_rate > 1:
        parser.error("--min-stable-rate must be between 0 and 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output or args.blind_results.with_name("revealed-results.json")
    if output.resolve() in {args.blind_results.resolve(), args.key.resolve()}:
        raise ValueError("--output must differ from --blind-results and --key")
    public_root = args.bundle.resolve().parent
    resolved_output = output.resolve()
    if resolved_output == public_root or public_root in resolved_output.parents:
        raise ValueError("--output must not be inside the public artifact directory")

    bundle = judge.load_public_bundle(args.bundle)
    bundle_sha256 = sha256_file(args.bundle)
    blind_results_sha256 = sha256_file(args.blind_results)
    blind = judge.validate_blind_results(
        load_object(args.blind_results, "blind results")
    )
    if bundle_sha256 != blind["bundle_sha256"]:
        raise ValueError("public bundle SHA-256 differs from blind results")
    if bundle["run_id"] != blind["source_run_id"]:
        raise ValueError("public bundle run_id differs from blind results")
    bundle_pair_ids = {pair["id"] for pair in bundle["pairs"]}
    blind_pair_ids = {pair["pair_id"] for pair in blind["pairs"]}
    if bundle_pair_ids != blind_pair_ids:
        raise ValueError("public bundle pair ids differ from blind results")
    verify_blind_matches_bundle(blind, bundle)
    calibration = blind.get("calibration")
    if not isinstance(calibration, dict) or not calibration.get("passed"):
        raise RuntimeError("cannot reveal: calibration gate is missing or failed")
    reliability = reliability_report(blind, min_stable_rate=args.min_stable_rate)
    if not reliability["passed"] and not args.allow_inconclusive:
        raise RuntimeError(
            "cannot reveal: blind reliability gate failed: "
            + "; ".join(reliability["failures"])
        )
    key = load_object(args.key, "private key")
    expected_key_commitment = bundle["metadata"].get("key_commitment_sha256")
    if not isinstance(expected_key_commitment, str):
        raise ValueError("public bundle has no private key commitment")
    if private_key_commitment_sha256(key) != expected_key_commitment:
        raise ValueError("private key differs from public bundle commitment")
    revealed = judge.reveal_results(
        blind,
        key,
        bundle_sha256=bundle_sha256,
        key_sha256=sha256_file(args.key),
    )
    revealed["blind_results_sha256"] = blind_results_sha256
    revealed["blind_reliability"] = reliability

    if output.exists():
        existing = load_object(output, "existing revealed results")
        if canonical_json(existing) != canonical_json(revealed):
            raise RuntimeError(f"existing revealed results differ: {output}")
    else:
        atomic_write_json(output, revealed)

    print(f"Wrote revealed auto-judge results: {output}")
    print(json.dumps(revealed["arm_summaries"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
