from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import re
import shutil
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
SIMPLE_MAN_RUNTIME_MARKER = "## Simple Man runtime policy"

BASELINE_ARM = "baseline"
NATIVE_LOW_ARM = "native_low"
SIMPLE_MAN_RUNTIME_ARM = "simple_man_runtime"
DEFAULT_ARMS = (NATIVE_LOW_ARM, SIMPLE_MAN_RUNTIME_ARM)

_PROMPT_CONTAMINATION_PATTERNS = (
    ("Simple Man name", re.compile(r"\bsimple[ _-]+man\b", re.IGNORECASE)),
    ("Simple Man runtime marker", re.compile(re.escape(SIMPLE_MAN_RUNTIME_MARKER), re.IGNORECASE)),
    ("native-low arm", re.compile(r"\bnative[ _-]+low\b", re.IGNORECASE)),
    ("model verbosity treatment", re.compile(r"\bmodel[ _-]+verbosity\b", re.IGNORECASE)),
    (
        "benchmark treatment arm",
        re.compile(r"\b(?:benchmark|control|treatment|candidate)[ _-]+arm\b", re.IGNORECASE),
    ),
)
_SAFE_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NIX_SSL_CERT_FILE",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_PROXY_ENV_KEYS = frozenset(
    {"ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"}
)


@dataclass(frozen=True)
class ArmSpec:
    name: str
    model_verbosity: str
    agents_text: str | None

    @property
    def policy_sha256(self) -> str:
        return sha256_text(self.agents_text or "")


@dataclass(frozen=True)
class RunKey:
    task_id: str
    arm: str
    trial: int

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        if not self.arm:
            raise ValueError("arm must not be empty")
        if self.trial < 1:
            raise ValueError("trial must be >= 1")


@dataclass(frozen=True)
class IsolatedCodexEnvironment:
    root: Path
    home: Path
    codex_home: Path
    workspace: Path
    env: dict[str, str]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        mode=mode,
    )


def atomic_copy_file(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as destination_file:
            with source.open("rb") as source_file:
                shutil.copyfileobj(source_file, destination_file)
                destination_file.flush()
                os.fsync(destination_file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def load_prompts(path: Path) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            prompt = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(prompt, dict):
            raise ValueError(f"{path}:{line_number}: prompt must be an object")

        task_id = prompt.get("id")
        category = prompt.get("category")
        text = prompt.get("prompt")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"{path}:{line_number}: id must be a non-empty string")
        if task_id in seen:
            raise ValueError(f"{path}:{line_number}: duplicate id: {task_id}")
        if not isinstance(category, str) or not category:
            raise ValueError(f"{path}:{line_number}: category must be a non-empty string")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{path}:{line_number}: prompt must be a non-empty string")
        language = prompt.get("language")
        if language is not None and (not isinstance(language, str) or not language):
            raise ValueError(f"{path}:{line_number}: language must be a non-empty string")
        verified_context = prompt.get("verified_context")
        if not isinstance(verified_context, str) or not verified_context.strip():
            raise ValueError(
                f"{path}:{line_number}: verified_context must be a non-empty string"
            )

        seen.add(task_id)
        prompts.append(prompt)

    if not prompts:
        raise ValueError(f"{path}: no prompts found")
    return prompts


def prompt_corpus_sha256(prompts: Sequence[Mapping[str, Any]]) -> str:
    return sha256_text(canonical_json(list(prompts)) + "\n")


def prompt_contamination(prompt: str) -> list[str]:
    return [label for label, pattern in _PROMPT_CONTAMINATION_PATTERNS if pattern.search(prompt)]


def validate_prompt_contamination(prompts: Sequence[Mapping[str, Any]]) -> None:
    failures: list[str] = []
    for prompt in prompts:
        matches = prompt_contamination(str(prompt["prompt"]))
        if matches:
            failures.append(f"{prompt['id']}: {', '.join(matches)}")
    if failures:
        raise ValueError("prompt treatment contamination: " + "; ".join(failures))


def build_arm_specs(runtime_policy: str) -> dict[str, ArmSpec]:
    if runtime_policy.count(SIMPLE_MAN_RUNTIME_MARKER) != 1:
        raise ValueError(
            "Simple Man runtime policy must contain exactly one marker: "
            f"{SIMPLE_MAN_RUNTIME_MARKER!r}"
        )
    return {
        BASELINE_ARM: ArmSpec(BASELINE_ARM, "medium", None),
        NATIVE_LOW_ARM: ArmSpec(NATIVE_LOW_ARM, "low", None),
        SIMPLE_MAN_RUNTIME_ARM: ArmSpec(
            SIMPLE_MAN_RUNTIME_ARM,
            "low",
            runtime_policy,
        ),
    }


def private_run_id(config_sha256: str, key: RunKey) -> str:
    identity = {
        "config_sha256": config_sha256,
        "task_id": key.task_id,
        "arm": key.arm,
        "trial": key.trial,
    }
    return "run_" + sha256_text(canonical_json(identity))[:24]


def _blind_digest(secret: str, purpose: str, value: str) -> bytes:
    if not secret:
        raise ValueError("blinding secret must not be empty")
    return hmac.new(
        secret.encode("utf-8"),
        f"{purpose}\0{value}".encode("utf-8"),
        hashlib.sha256,
    ).digest()


def public_pair_id(
    blinding_secret: str,
    public_run_id: str,
    task_id: str,
    trial: int,
    pair_index: int,
) -> str:
    # Treatment names/private run ids stay out; secret HMAC prevents brute-force decoding.
    identity = {
        "public_run_id": public_run_id,
        "task_id": task_id,
        "trial": trial,
        "pair_index": pair_index,
    }
    return "pair_" + _blind_digest(
        blinding_secret,
        "pair-id",
        canonical_json(identity),
    ).hex()[:24]


def _blind_rank(blinding_secret: str, purpose: str, pair_id: str) -> bytes:
    return _blind_digest(blinding_secret, purpose, pair_id)


def block_balanced_left_assignments(
    blinding_secret: str,
    pair_ids: Sequence[str],
    *,
    block_id: str,
) -> dict[str, bool]:
    ranked = sorted(
        pair_ids,
        key=lambda pair_id: _blind_rank(
            blinding_secret,
            f"side-rank:{block_id}",
            pair_id,
        ),
    )
    first_left_count = len(ranked) // 2
    if len(ranked) % 2:
        extra = _blind_digest(blinding_secret, "side-extra", block_id)[0] & 1
        first_left_count += int(extra)
    first_left = set(ranked[:first_left_count])
    return {pair_id: pair_id in first_left for pair_id in pair_ids}


def detect_language(text: str) -> str:
    return "ru" if re.search(r"[А-Яа-яЁё]", text) else "en"


def _metadata_contains_arm_name(metadata: Mapping[str, Any], arms: Sequence[str]) -> str | None:
    serialized = canonical_json(metadata).casefold()
    for arm in arms:
        variants = {arm.casefold(), arm.replace("_", " ").casefold()}
        for variant in variants:
            if variant and variant in serialized:
                return arm
    return None


def _public_treatment_leaks(value: Mapping[str, Any], arms: Sequence[str]) -> list[str]:
    def strings(item: Any) -> Iterator[str]:
        if isinstance(item, str):
            yield item
        elif isinstance(item, Mapping):
            for key, nested in item.items():
                yield str(key)
                yield from strings(nested)
        elif isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            for nested in item:
                yield from strings(nested)

    def normalized(text: str) -> str:
        folded = unicodedata.normalize("NFKC", text).casefold()
        return " ".join(re.sub(r"[\W_]+", " ", folded).split())

    phrases = [(arm, normalized(arm)) for arm in arms]
    if SIMPLE_MAN_RUNTIME_ARM in arms:
        phrases.append(("Simple Man name", "simple man"))
    phrases.append(("model verbosity treatment", "model verbosity"))

    leaks: set[str] = set()
    for text in strings(value):
        haystack = f" {normalized(text)} "
        for label, phrase in phrases:
            if phrase and f" {phrase} " in haystack:
                leaks.add(label)
    return sorted(leaks)


def _index_results(results: Sequence[Mapping[str, Any]]) -> dict[RunKey, Mapping[str, Any]]:
    indexed: dict[RunKey, Mapping[str, Any]] = {}
    private_run_ids: set[str] = set()
    for result in results:
        key = RunKey(
            task_id=str(result["task_id"]),
            arm=str(result["arm"]),
            trial=int(result["trial"]),
        )
        if key in indexed:
            raise ValueError(f"duplicate result: {key.task_id}/{key.arm}/{key.trial}")
        run_id = str(result["run_id"])
        if run_id in private_run_ids:
            raise ValueError(f"duplicate private run_id: {run_id}")
        if not isinstance(result.get("text"), str) or not result["text"].strip():
            raise ValueError(f"empty result text: {key.task_id}/{key.arm}/{key.trial}")
        indexed[key] = result
        private_run_ids.add(run_id)
    return indexed


def build_blind_bundle(
    *,
    public_run_id: str,
    metadata: Mapping[str, Any],
    prompts: Sequence[Mapping[str, Any]],
    arms: Sequence[str],
    trials: int,
    blinding_secret: str,
    results: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(arms) < 2:
        raise ValueError("at least two arms are required")
    if len(set(arms)) != len(arms):
        raise ValueError("arms must be unique")
    leaked_arm = _metadata_contains_arm_name(metadata, arms)
    if leaked_arm:
        raise ValueError(f"public metadata contains arm name: {leaked_arm}")

    indexed = _index_results(results)
    for prompt in prompts:
        verified_context = prompt.get("verified_context")
        if not isinstance(verified_context, str) or not verified_context.strip():
            raise ValueError(f"verified_context must be a non-empty string: {prompt['id']}")

    public_pairs: list[dict[str, Any]] = []
    private_pairs: dict[str, dict[str, str]] = {}

    arm_pairs = list(itertools.combinations(arms, 2))
    pair_specs: list[tuple[Mapping[str, Any], int, int, str, str, str]] = []
    side_assignments: dict[str, bool] = {}
    for pair_index, (first_arm, second_arm) in enumerate(arm_pairs, 1):
        block: list[str] = []
        for prompt in prompts:
            task_id = str(prompt["id"])
            for trial in range(1, trials + 1):
                pair_id = public_pair_id(
                    blinding_secret,
                    public_run_id,
                    task_id,
                    trial,
                    pair_index,
                )
                block.append(pair_id)
                pair_specs.append(
                    (prompt, trial, pair_index, first_arm, second_arm, pair_id)
                )
        side_assignments.update(
            block_balanced_left_assignments(
                blinding_secret,
                block,
                block_id=f"comparison-{pair_index}",
            )
        )

    pair_specs.sort(
        key=lambda item: _blind_rank(blinding_secret, "pair-order", item[-1])
    )
    for prompt, trial, _pair_index, first_arm, second_arm, pair_id in pair_specs:
        task_id = str(prompt["id"])
        first = indexed.get(RunKey(task_id, first_arm, trial))
        second = indexed.get(RunKey(task_id, second_arm, trial))
        if first is None or second is None:
            missing = first_arm if first is None else second_arm
            raise ValueError(f"missing result: {task_id}/{missing}/{trial}")

        if side_assignments[pair_id]:
            left_arm, left = first_arm, first
            right_arm, right = second_arm, second
        else:
            left_arm, left = second_arm, second
            right_arm, right = first_arm, first

        public_pair: dict[str, Any] = {
            "id": pair_id,
            "task_id": task_id,
            "category": str(prompt["category"]),
            "language": str(
                prompt.get("language") or detect_language(str(prompt["prompt"]))
            ),
            "prompt": str(prompt["prompt"]),
            "left": {"text": str(left["text"])},
            "right": {"text": str(right["text"])},
            "verified_context": str(prompt["verified_context"]),
        }
        public_pairs.append(public_pair)
        private_pairs[pair_id] = {
            "left_arm": left_arm,
            "right_arm": right_arm,
            "left_run_id": str(left["run_id"]),
            "right_run_id": str(right["run_id"]),
        }

    expected_results = len(prompts) * len(arms) * trials
    if len(indexed) != expected_results:
        raise ValueError(f"unexpected results: expected {expected_results}, got {len(indexed)}")

    public = {
        "schema_version": SCHEMA_VERSION,
        "run_id": public_run_id,
        "metadata": dict(metadata),
        "pairs": public_pairs,
    }
    public_leaks = _public_treatment_leaks(public, arms)
    if public_leaks:
        raise ValueError(
            "public bundle contains treatment name: " + ", ".join(public_leaks)
        )
    private = {
        "schema_version": SCHEMA_VERSION,
        "run_id": public_run_id,
        "pairs": private_pairs,
    }
    return public, private


def assert_arm_environment(spec: ArmSpec, environment: IsolatedCodexEnvironment) -> None:
    instruction_files = list(environment.home.rglob("AGENTS.md"))
    instruction_texts = [path.read_text(encoding="utf-8") for path in instruction_files]
    marker_count = sum(text.count(SIMPLE_MAN_RUNTIME_MARKER) for text in instruction_texts)
    name_count = sum(len(re.findall(r"\bsimple[ _-]+man\b", text, re.IGNORECASE)) for text in instruction_texts)

    if spec.name == SIMPLE_MAN_RUNTIME_ARM:
        expected_path = environment.codex_home / "AGENTS.md"
        if instruction_files != [expected_path]:
            raise ValueError("Simple Man arm must have exactly one CODEX_HOME/AGENTS.md")
        if marker_count != 1:
            raise ValueError("Simple Man arm must have exactly one runtime marker")
        if name_count < 1:
            raise ValueError("Simple Man arm runtime name is missing")
    elif marker_count or name_count or instruction_files:
        raise ValueError(f"{spec.name} arm contains Simple Man instructions")


def safe_environment() -> dict[str, str]:
    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    for key in _PROXY_ENV_KEYS:
        value = env.get(key)
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or "@" in value:
            raise ValueError(
                f"{key} contains proxy credentials; use a credential-free proxy URL"
            )
    return env


@contextmanager
def isolated_codex_environment(
    *,
    auth_source: Path,
    spec: ArmSpec,
    auth_sink: Path | None = None,
) -> Iterator[IsolatedCodexEnvironment]:
    # Neutral path: cwd/HOME paths are model-visible and must not reveal an arm.
    with tempfile.TemporaryDirectory(prefix="codex-blind-") as temporary:
        root = Path(temporary)
        home = root / "home"
        codex_home = home / ".codex"
        workspace = root / "workspace"
        codex_home.mkdir(parents=True, mode=0o700)
        workspace.mkdir(mode=0o700)

        if not auth_source.is_file():
            raise FileNotFoundError("Codex auth file not found")
        auth_destination = codex_home / "auth.json"
        atomic_copy_file(auth_source, auth_destination)
        if spec.agents_text is not None:
            atomic_write_text(codex_home / "AGENTS.md", spec.agents_text, mode=0o600)

        scratch = root / "tmp"
        scratch.mkdir(mode=0o700)
        env = safe_environment()
        env.setdefault("PATH", os.defpath)
        env["HOME"] = str(home)
        env["CODEX_HOME"] = str(codex_home)
        env["TMPDIR"] = str(scratch)
        env["TMP"] = str(scratch)
        env["TEMP"] = str(scratch)
        isolated = IsolatedCodexEnvironment(
            root=root,
            home=home,
            codex_home=codex_home,
            workspace=workspace,
            env=env,
        )
        assert_arm_environment(spec, isolated)
        try:
            yield isolated
        finally:
            if auth_sink is not None:
                atomic_copy_file(auth_destination, auth_sink)


def parse_codex_jsonl(path: Path) -> tuple[str, dict[str, int]]:
    final_text = ""
    usage: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                final_text = str(item.get("text", ""))
        elif event.get("type") == "turn.completed":
            usage = {
                key: int(value)
                for key, value in (event.get("usage") or {}).items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    if not final_text.strip():
        raise ValueError("Codex JSONL has no final agent_message")
    if not usage:
        raise ValueError("Codex JSONL has no turn.completed usage")
    return final_text, usage
