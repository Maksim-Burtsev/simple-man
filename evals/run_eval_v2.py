"""Offline, sealed answer/judge chain for eval v2."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import secrets
import stat
import subprocess
import tempfile
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
RUNNER_ID = "eval_v2_fake_v3"
DEFAULT_MODEL = "preregistered-user-model"
DEFAULT_EFFORT = "preregistered-user-effort"
DEFAULT_CLI = "offline-fake-cli-v1"
OUTPUT_TREATMENTS = ("A", "B", "C", "generic")
ACTIVATION_DESCRIPTIONS = ("D0", "D1")
OWNER_FILE = ".eval-v2-owner.json"
OWNER = {"schema_version": 1, "owner": "simple-man-eval-v2"}


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
    except FileNotFoundError as exc:
        raise ValueError(f"missing artifact: {path}") from exc
    return lib._json_object(text, str(path))


def _read_jsonl(path: Path, *, private: bool) -> list[dict[str, Any]]:
    try:
        text = _read_bytes(path, private=private).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: malformed UTF-8") from exc
    except FileNotFoundError as exc:
        raise ValueError(f"missing artifact: {path}") from exc
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


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        try:
            right.relative_to(left)
            return True
        except ValueError:
            return False


def _root(value: str) -> Path:
    root = Path(value).expanduser().absolute()
    resolved = root.resolve(strict=False)
    home = Path.home().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser().resolve(strict=False)
    protected = (ROOT.resolve(), CASES.resolve(), codex_home, home / ".codex/auth")
    if resolved == resolved.anchor or any(_overlaps(resolved, item.resolve(strict=False)) for item in protected):
        raise ValueError("eval output root must be a dedicated directory outside source, home, auth, and CODEX_HOME")
    if resolved in {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}:
        raise ValueError("eval output root cannot be a broad temporary root")
    return root


def _prepare_root(root: Path) -> None:
    descriptor = _open_directory(root, create=True, private=False)
    os.close(descriptor)
    names = set(_directory_names(root, private=False))
    marker = root / OWNER_FILE
    if OWNER_FILE in names:
        if _read_json(marker, private=True) != OWNER:
            raise ValueError("invalid eval output ownership marker")
        if names - {OWNER_FILE, "private", "public"}:
            raise ValueError("unexpected root artifact")
    elif names:
        raise ValueError("refusing to reuse unowned eval output root")
    else:
        _write_json(marker, OWNER, private=True, exclusive=True)
    _ensure_dir(root / "private", private=True)
    _ensure_dir(root / "public", private=False)


def validate_budget(plan: dict[str, Any]) -> None:
    lib.require_exact_fields(plan, {"schema_version", "hard_cap", "lanes", "planned_calls"}, "release plan")
    if plan["schema_version"] != 1 or type(plan["hard_cap"]) is not int or type(plan["planned_calls"]) is not int or plan["lanes"] != LANES:
        raise ValueError("release lanes must match preregistered contract")
    if plan["planned_calls"] != sum(LANES.values()) or plan["hard_cap"] != 280 or plan["planned_calls"] > plan["hard_cap"]:
        raise ValueError("release budget exceeded")


def _record(lane: str, case_slot: str, treatment: str, description: str, *, policy_role: str, conditional: str, trial: int = 1) -> dict[str, Any]:
    base = {"lane": lane, "case_slot": case_slot, "treatment": treatment, "description": description, "trial": trial, "model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT, "cli": DEFAULT_CLI, "policy_role": policy_role, "conditional": conditional}
    return {"call_id": f"call_{hashlib.sha256(lib.canonical_json(base).encode()).hexdigest()[:24]}", **base}


def _derived_records() -> list[dict[str, Any]]:
    output = lib.load_output_cases(CASES / "output-dev.jsonl")
    activation = lib.load_activation_cases(CASES / "activation-dev.jsonl")
    records = []
    for case in output:
        for treatment in OUTPUT_TREATMENTS:
            records.append(_record("dev_output", case["id"], treatment, "output", policy_role=f"dev_output_{treatment}", conditional="dev"))
    for case in activation:
        if case["execution"] == "routed":
            for description in ACTIVATION_DESCRIPTIONS:
                records.append(_record("dev_activation", case["id"], "activation", description, policy_role="activation_decider", conditional="dev"))
    for slot in range(1, 21):
        for description in ACTIVATION_DESCRIPTIONS:
            records.append(_record("primary_activation_holdout", f"activation-holdout-slot-{slot:02}", "activation", description, policy_role="activation_decider", conditional="after_holdout_freeze"))
    for slot in range(1, 16):
        for treatment in OUTPUT_TREATMENTS:
            records.append(_record("primary_output_holdout", f"output-holdout-slot-{slot:02}", treatment, "output", policy_role=f"holdout_output_{treatment}", conditional="after_holdout_freeze"))
    for slot in range(1, 9):
        records.append(_record("full_skill_parity", f"parity-slot-{slot:02}", "parity", "packaged-skill", policy_role="parity", conditional="after_holdout_freeze"))
    for slot in range(1, 29):
        records.append(_record("blind_judge_and_tiebreak", f"blind-judge-slot-{slot:02}", "judge", "strict-json", policy_role="blind_judge", conditional="after_answers"))
    for slot in range(1, 10):
        records.append(_record("primary_coding", f"coding-slot-{slot:02}", "coding", "hidden-validator", policy_role="coding", conditional="after_holdout_freeze"))
    for slot in range(1, 47):
        records.append(_record("compatibility", f"compatibility-slot-{slot:02}", "compatibility", "regression", policy_role="compatibility", conditional="after_holdout_freeze"))
    if len(records) != sum(LANES.values()) or len({record["call_id"] for record in records}) != len(records):
        raise ValueError("derived release plan mismatch")
    if {lane: sum(record["lane"] == lane for record in records) for lane in LANES} != LANES:
        raise ValueError("derived release lanes mismatch")
    return records


def build_plan(path: Path) -> dict[str, Any]:
    plan = _read_json(path, private=False)
    validate_budget(plan)
    records = _derived_records()
    return {"planned_calls": len(records), "hard_cap": plan["hard_cap"], "lanes": plan["lanes"], "records": records}


def _require_call_id(identity: dict[str, Any]) -> None:
    if not isinstance(identity, dict) or not isinstance(identity.get("call_id"), str) or not identity["call_id"].strip():
        raise ValueError("attempt identity must bind a call_id")


def start_attempt(path: Path, identity: dict[str, Any]) -> None:
    _require_call_id(identity)
    _ensure_dir(path, private=True)
    _write_json(path / "started.json", {"schema_version": 1, "status": "started", "identity": identity}, private=True, exclusive=True)


def finish_attempt(path: Path, result: dict[str, Any], status: str = "completed") -> None:
    if status not in {"completed", "failed", "interrupted"} or not _exists(path / "started.json", private=True):
        raise ValueError("invalid attempt finish")
    _write_json(path / "result.json", {"schema_version": 1, "status": status, "result": result}, private=True, exclusive=True)


def load_attempt(path: Path) -> dict[str, Any]:
    started = _read_json(path / "started.json", private=True)
    lib.require_exact_fields(started, {"schema_version", "status", "identity"}, "started attempt")
    if started["schema_version"] != 1 or started["status"] != "started":
        raise ValueError("invalid started attempt")
    _require_call_id(started["identity"])
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


def _load_key(root: Path, purpose: str) -> str:
    value = _read_json(root / "private" / f"{purpose}-key.json", private=True)
    lib.require_exact_fields(value, {"schema_version", "purpose", "key"}, "private key")
    if value["schema_version"] != 1 or value["purpose"] != purpose or not isinstance(value["key"], str) or len(value["key"]) != 64 or any(char not in "0123456789abcdef" for char in value["key"]):
        raise ValueError("invalid private key")
    return value["key"]


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(lib.canonical_json(value).encode()).hexdigest()


def _manifest_value(output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    records = plan["records"]
    return {
        "schema_version": 2,
        "runner": RUNNER_ID,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "output_cases_sha256": _sha256_value(output_cases),
        "activation_cases_sha256": _sha256_value(activation_cases),
        "plan_sha256": _sha256_value(plan),
        "model": DEFAULT_MODEL,
        "effort": DEFAULT_EFFORT,
        "cli": DEFAULT_CLI,
        "policy_roles_sha256": _sha256_value([{ "call_id": record["call_id"], "policy_role": record["policy_role"]} for record in records]),
        "limits_sha256": _sha256_value({"hard_cap": plan["hard_cap"], "lanes": plan["lanes"], "planned_calls": plan["planned_calls"]}),
    }


def _manifest(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    value = _manifest_value(output_cases, activation_cases, plan)
    path = root / "private/manifest.json"
    if _exists(path, private=True):
        if _read_json(path, private=True) != value:
            raise ValueError("manifest mismatch")
    else:
        _write_json(path, value, private=True, exclusive=True)
    return value


def _plan_record(plan: dict[str, Any], lane: str, case_slot: str, treatment: str, description: str) -> dict[str, Any]:
    matches = [record for record in plan["records"] if (record["lane"], record["case_slot"], record["treatment"], record["description"]) == (lane, case_slot, treatment, description)]
    if len(matches) != 1:
        raise ValueError("missing preregistered call identity")
    return matches[0]


def _schedule(output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], schedule_key: str, plan: dict[str, Any]) -> dict[str, Any]:
    calls = []
    sequence = 1
    by_output = {case["id"]: case for case in output_cases}
    for row in lib.balanced_schedule(output_cases, OUTPUT_TREATMENTS, schedule_key):
        record = _plan_record(plan, "dev_output", row["case_id"], row["arm"], "output")
        case = by_output[row["case_id"]]
        calls.append({"call_id": record["call_id"], "sequence": sequence, "kind": "output", "case_id": case["id"], "cluster_id": case["cluster_id"], "arm": row["arm"], "treatment": record["treatment"], "description": record["description"], "ordinal": row["ordinal"], "trial": record["trial"], "model": record["model"], "effort": record["effort"], "cli": record["cli"], "policy_role": record["policy_role"], "run_id": lib.opaque_id("run", schedule_key, {"call_id": record["call_id"]})})
        sequence += 1
    routed = [case for case in activation_cases if case["execution"] == "routed"]
    for row in lib.balanced_schedule(routed, ACTIVATION_DESCRIPTIONS, schedule_key):
        record = _plan_record(plan, "dev_activation", row["case_id"], "activation", row["arm"])
        calls.append({"call_id": record["call_id"], "sequence": sequence, "kind": "activation", "case_id": row["case_id"], "cluster_id": row["case_id"], "arm": row["arm"], "treatment": record["treatment"], "description": record["description"], "ordinal": row["ordinal"], "trial": record["trial"], "model": record["model"], "effort": record["effort"], "cli": record["cli"], "policy_role": record["policy_role"], "run_id": lib.opaque_id("run", schedule_key, {"call_id": record["call_id"]})})
        sequence += 1
    if len(calls) != LANES["dev_output"] + LANES["dev_activation"] or len({call["call_id"] for call in calls}) != len(calls):
        raise ValueError("invalid committed schedule")
    return {"schema_version": 2, "calls": calls}


def _load_or_create_schedule(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], schedule_key: str, plan: dict[str, Any]) -> dict[str, Any]:
    expected = _schedule(output_cases, activation_cases, schedule_key, plan)
    path = root / "private/schedule.json"
    if _exists(path, private=True):
        if _read_json(path, private=True) != expected:
            raise ValueError("committed schedule mismatch")
    else:
        _write_json(path, expected, private=True, exclusive=True)
    return expected


def _fake_response(case: dict[str, Any]) -> tuple[str, str]:
    facts = "; ".join(" / ".join(group) for fact in case["critical_facts"] for group in fact["groups"])
    return "", f"{case['id']} {facts}"


def _fake_activation_response(case: dict[str, Any]) -> tuple[str, str]:
    return "", lib.canonical_json({"activate": case["expected"] == "activate"})


def _answer_payload(identity: dict[str, Any], commentary: str, final: str) -> dict[str, Any]:
    commentary_tokens = len(commentary.split())
    final_tokens = len(final.split())
    return {**identity, "commentary": commentary, "final": final, "commentary_visible_tokens": commentary_tokens, "final_visible_tokens": final_tokens, "visible_output_tokens": commentary_tokens + final_tokens, "input_tokens": 100, "cached_input_tokens": 80, "uncached_input_tokens": 20, "output_tokens": final_tokens, "latency_ms": 1}


ANSWER_METRICS = {"commentary", "final", "commentary_visible_tokens", "final_visible_tokens", "visible_output_tokens", "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "latency_ms"}


def _assert_completed_attempt_inventory(path: Path) -> None:
    if set(_directory_names(path, private=True)) != {"started.json", "raw.jsonl", "result.json"}:
        raise ValueError("attempt artifact inventory mismatch")


def _attempt_state(path: Path, expected_identity: dict[str, Any]) -> dict[str, Any]:
    loaded = load_attempt(path)
    if loaded["identity"] != expected_identity:
        raise ValueError("attempt identity tampered")
    names = set(_directory_names(path, private=True))
    if loaded["status"] == "started":
        if names != {"started.json"}:
            raise ValueError("started attempt artifact inventory mismatch")
    elif loaded["status"] in {"failed", "interrupted"}:
        if names != {"started.json", "result.json"}:
            raise ValueError("consumed attempt artifact inventory mismatch")
    else:
        _assert_completed_attempt_inventory(path)
    return loaded


def _load_answer_attempt(path: Path, expected_identity: dict[str, Any]) -> dict[str, Any]:
    loaded = _attempt_state(path, expected_identity)
    if loaded["status"] != "completed":
        raise ValueError("answer attempt is not completed")
    result = loaded["result"]
    lib.require_exact_fields(result, set(expected_identity) | ANSWER_METRICS | {"raw_sha256"}, "answer result")
    if any(result[key] != value for key, value in expected_identity.items()):
        raise ValueError("answer identity tampered")
    raw_path = path / "raw.jsonl"
    rows = _read_jsonl(raw_path, private=True)
    if len(rows) != 1 or result["raw_sha256"] != _sha(raw_path, private=True):
        raise ValueError("answer raw evidence mismatch")
    payload = {key: value for key, value in result.items() if key != "raw_sha256"}
    if lib.canonical_json(rows[0]) != lib.canonical_json(payload):
        raise ValueError("answer result does not reconstruct from raw")
    if result["visible_output_tokens"] != result["commentary_visible_tokens"] + result["final_visible_tokens"] or result["uncached_input_tokens"] != result["input_tokens"] - result["cached_input_tokens"]:
        raise ValueError("answer metric derivation mismatch")
    return result


def _answer_runs(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], schedule: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    lib.require_exact_fields(schedule, {"schema_version", "calls"}, "schedule")
    if schedule["schema_version"] != 2 or not isinstance(schedule["calls"], list) or len(schedule["calls"]) != 84:
        raise ValueError("invalid schedule")
    case_by_key = {("output", case["id"]): case for case in output_cases}
    case_by_key.update({("activation", case["id"]): case for case in activation_cases})
    expected_ids = {call.get("run_id") for call in schedule["calls"]}
    if len(expected_ids) != len(schedule["calls"]) or not all(isinstance(run_id, str) for run_id in expected_ids):
        raise ValueError("invalid scheduled call")
    attempts = root / "private/attempts"
    _ensure_dir(attempts, private=True)
    existing = set(_directory_names(attempts, private=True, directories_only=True))
    if not existing.issubset(expected_ids):
        raise ValueError("unexpected answer attempt inventory")
    runs: list[dict[str, Any]] = []
    spent: list[str] = []
    for call in schedule["calls"]:
        identity = dict(call)
        attempt = attempts / call["run_id"]
        if _exists(attempt, private=True):
            state = _attempt_state(attempt, identity)
            if state["status"] == "completed":
                runs.append(_load_answer_attempt(attempt, identity))
            else:
                spent.append(identity["call_id"])
            continue
        case = case_by_key.get((call["kind"], call["case_id"]))
        if not case:
            raise ValueError("unknown scheduled case")
        start_attempt(attempt, identity)
        commentary, final = _fake_response(case) if call["kind"] == "output" else _fake_activation_response(case)
        payload = _answer_payload(identity, commentary, final)
        raw = attempt / "raw.jsonl"
        _write_jsonl(raw, [payload], private=True, exclusive=True)
        finish_attempt(attempt, {**payload, "raw_sha256": _sha(raw, private=True)})
        runs.append(payload)
    return runs, sorted(spent)


def _load_runs(root: Path, schedule: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = root / "private/attempts"
    expected = {call["run_id"] for call in schedule["calls"]}
    if set(_directory_names(attempts, private=True, directories_only=True)) != expected:
        raise ValueError("answer artifact inventory mismatch")
    return [_load_answer_attempt(attempts / call["run_id"], dict(call)) for call in schedule["calls"]]


def _public_scan(root: Path, artifacts: list[dict[str, Any]], runs: list[dict[str, Any]]) -> None:
    private_ids = {str(run[key]) for run in runs for key in ("run_id", "call_id") if key in run}
    lib.assert_public_safe({"artifacts": artifacts}, arm_aliases=set(OUTPUT_TREATMENTS) | set(ACTIVATION_DESCRIPTIONS), private_ids=private_ids, protected_roots={ROOT, CASES, root, root / "private", Path.home(), Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))})


def _assert_public_inventory(root: Path, allowed: set[str], runs: list[dict[str, Any]]) -> None:
    public = root / "public"
    names = set(_directory_names(public, private=False))
    if names != allowed:
        raise ValueError("public artifact inventory mismatch")
    _public_scan(root, [_read_json(public / name, private=False) for name in sorted(allowed)], runs)


def _mapping_and_bundle(root: Path, output_cases: list[dict[str, Any]], runs: list[dict[str, Any]], schedule_key: str, mapping_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    output_runs = [run for run in runs if run["kind"] == "output"]
    expected_mapping = lib.build_private_mapping(output_cases, output_runs, schedule_key)
    mapping_path = root / "private/mapping.json"
    if _exists(mapping_path, private=True):
        mapping = _read_json(mapping_path, private=True)
        if mapping != expected_mapping:
            raise ValueError("mapping does not match committed schedule and runs")
    else:
        _write_json(mapping_path, expected_mapping, private=True, exclusive=True)
        mapping = expected_mapping
    commitment = lib.mapping_commitment(mapping, mapping_key)
    bundle = lib.build_public_bundle(output_cases, output_runs, mapping, commitment=commitment, protected_roots={ROOT, CASES, root, root / "private", Path.home()})
    bundle_path = root / "public/bundle.json"
    if _exists(bundle_path, private=False):
        if _read_json(bundle_path, private=False) != bundle:
            raise ValueError("frozen public bundle mismatch")
    else:
        _assert_public_inventory(root, set(), runs)
        _write_json(bundle_path, bundle, private=False, exclusive=True)
    _assert_public_inventory(root, {"bundle.json"}, runs)
    return mapping, bundle


def _bundle_sha256(bundle: dict[str, Any]) -> str:
    return _sha256_value(bundle)


def _load_chain(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any], *, public_allowed: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any], str, str]:
    if _read_json(root / "private/manifest.json", private=True) != _manifest_value(output_cases, activation_cases, plan):
        raise ValueError("manifest does not reconstruct from fixed identities")
    schedule_key = _load_key(root, "schedule")
    mapping_key = _load_key(root, "mapping")
    seal_key = _load_key(root, "seal")
    schedule = _read_json(root / "private/schedule.json", private=True)
    expected_schedule = _schedule(output_cases, activation_cases, schedule_key, plan)
    if schedule != expected_schedule:
        raise ValueError("schedule does not reconstruct from fixed identities")
    runs = _load_runs(root, expected_schedule)
    mapping = _read_json(root / "private/mapping.json", private=True)
    expected_mapping = lib.build_private_mapping(output_cases, [run for run in runs if run["kind"] == "output"], schedule_key)
    if mapping != expected_mapping:
        raise ValueError("mapping does not reconstruct from fixed identities")
    bundle = lib.build_public_bundle(output_cases, [run for run in runs if run["kind"] == "output"], mapping, commitment=lib.mapping_commitment(mapping, mapping_key), protected_roots={ROOT, CASES, root, root / "private", Path.home()})
    if _read_json(root / "public/bundle.json", private=False) != bundle:
        raise ValueError("frozen public bundle mismatch")
    _assert_public_inventory(root, public_allowed, runs)
    return expected_schedule, runs, mapping, bundle, mapping_key, seal_key


def _fake_judgment() -> dict[str, Any]:
    return lib.validate_judgment('{"quality":"tie","naturalness":"tie","flags":{"left":[],"right":[]},"rationale":"same offline fake answer"}')


def _judge_records(plan: dict[str, Any]) -> list[dict[str, Any]]:
    records = [record for record in plan["records"] if record["lane"] == "blind_judge_and_tiebreak"]
    if len(records) != LANES["blind_judge_and_tiebreak"]:
        raise ValueError("invalid judge plan")
    return records


def _judge_identity(pair: dict[str, Any], bundle_sha256: str, plan: dict[str, Any], pairs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if pairs is None:
        pairs = [pair]
    ordered = sorted(pairs, key=lambda item: item["pair_id"])
    matching = [index for index, item in enumerate(ordered) if item["pair_id"] == pair["pair_id"]]
    if len(matching) != 1:
        raise ValueError("unknown judge pair")
    record = _judge_records(plan)[matching[0]]
    return {"call_id": record["call_id"], "pair_id": pair["pair_id"], "bundle_sha256": bundle_sha256, "model": record["model"], "effort": record["effort"], "cli": record["cli"], "policy_role": record["policy_role"]}


def _judge_manifest_value(bundle: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    bundle_sha256 = _bundle_sha256(bundle)
    identities = [_judge_identity(pair, bundle_sha256, plan, bundle["pairs"]) for pair in bundle["pairs"]]
    return {"schema_version": 2, "judge": "strict_fake_v3", "runner": RUNNER_ID, "bundle_sha256": bundle_sha256, "model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT, "cli": DEFAULT_CLI, "policy_role": "blind_judge", "call_ids": [identity["call_id"] for identity in identities]}


def _load_judgment_attempt(path: Path, identity: dict[str, Any]) -> dict[str, Any]:
    loaded = _attempt_state(path, identity)
    if loaded["status"] != "completed":
        raise ValueError("judge attempt is not completed")
    result = loaded["result"]
    lib.require_exact_fields(result, {"pair_id", "judgment", "raw_sha256"}, "judge result")
    if result["pair_id"] != identity["pair_id"]:
        raise ValueError("judge identity tampered")
    lib.validate_judgment(result["judgment"])
    raw = path / "raw.jsonl"
    rows = _read_jsonl(raw, private=True)
    if len(rows) != 1 or result["raw_sha256"] != _sha(raw, private=True):
        raise ValueError("judge raw evidence mismatch")
    payload = {"pair_id": result["pair_id"], "judgment": result["judgment"]}
    if lib.canonical_json(rows[0]) != lib.canonical_json(payload):
        raise ValueError("judge result does not reconstruct from raw")
    return payload


def _run_or_load_judges(root: Path, bundle: dict[str, Any], plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    bundle_sha256 = _bundle_sha256(bundle)
    expected = {pair["pair_id"] for pair in bundle["pairs"]}
    directory = root / "private/judge-attempts"
    _ensure_dir(directory, private=True)
    names = set(_directory_names(directory, private=True, directories_only=True))
    if not names.issubset(expected):
        raise ValueError("unexpected judge attempt inventory")
    manifest = root / "private/judge-manifest.json"
    expected_manifest = _judge_manifest_value(bundle, plan)
    if _exists(manifest, private=True):
        if _read_json(manifest, private=True) != expected_manifest:
            raise ValueError("judge manifest mismatch")
    else:
        _write_json(manifest, expected_manifest, private=True, exclusive=True)
    rows = []
    spent = []
    for pair in bundle["pairs"]:
        identity = _judge_identity(pair, bundle_sha256, plan, bundle["pairs"])
        path = directory / pair["pair_id"]
        if _exists(path, private=True):
            state = _attempt_state(path, identity)
            if state["status"] == "completed":
                rows.append(_load_judgment_attempt(path, identity))
            else:
                spent.append(identity["call_id"])
            continue
        start_attempt(path, identity)
        left = pair["left"]["commentary"] + "\n" + pair["left"]["final"]
        right = pair["right"]["commentary"] + "\n" + pair["right"]["final"]
        lib.build_judge_payload({"prompt": pair["prompt"], "verified_context": pair["verified_context"], "deliverable": "final"}, left, right)
        payload = {"pair_id": pair["pair_id"], "judgment": _fake_judgment()}
        raw = path / "raw.jsonl"
        _write_jsonl(raw, [payload], private=True, exclusive=True)
        finish_attempt(path, {**payload, "raw_sha256": _sha(raw, private=True)})
        rows.append(payload)
    aggregate = root / "private/judgments.jsonl"
    if spent:
        if _exists(aggregate, private=True):
            raise ValueError("incomplete judge chain has judgment aggregate")
        return rows, sorted(spent)
    if _exists(aggregate, private=True):
        if _read_jsonl(aggregate, private=True) != rows:
            raise ValueError("judgment aggregate mismatch")
    else:
        _write_jsonl(aggregate, rows, private=True, exclusive=True)
    return rows, []


def _load_judgments(root: Path, bundle: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    expected = {pair["pair_id"] for pair in bundle["pairs"]}
    directory = root / "private/judge-attempts"
    if set(_directory_names(directory, private=True, directories_only=True)) != expected:
        raise ValueError("judge artifact inventory mismatch")
    bundle_sha256 = _bundle_sha256(bundle)
    rows = [_load_judgment_attempt(directory / pair["pair_id"], _judge_identity(pair, bundle_sha256, plan, bundle["pairs"])) for pair in bundle["pairs"]]
    if _read_jsonl(root / "private/judgments.jsonl", private=True) != rows:
        raise ValueError("judgment aggregate mismatch")
    if _read_json(root / "private/judge-manifest.json", private=True) != _judge_manifest_value(bundle, plan):
        raise ValueError("judge manifest does not reconstruct from fixed identities")
    return rows


def _assert_private_inventory(root: Path, *, revealed: bool = False) -> None:
    allowed = {"manifest.json", "mapping-key.json", "seal-key.json", "schedule-key.json", "schedule.json", "mapping.json", "attempts", "judge-attempts", "judgments.jsonl", "judge-manifest.json"}
    if revealed:
        allowed.add("revealed.json")
    if not set(_directory_names(root / "private", private=True)).issubset(allowed):
        raise ValueError("unexpected private artifact")


def _config_sha256() -> str:
    return _sha256_value({"schema_version": 2, "runner": RUNNER_ID, "judge": "strict_fake_v3"})


def _seal(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    _assert_private_inventory(root)
    _, runs, mapping, bundle, mapping_key, seal_key = _load_chain(root, output_cases, activation_cases, plan, public_allowed={"bundle.json"})
    judgments = _load_judgments(root, bundle, plan)
    required = {"manifest.json", "mapping-key.json", "seal-key.json", "schedule-key.json", "schedule.json", "mapping.json", "attempts", "judge-attempts", "judgments.jsonl", "judge-manifest.json"}
    if set(_directory_names(root / "private", private=True)) != required:
        raise ValueError("incomplete private artifact inventory")
    payload = {"config_sha256": _config_sha256(), "manifest_sha256": _sha(root / "private/manifest.json", private=True), "schedule_sha256": _sha(root / "private/schedule.json", private=True), "bundle_sha256": _sha(root / "public/bundle.json", private=False), "mapping_commitment": lib.mapping_commitment(mapping, mapping_key), "judgments_sha256": _sha(root / "private/judgments.jsonl", private=True), "judge_manifest_sha256": _sha(root / "private/judge-manifest.json", private=True)}
    _write_json(root / "public/seal.json", lib.build_seal(payload, seal_key), private=False, exclusive=True)
    _assert_public_inventory(root, {"bundle.json", "seal.json"}, runs)
    return {"status": "sealed", "judgments": len(judgments)}


def _reveal(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    _assert_private_inventory(root, revealed=True)
    _, runs, mapping, bundle, mapping_key, seal_key = _load_chain(root, output_cases, activation_cases, plan, public_allowed={"bundle.json", "seal.json"})
    judgments = _load_judgments(root, bundle, plan)
    seal = _read_json(root / "public/seal.json", private=False)
    result = lib.reveal(mapping, seal, mapping_key=mapping_key, seal_key=seal_key, config_sha256=_config_sha256(), manifest_sha256=_sha(root / "private/manifest.json", private=True), schedule_sha256=_sha(root / "private/schedule.json", private=True), bundle_sha256=_sha(root / "public/bundle.json", private=False), judgments_sha256=_sha(root / "private/judgments.jsonl", private=True), judge_manifest_sha256=_sha(root / "private/judge-manifest.json", private=True), judgments=judgments)
    path = root / "private/revealed.json"
    if _exists(path, private=True):
        if _read_json(path, private=True) != result:
            raise ValueError("revealed result mismatch")
    else:
        _write_json(path, result, private=True, exclusive=True)
    _assert_public_inventory(root, {"bundle.json", "seal.json"}, runs)
    return result


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run")
    dry.add_argument("--plan", type=Path, required=True)
    for name in ("answers", "judge", "seal", "reveal"):
        command = sub.add_parser(name)
        command.add_argument("--root", required=True)
        if name in {"answers", "judge"}:
            command.add_argument("--fake", action="store_true")
        if name == "answers":
            command.add_argument("--secret", required=True)
    args = parser.parse_args(argv)
    if args.command == "dry-run":
        return build_plan(args.plan)
    plan = build_plan(ROOT / "evals/release-plan.json")
    output_cases = lib.load_output_cases(CASES / "output-dev.jsonl")
    activation_cases = lib.load_activation_cases(CASES / "activation-dev.jsonl")
    root = _root(args.root)
    _prepare_root(root)
    if args.command in {"answers", "judge", "seal"} and _exists(root / "public/seal.json", private=False):
        raise ValueError("seal is terminal")
    if args.command == "answers":
        if not args.fake:
            raise ValueError("live calls are disabled in Phase A")
        _assert_private_inventory(root)
        _manifest(root, output_cases, activation_cases, plan)
        mapping_key = _load_or_create_key(root, "mapping", args.secret)
        _load_or_create_key(root, "seal", args.secret)
        schedule_key = _load_or_create_key(root, "schedule", args.secret)
        schedule = _load_or_create_schedule(root, output_cases, activation_cases, schedule_key, plan)
        runs, spent = _answer_runs(root, output_cases, activation_cases, schedule)
        if spent:
            if _exists(root / "private/mapping.json", private=True) or _exists(root / "public/bundle.json", private=False):
                raise ValueError("incomplete answer chain has frozen artifacts")
            return {"status": "incomplete", "unsealable": True, "runs": len(runs), "spent_call_ids": spent}
        _mapping_and_bundle(root, output_cases, runs, schedule_key, mapping_key)
        return {"status": "answered", "runs": len(runs)}
    if args.command == "reveal" and not _exists(root / "public/seal.json", private=False):
        raise ValueError("reveal requires seal")
    _assert_private_inventory(root, revealed=args.command == "reveal")
    _, runs, _, bundle, _, _ = _load_chain(root, output_cases, activation_cases, plan, public_allowed={"bundle.json", "seal.json"} if args.command == "reveal" else {"bundle.json"})
    if args.command == "judge":
        if not args.fake:
            raise ValueError("live calls are disabled in Phase A")
        _, spent = _run_or_load_judges(root, bundle, plan)
        if spent:
            return {"status": "incomplete", "unsealable": True, "spent_call_ids": spent}
        return {"status": "judged", "judgments": len(bundle["pairs"])}
    if args.command == "seal":
        return _seal(root, output_cases, activation_cases, plan)
    return _reveal(root, output_cases, activation_cases, plan)


if __name__ == "__main__":
    print(lib.canonical_json(main()))
