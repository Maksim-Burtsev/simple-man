"""Pure, credential-free primitives for the compact eval v2 gates."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import random
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any


OUTPUT_CATEGORIES = {
    "status", "final", "review", "security", "setup", "diagnosis", "plan",
    "failed_validation", "destructive_risk", "detailed_override", "teaching_override",
    "creative_override",
}
JUDGE_FLAGS = {
    "factual_error", "missing_material_fact", "false_validation_claim",
    "safety_or_approval_loss", "detail_override_loss", "constraint_violation",
    "unnatural_compression", "unnecessary_content",
}


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def canonical_json(value: Any) -> str:
    _finite(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_exact_fields(value: Any, fields: set[str], name: str = "object") -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} must contain exactly {sorted(fields)}")
    return value


def _json_object(line: str, source: str) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{source}: duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(line, object_pairs_hook=no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{source}: invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{source}: JSON object required")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: malformed UTF-8") from exc
    rows = [_json_object(line, f"{path}:{number}") for number, line in enumerate(text.splitlines(), 1) if line.strip()]
    if not rows:
        raise ValueError(f"{path}: empty JSONL")
    return rows


def strict_json_object(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: malformed UTF-8") from exc
    return _json_object(text, str(path))


def strict_jsonl(path: Path) -> list[dict[str, Any]]:
    return _load_jsonl(path)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


OUTPUT_FIELDS = {"id", "kind", "cluster_id", "language", "category", "requested_shape", "prompt", "verified_context", "critical_facts", "forbidden_claims", "structure"}
ACTIVATION_FIELDS = {"id", "kind", "language", "activation_class", "execution", "expected", "protected_near_miss", "prompt"}
IDENTITY_FIELDS = {"arm", "candidate", "policy", "winner", "mapping"}
STRUCTURE_FIELDS = {"exact_nonempty_lines", "exact_paragraphs", "min_words", "max_words", "exact_top_level_items", "required_code_fence"}


def _reject_identity(value: Any) -> None:
    if isinstance(value, dict):
        if IDENTITY_FIELDS.intersection(value):
            raise ValueError("identity field is forbidden")
        for item in value.values():
            _reject_identity(item)
    elif isinstance(value, list):
        for item in value:
            _reject_identity(item)


def _validate_output_case(row: dict[str, Any]) -> None:
    require_exact_fields(row, OUTPUT_FIELDS, "output case")
    _reject_identity(row)
    for field in ("id", "cluster_id", "prompt"):
        _string(row[field], field)
    if row["kind"] != "output" or row["language"] not in {"en", "ru"} or row["category"] not in OUTPUT_CATEGORIES or row["requested_shape"] not in {"compact", "normal", "detailed", "teaching", "creative"}:
        raise ValueError("invalid output case enum")
    if not isinstance(row["verified_context"], dict) or not isinstance(row["critical_facts"], list) or not row["critical_facts"] or not isinstance(row["forbidden_claims"], list) or not isinstance(row["structure"], dict):
        raise ValueError("invalid output case payload")
    for fact in row["critical_facts"]:
        require_exact_fields(fact, {"id", "scope", "groups"}, "critical fact")
        if not isinstance(fact["id"], str) or fact["scope"] not in {"commentary", "final", "visible"} or not isinstance(fact["groups"], list) or not fact["groups"]:
            raise ValueError("invalid critical fact")
        if any(not isinstance(group, list) or not group or any(not isinstance(term, str) or not term for term in group) for group in fact["groups"]):
            raise ValueError("invalid critical fact groups")
    for claim in row["forbidden_claims"]:
        require_exact_fields(claim, {"id", "any_of"}, "forbidden claim")
        if not isinstance(claim["id"], str) or not isinstance(claim["any_of"], list) or not claim["any_of"] or any(not isinstance(term, str) or not term for term in claim["any_of"]):
            raise ValueError("invalid forbidden claim")
    if set(row["structure"]) - STRUCTURE_FIELDS:
        raise ValueError("unknown structure rule")
    for key, value in row["structure"].items():
        if key == "required_code_fence":
            if type(value) is not bool: raise ValueError("invalid structure rule")
        elif type(value) is not int or value < 1:
            raise ValueError("invalid structure rule")
    if row["requested_shape"] == "compact" and "max_words" in row["structure"]:
        raise ValueError("compact holdout cases cannot add an artificial max words rule")


def _validate_activation_case(row: dict[str, Any]) -> None:
    require_exact_fields(row, ACTIVATION_FIELDS, "activation case")
    _reject_identity(row)
    _string(row["id"], "id"); _string(row["prompt"], "prompt")
    valid = row["kind"] == "activation" and row["language"] in {"en", "ru"} and row["activation_class"] in {"explicit", "implicit", "negative"} and row["execution"] in {"mechanical", "routed"} and row["expected"] in {"activate", "do_not_activate"} and row["protected_near_miss"] in {None, "detailed", "teaching", "creative"}
    if not valid:
        raise ValueError("invalid activation case enum")
    expected = {"explicit": ("mechanical", "activate"), "implicit": ("routed", "activate"), "negative": ("routed", "do_not_activate")}[row["activation_class"]]
    if (row["execution"], row["expected"]) != expected or (row["protected_near_miss"] is not None and row["activation_class"] != "negative"):
        raise ValueError("invalid activation case contract")


def load_output_cases(path: Path) -> list[dict[str, Any]]:
    rows = _load_jsonl(path)
    for row in rows: _validate_output_case(row)
    if len(rows) != 12 or len({row["id"] for row in rows}) != 12:
        raise ValueError("output corpus must contain exactly 12 unique cases")
    return rows


def load_activation_cases(path: Path) -> list[dict[str, Any]]:
    rows = _load_jsonl(path)
    for row in rows: _validate_activation_case(row)
    if len(rows) != 20 or len({row["id"] for row in rows}) != 20:
        raise ValueError("activation corpus must contain exactly 20 unique cases")
    return rows


def validate_holdout_case(case: dict[str, Any]) -> None:
    if not isinstance(case, dict):
        raise ValueError("holdout case must be an object")
    if case.get("kind") == "output": _validate_output_case(case)
    elif case.get("kind") == "activation": _validate_activation_case(case)
    else: raise ValueError("unknown holdout kind")


def hmac_digest(secret: bytes | str, value: Any) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.new(key, canonical_json(value).encode(), hashlib.sha256).hexdigest()


def opaque_id(prefix: str, secret: bytes | str, value: Any) -> str:
    return f"{prefix}_{hmac_digest(secret, value)[:24]}"


def balanced_schedule(cases: list[dict[str, Any]], arms: tuple[str, ...], secret: bytes | str) -> list[dict[str, Any]]:
    if not arms:
        raise ValueError("arms required")
    orders = [arms[offset:] + arms[:offset] for offset in range(len(arms))]
    schedule: list[dict[str, Any]] = []
    for index, case in enumerate(sorted(cases, key=lambda item: hmac_digest(secret, {"case": item["id"]}))):
        order = orders[index % len(arms)]
        schedule.extend({"case_id": case["id"], "arm": arm, "ordinal": ordinal} for ordinal, arm in enumerate(order))
    return schedule


def balanced_sides(cases: list[dict[str, Any]], secret: bytes | str) -> dict[str, bool]:
    # A keyed ordering is the preregistered odd-stratum coin: alternating it
    # keeps every singleton stratum random while global left/right differs by at most one.
    ordered = sorted(cases, key=lambda case: hmac_digest(secret, {"side_coin": case["id"]}))
    return {case["id"]: index % 2 == 0 for index, case in enumerate(ordered)}


def build_private_mapping(cases: list[dict[str, Any]], runs: list[dict[str, Any]], secret: bytes | str) -> dict[str, Any]:
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for run in runs:
        if run.get("arm") not in {"A", "B"} or not isinstance(run.get("run_id"), str):
            raise ValueError("private runs must have A/B arm and run id")
        arms = by_case.setdefault(run.get("case_id"), {})
        if run["arm"] in arms:
            raise ValueError("duplicate private arm run")
        arms[run["arm"]] = run
    sides = balanced_sides(cases, secret)
    pairs: dict[str, Any] = {}
    for case in cases:
        arms = by_case.get(case["id"], {})
        if set(arms) != {"A", "B"}:
            raise ValueError("missing private pair counterpart")
        pair_id = opaque_id("pair", secret, {"case": case["id"]})
        left, right = ("A", "B") if sides[case["id"]] else ("B", "A")
        pairs[pair_id] = {
            "left": {"arm": left, "run_id": arms[left]["run_id"]},
            "right": {"arm": right, "run_id": arms[right]["run_id"]},
        }
    nonce = hashlib.sha256((secret.encode() if isinstance(secret, str) else secret) + b":commitment").hexdigest()
    return {"schema_version": 1, "eval_id": opaque_id("eval", secret, "eval"), "commitment_nonce": nonce, "pairs": pairs}


def mapping_commitment(mapping: dict[str, Any], key: bytes | str | None = None) -> str:
    require_exact_fields(mapping, {"schema_version", "eval_id", "commitment_nonce", "pairs"}, "mapping")
    if mapping["schema_version"] != 1 or not re.fullmatch(r"[0-9a-f]{64}", mapping["commitment_nonce"]):
        raise ValueError("invalid mapping")
    return hmac_digest(key, mapping) if key is not None else hashlib.sha256(canonical_json(mapping).encode()).hexdigest()


def _normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def assert_public_safe(bundle: dict[str, Any], *, arm_aliases: set[str] | None = None, private_ids: set[str] | None = None, protected_roots: set[Path] | None = None) -> None:
    forbidden = {"arm", "candidate", "winner", "runid", "private", "authorization", "secret"}
    aliases = "|".join(re.escape(alias) for alias in sorted(arm_aliases or {"A", "B"}) if alias)
    identifiers = {_normalized(identifier) for identifier in private_ids or set()}
    roots = [unicodedata.normalize("NFKC", str(root)).casefold() for root in protected_roots or set()]
    def walk(value: Any, *, top_level: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = _normalized(key)
                if normalized in forbidden or (normalized == "mapping" and not (top_level and key == "mapping_commitment")):
                    raise ValueError("public identity leak")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            raw = unicodedata.normalize("NFKC", value)
            text = _normalized(raw)
            if identifiers.intersection({text}) or any(identifier and identifier in text for identifier in identifiers):
                raise ValueError("public text leak")
            if aliases and re.search(rf"\b(?:arm|variant|treatment)\s*[-_: ]*\s*(?:{aliases})\b", raw, re.IGNORECASE):
                raise ValueError("public arm leak")
            if re.search(r"\bBearer\s+\S+|\b(?:api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*\S+", raw, re.IGNORECASE):
                raise ValueError("public secret leak")
            if any(root and root in raw.casefold() for root in roots) or re.search(r"/(?:Users|home|private|var|tmp)/", raw, re.IGNORECASE):
                raise ValueError("public path leak")
    walk(bundle, top_level=True)


def build_public_bundle(cases: list[dict[str, Any]], runs: list[dict[str, Any]], mapping: dict[str, Any], *, commitment: str | None = None, protected_roots: set[Path] | None = None) -> dict[str, Any]:
    mapping_commitment(mapping)
    run_by_id = {run.get("run_id"): run for run in runs}
    case_by_id = {case["id"]: case for case in cases}
    pairs: list[dict[str, Any]] = []
    for pair_id, pair in sorted(mapping["pairs"].items()):
        require_exact_fields(pair, {"left", "right"}, "mapping pair")
        require_exact_fields(pair["left"], {"arm", "run_id"}, "mapping side")
        require_exact_fields(pair["right"], {"arm", "run_id"}, "mapping side")
        left, right = run_by_id.get(pair["left"]["run_id"]), run_by_id.get(pair["right"]["run_id"])
        if not left or not right or left["case_id"] != right["case_id"] or left.get("arm") != pair["left"]["arm"] or right.get("arm") != pair["right"]["arm"] or left["arm"] == right["arm"]:
            raise ValueError("invalid mapping run reference")
        case = case_by_id.get(left["case_id"])
        if not case:
            raise ValueError("unknown mapping case")
        pairs.append({"pair_id": pair_id, "case_id": case["id"], "language": case["language"], "prompt": case["prompt"], "verified_context": case["verified_context"], "response_A": {"commentary": left["commentary"], "final": left["final"]}, "response_B": {"commentary": right["commentary"], "final": right["final"]}})
    bundle = {"schema_version": 1, "mapping_commitment": commitment or mapping_commitment(mapping), "pairs": pairs}
    assert_public_safe(bundle, arm_aliases={"A", "B"}, private_ids={str(run["run_id"]) for run in runs}, protected_roots=protected_roots)
    return bundle


def build_judge_payload(case: dict[str, Any], left: str, right: str) -> dict[str, Any]:
    return {"untrusted_task": {"prompt": case["prompt"], "verified_context": case["verified_context"], "deliverable": case.get("deliverable", case.get("requested_shape", "final"))}, "left": left, "right": right}


def validate_judgment(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, str):
        if raw.strip() != raw:
            raise ValueError("judge output must be exact JSON")
        value = _json_object(raw, "judgment")
    else:
        value = raw
    require_exact_fields(value, {"quality", "naturalness", "flags", "rationale"}, "judgment")
    if value["quality"] not in {"left", "right", "tie", "both_bad"} or value["naturalness"] not in {"left", "right", "tie"}:
        raise ValueError("invalid judgment choice")
    require_exact_fields(value["flags"], {"left", "right"}, "judgment flags")
    for flags in value["flags"].values():
        if not isinstance(flags, list) or len(set(flags)) != len(flags) or any(flag not in JUDGE_FLAGS for flag in flags):
            raise ValueError("invalid judgment flags")
    if not isinstance(value["rationale"], str) or len(value["rationale"]) > 600:
        raise ValueError("invalid rationale")
    return value


def _scope_text(response: dict[str, str], scope: str) -> str:
    if scope == "commentary": return response.get("commentary", "")
    if scope == "final": return response.get("final", "")
    if scope == "visible": return f"{response.get('commentary', '')}\n{response.get('final', '')}"
    raise ValueError("unknown fact scope")


def _match(text: str, phrase: str) -> bool:
    return unicodedata.normalize("NFC", phrase).casefold() in unicodedata.normalize("NFC", text).casefold()


def _forbidden_match(text: str, phrase: str) -> bool:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).casefold())
    target = unicodedata.normalize("NFC", phrase).casefold()
    for match in re.finditer(re.escape(target), normalized):
        prefix = normalized[max(0, match.start() - 24):match.start()]
        if re.search(r"\b(?:not|no|never|не)\s*$", prefix):
            continue
        return True
    return False


def check_critical_facts(case: dict[str, Any], response: dict[str, str]) -> dict[str, Any]:
    missing = []
    for fact in case.get("critical_facts", []):
        if not isinstance(fact, dict) or not isinstance(fact.get("groups"), list):
            raise ValueError("invalid critical fact")
        text = _scope_text(response, fact.get("scope", "visible"))
        if not all(any(isinstance(term, str) and _match(text, term) for term in group) for group in fact["groups"]):
            missing.append(fact.get("id", "unknown"))
    visible = _scope_text(response, "visible")
    forbidden = [claim.get("id", "unknown") for claim in case.get("forbidden_claims", []) if any(_forbidden_match(visible, phrase) for phrase in claim.get("any_of", []))]
    structure = []
    rules = case.get("structure", {})
    lines = [line for line in visible.splitlines() if line.strip()]
    if "exact_nonempty_lines" in rules and len(lines) != rules["exact_nonempty_lines"]: structure.append("exact_nonempty_lines")
    if "exact_paragraphs" in rules and len([part for part in re.split(r"\n\s*\n", visible.strip()) if part]) != rules["exact_paragraphs"]: structure.append("exact_paragraphs")
    words = visible.split()
    if "min_words" in rules and len(words) < rules["min_words"]: structure.append("min_words")
    if "max_words" in rules and len(words) > rules["max_words"]: structure.append("max_words")
    if "exact_top_level_items" in rules and len([line for line in lines if re.match(r"^\s*\d+[.)]\s+", line)]) != rules["exact_top_level_items"]: structure.append("exact_top_level_items")
    if rules.get("required_code_fence") and "```" not in visible: structure.append("required_code_fence")
    return {"passed": not (missing or forbidden or structure), "missing": missing, "forbidden": forbidden, "structure": structure}


def activation_confusion_matrix(cases: list[dict[str, Any]], predictions: dict[str, bool]) -> dict[str, Any]:
    expected_ids = {case["id"] for case in cases}
    if set(predictions) != expected_ids or any(type(value) is not bool for value in predictions.values()):
        raise ValueError("activation predictions must exactly match cases")
    routed = [case for case in cases if case["execution"] == "routed"]
    tp = sum(predictions[case["id"]] for case in routed if case["expected"] == "activate")
    fn = sum(not predictions[case["id"]] for case in routed if case["expected"] == "activate")
    fp = sum(predictions[case["id"]] for case in routed if case["expected"] == "do_not_activate")
    tn = sum(not predictions[case["id"]] for case in routed if case["expected"] == "do_not_activate")
    explicit = [case for case in cases if case["execution"] == "mechanical"]
    protected = {kind: sum(predictions[case["id"]] for case in routed if case["protected_near_miss"] == kind) for kind in ("detailed", "teaching", "creative")}
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "implicit_recall": tp / (tp + fn) if tp + fn else None, "precision": tp / (tp + fp) if tp + fp else None, "explicit_accuracy": sum(predictions[case["id"]] for case in explicit) / len(explicit) if explicit else None, "protected_false_positives": protected}


def _metrics(run: dict[str, Any]) -> dict[str, float]:
    keys = ("commentary_visible_tokens", "final_visible_tokens", "input_tokens", "cached_input_tokens", "output_tokens", "latency_ms")
    result: dict[str, float] = {}
    for key in keys:
        value = run.get(key)
        if type(value) not in {int, float} or value < 0 or not math.isfinite(value):
            raise ValueError(f"invalid metric {key}")
        result[key] = value
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise ValueError("cached input exceeds input")
    result["visible_output_tokens"] = result["commentary_visible_tokens"] + result["final_visible_tokens"]
    result["uncached_input_tokens"] = result["input_tokens"] - result["cached_input_tokens"]
    return result


def pair_measurements(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str, str], dict[str, dict[str, Any]]] = {}
    for run in runs:
        key = (run.get("case_id"), run.get("trial"), run.get("model"), run.get("effort"))
        if not isinstance(key[0], str) or type(key[1]) is not int or not isinstance(key[2], str) or not isinstance(key[3], str) or run.get("arm") not in {"A", "B"}:
            raise ValueError("invalid pairing identity")
        bucket = grouped.setdefault(key, {})
        if run["arm"] in bucket: raise ValueError("duplicate pair counterpart")
        bucket[run["arm"]] = _metrics(run)
    paired = []
    for key, sides in sorted(grouped.items()):
        if set(sides) != {"A", "B"}: raise ValueError("missing pair counterpart")
        paired.append({"case_id": key[0], "cluster_id": key[0], "trial": key[1], "model": key[2], "effort": key[3], "A": sides["A"], "B": sides["B"]})
    return paired


def paired_summary(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    if not pairs: raise ValueError("pairs required")
    metrics = sorted(pairs[0]["A"])
    summary = {}
    for metric in metrics:
        deltas = [pair["B"][metric] - pair["A"][metric] for pair in pairs]
        reductions = [(pair["A"][metric] - pair["B"][metric]) / pair["A"][metric] for pair in pairs if pair["A"][metric] != 0]
        summary[metric] = {"delta": statistics.median(deltas), "relative_reduction": statistics.median(reductions) if reductions else None}
    summary["estimate"] = {"estimated_session_net": {"value": None, "assumptions": "estimate only; not billing or cost savings"}}
    return summary


def clustered_bootstrap_ci(pairs: list[dict[str, Any]], metric: str, *, seed: int, iterations: int) -> tuple[float, float]:
    if iterations <= 0 or not pairs: raise ValueError("bootstrap requires pairs and iterations")
    clusters: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs: clusters.setdefault(pair["cluster_id"], []).append(pair)
    ids = sorted(clusters)
    rng = random.Random(seed)
    values = []
    for _ in range(iterations):
        sampled = [pair for cluster in (rng.choice(ids) for _ in ids) for pair in clusters[cluster]]
        reductions = [(pair["A"][metric] - pair["B"][metric]) / pair["A"][metric] for pair in sampled if pair["A"][metric] != 0]
        if not reductions:
            raise ValueError("zero baseline metric")
        values.append(statistics.median(reductions))
    values.sort()
    return values[int((iterations - 1) * 0.025)], values[int((iterations - 1) * 0.975)]


def build_seal(payload: dict[str, Any], key: bytes | str) -> dict[str, Any]:
    fields = {"config_sha256", "manifest_sha256", "schedule_sha256", "bundle_sha256", "mapping_commitment", "judgments_sha256", "judge_manifest_sha256"}
    require_exact_fields(payload, fields, "seal payload")
    result = dict(payload)
    result["hmac"] = hmac_digest(key, payload)
    return result


def verify_seal(seal: dict[str, Any], key: bytes | str) -> None:
    fields = {"config_sha256", "manifest_sha256", "schedule_sha256", "bundle_sha256", "mapping_commitment", "judgments_sha256", "judge_manifest_sha256", "hmac"}
    require_exact_fields(seal, fields, "seal")
    payload = {key_: value for key_, value in seal.items() if key_ != "hmac"}
    if not hmac.compare_digest(seal["hmac"], hmac_digest(key, payload)):
        raise ValueError("invalid seal HMAC")


def reveal(mapping: dict[str, Any], seal: dict[str, Any], *, mapping_key: bytes | str, seal_key: bytes | str, config_sha256: str, manifest_sha256: str, schedule_sha256: str, bundle_sha256: str, judgments_sha256: str, judge_manifest_sha256: str, judgments: list[dict[str, Any]]) -> dict[str, Any]:
    commitment = mapping_commitment(mapping, mapping_key)
    verify_seal(seal, seal_key)
    expected = {"config_sha256": config_sha256, "manifest_sha256": manifest_sha256, "schedule_sha256": schedule_sha256, "bundle_sha256": bundle_sha256, "mapping_commitment": commitment, "judgments_sha256": judgments_sha256, "judge_manifest_sha256": judge_manifest_sha256}
    if any(seal[key] != value for key, value in expected.items()):
        raise ValueError("seal artifact mismatch")
    remapped = []
    for row in judgments:
        pair = mapping["pairs"].get(row["pair_id"])
        if not pair:
            raise ValueError("unknown remapped judgment")
        remapped.append({"pair_id": row["pair_id"], "left_arm": pair["left"]["arm"], "right_arm": pair["right"]["arm"], "judgment": row["judgment"]})
    return {"status": "revealed", "mapping_commitment": commitment, "judgments": remapped}
