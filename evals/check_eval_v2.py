#!/usr/bin/env python3
"""Credential-free integration gates for the compact eval v2 harness."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import eval_v2_lib as lib
import run_eval_v2 as runner


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "evals/release-plan.json"
HOLDOUT_SCHEMA = ROOT / "evals/schemas/holdout.schema.json"


def _read_json(path: Path) -> dict[str, Any]:
    return lib.strict_json_object(path)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(lib.canonical_json(value) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return lib.strict_jsonl(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(lib.canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _expect_rejected(action: Callable[[], object], label: str) -> None:
    try:
        action()
    except (OSError, ValueError):
        return
    raise AssertionError(f"tampered {label} was accepted")


def _tamper_mapping(root: Path) -> None:
    path = root / "private/mapping.json"
    value = _read_json(path)
    pair = next(iter(value["pairs"].values()))
    pair["left"]["arm"] = "B" if pair["left"]["arm"] == "A" else "A"
    _write_json(path, value)


def _first_answer_attempt(root: Path) -> Path:
    return sorted((root / "private/attempts").iterdir())[0]


def _tamper_run_raw(root: Path) -> None:
    path = _first_answer_attempt(root) / "raw.jsonl"
    rows = _read_jsonl(path)
    rows[0]["final"] += " tampered"
    _write_jsonl(path, rows)


def _tamper_run_result(root: Path) -> None:
    path = _first_answer_attempt(root) / "result.json"
    value = _read_json(path)
    value["result"]["final"] += " tampered"
    _write_json(path, value)


def _tamper_bundle(root: Path) -> None:
    path = root / "public/bundle.json"
    value = _read_json(path)
    value["pairs"][0]["prompt"] += " tampered"
    _write_json(path, value)


def _tamper_judgment(root: Path) -> None:
    path = root / "private/judgments.jsonl"
    rows = _read_jsonl(path)
    rows[0]["judgment"]["quality"] = "left"
    _write_jsonl(path, rows)


def _tamper_seal(root: Path) -> None:
    path = root / "public/seal.json"
    value = _read_json(path)
    value["hmac"] = "0" * 64
    _write_json(path, value)


TAMPERS: dict[str, Callable[[Path], None]] = {
    "mapping": _tamper_mapping,
    "run_raw": _tamper_run_raw,
    "run_result": _tamper_run_result,
    "bundle": _tamper_bundle,
    "judgment": _tamper_judgment,
    "seal": _tamper_seal,
}


def _public_bundle_is_blind(root: Path) -> bool:
    bundle = _read_json(root / "public/bundle.json")
    serialized = lib.canonical_json(bundle)
    private_ids = {
        path.name for path in (root / "private/attempts").iterdir() if path.is_dir()
    }
    if '"arm"' in serialized or '"run_id"' in serialized:
        return False
    if any(identifier in serialized for identifier in private_ids):
        return False
    lib.assert_public_safe(
        bundle,
        arm_aliases={"A", "B", "generic terse"},
        private_ids=private_ids,
        protected_roots={ROOT, root, root / "private"},
    )
    return True


def release_dry_run() -> dict[str, Any]:
    plan = runner.main(["dry-run", "--plan", str(PLAN)])
    holdout_content = list((ROOT / "evals/cases").glob("holdout*"))
    holdout_dir = ROOT / "evals/holdout"
    if holdout_dir.exists():
        holdout_content.extend(path for path in holdout_dir.rglob("*") if path.is_file())
    if not HOLDOUT_SCHEMA.is_file():
        raise AssertionError("holdout schema is missing")
    return {
        "planned_calls": plan["planned_calls"],
        "hard_cap": plan["hard_cap"],
        "records": len(plan["records"]),
        "holdout_content_present": bool(holdout_content),
    }


def run_gates() -> dict[str, Any]:
    dry = release_dry_run()
    if dry != {
        "planned_calls": 275,
        "hard_cap": 280,
        "records": 275,
        "holdout_content_present": False,
    }:
        raise AssertionError("release dry-run does not match the preregistered contract")

    with tempfile.TemporaryDirectory(prefix="simple-man-eval-v2-check-") as temporary:
        temporary_root = Path(temporary)
        sealed = temporary_root / "sealed"
        answers = runner.main(
            ["answers", "--root", str(sealed), "--fake", "--secret", "offline-check"]
        )
        reveal_before_seal_rejected = False
        try:
            runner.main(["reveal", "--root", str(sealed)])
        except ValueError:
            reveal_before_seal_rejected = True
        judged = runner.main(["judge", "--root", str(sealed), "--fake"])
        runner.main(["seal", "--root", str(sealed)])
        public_bundle_blind = _public_bundle_is_blind(sealed)

        success = temporary_root / "success"
        shutil.copytree(sealed, success)
        if runner.main(["reveal", "--root", str(success)])["status"] != "revealed":
            raise AssertionError("sealed fake chain did not reveal")

        rejected: list[str] = []
        for label, tamper in TAMPERS.items():
            mutant = temporary_root / f"tampered-{label}"
            shutil.copytree(sealed, mutant)
            tamper(mutant)
            _expect_rejected(
                lambda mutant=mutant: runner.main(["reveal", "--root", str(mutant)]),
                label,
            )
            rejected.append(label)

    try:
        import coding_gate
    except ModuleNotFoundError as exc:
        raise AssertionError("coding gate is missing") from exc
    coding = coding_gate.self_check()
    return {
        "passed": True,
        "fake_answer_calls": answers["runs"],
        "fake_judgments": judged["judgments"],
        "tamper_rejections": rejected,
        "reveal_before_seal_rejected": reveal_before_seal_rejected,
        "public_bundle_blind": public_bundle_blind,
        "coding_fixtures": coding["fixtures"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("gates", "release-dry-run"))
    args = parser.parse_args(argv)
    value = run_gates() if args.command == "gates" else release_dry_run()
    print(lib.canonical_json(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
