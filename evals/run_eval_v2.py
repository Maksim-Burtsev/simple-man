"""Offline answer/judge/seal/reveal runner for eval v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import eval_v2_lib as lib


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/cases"


def _read(path: Path) -> dict[str, Any]:
    return lib.strict_json_object(path)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lib.canonical_json(value) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return lib.sha256_file(path)


def validate_budget(plan: dict[str, Any]) -> None:
    lib.require_exact_fields(plan, {"schema_version", "hard_cap", "lanes", "planned_calls"}, "release plan")
    if any(type(plan[key]) is not int for key in ("schema_version", "hard_cap", "planned_calls")) or plan["schema_version"] != 1:
        raise ValueError("invalid release plan")
    if type(plan["lanes"]) is not dict or any(type(value) is not int or value < 0 for value in plan["lanes"].values()):
        raise ValueError("invalid release lanes")
    if sum(plan["lanes"].values()) != plan["planned_calls"] or plan["planned_calls"] > plan["hard_cap"] or plan["hard_cap"] > 280:
        raise ValueError("release budget exceeded")


def build_plan(path: Path) -> dict[str, Any]:
    plan = _read(path)
    validate_budget(plan)
    return {"planned_calls": plan["planned_calls"], "hard_cap": plan["hard_cap"], "lanes": plan["lanes"]}


def start_attempt(path: Path, identity: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    target = path / "started.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "status": "started", "identity": identity}, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def finish_attempt(path: Path, result: dict[str, Any], status: str = "completed") -> None:
    if status not in {"completed", "failed", "interrupted"} or not (path / "started.json").is_file():
        raise ValueError("invalid attempt finish")
    _write(path / "result.json", {"schema_version": 1, "status": status, "result": result})


def load_attempt(path: Path) -> dict[str, Any]:
    started = _read(path / "started.json")
    lib.require_exact_fields(started, {"schema_version", "status", "identity"}, "started attempt")
    if started["schema_version"] != 1 or started["status"] != "started" or not isinstance(started["identity"], dict):
        raise ValueError("invalid started attempt")
    result_path = path / "result.json"
    if not result_path.exists():
        return {"status": "started", "identity": started["identity"]}
    result = _read(result_path)
    lib.require_exact_fields(result, {"schema_version", "status", "result"}, "attempt result")
    if result["schema_version"] != 1 or result["status"] not in {"completed", "failed", "interrupted"} or not isinstance(result["result"], dict):
        raise ValueError("invalid result")
    return {"status": result["status"], "identity": started["identity"], "result": result["result"]}


def _manifest(root: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    path = root / "private/manifest.json"
    value = {"schema_version": 1, "output_cases_sha256": hashlib.sha256(lib.canonical_json(cases).encode()).hexdigest(), "runner": "eval_v2_fake_v1"}
    if path.exists() and _read(path) != value:
        raise ValueError("manifest mismatch")
    _write(path, value)
    return value


def _fake_response(case: dict[str, Any], arm: str) -> tuple[str, str]:
    facts = "; ".join(" / ".join(group) for fact in case["critical_facts"] for group in fact["groups"])
    return "", f"{case['id']} {facts}" if arm == "A" else f"{case['id']} {facts}"


def _answer_runs(root: Path, cases: list[dict[str, Any]], secret: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for case in cases:
        for arm in ("A", "B"):
            run_id = lib.opaque_id("call", secret, {"case": case["id"], "arm": arm})
            attempt = root / "private/attempts" / run_id
            if attempt.exists():
                loaded = load_attempt(attempt)
                if loaded["status"] != "completed":
                    raise ValueError("started attempt is spent and cannot be retried")
                runs.append(loaded["result"])
                continue
            identity = {"case_id": case["id"], "arm": arm, "run_id": run_id}
            start_attempt(attempt, identity)
            commentary, final = _fake_response(case, arm)
            raw = attempt / "raw.jsonl"
            raw.write_text(lib.canonical_json({"commentary": commentary, "final": final}) + "\n", encoding="utf-8")
            result = {**identity, "commentary": commentary, "final": final, "raw_sha256": _sha(raw), "commentary_visible_tokens": len(commentary.split()), "final_visible_tokens": len(final.split()), "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": len(final.split()), "latency_ms": 1}
            finish_attempt(attempt, result)
            runs.append(result)
    return runs


def _load_runs(root: Path) -> list[dict[str, Any]]:
    runs = []
    attempts = root / "private/attempts"
    if not attempts.is_dir():
        raise ValueError("answer attempts missing")
    for path in sorted(attempts.iterdir()):
        loaded = load_attempt(path)
        if loaded["status"] != "completed":
            raise ValueError("incomplete attempt")
        result = loaded["result"]
        raw = path / "raw.jsonl"
        if not raw.is_file() or result.get("raw_sha256") != _sha(raw):
            raise ValueError("raw attempt tampered")
        identity = loaded["identity"]
        if any(result.get(key) != value for key, value in identity.items()):
            raise ValueError("attempt identity tampered")
        runs.append(result)
    return runs


def _judgments(root: Path, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    path = root / "private/judgments.jsonl"
    rows = []
    for pair in bundle["pairs"]:
        left = pair["response_A"]["commentary"] + "\n" + pair["response_A"]["final"]
        right = pair["response_B"]["commentary"] + "\n" + pair["response_B"]["final"]
        lib.build_judge_payload({"prompt": pair["prompt"], "verified_context": pair["verified_context"], "deliverable": "final"}, left, right)
        judgment = lib.validate_judgment('{"quality":"tie","naturalness":"tie","flags":{"left":[],"right":[]},"rationale":"same offline fake answer"}')
        rows.append({"pair_id": pair["pair_id"], "judgment": judgment})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lib.canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    return rows


def _load_judgments(root: Path, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    path = root / "private/judgments.jsonl"
    if not path.is_file(): raise ValueError("judgments missing")
    rows = lib.strict_jsonl(path)
    expected = {pair["pair_id"] for pair in bundle["pairs"]}
    if {row.get("pair_id") for row in rows} != expected or len(rows) != len(expected): raise ValueError("judgment inventory mismatch")
    for row in rows:
        lib.require_exact_fields(row, {"pair_id", "judgment"}, "judgment record")
        lib.validate_judgment(row["judgment"])
    return rows


def _root(value: str) -> Path:
    return Path(value).resolve()


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run"); dry.add_argument("--plan", type=Path, required=True)
    for name in ("answers", "judge", "seal", "reveal"):
        command = sub.add_parser(name); command.add_argument("--root", required=True)
        if name in {"answers", "judge"}: command.add_argument("--fake", action="store_true")
        if name == "answers": command.add_argument("--secret", required=True)
    args = parser.parse_args(argv)
    if args.command == "dry-run":
        return build_plan(args.plan)
    root = _root(args.root)
    if args.command in {"answers", "judge", "seal"} and (root / "public/seal.json").exists():
        raise ValueError("seal is terminal")
    cases = lib.load_output_cases(CASES / "output-dev.jsonl")
    if args.command == "answers":
        if not args.fake: raise ValueError("live calls are disabled in Phase A")
        _manifest(root, cases)
        runs = _answer_runs(root, cases, args.secret)
        mapping = lib.build_private_mapping(cases, runs, args.secret)
        _write(root / "private/mapping.json", mapping)
        return {"status": "answered", "runs": len(runs)}
    if args.command == "reveal" and not (root / "public/seal.json").is_file():
        raise ValueError("reveal requires seal")
    mapping = _read(root / "private/mapping.json")
    runs = _load_runs(root)
    bundle = lib.build_public_bundle(cases, runs, mapping)
    bundle_path = root / "public/bundle.json"
    if args.command == "judge":
        if not args.fake: raise ValueError("live calls are disabled in Phase A")
        judge_attempt = root / "private/judge-attempt"
        if judge_attempt.exists():
            loaded = load_attempt(judge_attempt)
            if loaded["status"] != "completed":
                raise ValueError("started judge attempt is spent and cannot be retried")
            return {"status": "judged", "judgments": loaded["result"]["judgments"]}
        start_attempt(judge_attempt, {"kind": "judge", "bundle_sha256": hashlib.sha256(lib.canonical_json(bundle).encode()).hexdigest()})
        _write(bundle_path, bundle)
        rows = _judgments(root, bundle)
        _write(root / "private/judge-manifest.json", {"schema_version": 1, "judge": "strict_fake_v1"})
        raw = judge_attempt / "raw.jsonl"
        raw.write_text("".join(lib.canonical_json(row) + "\n" for row in rows), encoding="utf-8")
        finish_attempt(judge_attempt, {"judgments": len(rows), "raw_sha256": _sha(raw)})
        return {"status": "judged", "judgments": len(rows)}
    if not bundle_path.is_file() or _read(bundle_path) != bundle:
        raise ValueError("public bundle tampered")
    judgments = _load_judgments(root, bundle)
    manifest = root / "private/manifest.json"
    judge_manifest = root / "private/judge-manifest.json"
    if args.command == "seal":
        payload = {"config_sha256": hashlib.sha256(b"eval_v2_phase_a").hexdigest(), "manifest_sha256": _sha(manifest), "bundle_sha256": _sha(bundle_path), "mapping_commitment": lib.mapping_commitment(mapping), "judgments_sha256": _sha(root / "private/judgments.jsonl"), "judge_manifest_sha256": _sha(judge_manifest)}
        _write(root / "public/seal.json", lib.build_seal(payload, mapping["commitment_nonce"]))
        return {"status": "sealed", "judgments": len(judgments)}
    seal_path = root / "public/seal.json"
    if not seal_path.is_file(): raise ValueError("reveal requires seal")
    result = lib.reveal(mapping, _read(seal_path), config_sha256=hashlib.sha256(b"eval_v2_phase_a").hexdigest(), manifest_sha256=_sha(manifest), bundle_sha256=_sha(bundle_path), judgments_sha256=_sha(root / "private/judgments.jsonl"), judge_manifest_sha256=_sha(judge_manifest))
    _write(root / "private/revealed.json", result)
    return result


if __name__ == "__main__":
    print(lib.canonical_json(main()))
