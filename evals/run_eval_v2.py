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
RUNNER_ID = "eval_v2_v4"
FAKE_EXECUTOR_ID = "offline-fake-v4"
JUDGE_CONTRACT_ID = "strict-json-blind-v1"
FAKE_JUDGE_ID = "offline-fake-judge-v4"
OUTPUT_TREATMENTS = ("A", "B", "C", "generic")
ACTIVATION_DESCRIPTIONS = ("D0", "D1")
OWNER_FILE = ".eval-v2-owner.json"
OWNER = {"schema_version": 1, "owner": "simple-man-eval-v2"}
MANIFEST_SOURCE_ROOT = ROOT

PLAN_FIELDS = {
    "schema_version", "hard_cap", "lanes", "planned_calls", "execution_contract",
    "arm_policies", "description_policies", "comparison_contract", "scoring_contract",
    "bootstrap_contract",
}
POLICY_CONFIG_FIELDS = {"state", "source", "offline_only"}
COMPARISON_FIELDS = {
    "comparison_id", "baseline_arm", "candidate_arm", "case_set", "max_pairs",
    "max_judge_calls", "judge_call_slots", "condition",
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
    lib.require_exact_fields(plan, PLAN_FIELDS, "release plan")
    if plan["schema_version"] != 2 or type(plan["hard_cap"]) is not int or type(plan["planned_calls"]) is not int or plan["lanes"] != LANES:
        raise ValueError("release lanes must match preregistered contract")
    if plan["planned_calls"] != sum(LANES.values()) or plan["hard_cap"] != 280 or plan["planned_calls"] > plan["hard_cap"]:
        raise ValueError("release budget exceeded")

    execution = lib.require_exact_fields(
        plan["execution_contract"], {"cli", "lanes", "unavailable_or_substituted"},
        "execution contract",
    )
    if execution["cli"] != "codex-cli 0.145.0" or execution["unavailable_or_substituted"] != "INCONCLUSIVE" or set(execution["lanes"]) != set(LANES):
        raise ValueError("invalid execution contract")
    expected_execution = {
        **{lane: ("gpt-5.6-sol", "high") for lane in LANES},
        "blind_judge_and_tiebreak": ("gpt-5.6-terra", "medium"),
        "primary_coding": ("gpt-5.6-sol", "xhigh"),
        "compatibility": ("gpt-5.5", "high"),
    }
    for lane, (model, effort) in expected_execution.items():
        if execution["lanes"].get(lane) != {"model": model, "effort": effort}:
            raise ValueError("invalid lane execution identity")

    for name, expected_names in (("arm_policies", set(OUTPUT_TREATMENTS)), ("description_policies", set(ACTIVATION_DESCRIPTIONS))):
        policies = plan[name]
        if not isinstance(policies, dict) or set(policies) != expected_names:
            raise ValueError(f"invalid {name}")
        for policy in policies.values():
            lib.require_exact_fields(policy, POLICY_CONFIG_FIELDS, "policy config")
            source = policy["source"]
            if not isinstance(policy["state"], str) or not policy["state"] or type(policy["offline_only"]) is not bool or (source is not None and (not isinstance(source, str) or not source)):
                raise ValueError("invalid policy config")
            if (source is None) != policy["offline_only"]:
                raise ValueError("unfrozen policy must be offline-only")

    comparison = lib.require_exact_fields(plan["comparison_contract"], {"judge_cap", "comparisons"}, "comparison contract")
    if comparison["judge_cap"] != LANES["blind_judge_and_tiebreak"] or not isinstance(comparison["comparisons"], list):
        raise ValueError("invalid comparison cap")
    slots: list[int] = []
    seen: set[str] = set()
    for item in comparison["comparisons"]:
        lib.require_exact_fields(item, COMPARISON_FIELDS, "comparison config")
        if not isinstance(item["comparison_id"], str) or not item["comparison_id"] or item["comparison_id"] in seen or item["baseline_arm"] == item["candidate_arm"]:
            raise ValueError("invalid comparison identity")
        if type(item["max_pairs"]) is not int or item["max_pairs"] < 1 or type(item["max_judge_calls"]) is not int or not 0 <= item["max_judge_calls"] <= item["max_pairs"]:
            raise ValueError("invalid comparison allocation")
        disposition = item["judge_call_slots"]
        if item["max_judge_calls"] == 0:
            if disposition is not None:
                raise ValueError("non-judged comparison has judge slots")
        else:
            if not isinstance(disposition, list) or len(disposition) != 2 or any(type(value) is not int for value in disposition):
                raise ValueError("invalid judge slot range")
            start, end = disposition
            if end - start + 1 != item["max_judge_calls"]:
                raise ValueError("judge slot range does not match allocation")
            slots.extend(range(start, end + 1))
        seen.add(item["comparison_id"])
    if slots != list(range(1, comparison["judge_cap"] + 1)):
        raise ValueError("judge call slots must exactly cover the cap")

    lib.require_exact_fields(plan["scoring_contract"], {"quality_order", "deterministic_checks", "judge_schema", "visible_tokenizer", "tokenizer_version"}, "scoring contract")
    lib.require_exact_fields(plan["bootstrap_contract"], {"method", "cluster_field", "confidence", "iterations"}, "bootstrap contract")


def _record(execution: dict[str, Any], lane: str, case_slot: str, treatment: str, description: str, *, policy_role: str, conditional: str, trial: int = 1) -> dict[str, Any]:
    identity = execution["lanes"][lane]
    base = {"lane": lane, "case_slot": case_slot, "treatment": treatment, "description": description, "trial": trial, "model": identity["model"], "effort": identity["effort"], "cli": execution["cli"], "policy_role": policy_role, "conditional": conditional}
    return {"call_id": f"call_{hashlib.sha256(lib.canonical_json(base).encode()).hexdigest()[:24]}", **base}


def _derived_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    output = lib.load_output_cases(CASES / "output-dev.jsonl")
    activation = lib.load_activation_cases(CASES / "activation-dev.jsonl")
    execution = config["execution_contract"]
    records = []
    for case in output:
        for treatment in OUTPUT_TREATMENTS:
            records.append(_record(execution, "dev_output", case["id"], treatment, "output", policy_role=f"dev_output_{treatment}", conditional="dev"))
    for case in activation:
        if case["execution"] == "routed":
            for description in ACTIVATION_DESCRIPTIONS:
                records.append(_record(execution, "dev_activation", case["id"], "activation", description, policy_role="activation_decider", conditional="dev"))
    for slot in range(1, 21):
        for description in ACTIVATION_DESCRIPTIONS:
            records.append(_record(execution, "primary_activation_holdout", f"activation-holdout-slot-{slot:02}", "activation", description, policy_role="activation_decider", conditional="after_holdout_freeze"))
    for slot in range(1, 16):
        for treatment in OUTPUT_TREATMENTS:
            records.append(_record(execution, "primary_output_holdout", f"output-holdout-slot-{slot:02}", treatment, "output", policy_role=f"holdout_output_{treatment}", conditional="after_holdout_freeze"))
    for slot in range(1, 9):
        records.append(_record(execution, "full_skill_parity", f"parity-slot-{slot:02}", "parity", "packaged-skill", policy_role="parity", conditional="after_holdout_freeze"))
    for slot in range(1, 29):
        allocation = next(item for item in config["comparison_contract"]["comparisons"] if item["judge_call_slots"] and item["judge_call_slots"][0] <= slot <= item["judge_call_slots"][1])
        records.append(_record(execution, "blind_judge_and_tiebreak", f"{allocation['comparison_id']}-slot-{slot:02}", "judge", "strict-json", policy_role="blind_judge", conditional=allocation["condition"]))
    for slot in range(1, 10):
        records.append(_record(execution, "primary_coding", f"coding-slot-{slot:02}", "coding", "hidden-validator", policy_role="coding", conditional="after_holdout_freeze"))
    for slot in range(1, 47):
        records.append(_record(execution, "compatibility", f"compatibility-slot-{slot:02}", "compatibility", "regression", policy_role="compatibility", conditional="after_holdout_freeze"))
    if len(records) != sum(LANES.values()) or len({record["call_id"] for record in records}) != len(records):
        raise ValueError("derived release plan mismatch")
    if {lane: sum(record["lane"] == lane for record in records) for lane in LANES} != LANES:
        raise ValueError("derived release lanes mismatch")
    return records


def build_plan(path: Path) -> dict[str, Any]:
    config = _read_json(path, private=False)
    validate_budget(config)
    records = _derived_records(config)
    return {"planned_calls": len(records), "hard_cap": config["hard_cap"], "lanes": config["lanes"], "records": records, "execution_contract": config["execution_contract"], "arm_policies": config["arm_policies"], "description_policies": config["description_policies"], "comparison_contract": config["comparison_contract"], "scoring_contract": config["scoring_contract"], "bootstrap_contract": config["bootstrap_contract"]}


def execution_identity_status(record: dict[str, Any], *, actual_model: str | None, actual_effort: str | None, actual_cli: str | None) -> dict[str, Any]:
    expected = {key: record.get(key) for key in ("model", "effort", "cli")}
    actual = {"model": actual_model, "effort": actual_effort, "cli": actual_cli}
    if actual == expected:
        return {"status": "READY", "identity": expected}
    return {"status": "INCONCLUSIVE", "reason": "required execution identity unavailable or substituted", "expected": expected, "actual": actual}


def live_execution_status(
    plan: dict[str, Any],
    record: dict[str, Any],
    *,
    actual_model: str | None,
    actual_effort: str | None,
    arm_policy_sha256: dict[str, str],
    description_policy_sha256: dict[str, str],
    evaluated_head_sha: str,
    evaluated_tree_sha: str,
) -> dict[str, Any]:
    registered = [item for item in plan.get("records", []) if item.get("call_id") == record.get("call_id")]
    if len(registered) != 1 or registered[0] != record:
        return {"status": "INCONCLUSIVE", "reason": "call record is not exactly preregistered"}
    if actual_model != record["model"] or actual_effort != record["effort"]:
        return {"status": "INCONCLUSIVE", "reason": "required execution identity unavailable or substituted"}
    for configs in (plan["arm_policies"], plan["description_policies"]):
        if any(policy["offline_only"] or policy["source"] is None for policy in configs.values()):
            return {"status": "INCONCLUSIVE", "reason": "live policy or description is not frozen"}
    actual_cli = _probe_cli_identity()
    identity = execution_identity_status(
        record,
        actual_model=actual_model,
        actual_effort=actual_effort,
        actual_cli=actual_cli,
    )
    if identity["status"] != "READY":
        return identity
    source_hashes = _source_hashes(plan)
    expected_hashes = (
        (arm_policy_sha256, _policy_hashes(plan["arm_policies"], source_hashes)),
        (description_policy_sha256, _policy_hashes(plan["description_policies"], source_hashes)),
    )
    for supplied, expected in expected_hashes:
        if supplied != expected:
            return {"status": "INCONCLUSIVE", "reason": "live policy or description hash is unavailable"}
    git_state = _live_git_state()
    actual_head_sha = git_state["head_sha"]
    actual_tree_sha = git_state["tree_sha"]
    valid_git = all(
        isinstance(value, str) and len(value) in {40, 64} and not any(char not in "0123456789abcdef" for char in value)
        for value in (actual_head_sha, actual_tree_sha, evaluated_head_sha, evaluated_tree_sha)
    )
    if not valid_git or not git_state["clean"] or actual_head_sha != evaluated_head_sha or actual_tree_sha != evaluated_tree_sha:
        return {"status": "INCONCLUSIVE", "reason": "evaluated HEAD is dirty or changed"}
    return {"status": "READY", "identity": identity["identity"], "evaluated_head_sha": evaluated_head_sha, "evaluated_tree_sha": evaluated_tree_sha}


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


def _git_identity() -> dict[str, str]:
    values = {}
    for name, revision in (("head_sha", "HEAD"), ("tree_sha", "HEAD^{tree}")):
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", revision],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        value = completed.stdout.strip()
        if completed.returncode != 0 or not value or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("evaluated git identity unavailable")
        values[name] = value
    return values


def _probe_cli_identity() -> str | None:
    try:
        completed = subprocess.run(
            ["codex", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _live_git_state() -> dict[str, Any]:
    identity = _git_identity()
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=all"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError("evaluated git worktree status unavailable")
    return {**identity, "clean": not completed.stdout.strip()}


def _manifest_relative_paths(plan: dict[str, Any] | None = None) -> list[str]:
    root = MANIFEST_SOURCE_ROOT
    fixed = {
        "AGENTS.md", "AGENTS.md.snippet", "CLAUDE.md", "GEMINI.md",
        "skills/simple-man/SKILL.md",
        "evals/release-plan.json", "evals/schemas/holdout.schema.json",
        "evals/cases/output-dev.jsonl", "evals/cases/activation-dev.jsonl",
        "evals/prompts/coding_tasks.jsonl", "evals/prompts/reference_compression.jsonl",
        "tests/test_eval_v2.py", "tests/test_eval_v2_gates.py", "tests/test_coding_gate.py",
    }
    if plan:
        for configs in (plan["arm_policies"], plan["description_policies"]):
            for policy in configs.values():
                source = policy["source"]
                if source is None:
                    continue
                relative = Path(source)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("policy source must stay inside evaluated source")
                fixed.add(relative.as_posix())
    fixed.update(path.relative_to(root).as_posix() for path in (root / "evals").glob("*.py"))
    for directory in (
        "evals/coding_workers", "evals/fixtures/skill-comparison", "evals/policies",
        "evals/hidden_validators", "plugins/simple-man",
    ):
        base = root / directory
        if base.exists():
            fixed.update(
                path.relative_to(root).as_posix()
                for path in base.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
            )
    return sorted(fixed)


def _source_hashes(plan: dict[str, Any]) -> dict[str, str]:
    hashes = {}
    for relative in _manifest_relative_paths(plan):
        path = MANIFEST_SOURCE_ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing or unsafe manifest source: {relative}")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _policy_hashes(config: dict[str, Any], source_hashes: dict[str, str]) -> dict[str, str]:
    result = {}
    for name, policy in sorted(config.items()):
        source = policy["source"]
        if source is None:
            if not policy["offline_only"]:
                raise ValueError("live policy hash is missing")
            result[name] = _sha256_value({"offline_fake_placeholder": True, "name": name, **policy})
        else:
            if policy["offline_only"] or source not in source_hashes:
                raise ValueError("policy source is not bound by manifest")
            result[name] = source_hashes[source]
    return result


def _manifest_value(output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any], *, release_eligible: bool = False) -> dict[str, Any]:
    records = plan["records"]
    source_hashes = _source_hashes(plan)
    git_state = _live_git_state()
    if release_eligible and not git_state["clean"]:
        raise ValueError("release manifest requires a clean evaluated HEAD")
    return {
        "schema_version": 3,
        "runner": RUNNER_ID,
        "execution_mode": "live_release" if release_eligible else "offline_fake",
        "release_eligible": release_eligible,
        "fake_executor": FAKE_EXECUTOR_ID,
        "evaluated_git": {"head_sha": git_state["head_sha"], "tree_sha": git_state["tree_sha"]},
        "worktree_clean": git_state["clean"],
        "source_sha256": source_hashes,
        "arm_policy_sha256": _policy_hashes(plan["arm_policies"], source_hashes),
        "description_policy_sha256": _policy_hashes(plan["description_policies"], source_hashes),
        "output_cases_sha256": _sha256_value(output_cases),
        "activation_cases_sha256": _sha256_value(activation_cases),
        "plan_sha256": _sha256_value(plan),
        "execution_contract_sha256": _sha256_value(plan["execution_contract"]),
        "comparison_contract_sha256": _sha256_value(plan["comparison_contract"]),
        "scoring_contract_sha256": _sha256_value(plan["scoring_contract"]),
        "bootstrap_contract_sha256": _sha256_value(plan["bootstrap_contract"]),
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
        calls.append({"call_id": record["call_id"], "sequence": sequence, "kind": "output", "case_id": case["id"], "cluster_id": case["cluster_id"], "arm": row["arm"], "treatment": record["treatment"], "description": record["description"], "ordinal": row["ordinal"], "trial": record["trial"], "model": record["model"], "effort": record["effort"], "cli": record["cli"], "executor": FAKE_EXECUTOR_ID, "policy_role": record["policy_role"], "run_id": lib.opaque_id("run", schedule_key, {"call_id": record["call_id"]})})
        sequence += 1
    routed = [case for case in activation_cases if case["execution"] == "routed"]
    for row in lib.balanced_schedule(routed, ACTIVATION_DESCRIPTIONS, schedule_key):
        record = _plan_record(plan, "dev_activation", row["case_id"], "activation", row["arm"])
        calls.append({"call_id": record["call_id"], "sequence": sequence, "kind": "activation", "case_id": row["case_id"], "cluster_id": row["case_id"], "arm": row["arm"], "treatment": record["treatment"], "description": record["description"], "ordinal": row["ordinal"], "trial": record["trial"], "model": record["model"], "effort": record["effort"], "cli": record["cli"], "executor": FAKE_EXECUTOR_ID, "policy_role": record["policy_role"], "run_id": lib.opaque_id("run", schedule_key, {"call_id": record["call_id"]})})
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
ANSWER_COMMITMENT = "answer-commitment.json"
JUDGMENT_COMMITMENT = "judgment-commitment.json"


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


def _completed_attempt_hashes(directory: Path, expected_ids: set[str]) -> list[dict[str, Any]]:
    if set(_directory_names(directory, private=True, directories_only=True)) != expected_ids:
        raise ValueError("committed attempt inventory mismatch")
    rows = []
    for attempt_id in sorted(expected_ids):
        path = directory / attempt_id
        names = _directory_names(path, private=True)
        if names != ["raw.jsonl", "result.json", "started.json"]:
            raise ValueError("committed attempt file inventory mismatch")
        rows.append({"attempt_id": attempt_id, "files": {name: _sha(path / name, private=True) for name in names}})
    return rows


def _signed_commitment(kind: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    body = {"schema_version": 1, "kind": kind, "payload": payload}
    return {**body, "hmac": lib.hmac_digest(key, body)}


def _freeze_or_verify_commitment(path: Path, expected: dict[str, Any]) -> None:
    if _exists(path, private=True):
        if _read_json(path, private=True) != expected:
            raise ValueError("runner commitment mismatch")
    else:
        _write_json(path, expected, private=True, exclusive=True)


def _verify_commitment(path: Path, expected: dict[str, Any]) -> None:
    if not _exists(path, private=True) or _read_json(path, private=True) != expected:
        raise ValueError("runner commitment mismatch")


def _answer_commitment_value(root: Path, schedule: dict[str, Any], key: str) -> dict[str, Any]:
    expected_ids = {call["run_id"] for call in schedule["calls"]}
    payload = {
        "schedule_sha256": _sha(root / "private/schedule.json", private=True),
        "attempts": _completed_attempt_hashes(root / "private/attempts", expected_ids),
    }
    return _signed_commitment("answers", payload, key)


def _freeze_or_verify_answer_commitment(root: Path, schedule: dict[str, Any], key: str) -> None:
    _freeze_or_verify_commitment(root / "private" / ANSWER_COMMITMENT, _answer_commitment_value(root, schedule, key))


def _verify_answer_commitment(root: Path, schedule: dict[str, Any], key: str) -> None:
    _verify_commitment(root / "private" / ANSWER_COMMITMENT, _answer_commitment_value(root, schedule, key))


def _public_scan(root: Path, artifacts: list[dict[str, Any]], runs: list[dict[str, Any]]) -> None:
    private_ids = {str(run[key]) for run in runs for key in ("run_id", "call_id") if key in run}
    lib.assert_public_safe({"artifacts": artifacts}, arm_aliases=set(OUTPUT_TREATMENTS) | set(ACTIVATION_DESCRIPTIONS), private_ids=private_ids, protected_roots={ROOT, CASES, root, root / "private", Path.home(), Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))})


def _assert_public_inventory(root: Path, allowed: set[str], runs: list[dict[str, Any]]) -> None:
    public = root / "public"
    names = set(_directory_names(public, private=False))
    if names != allowed:
        raise ValueError("public artifact inventory mismatch")
    _public_scan(root, [_read_json(public / name, private=False) for name in sorted(allowed)], runs)


def _dev_comparisons(output_cases: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, Any]]:
    matches = [item for item in plan["comparison_contract"]["comparisons"] if item["comparison_id"] == "dev-naturalness-b-c"]
    if len(matches) != 1:
        raise ValueError("missing preregistered dev comparison")
    item = matches[0]
    case_ids = [case["id"] for case in output_cases]
    if item["case_set"] != "output-dev" or item["max_pairs"] != len(case_ids) or item["max_judge_calls"] != len(case_ids):
        raise ValueError("dev comparison does not match corpus")
    return [{
        "comparison_id": item["comparison_id"],
        "baseline_arm": item["baseline_arm"],
        "candidate_arm": item["candidate_arm"],
        "run_selectors": [
            {
                "case_id": case_id,
                "trial": 1,
                "model": plan["execution_contract"]["lanes"]["dev_output"]["model"],
                "effort": plan["execution_contract"]["lanes"]["dev_output"]["effort"],
                "cli": plan["execution_contract"]["cli"],
            }
            for case_id in case_ids
        ],
    }]


def _mapping_and_bundle(root: Path, output_cases: list[dict[str, Any]], runs: list[dict[str, Any]], schedule_key: str, mapping_key: str, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    output_runs = [run for run in runs if run["kind"] == "output"]
    expected_mapping = lib.build_private_mapping(output_cases, output_runs, schedule_key, comparisons=_dev_comparisons(output_cases, plan))
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
    _verify_answer_commitment(root, expected_schedule, seal_key)
    mapping = _read_json(root / "private/mapping.json", private=True)
    expected_mapping = lib.build_private_mapping(output_cases, [run for run in runs if run["kind"] == "output"], schedule_key, comparisons=_dev_comparisons(output_cases, plan))
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


def _comparison_config(plan: dict[str, Any], comparison_id: str) -> dict[str, Any]:
    matches = [item for item in plan["comparison_contract"]["comparisons"] if item["comparison_id"] == comparison_id]
    if len(matches) != 1:
        raise ValueError("unknown comparison identity")
    return matches[0]


def _judge_pairs(bundle: dict[str, Any], mapping: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = []
    for pair in bundle["pairs"]:
        private = mapping["pairs"].get(pair["pair_id"])
        if not private:
            raise ValueError("judge pair is not in sealed mapping")
        if _comparison_config(plan, private["comparison_id"])["max_judge_calls"]:
            pairs.append(pair)
    return pairs


def _judge_identity(pair: dict[str, Any], bundle_sha256: str, plan: dict[str, Any], pairs: list[dict[str, Any]], mapping: dict[str, Any]) -> dict[str, Any]:
    private = mapping["pairs"].get(pair.get("pair_id"))
    if not private:
        raise ValueError("unknown judge pair")
    comparison = _comparison_config(plan, private["comparison_id"])
    disposition = comparison["judge_call_slots"]
    if not disposition:
        raise ValueError("comparison has no judge call allocation")
    comparison_pairs = sorted(
        item["pair_id"] for item in pairs
        if mapping["pairs"].get(item.get("pair_id"), {}).get("comparison_id") == comparison["comparison_id"]
    )
    matching = [index for index, pair_id in enumerate(comparison_pairs) if pair_id == pair["pair_id"]]
    if len(matching) != 1 or matching[0] >= comparison["max_judge_calls"]:
        raise ValueError("unknown judge pair")
    records = _judge_records(plan)
    record_index = disposition[0] - 1 + matching[0]
    if record_index >= len(records) or record_index >= disposition[1]:
        raise ValueError("judge call budget exceeded")
    record = records[record_index]
    return {"call_id": record["call_id"], "pair_id": pair["pair_id"], "bundle_sha256": bundle_sha256, "model": record["model"], "effort": record["effort"], "cli": record["cli"], "executor": FAKE_JUDGE_ID, "policy_role": record["policy_role"]}


def _judge_manifest_value(bundle: dict[str, Any], mapping: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    bundle_sha256 = _bundle_sha256(bundle)
    pairs = _judge_pairs(bundle, mapping, plan)
    identities = [_judge_identity(pair, bundle_sha256, plan, bundle["pairs"], mapping) for pair in pairs]
    records = _judge_records(plan)
    used = [identity["call_id"] for identity in identities]
    reserved = [record["call_id"] for record in records if record["call_id"] not in set(used)]
    judge_identity = plan["execution_contract"]["lanes"]["blind_judge_and_tiebreak"]
    return {"schema_version": 3, "judge": JUDGE_CONTRACT_ID, "executor": FAKE_JUDGE_ID, "runner": RUNNER_ID, "bundle_sha256": bundle_sha256, "model": judge_identity["model"], "effort": judge_identity["effort"], "cli": plan["execution_contract"]["cli"], "policy_role": "blind_judge", "used_call_ids": used, "reserved_call_ids": reserved, "comparison_contract_sha256": _sha256_value(plan["comparison_contract"])}


def _judgment_commitment_value(root: Path, bundle: dict[str, Any], mapping: dict[str, Any], plan: dict[str, Any], key: str) -> dict[str, Any]:
    expected_ids = {pair["pair_id"] for pair in _judge_pairs(bundle, mapping, plan)}
    payload = {
        "bundle_sha256": _sha(root / "public/bundle.json", private=False),
        "judge_manifest_sha256": _sha(root / "private/judge-manifest.json", private=True),
        "judgments_sha256": _sha(root / "private/judgments.jsonl", private=True),
        "attempts": _completed_attempt_hashes(root / "private/judge-attempts", expected_ids),
    }
    return _signed_commitment("judgments", payload, key)


def _freeze_or_verify_judgment_commitment(root: Path, bundle: dict[str, Any], mapping: dict[str, Any], plan: dict[str, Any], key: str) -> None:
    _freeze_or_verify_commitment(root / "private" / JUDGMENT_COMMITMENT, _judgment_commitment_value(root, bundle, mapping, plan, key))


def _verify_judgment_commitment(root: Path, bundle: dict[str, Any], mapping: dict[str, Any], plan: dict[str, Any], key: str) -> None:
    _verify_commitment(root / "private" / JUDGMENT_COMMITMENT, _judgment_commitment_value(root, bundle, mapping, plan, key))


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


def _run_or_load_judges(root: Path, bundle: dict[str, Any], mapping: dict[str, Any], plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    bundle_sha256 = _bundle_sha256(bundle)
    pairs = _judge_pairs(bundle, mapping, plan)
    expected = {pair["pair_id"] for pair in pairs}
    directory = root / "private/judge-attempts"
    _ensure_dir(directory, private=True)
    names = set(_directory_names(directory, private=True, directories_only=True))
    if not names.issubset(expected):
        raise ValueError("unexpected judge attempt inventory")
    manifest = root / "private/judge-manifest.json"
    expected_manifest = _judge_manifest_value(bundle, mapping, plan)
    if _exists(manifest, private=True):
        if _read_json(manifest, private=True) != expected_manifest:
            raise ValueError("judge manifest mismatch")
    else:
        _write_json(manifest, expected_manifest, private=True, exclusive=True)
    rows = []
    spent = []
    for pair in pairs:
        identity = _judge_identity(pair, bundle_sha256, plan, bundle["pairs"], mapping)
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
        if _exists(root / "private" / JUDGMENT_COMMITMENT, private=True):
            raise ValueError("incomplete judge chain has frozen commitment")
        return rows, sorted(spent)
    if _exists(aggregate, private=True):
        if _read_jsonl(aggregate, private=True) != rows:
            raise ValueError("judgment aggregate mismatch")
    else:
        _write_jsonl(aggregate, rows, private=True, exclusive=True)
    _freeze_or_verify_judgment_commitment(root, bundle, mapping, plan, _load_key(root, "seal"))
    return rows, []


def _load_judgments(root: Path, bundle: dict[str, Any], mapping: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = _judge_pairs(bundle, mapping, plan)
    expected = {pair["pair_id"] for pair in pairs}
    directory = root / "private/judge-attempts"
    if set(_directory_names(directory, private=True, directories_only=True)) != expected:
        raise ValueError("judge artifact inventory mismatch")
    bundle_sha256 = _bundle_sha256(bundle)
    rows = [_load_judgment_attempt(directory / pair["pair_id"], _judge_identity(pair, bundle_sha256, plan, bundle["pairs"], mapping)) for pair in pairs]
    if _read_jsonl(root / "private/judgments.jsonl", private=True) != rows:
        raise ValueError("judgment aggregate mismatch")
    if _read_json(root / "private/judge-manifest.json", private=True) != _judge_manifest_value(bundle, mapping, plan):
        raise ValueError("judge manifest does not reconstruct from fixed identities")
    _verify_judgment_commitment(root, bundle, mapping, plan, _load_key(root, "seal"))
    return rows


def _assert_private_inventory(root: Path, *, revealed: bool = False) -> None:
    allowed = {"manifest.json", "mapping-key.json", "seal-key.json", "schedule-key.json", "schedule.json", "mapping.json", "attempts", "judge-attempts", "judgments.jsonl", "judge-manifest.json", ANSWER_COMMITMENT, JUDGMENT_COMMITMENT}
    if revealed:
        allowed.add("revealed.json")
    if not set(_directory_names(root / "private", private=True)).issubset(allowed):
        raise ValueError("unexpected private artifact")


def _config_sha256(plan: dict[str, Any]) -> str:
    return _sha256_value({"schema_version": 3, "runner": RUNNER_ID, "judge": JUDGE_CONTRACT_ID, "execution_contract": plan["execution_contract"], "comparison_contract": plan["comparison_contract"], "scoring_contract": plan["scoring_contract"], "bootstrap_contract": plan["bootstrap_contract"]})


def _seal(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    _assert_private_inventory(root)
    _, runs, mapping, bundle, mapping_key, seal_key = _load_chain(root, output_cases, activation_cases, plan, public_allowed={"bundle.json"})
    judgments = _load_judgments(root, bundle, mapping, plan)
    required = {"manifest.json", "mapping-key.json", "seal-key.json", "schedule-key.json", "schedule.json", "mapping.json", "attempts", "judge-attempts", "judgments.jsonl", "judge-manifest.json", ANSWER_COMMITMENT, JUDGMENT_COMMITMENT}
    if set(_directory_names(root / "private", private=True)) != required:
        raise ValueError("incomplete private artifact inventory")
    payload = {"config_sha256": _config_sha256(plan), "manifest_sha256": _sha(root / "private/manifest.json", private=True), "schedule_sha256": _sha(root / "private/schedule.json", private=True), "bundle_sha256": _sha(root / "public/bundle.json", private=False), "mapping_commitment": lib.mapping_commitment(mapping, mapping_key), "judgments_sha256": _sha(root / "private/judgments.jsonl", private=True), "judge_manifest_sha256": _sha(root / "private/judge-manifest.json", private=True), "answer_commitment_sha256": _sha(root / "private" / ANSWER_COMMITMENT, private=True), "judgment_commitment_sha256": _sha(root / "private" / JUDGMENT_COMMITMENT, private=True)}
    _write_json(root / "public/seal.json", lib.build_seal(payload, seal_key), private=False, exclusive=True)
    _assert_public_inventory(root, {"bundle.json", "seal.json"}, runs)
    return {"status": "sealed", "judgments": len(judgments)}


def _reveal(root: Path, output_cases: list[dict[str, Any]], activation_cases: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    _assert_private_inventory(root, revealed=True)
    _, runs, mapping, bundle, mapping_key, seal_key = _load_chain(root, output_cases, activation_cases, plan, public_allowed={"bundle.json", "seal.json"})
    judgments = _load_judgments(root, bundle, mapping, plan)
    seal = _read_json(root / "public/seal.json", private=False)
    result = lib.reveal(mapping, seal, mapping_key=mapping_key, seal_key=seal_key, config_sha256=_config_sha256(plan), manifest_sha256=_sha(root / "private/manifest.json", private=True), schedule_sha256=_sha(root / "private/schedule.json", private=True), bundle_sha256=_sha(root / "public/bundle.json", private=False), judgments_sha256=_sha(root / "private/judgments.jsonl", private=True), judge_manifest_sha256=_sha(root / "private/judge-manifest.json", private=True), answer_commitment_sha256=_sha(root / "private" / ANSWER_COMMITMENT, private=True), judgment_commitment_sha256=_sha(root / "private" / JUDGMENT_COMMITMENT, private=True), judgments=judgments)
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
        seal_key = _load_or_create_key(root, "seal", args.secret)
        schedule_key = _load_or_create_key(root, "schedule", args.secret)
        schedule = _load_or_create_schedule(root, output_cases, activation_cases, schedule_key, plan)
        runs, spent = _answer_runs(root, output_cases, activation_cases, schedule)
        if spent:
            if _exists(root / "private/mapping.json", private=True) or _exists(root / "public/bundle.json", private=False) or _exists(root / "private" / ANSWER_COMMITMENT, private=True):
                raise ValueError("incomplete answer chain has frozen artifacts")
            return {"status": "incomplete", "unsealable": True, "runs": len(runs), "spent_call_ids": spent}
        _freeze_or_verify_answer_commitment(root, schedule, seal_key)
        _mapping_and_bundle(root, output_cases, runs, schedule_key, mapping_key, plan)
        return {"status": "answered", "runs": len(runs)}
    if args.command == "reveal" and not _exists(root / "public/seal.json", private=False):
        raise ValueError("reveal requires seal")
    _assert_private_inventory(root, revealed=args.command == "reveal")
    _, runs, mapping, bundle, _, _ = _load_chain(root, output_cases, activation_cases, plan, public_allowed={"bundle.json", "seal.json"} if args.command == "reveal" else {"bundle.json"})
    if args.command == "judge":
        if not args.fake:
            raise ValueError("live calls are disabled in Phase A")
        _, spent = _run_or_load_judges(root, bundle, mapping, plan)
        if spent:
            return {"status": "incomplete", "unsealable": True, "spent_call_ids": spent}
        return {"status": "judged", "judgments": len(_judge_pairs(bundle, mapping, plan))}
    if args.command == "seal":
        return _seal(root, output_cases, activation_cases, plan)
    return _reveal(root, output_cases, activation_cases, plan)


if __name__ == "__main__":
    print(lib.canonical_json(main()))
