#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from review_lib import private_key_commitment_sha256


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "evals" / "review_app"
ALLOWED_CHOICES = {"left", "right", "tie", "both_bad"}
ALLOWED_FLAGS = {
    "missing_fact",
    "too_terse",
    "too_verbose",
    "hard_to_scan",
    "needs_followup",
    "unsupported_claim",
}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ReviewStore:
    def __init__(
        self,
        bundle_path: Path,
        key_path: Path,
        ratings_path: Path,
        results_path: Path | None = None,
    ) -> None:
        self.bundle_path = bundle_path
        self.key_path = key_path
        self.ratings_path = ratings_path
        self.results_path = results_path or ratings_path.with_name("results.json")
        self.bundle = load_json(bundle_path)
        self.key = load_json(key_path)
        self.bundle_sha256 = sha256_file(bundle_path)
        self.key_sha256 = sha256_file(key_path)
        self._lock = threading.Lock()
        self._validate_inputs()
        self._pairs = list(self.bundle["pairs"])
        self._pairs_by_id = {pair["id"]: pair for pair in self._pairs}
        self._ratings = self._load_or_create_ratings()

    def _validate_inputs(self) -> None:
        if self.bundle.get("schema_version") != 1:
            raise ValueError("bundle schema_version must be 1")
        if self.key.get("schema_version") != 1:
            raise ValueError("key schema_version must be 1")
        run_id = self.bundle.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("bundle run_id must be a non-empty string")
        if self.key.get("run_id") != run_id:
            raise ValueError("bundle and key run_id differ")
        metadata = self.bundle.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("bundle metadata must be an object")
        commitment = metadata.get("key_commitment_sha256")
        if not isinstance(commitment, str):
            raise ValueError("bundle metadata has no key commitment")
        if commitment != private_key_commitment_sha256(self.key):
            raise ValueError("private key differs from public bundle commitment")

        pairs = self.bundle.get("pairs")
        key_pairs = self.key.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            raise ValueError("bundle pairs must be a non-empty list")
        if not isinstance(key_pairs, dict):
            raise ValueError("key pairs must be an object")

        ids: list[str] = []
        allowed_pair_fields = {
            "id",
            "task_id",
            "category",
            "language",
            "prompt",
            "verified_context",
            "left",
            "right",
        }
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("each bundle pair must be an object")
            pair_id = pair.get("id")
            if not isinstance(pair_id, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]{1,200}", pair_id
            ):
                raise ValueError("each bundle pair id must be 1-200 safe characters")
            ids.append(pair_id)
            unknown_pair_fields = set(pair) - allowed_pair_fields
            if unknown_pair_fields:
                raise ValueError(
                    f"{pair_id}: public pair has unknown fields: "
                    f"{sorted(unknown_pair_fields)}"
                )
            required_strings = {
                "task_id": 200,
                "category": 100,
                "language": 20,
                "prompt": 100_000,
            }
            for field, limit in required_strings.items():
                value = pair.get(field)
                if not isinstance(value, str) or not value or len(value) > limit:
                    raise ValueError(
                        f"{pair_id}: {field} must be a non-empty string <= {limit}"
                    )
            context = pair.get("verified_context")
            if context is not None and (
                not isinstance(context, str) or len(context) > 100_000
            ):
                raise ValueError(
                    f"{pair_id}: verified_context must be a string <= 100000"
                )
            for side in ("left", "right"):
                answer = pair.get(side)
                if not isinstance(answer, dict) or not isinstance(
                    answer.get("text"), str
                ):
                    raise ValueError(f"{pair_id}: {side}.text must be a string")
                if len(answer["text"]) > 1_000_000:
                    raise ValueError(
                        f"{pair_id}: {side}.text exceeds 1000000 characters"
                    )
                unknown_answer_fields = set(answer) - {"text"}
                if unknown_answer_fields:
                    raise ValueError(
                        f"{pair_id}: public {side} has unknown fields: "
                        f"{sorted(unknown_answer_fields)}"
                    )
        if len(ids) != len(set(ids)):
            raise ValueError("bundle pair ids must be unique")
        if set(ids) != set(key_pairs):
            raise ValueError("bundle and key pair ids differ")

        for pair_id, mapping in key_pairs.items():
            if not isinstance(mapping, dict):
                raise ValueError(f"{pair_id}: key mapping must be an object")
            unknown_mapping_fields = set(mapping) - {
                "left_arm",
                "right_arm",
                "left_run_id",
                "right_run_id",
            }
            if unknown_mapping_fields:
                raise ValueError(
                    f"{pair_id}: key mapping has unknown fields: "
                    f"{sorted(unknown_mapping_fields)}"
                )
            for field in ("left_arm", "right_arm"):
                value = mapping.get(field)
                if not isinstance(value, str) or not re.fullmatch(
                    r"[a-z][a-z0-9_]{0,99}", value
                ):
                    raise ValueError(f"{pair_id}: key mapping needs {field}")
            if mapping["left_arm"] == mapping["right_arm"]:
                raise ValueError(f"{pair_id}: left_arm and right_arm must differ")

    @staticmethod
    def _validate_flags(flags: object, *, prefix: str) -> None:
        if not isinstance(flags, dict) or set(flags) != {"left", "right"}:
            raise ValueError(f"{prefix} flags must contain exactly left and right")
        for side in ("left", "right"):
            values = flags[side]
            if not isinstance(values, list) or any(
                value not in ALLOWED_FLAGS for value in values
            ):
                raise ValueError(f"{prefix} {side} flags are invalid")
            if len(values) != len(set(values)):
                raise ValueError(f"{prefix} {side} flags must be unique")

    @staticmethod
    def _validate_rating(pair_id: str, rating: object) -> None:
        if not isinstance(rating, dict):
            raise ValueError(f"{pair_id}: rating must be an object")
        unknown = set(rating) - {"choice", "flags", "note", "updated_at"}
        if unknown:
            raise ValueError(f"{pair_id}: rating has unknown fields: {sorted(unknown)}")
        if rating.get("choice") not in ALLOWED_CHOICES:
            raise ValueError(f"{pair_id}: rating choice is invalid")
        ReviewStore._validate_flags(rating.get("flags"), prefix=f"{pair_id}: rating")
        note = rating.get("note")
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError(f"{pair_id}: rating note is invalid")
        updated_at = rating.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError(f"{pair_id}: rating updated_at is invalid")

    def _load_or_create_ratings(self) -> dict[str, Any]:
        if self.ratings_path.exists():
            ratings = load_json(self.ratings_path)
            if ratings.get("schema_version") != 1:
                raise ValueError("ratings schema_version must be 1")
            if ratings.get("run_id") != self.bundle["run_id"]:
                raise ValueError("ratings run_id differs from bundle")
            if ratings.get("bundle_sha256") != self.bundle_sha256:
                raise ValueError("ratings bundle_sha256 differs from bundle")
            if ratings.get("key_sha256") != self.key_sha256:
                raise ValueError("ratings key_sha256 differs from key")
            if not isinstance(ratings.get("ratings"), dict):
                raise ValueError("ratings.ratings must be an object")
            unknown = set(ratings["ratings"]) - set(self._pairs_by_id)
            if unknown:
                raise ValueError(f"ratings contain unknown pair ids: {sorted(unknown)}")
            sealed_at = ratings.get("sealed_at")
            if sealed_at is not None and (
                not isinstance(sealed_at, str) or not sealed_at
            ):
                raise ValueError("ratings sealed_at must be null or a non-empty string")
            for pair_id, rating in ratings["ratings"].items():
                self._validate_rating(pair_id, rating)
            return ratings

        ratings = {
            "schema_version": 1,
            "run_id": self.bundle["run_id"],
            "bundle_sha256": self.bundle_sha256,
            "key_sha256": self.key_sha256,
            "created_at": utc_now(),
            "sealed_at": None,
            "ratings": {},
        }
        atomic_write_json(self.ratings_path, ratings)
        return ratings

    @property
    def sealed(self) -> bool:
        return bool(self._ratings.get("sealed_at"))

    def state(self, requested_index: int | None = None) -> dict[str, Any]:
        with self._lock:
            ratings = self._ratings["ratings"]
            total = len(self._pairs)
            if requested_index is None:
                requested_index = next(
                    (
                        index
                        for index, pair in enumerate(self._pairs)
                        if pair["id"] not in ratings
                    ),
                    max(total - 1, 0),
                )
            if requested_index < 0 or requested_index >= total:
                raise IndexError("pair index out of range")
            pair = self._pairs[requested_index]
            return {
                "run_id": self.bundle["run_id"],
                "total": total,
                "rated_count": len(ratings),
                "current_index": requested_index,
                "completed": len(ratings) == total,
                "sealed": self.sealed,
                "rated_ids": sorted(ratings),
                "pair": pair,
                "rating": ratings.get(pair["id"]),
            }

    def rate(self, pair_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.sealed:
                raise PermissionError("review is sealed")
            if pair_id not in self._pairs_by_id:
                raise KeyError("unknown pair id")
            unknown = set(payload) - {"choice", "flags", "note"}
            if unknown:
                raise ValueError(f"rating has unknown fields: {sorted(unknown)}")
            choice = payload.get("choice")
            if choice not in ALLOWED_CHOICES:
                raise ValueError(
                    f"choice must be one of: {', '.join(sorted(ALLOWED_CHOICES))}"
                )
            flags = payload.get("flags", {"left": [], "right": []})
            self._validate_flags(flags, prefix="rating")
            note = payload.get("note", "")
            if not isinstance(note, str):
                raise ValueError("note must be a string")
            if len(note) > 4000:
                raise ValueError("note is too long")

            self._ratings["ratings"][pair_id] = {
                "choice": choice,
                "flags": {
                    "left": list(flags["left"]),
                    "right": list(flags["right"]),
                },
                "note": note.strip(),
                "updated_at": utc_now(),
            }
            atomic_write_json(self.ratings_path, self._ratings)
            index = next(
                index for index, pair in enumerate(self._pairs) if pair["id"] == pair_id
            )
            return self.state_unlocked(index)

    def state_unlocked(self, requested_index: int) -> dict[str, Any]:
        ratings = self._ratings["ratings"]
        pair = self._pairs[requested_index]
        return {
            "run_id": self.bundle["run_id"],
            "total": len(self._pairs),
            "rated_count": len(ratings),
            "current_index": requested_index,
            "completed": len(ratings) == len(self._pairs),
            "sealed": self.sealed,
            "rated_ids": sorted(ratings),
            "pair": pair,
            "rating": ratings.get(pair["id"]),
        }

    def seal(self) -> dict[str, Any]:
        with self._lock:
            if len(self._ratings["ratings"]) != len(self._pairs):
                missing = len(self._pairs) - len(self._ratings["ratings"])
                raise RuntimeError(f"cannot seal: {missing} pairs are unrated")
            if not self.sealed:
                self._ratings["sealed_at"] = utc_now()
                atomic_write_json(self.ratings_path, self._ratings)
            results = self.results_unlocked()
            atomic_write_json(self.results_path, results)
            return results

    def results(self) -> dict[str, Any]:
        with self._lock:
            if not self.sealed:
                raise PermissionError("results stay blind until review is sealed")
            results = self.results_unlocked()
            if not self.results_path.exists():
                atomic_write_json(self.results_path, results)
            return results

    def results_unlocked(self) -> dict[str, Any]:
        wins: Counter[str] = Counter()
        verdicts: Counter[str] = Counter()
        flags: defaultdict[str, Counter[str]] = defaultdict(Counter)
        details: list[dict[str, Any]] = []
        for pair in self._pairs:
            pair_id = pair["id"]
            rating = self._ratings["ratings"][pair_id]
            mapping = self.key["pairs"][pair_id]
            choice = rating["choice"]
            verdicts[choice] += 1
            winner = (
                mapping.get(f"{choice}_arm") if choice in {"left", "right"} else None
            )
            if winner is not None:
                wins[winner] += 1
            side_flags = rating["flags"]
            arm_flags = {
                mapping["left_arm"]: list(side_flags["left"]),
                mapping["right_arm"]: list(side_flags["right"]),
            }
            for arm, values in arm_flags.items():
                if values:
                    flags[arm].update(values)
            details.append(
                {
                    "pair_id": pair_id,
                    "task_id": pair.get("task_id"),
                    "choice": choice,
                    "winner": winner,
                    "left_arm": mapping["left_arm"],
                    "right_arm": mapping["right_arm"],
                    "flags": side_flags,
                    "arm_flags": arm_flags,
                    "note": rating["note"],
                }
            )
        return {
            "run_id": self.bundle["run_id"],
            "sealed_at": self._ratings["sealed_at"],
            "total": len(self._pairs),
            "wins": dict(sorted(wins.items())),
            "verdict_counts": dict(sorted(verdicts.items())),
            "flag_counts": {
                arm: dict(sorted(counts.items()))
                for arm, counts in sorted(flags.items())
            },
            "pairs": details,
        }


def make_handler(store: ReviewStore, token: str, app_dir: Path = APP_DIR):
    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "SimpleManReview/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _authorized(self) -> bool:
            supplied = self.headers.get("X-Review-Token", "")
            return bool(supplied) and hmac.compare_digest(supplied, token)

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json(status, {"error": message})

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length <= 0 or length > 16_384:
                raise ValueError("JSON body must be between 1 and 16384 bytes")
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _require_api_auth(self) -> bool:
            if self._authorized():
                return True
            self._error(HTTPStatus.FORBIDDEN, "invalid review token")
            return False

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/api/health":
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            if parsed.path.startswith("/api/"):
                if not self._require_api_auth():
                    return
                try:
                    if parsed.path == "/api/state":
                        query = parse_qs(parsed.query)
                        index = int(query["index"][0]) if "index" in query else None
                        self._json(HTTPStatus.OK, store.state(index))
                        return
                    if parsed.path == "/api/results":
                        self._json(HTTPStatus.OK, store.results())
                        return
                except (ValueError, IndexError) as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except PermissionError as exc:
                    self._error(HTTPStatus.CONFLICT, str(exc))
                    return
                self._error(HTTPStatus.NOT_FOUND, "unknown API route")
                return

            static = STATIC_FILES.get(parsed.path)
            if not static:
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            filename, content_type = static
            path = app_dir / filename
            try:
                body = path.read_bytes()
            except FileNotFoundError:
                self._error(HTTPStatus.NOT_FOUND, f"missing UI asset: {filename}")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            prefix = "/api/ratings/"
            if not parsed.path.startswith(prefix) or not self._require_api_auth():
                if not parsed.path.startswith(prefix):
                    self._error(HTTPStatus.NOT_FOUND, "unknown API route")
                return
            pair_id = unquote(parsed.path[len(prefix) :])
            try:
                payload = self._read_json()
                self._json(HTTPStatus.OK, store.rate(pair_id, payload))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except KeyError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
            except PermissionError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != "/api/seal":
                self._error(HTTPStatus.NOT_FOUND, "unknown API route")
                return
            if not self._require_api_auth():
                return
            try:
                self._json(HTTPStatus.OK, store.seal())
            except RuntimeError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))

    return ReviewHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a local blind-review session.")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--ratings", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token")
    parser.add_argument("--no-open", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("review server only binds to localhost")
    ratings_path = args.ratings or args.key.with_name("ratings.json")
    store = ReviewStore(args.bundle, args.key, ratings_path)
    token = args.token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, token))
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/?token={token}"
    print(f"Review URL: {url}", flush=True)
    print(f"Ratings: {ratings_path}", flush=True)
    print(f"Results: {store.results_path}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
