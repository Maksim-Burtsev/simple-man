import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import review_server  # noqa: E402


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bundle_path = root / "bundle.json"
        self.key_path = root / "key.json"
        self.ratings_path = root / "ratings.json"
        write_json(
            self.bundle_path,
            {
                "schema_version": 1,
                "run_id": "smoke-1",
                "metadata": {"model": "test"},
                "pairs": [
                    {
                        "id": "p1",
                        "task_id": "task-1",
                        "category": "status",
                        "language": "en",
                        "prompt": "Status?",
                        "left": {"text": "Short."},
                        "right": {"text": "Long answer."},
                    },
                    {
                        "id": "p2",
                        "task_id": "task-2",
                        "category": "risk",
                        "language": "ru",
                        "prompt": "Риск?",
                        "left": {"text": "Есть риск."},
                        "right": {"text": "Риска нет."},
                    },
                ],
            },
        )
        write_json(
            self.key_path,
            {
                "schema_version": 1,
                "run_id": "smoke-1",
                "pairs": {
                    "p1": {
                        "left_arm": "simple_man_runtime",
                        "right_arm": "native_low",
                        "left_run_id": "l1",
                        "right_run_id": "r1",
                    },
                    "p2": {
                        "left_arm": "native_low",
                        "right_arm": "simple_man_runtime",
                        "left_run_id": "l2",
                        "right_run_id": "r2",
                    },
                },
            },
        )
        self.store = review_server.ReviewStore(
            self.bundle_path, self.key_path, self.ratings_path
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_state_stays_blind_and_resumes_first_unrated_pair(self):
        state = self.store.state()
        self.assertEqual(state["pair"]["id"], "p1")
        self.assertNotIn("arm", json.dumps(state))

        self.store.rate(
            "p1",
            {"choice": "left", "flags": {"left": [], "right": []}, "note": ""},
        )
        resumed = self.store.state()

        self.assertEqual(resumed["pair"]["id"], "p2")
        self.assertEqual(resumed["rated_count"], 1)

    def test_seal_requires_all_pairs_and_reveals_arm_results(self):
        self.store.rate(
            "p1",
            {
                "choice": "left",
                "flags": {"left": ["too_verbose"], "right": []},
                "note": "",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "1 pairs are unrated"):
            self.store.seal()

        self.store.rate(
            "p2",
            {"choice": "right", "flags": {"left": [], "right": []}, "note": "best"},
        )
        results = self.store.seal()

        self.assertEqual(results["wins"], {"simple_man_runtime": 2})
        self.assertEqual(
            results["flag_counts"],
            {"simple_man_runtime": {"too_verbose": 1}},
        )
        self.assertEqual(json.loads(self.store.results_path.read_text()), results)
        with self.assertRaisesRegex(PermissionError, "sealed"):
            self.store.rate(
                "p1",
                {"choice": "tie", "flags": {"left": [], "right": []}, "note": ""},
            )

    def test_public_bundle_rejects_arm_leak(self):
        bundle = json.loads(self.bundle_path.read_text())
        bundle["pairs"][0]["left"]["arm"] = "secret"
        write_json(self.bundle_path, bundle)

        with self.assertRaisesRegex(ValueError, "public left has unknown fields"):
            review_server.ReviewStore(
                self.bundle_path, self.key_path, self.ratings_path.with_name("other.json")
            )

    def test_rating_validation_rejects_unknown_choice_and_flag(self):
        with self.assertRaisesRegex(ValueError, "choice must be"):
            self.store.rate(
                "p1",
                {"choice": "maybe", "flags": {"left": [], "right": []}, "note": ""},
            )
        with self.assertRaisesRegex(ValueError, "left flags are invalid"):
            self.store.rate(
                "p1",
                {
                    "choice": "tie",
                    "flags": {"left": ["arm_a"], "right": []},
                    "note": "",
                },
            )
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.store.rate(
                "p1",
                {
                    "choice": "tie",
                    "flags": {"left": [], "right": []},
                    "note": "",
                    "left_arm": "leak",
                },
            )

    def test_ties_are_verdicts_not_arm_wins(self):
        self.store.rate(
            "p1",
            {"choice": "tie", "flags": {"left": [], "right": []}, "note": ""},
        )
        self.store.rate(
            "p2",
            {"choice": "both_bad", "flags": {"left": [], "right": []}, "note": ""},
        )

        results = self.store.seal()

        self.assertEqual(results["wins"], {})
        self.assertEqual(results["verdict_counts"], {"both_bad": 1, "tie": 1})
        self.assertIsNone(results["pairs"][0]["winner"])

    def test_resume_rejects_changed_bundle_or_malformed_rating(self):
        self.store.rate(
            "p1",
            {"choice": "left", "flags": {"left": [], "right": []}, "note": ""},
        )
        bundle = json.loads(self.bundle_path.read_text())
        bundle["pairs"][0]["prompt"] = "Changed prompt"
        write_json(self.bundle_path, bundle)

        with self.assertRaisesRegex(ValueError, "bundle_sha256 differs"):
            review_server.ReviewStore(
                self.bundle_path, self.key_path, self.ratings_path
            )

        ratings = json.loads(self.ratings_path.read_text())
        ratings["bundle_sha256"] = review_server.sha256_file(self.bundle_path)
        ratings["ratings"]["p1"]["choice"] = "unknown"
        write_json(self.ratings_path, ratings)
        with self.assertRaisesRegex(ValueError, "rating choice is invalid"):
            review_server.ReviewStore(
                self.bundle_path, self.key_path, self.ratings_path
            )


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        bundle = root / "bundle.json"
        key = root / "key.json"
        write_json(
            bundle,
            {
                "schema_version": 1,
                "run_id": "http-1",
                "pairs": [
                    {
                        "id": "p1",
                        "task_id": "task",
                        "category": "status",
                        "language": "en",
                        "prompt": "Status?",
                        "left": {"text": "A"},
                        "right": {"text": "B"},
                    }
                ],
            },
        )
        write_json(
            key,
            {
                "schema_version": 1,
                "run_id": "http-1",
                "pairs": {
                    "p1": {
                        "left_arm": "native_low",
                        "right_arm": "simple_man_runtime",
                    }
                },
            },
        )
        store = review_server.ReviewStore(bundle, key, root / "ratings.json")
        handler = review_server.make_handler(store, "secret", review_server.APP_DIR)
        self.server = review_server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path: str, *, token: str | None = None, method: str = "GET", data=None):
        headers = {}
        if token:
            headers["X-Review-Token"] = token
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        return urllib.request.urlopen(
            urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        )

    def test_api_requires_token_and_never_leaks_arm_before_seal(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/state")
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

        with self.request("/api/state", token="secret") as response:
            body = response.read().decode()
        self.assertNotIn("simple_man_runtime", body)
        self.assertNotIn("native_low", body)

    def test_static_app_has_security_headers_and_no_path_traversal(self):
        with self.request("/") as response:
            body = response.read().decode()
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn('<meta name="referrer" content="no-referrer">', body)

        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/../private/key.json")
        self.assertEqual(denied.exception.code, 404)
        denied.exception.close()

    def test_http_rating_seal_and_results(self):
        with self.request(
            "/api/ratings/p1",
            token="secret",
            method="PUT",
            data={"choice": "right", "flags": {"left": [], "right": []}, "note": ""},
        ) as response:
            self.assertEqual(response.status, 200)
        with self.request("/api/seal", token="secret", method="POST") as response:
            results = json.load(response)

        self.assertEqual(results["wins"], {"simple_man_runtime": 1})


if __name__ == "__main__":
    unittest.main()
