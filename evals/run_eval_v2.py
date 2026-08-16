"""Offline, sealed answer/judge chain for eval v2."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any

import eval_v2_lib as lib


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/cases"
LANES = {
    "dev_output": 48, "dev_activation": 36, "primary_activation_holdout": 40,
    "primary_output_holdout": 60, "full_skill_parity": 8,
    "blind_judge_and_tiebreak": 28, "primary_coding": 9, "compatibility": 46,
}


def _open_directory(path: Path, *, create: bool, private: bool) -> int:
    raw = path.absolute()
    probe = raw
    while probe != probe.parent:
        if str(probe) not in {"/var", "/tmp"}:
            try:
                if stat.S_ISLNK(os.lstat(probe).st_mode):
                    raise ValueError(f"unsafe symlinked path: {raw}")
            except FileNotFoundError:
                pass
        probe = probe.parent
    absolute = raw.resolve(strict=False)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for name in absolute.parts[1:]:
            try:
                child = os.open(name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700 if private else 0o755, dir_fd=descriptor)
                child = os.open(name, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(f"unsafe symlinked path: {absolute}") from exc
                raise
            os.close(descriptor)
            descriptor = child
        if private:
            os.fchmod(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _ensure_dir(path: Path, *, private: bool) -> None:
    descriptor = _open_directory(path, create=True, private=private)
    os.close(descriptor)


def _read_bytes(path: Path, *, private: bool) -> bytes:
    parent = _open_directory(path.parent, create=False, private=private)
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EISDIR}:
                raise ValueError(f"unsafe file: {path}") from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(f"unsafe file: {path}")
            chunks = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _exists(path: Path, *, private: bool) -> bool:
    try:
        parent = _open_directory(path.parent, create=False, private=private)
    except FileNotFoundError:
        return False
    try:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"unsafe symlinked path: {path}")
        return True
    finally:
        os.close(parent)


def _write_bytes(path: Path, data: bytes, *, private: bool, exclusive: bool = False) -> None:
    parent = _open_directory(path.parent, create=True, private=private)
    mode = 0o600 if private else 0o644
    try:
        try:
            prior = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            prior = None
        if prior and stat.S_ISLNK(prior.st_mode):
            raise ValueError(f"unsafe symlinked file: {path}")
        if exclusive:
            descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=parent)
            try:
                os.write(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent)
            return
        for _ in range(16):
            temporary = f".{path.name}.{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=parent)
            except FileExistsError:
                continue
            try:
                os.write(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            return
        raise RuntimeError("unable to allocate safe temporary artifact")
    finally:
        os.close(parent)


def _json_bytes(value: Any) -> bytes:
    return (lib.canonical_json(value) + "\n").encode("utf-8")


def _read_json(path: Path, *, private: bool) -> dict[str, Any]:
    try:
        text = _read_bytes(path, private=private).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: malformed UTF-8") from exc
    return lib._json_object(text, str(path))


def _read_jsonl(path: Path, *, private: bool) -> list[dict[str, Any]]:
    try:
        text = _read_bytes(path, private=private).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: malformed UTF-8") from exc
    rows = [lib._json_object(line, f"{path}:{number}") for number, line in enumerate(text.splitlines(), 1) if line.strip()]
    if not rows:
        raise ValueError(f"{path}: empty JSONL")
    return rows


def _write_json(path: Path, value: Any, *, private: bool, exclusive: bool = False) -> None:
    _write_bytes(path, _json_bytes(value), private=private, exclusive=exclusive)


def _write_jsonl(path: Path, rows: list[dict[str, Any]], *, private: bool, exclusive: bool = False) -> None:
    if not rows:
        raise ValueError("empty JSONL artifact")
    _write_bytes(path, "".join(lib.canonical_json(row) + "\n" for row in rows).encode("utf-8"), private=private, exclusive=exclusive)


def _sha(path: Path, *, private: bool) -> str:
    return hashlib.sha256(_read_bytes(path, private=private)).hexdigest()


def _directory_names(path: Path, *, private: bool, directories_only: bool = False) -> list[str]:
    descriptor = _open_directory(path, create=False, private=private)
    try:
        names = os.listdir(descriptor)
        for name in names:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or (directories_only and not stat.S_ISDIR(info.st_mode)):
                raise ValueError(f"unsafe artifact inventory: {path / name}")
        return sorted(names)
    finally:
        os.close(descriptor)


def _root(value: str) -> Path:
    root = Path(value).absolute()
    resolved = root.resolve(strict=False)
    for protected in (ROOT.resolve(), CASES.resolve()):
        try:
            resolved.relative_to(protected)
        except ValueError:
            continue
        raise ValueError("eval output root must be outside source and corpora")
    return root


def _prepare_root(root: Path) -> None:
    descriptor = _open_directory(root, create=True, private=False)
    os.close(descriptor)
    _ensure_dir(root / "private", private=True)
    _ensure_dir(root / "public", private=False)


def validate_budget(plan: dict[str, Any]) -> None:
    lib.require_exact_fields(plan, {"schema_version", "hard_cap", "lanes", "planned_calls"}, "release plan")
    if plan["schema_version"] != 1 or type(plan["hard_cap"]) is not int or type(plan["planned_calls"]) is not int or plan["lanes"] != LANES:
        raise ValueError("release lanes must match preregistered contract")
    if plan["planned_calls"] != sum(LANES.values()) or plan["hard_cap"] != 280 or plan["planned_calls"] > plan["hard_cap"]:
        raise ValueError("release budget exceeded")


def build_plan(path: Path) -> dict[str, Any]:
    plan = _read_json(path, private=False)
    validate_budget(plan)
    records = [{"lane": lane, "ordinal": ordinal} for lane, count in LANES.items() for ordinal in range(1, count + 1)]
    if len(records) != plan["planned_calls"]:
        raise ValueError("derived release plan mismatch")
    return {"planned_calls": len(records), "hard_cap": plan["hard_cap"], "lanes": plan["lanes"], "records": records}


def start_attempt(path: Path, identity: dict[str, Any]) -> None:
    _ensure_dir(path, private=True)
    _write_json(path / "started.json", {"schema_version": 1, "status": "started", "identity": identity}, private=True, exclusive=True)


def finish_attempt(path: Path, result: dict[str, Any], status: str = "completed") -> None:
    if status not in {"completed", "failed", "interrupted"} or not _exists(path / "started.json", private=True):
        raise ValueError("invalid attempt finish")
    _write_json(path / "result.json", {"schema_version": 1, "status": status, "result": result}, private=True, exclusive=True)


def load_attempt(path: Path) -> dict[str, Any]:
    started = _read_json(path / "started.json", private=True)
    lib.require_exact_fields(started, {"schema_version", "status", "identity"}, "started attempt")
    if started["schema_version"] != 1 or started["status"] != "started" or not isinstance(started["identity"], dict):
        raise ValueError("invalid started attempt")
    if not _exists(path / "result.json", private=True):
        return {"status": "started", "identity": started["identity"]}
    result = _read_json(path / "result.json", private=True)
    lib.require_exact_fields(result, {"schema_version", "status", "result"}, "attempt result")
    if result["schema_version"] != 1 or result["status"] not in {"completed", "failed", "interrupted"} or not isinstance(result["result"], dict):
        raise ValueError("invalid attempt result")
    return {"status": result["status"], "identity": started["identity"], "result": result["result"]}


def _key(secret: str, purpose: str) -> str:
    return lib.hmac_digest(secret, {"purpose": purpose})


def _load_or_create_key(root: Path, purpose: str, secret: str) -> str:
    path = root / "private" / f"{purpose}-key.json"
    value = {"schema_version": 1, "purpose": purpose, "key": _key(secret, purpose)}
    if _exists(path, private=True):
        if _read_json(path, private=True) != value:
            raise ValueError("private key mismatch")
    else:
        _write_json(path, value, private=True, exclusive=True)
    return value["key"]


def _manifest(root: Path, cases: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    value = {"schema_version": 1, "output_cases_sha256": hashlib.sha256(lib.canonical_json(cases).encode()).hexdigest(), "plan_sha256": hashlib.sha256(lib.canonical_json(plan).encode()).hexdigest(), "runner": "eval_v2_fake_v2"}
    path = root / "private/manifest.json"
    if _exists(path, private=True):
        if _read_json(path, private=True) != value: raise ValueError("manifest mismatch")
    else:
        _write_json(path, value, private=True, exclusive=True)
    return value


def _schedule(cases: list[dict[str, Any]], secret: str) -> dict[str, Any]:
    calls = []
    for sequence, row in enumerate(lib.balanced_schedule(cases, ("A", "B"), secret), 1):
        calls.append({"sequence": sequence, "case_id": row["case_id"], "arm": row["arm"], "ordinal": row["ordinal"], "run_id": lib.opaque_id("call", secret, {"case": row["case_id"], "arm": row["arm"]})})
    return {"schema_version": 1, "calls": calls}


def _load_or_create_schedule(root: Path, cases: list[dict[str, Any]], secret: str) -> dict[str, Any]:
    expected = _schedule(cases, secret)
    path = root / "private/schedule.json"
    if _exists(path, private=True):
        schedule = _read_json(path, private=True)
        if schedule != expected: raise ValueError("committed schedule mismatch")
    else:
        _write_json(path, expected, private=True, exclusive=True)
        schedule = expected
    return schedule


def _fake_response(case: dict[str, Any]) -> tuple[str, str]:
    facts = "; ".join(" / ".join(group) for fact in case["critical_facts"] for group in fact["groups"])
    return "", f"{case['id']} {facts}"


def _answer_payload(identity: dict[str, Any], commentary: str, final: str) -> dict[str, Any]:
    return {**identity, "commentary": commentary, "final": final, "commentary_visible_tokens": len(commentary.split()), "final_visible_tokens": len(final.split()), "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": len(final.split()), "latency_ms": 1}


def _answer_fields() -> set[str]:
    return {"sequence", "case_id", "arm", "run_id", "commentary", "final", "commentary_visible_tokens", "final_visible_tokens", "input_tokens", "cached_input_tokens", "output_tokens", "latency_ms"}


def _assert_completed_attempt_inventory(path: Path) -> None:
    if set(_directory_names(path, private=True)) != {"started.json", "raw.jsonl", "result.json"}:
        raise ValueError("attempt artifact inventory mismatch")


def _load_answer_attempt(path: Path, expected_identity: dict[str, Any]) -> dict[str, Any]:
    _assert_completed_attempt_inventory(path)
    loaded = load_attempt(path)
    if loaded["status"] != "completed" or loaded["identity"] != expected_identity:
        raise ValueError("answer attempt is not resumable")
    result = loaded["result"]
    lib.require_exact_fields(result, _answer_fields() | {"raw_sha256"}, "answer result")
    if any(result[key] != value for key, value in expected_identity.items()):
        raise ValueError("answer identity tampered")
    raw_path = path / "raw.jsonl"
    rows = _read_jsonl(raw_path, private=True)
    if len(rows) != 1 or result["raw_sha256"] != _sha(raw_path, private=True):
        raise ValueError("answer raw evidence mismatch")
    payload = {key: value for key, value in result.items() if key != "raw_sha256"}
    if lib.canonical_json(rows[0]) != lib.canonical_json(payload):
        raise ValueError("answer result does not reconstruct from raw")
    return result


def _answer_runs(root: Path, cases: list[dict[str, Any]], schedule: dict[str, Any]) -> list[dict[str, Any]]:
    lib.require_exact_fields(schedule, {"schema_version", "calls"}, "schedule")
    if schedule["schema_version"] != 1 or not isinstance(schedule["calls"], list) or len(schedule["calls"]) != 24:
        raise ValueError("invalid schedule")
    case_by_id = {case["id"]: case for case in cases}
    expected_ids = set()
    runs = []
    attempts = root / "private/attempts"
    _ensure_dir(attempts, private=True)
    for call in schedule["calls"]:
        lib.require_exact_fields(call, {"sequence", "case_id", "arm", "ordinal", "run_id"}, "scheduled call")
        if type(call["sequence"]) is not int or call["case_id"] not in case_by_id or call["arm"] not in {"A", "B"} or type(call["ordinal"]) is not int or not isinstance(call["run_id"], str):
            raise ValueError("invalid scheduled call")
        expected_ids.add(call["run_id"])
    existing = _directory_names(attempts, private=True, directories_only=True)
    if len(expected_ids) != len(schedule["calls"]) or existing not in ([], sorted(expected_ids)):
        raise ValueError("unexpected answer attempt inventory")
    for call in schedule["calls"]:
        identity = {"sequence": call["sequence"], "case_id": call["case_id"], "arm": call["arm"], "run_id": call["run_id"]}
        attempt = attempts / call["run_id"]
        if _exists(attempt, private=True):
            runs.append(_load_answer_attempt(attempt, identity))
            continue
        start_attempt(attempt, identity)
        commentary, final = _fake_response(case_by_id[call["case_id"]])
        payload = _answer_payload(identity, commentary, final)
        raw = attempt / "raw.jsonl"
        _write_jsonl(raw, [payload], private=True, exclusive=True)
        result = {**payload, "raw_sha256": _sha(raw, private=True)}
        finish_attempt(attempt, result)
        runs.append(result)
    return runs


def _load_runs(root: Path, schedule: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = root / "private/attempts"
    expected = {call["run_id"] for call in schedule["calls"]}
    if set(_directory_names(attempts, private=True, directories_only=True)) != expected:
        raise ValueError("answer artifact inventory mismatch")
    return [_load_answer_attempt(attempts / call["run_id"], {"sequence": call["sequence"], "case_id": call["case_id"], "arm": call["arm"], "run_id": call["run_id"]}) for call in schedule["calls"]]


def _mapping_and_bundle(root: Path, cases: list[dict[str, Any]], runs: list[dict[str, Any]], secret: str, mapping_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_mapping = lib.build_private_mapping(cases, runs, secret)
    mapping_path = root / "private/mapping.json"
    if _exists(mapping_path, private=True):
        mapping = _read_json(mapping_path, private=True)
        if mapping != expected_mapping: raise ValueError("mapping does not match committed schedule and runs")
    else:
        _write_json(mapping_path, expected_mapping, private=True, exclusive=True)
        mapping = expected_mapping
    commitment = lib.mapping_commitment(mapping, mapping_key)
    bundle = lib.build_public_bundle(cases, runs, mapping, commitment=commitment, protected_roots={ROOT, root, root / "private"})
    bundle_path = root / "public/bundle.json"
    if _exists(bundle_path, private=False):
        if _read_json(bundle_path, private=False) != bundle: raise ValueError("frozen public bundle mismatch")
    else:
        _write_json(bundle_path, bundle, private=False, exclusive=True)
    return mapping, bundle


def _load_chain(root: Path, cases: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any], str, str]:
    schedule = _read_json(root / "private/schedule.json", private=True)
    runs = _load_runs(root, schedule)
    mapping = _read_json(root / "private/mapping.json", private=True)
    mapping_key = _read_json(root / "private/mapping-key.json", private=True)["key"]
    seal_key = _read_json(root / "private/seal-key.json", private=True)["key"]
    if not isinstance(mapping_key, str) or not isinstance(seal_key, str): raise ValueError("invalid private key")
    commitment = lib.mapping_commitment(mapping, mapping_key)
    bundle = lib.build_public_bundle(cases, runs, mapping, commitment=commitment, protected_roots={ROOT, root, root / "private"})
    if _read_json(root / "public/bundle.json", private=False) != bundle:
        raise ValueError("frozen public bundle mismatch")
    return schedule, runs, mapping, bundle, mapping_key, seal_key


def _fake_judgment() -> dict[str, Any]:
    return lib.validate_judgment('{"quality":"tie","naturalness":"tie","flags":{"left":[],"right":[]},"rationale":"same offline fake answer"}')


def _load_judgment_attempt(path: Path, pair_id: str, bundle_sha256: str) -> dict[str, Any]:
    _assert_completed_attempt_inventory(path)
    identity = {"pair_id": pair_id, "bundle_sha256": bundle_sha256}
    loaded = load_attempt(path)
    if loaded["status"] != "completed" or loaded["identity"] != identity:
        raise ValueError("judge attempt is not resumable")
    result = loaded["result"]
    lib.require_exact_fields(result, {"pair_id", "judgment", "raw_sha256"}, "judge result")
    if result["pair_id"] != pair_id: raise ValueError("judge identity tampered")
    lib.validate_judgment(result["judgment"])
    raw = path / "raw.jsonl"
    rows = _read_jsonl(raw, private=True)
    if len(rows) != 1 or result["raw_sha256"] != _sha(raw, private=True): raise ValueError("judge raw evidence mismatch")
    payload = {"pair_id": result["pair_id"], "judgment": result["judgment"]}
    if lib.canonical_json(rows[0]) != lib.canonical_json(payload): raise ValueError("judge result does not reconstruct from raw")
    return payload


def _run_or_load_judges(root: Path, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    bundle_sha256 = hashlib.sha256(lib.canonical_json(bundle).encode()).hexdigest()
    expected = {pair["pair_id"] for pair in bundle["pairs"]}
    directory = root / "private/judge-attempts"
    _ensure_dir(directory, private=True)
    names = _directory_names(directory, private=True, directories_only=True)
    if not set(names).issubset(expected): raise ValueError("unexpected judge attempt inventory")
    rows = []
    for pair in bundle["pairs"]:
        path = directory / pair["pair_id"]
        if _exists(path, private=True):
            rows.append(_load_judgment_attempt(path, pair["pair_id"], bundle_sha256))
            continue
        start_attempt(path, {"pair_id": pair["pair_id"], "bundle_sha256": bundle_sha256})
        left = pair["response_A"]["commentary"] + "\n" + pair["response_A"]["final"]
        right = pair["response_B"]["commentary"] + "\n" + pair["response_B"]["final"]
        lib.build_judge_payload({"prompt": pair["prompt"], "verified_context": pair["verified_context"], "deliverable": "final"}, left, right)
        payload = {"pair_id": pair["pair_id"], "judgment": _fake_judgment()}
        raw = path / "raw.jsonl"
        _write_jsonl(raw, [payload], private=True, exclusive=True)
        finish_attempt(path, {**payload, "raw_sha256": _sha(raw, private=True)})
        rows.append(payload)
    aggregate = root / "private/judgments.jsonl"
    if _exists(aggregate, private=True):
        if _read_jsonl(aggregate, private=True) != rows: raise ValueError("judgment aggregate mismatch")
    else:
        _write_jsonl(aggregate, rows, private=True, exclusive=True)
    manifest = {"schema_version": 1, "judge": "strict_fake_v2", "bundle_sha256": bundle_sha256}
    path = root / "private/judge-manifest.json"
    if _exists(path, private=True):
        if _read_json(path, private=True) != manifest: raise ValueError("judge manifest mismatch")
    else:
        _write_json(path, manifest, private=True, exclusive=True)
    return rows


def _load_judgments(root: Path, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    bundle_sha256 = hashlib.sha256(lib.canonical_json(bundle).encode()).hexdigest()
    expected = {pair["pair_id"] for pair in bundle["pairs"]}
    directory = root / "private/judge-attempts"
    if set(_directory_names(directory, private=True, directories_only=True)) != expected:
        raise ValueError("judge artifact inventory mismatch")
    rows = [_load_judgment_attempt(directory / pair["pair_id"], pair["pair_id"], bundle_sha256) for pair in bundle["pairs"]]
    if _read_jsonl(root / "private/judgments.jsonl", private=True) != rows:
        raise ValueError("judgment aggregate mismatch")
    return rows


def _assert_private_inventory(root: Path, *, revealed: bool = False) -> None:
    allowed = {"manifest.json", "mapping-key.json", "seal-key.json", "schedule.json", "mapping.json", "attempts", "judge-attempts", "judgments.jsonl", "judge-manifest.json"}
    if revealed: allowed.add("revealed.json")
    names = set(_directory_names(root / "private", private=True))
    if not names.issubset(allowed): raise ValueError("unexpected private artifact")


def _seal(root: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    _assert_private_inventory(root)
    schedule, _, mapping, bundle, mapping_key, seal_key = _load_chain(root, cases)
    judgments = _load_judgments(root, bundle)
    required = {"manifest.json", "mapping-key.json", "seal-key.json", "schedule.json", "mapping.json", "attempts", "judge-attempts", "judgments.jsonl", "judge-manifest.json"}
    if set(_directory_names(root / "private", private=True)) != required: raise ValueError("incomplete private artifact inventory")
    payload = {"config_sha256": hashlib.sha256(b"eval_v2_phase_a").hexdigest(), "manifest_sha256": _sha(root / "private/manifest.json", private=True), "schedule_sha256": _sha(root / "private/schedule.json", private=True), "bundle_sha256": _sha(root / "public/bundle.json", private=False), "mapping_commitment": lib.mapping_commitment(mapping, mapping_key), "judgments_sha256": _sha(root / "private/judgments.jsonl", private=True), "judge_manifest_sha256": _sha(root / "private/judge-manifest.json", private=True)}
    _write_json(root / "public/seal.json", lib.build_seal(payload, seal_key), private=False, exclusive=True)
    return {"status": "sealed", "judgments": len(judgments)}


def _reveal(root: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    _assert_private_inventory(root, revealed=True)
    _, _, mapping, bundle, mapping_key, seal_key = _load_chain(root, cases)
    judgments = _load_judgments(root, bundle)
    seal = _read_json(root / "public/seal.json", private=False)
    result = lib.reveal(mapping, seal, mapping_key=mapping_key, seal_key=seal_key, config_sha256=hashlib.sha256(b"eval_v2_phase_a").hexdigest(), manifest_sha256=_sha(root / "private/manifest.json", private=True), schedule_sha256=_sha(root / "private/schedule.json", private=True), bundle_sha256=_sha(root / "public/bundle.json", private=False), judgments_sha256=_sha(root / "private/judgments.jsonl", private=True), judge_manifest_sha256=_sha(root / "private/judge-manifest.json", private=True), judgments=judgments)
    path = root / "private/revealed.json"
    if _exists(path, private=True):
        if _read_json(path, private=True) != result: raise ValueError("revealed result mismatch")
    else:
        _write_json(path, result, private=True, exclusive=True)
    return result


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
    _prepare_root(root)
    if args.command in {"answers", "judge", "seal"} and _exists(root / "public/seal.json", private=False):
        raise ValueError("seal is terminal")
    cases = lib.load_output_cases(CASES / "output-dev.jsonl")
    if args.command == "answers":
        if not args.fake: raise ValueError("live calls are disabled in Phase A")
        _assert_private_inventory(root)
        plan = build_plan(ROOT / "evals/release-plan.json")
        _manifest(root, cases, plan)
        mapping_key = _load_or_create_key(root, "mapping", args.secret)
        _load_or_create_key(root, "seal", args.secret)
        schedule = _load_or_create_schedule(root, cases, args.secret)
        runs = _answer_runs(root, cases, schedule)
        _mapping_and_bundle(root, cases, runs, args.secret, mapping_key)
        return {"status": "answered", "runs": len(runs)}
    if args.command == "reveal" and not _exists(root / "public/seal.json", private=False):
        raise ValueError("reveal requires seal")
    _assert_private_inventory(root, revealed=args.command == "reveal")
    _, _, _, bundle, _, _ = _load_chain(root, cases)
    if args.command == "judge":
        if not args.fake: raise ValueError("live calls are disabled in Phase A")
        rows = _run_or_load_judges(root, bundle)
        return {"status": "judged", "judgments": len(rows)}
    if args.command == "seal":
        return _seal(root, cases)
    return _reveal(root, cases)


if __name__ == "__main__":
    print(lib.canonical_json(main()))
